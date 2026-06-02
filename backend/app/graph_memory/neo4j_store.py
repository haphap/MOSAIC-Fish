"""Neo4j Community Edition 图记忆存储（取代 Zep Cloud）。

单库 + ``graph_id`` 属性命名空间（CE 不支持多库）。提供 Zep 调用面所需的
最小存储原语：图的创建/删除、节点/边 upsert、分页读取、节点边遍历、
episode 落库、以及检索（P1 为全文检索；向量检索在 P2 接入）。

注意：实体抽取（LLM 把文本转成带类型实体/边）属于 P3，本文件只负责存储与读取。
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Optional
from uuid import uuid4

from neo4j import GraphDatabase

from ..config import Config
from ..utils.logger import get_logger
from .embedder import Embedder
from .models import GraphEdge, GraphEpisode, GraphNode, SearchResults

logger = get_logger('mirofish.neo4j_store')

_BASE_LABEL = "Entity"
_EPISODE_LABEL = "Episode"
_GRAPH_LABEL = "Graph"
_REL_TYPE = "RELATES"

# Lucene 特殊字符，全文检索前清洗为空格，避免查询语法错误
_LUCENE_SPECIAL = re.compile(r'[+\-!(){}\[\]^"~*?:\\/]|&&|\|\|')
_LABEL_SAFE = re.compile(r'[^A-Za-z0-9_]')


def _lucene(query: str) -> str:
    cleaned = _LUCENE_SPECIAL.sub(' ', query or '').strip()
    return cleaned or '*'


def _safe_label(label: str) -> str:
    """把任意实体类型名清洗成合法 Neo4j 标签（用于建动态标签）。"""
    safe = _LABEL_SAFE.sub('_', label).strip('_')
    if not safe:
        return 'Type'
    if safe[0].isdigit():
        safe = f'T_{safe}'
    return safe


class Neo4jStore:
    """对 Neo4j 的薄封装；线程安全（neo4j driver 自身线程安全）。"""

    def __init__(
        self,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        embedder: Optional[Embedder] = None,
    ) -> None:
        self.uri = uri or Config.NEO4J_URI
        self.user = user or Config.NEO4J_USER
        self.password = password or Config.NEO4J_PASSWORD
        self._driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        self.embedder = embedder if embedder is not None else Embedder()
        self._ensure_schema()

    def close(self) -> None:
        self._driver.close()

    def verify(self) -> None:
        """连通性自检（启动期调用，fail-loud）。"""
        self._driver.verify_connectivity()

    # ---- schema ----------------------------------------------------------

    def _ensure_schema(self) -> None:
        dim = Config.EMBEDDING_DIMENSIONS
        stmts = [
            f"CREATE CONSTRAINT miro_node_uuid IF NOT EXISTS "
            f"FOR (n:{_BASE_LABEL}) REQUIRE (n.graph_id, n.uuid) IS UNIQUE",
            f"CREATE CONSTRAINT miro_ep_uuid IF NOT EXISTS "
            f"FOR (e:{_EPISODE_LABEL}) REQUIRE (e.graph_id, e.uuid) IS UNIQUE",
            f"CREATE CONSTRAINT miro_graph_id IF NOT EXISTS "
            f"FOR (g:{_GRAPH_LABEL}) REQUIRE g.graph_id IS UNIQUE",
            f"CREATE INDEX miro_node_graph IF NOT EXISTS FOR (n:{_BASE_LABEL}) ON (n.graph_id)",
            f"CREATE FULLTEXT INDEX miro_node_ft IF NOT EXISTS "
            f"FOR (n:{_BASE_LABEL}) ON EACH [n.name, n.summary]",
            f"CREATE FULLTEXT INDEX miro_edge_ft IF NOT EXISTS "
            f"FOR ()-[r:{_REL_TYPE}]-() ON EACH [r.name, r.fact]",
            f"CREATE VECTOR INDEX miro_node_vec IF NOT EXISTS FOR (n:{_BASE_LABEL}) "
            f"ON (n.name_embedding) OPTIONS {{ indexConfig: {{ "
            f"`vector.dimensions`: {dim}, `vector.similarity_function`: 'cosine' }} }}",
            f"CREATE VECTOR INDEX miro_edge_vec IF NOT EXISTS FOR ()-[r:{_REL_TYPE}]-() "
            f"ON (r.fact_embedding) OPTIONS {{ indexConfig: {{ "
            f"`vector.dimensions`: {dim}, `vector.similarity_function`: 'cosine' }} }}",
        ]
        with self._driver.session() as s:
            for st in stmts:
                try:
                    s.run(st)
                except Exception as e:  # noqa: BLE001 - 已存在/版本差异时容错
                    logger.warning(f"schema 语句跳过: {st[:48]}... -> {str(e)[:80]}")
        logger.info("Neo4j schema 就绪")

    # ---- graph lifecycle -------------------------------------------------

    def create_graph(self, graph_id: str, name: str = "", description: str = "") -> None:
        with self._driver.session() as s:
            s.run(
                f"MERGE (g:{_GRAPH_LABEL} {{graph_id:$gid}}) "
                "SET g.name=$name, g.description=$desc, "
                "g.created_at=coalesce(g.created_at, timestamp())",
                gid=graph_id, name=name or "", desc=description or "",
            )

    def set_ontology(self, graph_ids, entities, edges) -> None:
        """记录本体的类型名（供 P3 抽取参考）。entities/edges 为 {名称: 模型}。"""
        names = list(entities.keys()) if isinstance(entities, dict) else []
        edge_names = list(edges.keys()) if isinstance(edges, dict) else []
        gids = graph_ids if isinstance(graph_ids, (list, tuple)) else [graph_ids]
        with self._driver.session() as s:
            for gid in gids:
                if not gid:
                    continue
                s.run(
                    f"MERGE (g:{_GRAPH_LABEL} {{graph_id:$gid}}) "
                    "SET g.entity_types=$ents, g.edge_types=$edges",
                    gid=gid, ents=names, edges=edge_names,
                )

    def delete_graph(self, graph_id: str) -> None:
        with self._driver.session() as s:
            s.run("MATCH (n) WHERE n.graph_id=$gid DETACH DELETE n", gid=graph_id)

    # ---- episodes --------------------------------------------------------

    def add_episode(self, graph_id: str, content: str, type_: str = "text") -> str:
        ep_uuid = str(uuid4())
        with self._driver.session() as s:
            s.run(
                f"MERGE (e:{_EPISODE_LABEL} {{graph_id:$gid, uuid:$uuid}}) "
                "SET e.content=$content, e.type=$type, e.created_at=timestamp()",
                gid=graph_id, uuid=ep_uuid, content=content or "", type=type_,
            )
        return ep_uuid

    def get_episode(self, uuid: str) -> Optional[GraphEpisode]:
        with self._driver.session() as s:
            rec = s.run(
                f"MATCH (e:{_EPISODE_LABEL} {{uuid:$uuid}}) RETURN e LIMIT 1", uuid=uuid
            ).single()
            if not rec:
                return None
            props = dict(rec["e"])
            return GraphEpisode(uuid=props.get("uuid", ""), content=props.get("content", ""))

    # ---- node / edge upsert ---------------------------------------------

    def upsert_node(self, graph_id: str, node: GraphNode) -> None:
        labels = [l for l in (node.labels or []) if l and l != _BASE_LABEL]
        label_set = "".join(f":{_safe_label(l)}" for l in labels)
        emb = None
        if self.embedder.available and node.name:
            emb = self.embedder.embed(f"{node.name} {node.summary or ''}".strip())
        sets = ["n.name=$name", "n.summary=$summary", "n.attributes=$attrs", "n.labels=$labels"]
        params: dict[str, Any] = dict(
            gid=graph_id, uuid=node.uuid, name=node.name or "", summary=node.summary or "",
            attrs=json.dumps(node.attributes or {}, ensure_ascii=False), labels=node.labels or [],
        )
        if emb is not None:
            sets.append("n.name_embedding=$emb")
            params["emb"] = emb
        set_clause = ", ".join(sets)
        if label_set:
            set_clause = f"n{label_set}, " + set_clause
        with self._driver.session() as s:
            s.run(
                f"MERGE (n:{_BASE_LABEL} {{graph_id:$gid, uuid:$uuid}}) SET {set_clause}",
                **params,
            )

    def upsert_edge(self, graph_id: str, edge: GraphEdge) -> None:
        emb = None
        if self.embedder.available and edge.fact:
            emb = self.embedder.embed(edge.fact)
        sets = ["r.name=$name", "r.fact=$fact", "r.attributes=$attrs",
                "r.graph_id=$gid", "r.uuid=$uuid"]
        params: dict[str, Any] = dict(
            gid=graph_id, uuid=edge.uuid, name=edge.name or "", fact=edge.fact or "",
            attrs=json.dumps(edge.attributes or {}, ensure_ascii=False),
            s=edge.source_node_uuid, t=edge.target_node_uuid,
        )
        if emb is not None:
            sets.append("r.fact_embedding=$emb")
            params["emb"] = emb
        with self._driver.session() as s:
            s.run(
                f"MATCH (a:{_BASE_LABEL} {{graph_id:$gid, uuid:$s}}), "
                f"(b:{_BASE_LABEL} {{graph_id:$gid, uuid:$t}}) "
                f"MERGE (a)-[r:{_REL_TYPE} {{uuid:$uuid}}]->(b) SET {', '.join(sets)}",
                **params,
            )

    # ---- node / edge read ------------------------------------------------

    def _to_node(self, n) -> GraphNode:
        props = dict(n)
        attrs = props.get("attributes")
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except json.JSONDecodeError:
                attrs = {}
        labels = props.get("labels") or [l for l in getattr(n, "labels", [])]
        return GraphNode(
            uuid=props.get("uuid", ""), name=props.get("name", ""),
            labels=list(labels), summary=props.get("summary", ""), attributes=attrs or {},
        )

    def _to_edge(self, r, source_uuid: str, target_uuid: str) -> GraphEdge:
        props = dict(r)
        attrs = props.get("attributes")
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except json.JSONDecodeError:
                attrs = {}
        return GraphEdge(
            uuid=props.get("uuid", ""), name=props.get("name", ""), fact=props.get("fact", ""),
            source_node_uuid=source_uuid, target_node_uuid=target_uuid, attributes=attrs or {},
        )

    def get_node(self, uuid: str) -> Optional[GraphNode]:
        with self._driver.session() as s:
            rec = s.run(
                f"MATCH (n:{_BASE_LABEL} {{uuid:$uuid}}) RETURN n LIMIT 1", uuid=uuid
            ).single()
            return self._to_node(rec["n"]) if rec else None

    def get_nodes_by_graph(
        self, graph_id: str, limit: int = 100, uuid_cursor: Optional[str] = None
    ) -> list[GraphNode]:
        cypher = f"MATCH (n:{_BASE_LABEL}) WHERE n.graph_id=$gid"
        params: dict[str, Any] = dict(gid=graph_id, limit=int(limit))
        if uuid_cursor:
            cypher += " AND n.uuid > $cursor"
            params["cursor"] = uuid_cursor
        cypher += " RETURN n ORDER BY n.uuid LIMIT $limit"
        with self._driver.session() as s:
            return [self._to_node(r["n"]) for r in s.run(cypher, **params)]

    def get_edges_by_graph(
        self, graph_id: str, limit: int = 100, uuid_cursor: Optional[str] = None
    ) -> list[GraphEdge]:
        cypher = (
            f"MATCH (a:{_BASE_LABEL})-[r:{_REL_TYPE}]->(b:{_BASE_LABEL}) WHERE r.graph_id=$gid"
        )
        params: dict[str, Any] = dict(gid=graph_id, limit=int(limit))
        if uuid_cursor:
            cypher += " AND r.uuid > $cursor"
            params["cursor"] = uuid_cursor
        cypher += " RETURN r, a.uuid AS s, b.uuid AS t ORDER BY r.uuid LIMIT $limit"
        with self._driver.session() as s:
            return [self._to_edge(r["r"], r["s"], r["t"]) for r in s.run(cypher, **params)]

    def get_node_edges(self, node_uuid: str) -> list[GraphEdge]:
        cypher = (
            f"MATCH (a:{_BASE_LABEL} {{uuid:$u}})-[r:{_REL_TYPE}]-(b:{_BASE_LABEL}) "
            "RETURN r, startNode(r).uuid AS s, endNode(r).uuid AS t"
        )
        with self._driver.session() as s:
            return [self._to_edge(r["r"], r["s"], r["t"]) for r in s.run(cypher, u=node_uuid)]

    # ---- search (P1: 全文检索；向量 + RRF 在 P2 接入) --------------------

    def search(
        self, graph_id: str, query: str, limit: int = 10, scope: str = "edges"
    ) -> SearchResults:
        if scope == "nodes":
            return SearchResults(nodes=self._search_nodes(graph_id, query, limit))
        return SearchResults(edges=self._search_edges(graph_id, query, limit))

    def _search_nodes(self, graph_id: str, query: str, limit: int) -> list[GraphNode]:
        cypher = (
            "CALL db.index.fulltext.queryNodes('miro_node_ft', $q) YIELD node, score "
            "WHERE node.graph_id=$gid RETURN node ORDER BY score DESC LIMIT $limit"
        )
        try:
            with self._driver.session() as s:
                recs = s.run(cypher, q=_lucene(query), gid=graph_id, limit=int(limit))
                return [self._to_node(r["node"]) for r in recs]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"节点全文检索失败: {str(e)[:100]}")
            return []

    def _search_edges(self, graph_id: str, query: str, limit: int) -> list[GraphEdge]:
        cypher = (
            "CALL db.index.fulltext.queryRelationships('miro_edge_ft', $q) "
            "YIELD relationship AS r, score WHERE r.graph_id=$gid "
            "RETURN r, startNode(r).uuid AS s, endNode(r).uuid AS t ORDER BY score DESC LIMIT $limit"
        )
        try:
            with self._driver.session() as s:
                recs = s.run(cypher, q=_lucene(query), gid=graph_id, limit=int(limit))
                return [self._to_edge(r["r"], r["s"], r["t"]) for r in recs]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"边全文检索失败: {str(e)[:100]}")
            return []


# ---- 进程级单例（被 MiroGraph 共享） -------------------------------------

_store: Optional[Neo4jStore] = None
_store_lock = threading.Lock()


def get_store() -> Neo4jStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = Neo4jStore()
    return _store
