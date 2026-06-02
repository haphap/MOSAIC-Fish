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
from uuid import NAMESPACE_URL, uuid4, uuid5

from neo4j import GraphDatabase

from ..config import Config
from ..utils.logger import get_logger
from .embedder import Embedder
from .extractor import Extractor
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
        extractor: Optional[Extractor] = None,
    ) -> None:
        self.uri = uri or Config.NEO4J_URI
        self.user = user or Config.NEO4J_USER
        self.password = password or Config.NEO4J_PASSWORD
        self._driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        self.embedder = embedder if embedder is not None else Embedder()
        self.extractor = extractor if extractor is not None else Extractor()
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
            # uuid 全局唯一（与按 uuid 查的 get_node/get_episode 语义一致；uuid4 本就全局唯一）。
            # 先丢弃早期的 (graph_id, uuid) 复合约束再建全局唯一约束（幂等）。
            "DROP CONSTRAINT miro_node_uuid IF EXISTS",
            "DROP CONSTRAINT miro_ep_uuid IF EXISTS",
            f"CREATE CONSTRAINT miro_node_uuid_u IF NOT EXISTS "
            f"FOR (n:{_BASE_LABEL}) REQUIRE n.uuid IS UNIQUE",
            f"CREATE CONSTRAINT miro_ep_uuid_u IF NOT EXISTS "
            f"FOR (e:{_EPISODE_LABEL}) REQUIRE e.uuid IS UNIQUE",
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
        """记录本体（类型名 + 描述 + source_targets）供抽取参考。

        ``entities`` = {名称: EntityModel 类}；``edges`` = {名称: (EdgeModel 类, [source_targets])}。
        从 pydantic 类的 ``__doc__`` 取描述。
        """
        ent_spec = []
        if isinstance(entities, dict):
            for name, model in entities.items():
                ent_spec.append({"name": name, "description": (getattr(model, "__doc__", "") or "").strip()})
        edge_spec = []
        if isinstance(edges, dict):
            for name, val in edges.items():
                model = val[0] if isinstance(val, tuple) else val
                sts = []
                if isinstance(val, tuple) and len(val) > 1 and val[1]:
                    for st in val[1]:
                        sts.append({
                            "source": getattr(st, "source", "Entity"),
                            "target": getattr(st, "target", "Entity"),
                        })
                edge_spec.append({
                    "name": name,
                    "description": (getattr(model, "__doc__", "") or "").strip(),
                    "source_targets": sts,
                })
        gids = graph_ids if isinstance(graph_ids, (list, tuple)) else [graph_ids]
        with self._driver.session() as s:
            for gid in gids:
                if not gid:
                    continue
                s.run(
                    f"MERGE (g:{_GRAPH_LABEL} {{graph_id:$gid}}) "
                    "SET g.entity_types=$ents, g.edge_types=$edges, "
                    "g.ontology_entities=$ent_json, g.ontology_edges=$edge_json",
                    gid=gid,
                    ents=[e["name"] for e in ent_spec], edges=[e["name"] for e in edge_spec],
                    ent_json=json.dumps(ent_spec, ensure_ascii=False),
                    edge_json=json.dumps(edge_spec, ensure_ascii=False),
                )

    def get_ontology(self, graph_id: str) -> dict:
        with self._driver.session() as s:
            rec = s.run(
                f"MATCH (g:{_GRAPH_LABEL} {{graph_id:$gid}}) "
                "RETURN g.ontology_entities AS e, g.ontology_edges AS x",
                gid=graph_id,
            ).single()
        if not rec:
            return {"entities": [], "edges": []}

        def _load(v):
            try:
                return json.loads(v) if v else []
            except (json.JSONDecodeError, TypeError):
                return []

        return {"entities": _load(rec["e"]), "edges": _load(rec["x"])}

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

    def find_node_by_name(self, graph_id: str, name: str) -> Optional[GraphNode]:
        """图内按精确名称查节点（实体去重用）。"""
        with self._driver.session() as s:
            rec = s.run(
                f"MATCH (n:{_BASE_LABEL} {{graph_id:$gid, name:$name}}) RETURN n LIMIT 1",
                gid=graph_id, name=name,
            ).single()
            return self._to_node(rec["n"]) if rec else None

    # ---- ingestion：文本 → LLM 抽取 → 去重 → 入图（P3） -----------------

    def ingest_text(self, graph_id: str, text: str) -> dict:
        """抽取 ``text`` 中的实体/关系并入图；按名称去重。返回 {entities, relations} 计数。

        抽取器不可用或失败时静默返回 0（episode 已由调用方落库，不中断流程）。
        """
        if not self.extractor.available:
            return {"entities": 0, "relations": 0}
        try:
            result = self.extractor.extract(text, self.get_ontology(graph_id))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ingest 抽取异常: {str(e)[:120]}")
            return {"entities": 0, "relations": 0}

        name_to_uuid: dict[str, str] = {}
        for ent in result.get("entities", []):
            name = ent["name"]
            etype = ent.get("type") or _BASE_LABEL
            existing = self.find_node_by_name(graph_id, name)
            node_uuid = existing.uuid if existing else str(uuid4())
            name_to_uuid[name] = node_uuid
            summary = ent.get("summary", "")
            if existing and existing.summary and not summary:
                summary = existing.summary  # 不用空摘要覆盖已有
            labels = [etype] if etype and etype != _BASE_LABEL else []
            self.upsert_node(graph_id, GraphNode(uuid=node_uuid, name=name, labels=labels, summary=summary))

        n_rel = 0
        for rel in result.get("relations", []):
            s_uuid = self._resolve_node(graph_id, rel["source"], name_to_uuid)
            t_uuid = self._resolve_node(graph_id, rel["target"], name_to_uuid)
            etype = rel.get("type") or _REL_TYPE
            # 确定性 uuid：同图同源同目标同关系类型 → 幂等，重复抽取更新而非堆积
            edge_uuid = str(uuid5(NAMESPACE_URL, f"{graph_id}|{s_uuid}|{t_uuid}|{etype}"))
            self.upsert_edge(graph_id, GraphEdge(
                uuid=edge_uuid, name=etype, fact=rel.get("fact", ""),
                source_node_uuid=s_uuid, target_node_uuid=t_uuid,
            ))
            n_rel += 1
        return {"entities": len(name_to_uuid), "relations": n_rel}

    def _resolve_node(self, graph_id: str, name: str, name_to_uuid: dict[str, str]) -> str:
        """把关系端点名解析成 uuid；未知端点自动补建裸节点。"""
        if name in name_to_uuid:
            return name_to_uuid[name]
        existing = self.find_node_by_name(graph_id, name)
        node_uuid = existing.uuid if existing else str(uuid4())
        if not existing:
            self.upsert_node(graph_id, GraphNode(uuid=node_uuid, name=name, labels=[]))
        name_to_uuid[name] = node_uuid
        return node_uuid

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

    # ---- search：向量 + 全文 的 RRF 混合检索（P2） ----------------------
    #
    # 两路召回（向量语义 + 全文 BM25）用 Reciprocal Rank Fusion 融合。
    # reranker 形参为兼容 Zep 保留：CE 无 cross_encoder，统一退化为 RRF。
    # 无 embedding（未配 key / query 向量化失败）时自动只走全文。

    _RRF_K = 60        # RRF 常数（越大越平滑）
    _POOL_MULT = 5     # 融合前每路候选池 = max(limit*MULT, 50)

    def search(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
        scope: str = "edges",
        reranker: str = "rrf",
    ) -> SearchResults:
        pool = max(int(limit) * self._POOL_MULT, 50)
        qvec = self.embedder.embed(query) if self.embedder.available else None
        if scope == "nodes":
            ft = self._ft_nodes(graph_id, query, pool)
            vec = self._vec_nodes(graph_id, qvec, pool) if qvec else []
            fused = self._rrf([vec, ft], lambda n: n.uuid, limit) if vec else ft[:limit]
            return SearchResults(nodes=fused)
        ft = self._ft_edges(graph_id, query, pool)
        vec = self._vec_edges(graph_id, qvec, pool) if qvec else []
        fused = self._rrf([vec, ft], lambda e: e.uuid, limit) if vec else ft[:limit]
        return SearchResults(edges=fused)

    @staticmethod
    def _rrf(result_lists, key, limit):
        scores: dict[str, float] = {}
        objs: dict[str, Any] = {}
        for lst in result_lists:
            for rank, obj in enumerate(lst):
                uid = key(obj)
                if not uid:
                    continue
                scores[uid] = scores.get(uid, 0.0) + 1.0 / (Neo4jStore._RRF_K + rank + 1)
                objs[uid] = obj
        ordered = sorted(scores, key=lambda u: scores[u], reverse=True)
        return [objs[u] for u in ordered[:limit]]

    def _run_node_search(self, cypher, params, label) -> list[GraphNode]:
        try:
            with self._driver.session() as s:
                return [self._to_node(r["node"]) for r in s.run(cypher, **params)]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{label}检索失败: {str(e)[:100]}")
            return []

    def _run_edge_search(self, cypher, params, label) -> list[GraphEdge]:
        try:
            with self._driver.session() as s:
                return [self._to_edge(r["r"], r["s"], r["t"]) for r in s.run(cypher, **params)]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{label}检索失败: {str(e)[:100]}")
            return []

    def _ft_nodes(self, graph_id, query, limit) -> list[GraphNode]:
        cypher = (
            "CALL db.index.fulltext.queryNodes('miro_node_ft', $q) YIELD node, score "
            "WHERE node.graph_id=$gid RETURN node ORDER BY score DESC LIMIT $limit"
        )
        return self._run_node_search(
            cypher, dict(q=_lucene(query), gid=graph_id, limit=int(limit)), "节点全文"
        )

    def _vec_nodes(self, graph_id, qvec, limit) -> list[GraphNode]:
        # CE 向量索引不支持按属性预过滤 → 取更大 k 再按 graph_id 过滤
        cypher = (
            "CALL db.index.vector.queryNodes('miro_node_vec', $k, $vec) YIELD node, score "
            "WHERE node.graph_id=$gid RETURN node ORDER BY score DESC LIMIT $limit"
        )
        return self._run_node_search(
            cypher, dict(k=int(limit) * 4, vec=qvec, gid=graph_id, limit=int(limit)), "节点向量"
        )

    def _ft_edges(self, graph_id, query, limit) -> list[GraphEdge]:
        cypher = (
            "CALL db.index.fulltext.queryRelationships('miro_edge_ft', $q) "
            "YIELD relationship AS r, score WHERE r.graph_id=$gid "
            "RETURN r, startNode(r).uuid AS s, endNode(r).uuid AS t ORDER BY score DESC LIMIT $limit"
        )
        return self._run_edge_search(
            cypher, dict(q=_lucene(query), gid=graph_id, limit=int(limit)), "边全文"
        )

    def _vec_edges(self, graph_id, qvec, limit) -> list[GraphEdge]:
        cypher = (
            "CALL db.index.vector.queryRelationships('miro_edge_vec', $k, $vec) "
            "YIELD relationship AS r, score WHERE r.graph_id=$gid "
            "RETURN r, startNode(r).uuid AS s, endNode(r).uuid AS t ORDER BY score DESC LIMIT $limit"
        )
        return self._run_edge_search(
            cypher, dict(k=int(limit) * 4, vec=qvec, gid=graph_id, limit=int(limit)), "边向量"
        )


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
