"""graph_memory：用 Neo4j Community Edition 取代 Zep Cloud 的图记忆层。

对外主入口是 ``get_graph_client()``（drop-in 替代 ``zep_cloud.client.Zep``）。
"""

from .factory import get_graph_client
from .models import (
    EpisodeResult,
    GraphEdge,
    GraphEpisode,
    GraphNode,
    SearchResults,
)

__all__ = [
    "get_graph_client",
    "GraphNode",
    "GraphEdge",
    "GraphEpisode",
    "EpisodeResult",
    "SearchResults",
]
