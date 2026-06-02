"""图记忆数据模型。

这些 dataclass 复刻 Zep SDK 返回对象的属性形状，使 ``MiroGraph`` 成为
``zep_cloud`` 的 drop-in 替代：业务代码普遍用
``getattr(x, 'uuid_', None) or getattr(x, 'uuid', '')`` 读 UUID，
故每个对象同时暴露 ``uuid`` 字段与 ``uuid_`` 属性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GraphNode:
    """实体节点（兼容 Zep node：uuid_/uuid/name/labels/summary/attributes）。"""

    uuid: str
    name: str = ""
    labels: list[str] = field(default_factory=list)
    summary: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def uuid_(self) -> str:
        return self.uuid


@dataclass
class GraphEdge:
    """关系边（兼容 Zep edge：uuid_/uuid/name/fact/source_node_uuid/target_node_uuid/attributes）。"""

    uuid: str
    name: str = ""
    fact: str = ""
    source_node_uuid: str = ""
    target_node_uuid: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def uuid_(self) -> str:
        return self.uuid


@dataclass
class GraphEpisode:
    """Episode（兼容 Zep episode：uuid_/uuid + processed 状态）。

    Neo4j 写入是同步的，episode 落库即视为已处理，故 ``processed`` 恒为 True。
    """

    uuid: str
    content: str = ""
    processed: bool = True

    @property
    def uuid_(self) -> str:
        return self.uuid


@dataclass
class EpisodeResult:
    """``graph.add`` / ``graph.add_batch`` 的返回元素（只需 uuid_/uuid）。"""

    uuid: str

    @property
    def uuid_(self) -> str:
        return self.uuid


@dataclass
class SearchResults:
    """``graph.search`` 返回对象，兼容 ``.nodes`` / ``.edges`` 属性。"""

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
