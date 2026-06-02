"""图记忆客户端工厂：按 ``GRAPH_MEMORY_BACKEND`` 选择后端。

这是上游对齐的关键收敛点——所有 ``Zep(api_key=...)`` 构造点改为调用
``get_graph_client(api_key=...)``，从而：
  - 默认 ``neo4j`` → 返回 MiroGraph（Neo4j 后端）；
  - 设 ``zep``    → 返回真实 Zep 客户端（回滚 / 对照用）。
"""

from __future__ import annotations

from typing import Any, Optional

from ..config import Config


def get_graph_client(api_key: Optional[str] = None, **kwargs: Any):
    backend = (getattr(Config, "GRAPH_MEMORY_BACKEND", "neo4j") or "neo4j").lower()
    if backend == "zep":
        from zep_cloud.client import Zep

        return Zep(api_key=api_key or Config.ZEP_API_KEY)

    from .client import MiroGraph

    return MiroGraph(api_key=api_key, **kwargs)
