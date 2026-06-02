"""``MiroGraph``：Zep 的 drop-in 替代客户端（Neo4j 后端）。

复刻 ``zep_cloud.client.Zep`` 的 ``client.graph.*`` 调用面与返回对象属性，
使上游业务 service 几乎无需改动（仅把构造点换成 ``get_graph_client()``）。

覆盖的 Zep 调用面：
  graph.create / delete / set_ontology / add / add_batch / search
  graph.node.get / get_by_graph_id / get_entity_edges
  graph.edge.get_by_graph_id
  graph.episode.get

注意（P1）：``add`` / ``add_batch`` 仅把文本 episode 落库，**尚未做 LLM 实体抽取**
（抽取在 P3 接入）；``search`` 为全文检索（向量在 P2 接入）。
"""

from __future__ import annotations

from typing import Any, Optional

from .models import EpisodeResult, SearchResults
from .neo4j_store import Neo4jStore, get_store


class _NodeNamespace:
    def __init__(self, store: Neo4jStore) -> None:
        self._store = store

    def get(self, uuid_: Optional[str] = None, uuid: Optional[str] = None, **_: Any):
        return self._store.get_node(uuid_ or uuid or "")

    def get_by_graph_id(
        self, graph_id: str, limit: int = 100, uuid_cursor: Optional[str] = None, **_: Any
    ):
        return self._store.get_nodes_by_graph(graph_id, limit, uuid_cursor)

    def get_entity_edges(self, node_uuid: Optional[str] = None, **_: Any):
        return self._store.get_node_edges(node_uuid or "")


class _EdgeNamespace:
    def __init__(self, store: Neo4jStore) -> None:
        self._store = store

    def get_by_graph_id(
        self, graph_id: str, limit: int = 100, uuid_cursor: Optional[str] = None, **_: Any
    ):
        return self._store.get_edges_by_graph(graph_id, limit, uuid_cursor)


class _EpisodeNamespace:
    def __init__(self, store: Neo4jStore) -> None:
        self._store = store

    def get(self, uuid_: Optional[str] = None, uuid: Optional[str] = None, **_: Any):
        return self._store.get_episode(uuid_ or uuid or "")


class _GraphNamespace:
    def __init__(self, store: Neo4jStore) -> None:
        self._store = store
        self.node = _NodeNamespace(store)
        self.edge = _EdgeNamespace(store)
        self.episode = _EpisodeNamespace(store)

    def create(self, graph_id: str, name: str = "", description: str = "", **_: Any) -> None:
        self._store.create_graph(graph_id, name, description)

    def delete(self, graph_id: str, **_: Any) -> None:
        self._store.delete_graph(graph_id)

    def set_ontology(
        self, graph_ids: Any = None, entities: Any = None, edges: Any = None, **_: Any
    ) -> None:
        self._store.set_ontology(graph_ids, entities or {}, edges or {})

    def add(self, graph_id: str, type: str = "text", data: str = "", **_: Any) -> EpisodeResult:
        # 落库 episode → LLM 抽取实体/边入图（抽取失败不影响 episode）。
        ep_uuid = self._store.add_episode(graph_id, data, type)
        self._store.ingest_text(graph_id, data)
        return EpisodeResult(uuid=ep_uuid)

    def add_batch(self, graph_id: str, episodes: Any = None, **_: Any) -> list[EpisodeResult]:
        results: list[EpisodeResult] = []
        for ep in episodes or []:
            data = getattr(ep, "data", None) or ""
            type_ = getattr(ep, "type", None) or "text"
            ep_uuid = self._store.add_episode(graph_id, data, type_)
            self._store.ingest_text(graph_id, data)
            results.append(EpisodeResult(uuid=ep_uuid))
        return results

    def search(
        self,
        graph_id: Optional[str] = None,
        query: str = "",
        limit: int = 10,
        scope: str = "edges",
        reranker: Optional[str] = None,  # 兼容保留；CE 无 cross_encoder，退化为 RRF
        **_: Any,
    ) -> SearchResults:
        return self._store.search(graph_id or "", query, limit, scope, reranker or "rrf")


class MiroGraph:
    """Zep 兼容客户端。``api_key`` 仅为签名兼容（Neo4j 用 Config.NEO4J_*）。"""

    def __init__(self, api_key: Optional[str] = None, store: Optional[Neo4jStore] = None, **_: Any):
        self._store = store or get_store()
        self.graph = _GraphNamespace(self._store)
