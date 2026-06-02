"""LLM 实体/边抽取（P3）。

复刻 Zep ``graph.add`` 的隐式知识图谱构建：把 episode 文本转成带类型的实体 +
实体间关系事实，遵循 ontology（若有）。沿用项目 ``LLMClient``（OpenAI 格式、
Config.LLM_*、JSON 模式）；任何失败都优雅降级（返回空，episode 仍照常落库）。
"""

from __future__ import annotations

from typing import Any, Optional

from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger

logger = get_logger('mirofish.extractor')

_SYSTEM = (
    "你是知识图谱抽取器，服务于社会舆情/事件模拟。"
    "只根据给定文本抽取实体与实体间关系，不要臆造文本之外的信息。"
    "输出语言与文本一致。必须严格输出 JSON。"
)

_SCHEMA = (
    '输出 JSON，结构如下：\n'
    '{\n'
    '  "entities": [{"name": "实体名", "type": "类型", "summary": "一句话简介"}],\n'
    '  "relations": [{"source": "源实体名", "target": "目标实体名", '
    '"type": "关系类型", "fact": "陈述该关系的一句事实"}]\n'
    '}\n'
    'relations 的 source/target 必须是 entities 里出现过的 name；无可抽取则返回空数组。'
)


class Extractor:
    """LLM 抽取器；``available`` 为假时（无 key/初始化失败）整体降级为不抽取。"""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self._llm = llm
        if self._llm is None:
            try:
                self._llm = LLMClient()
            except Exception as e:  # noqa: BLE001 - 无 key 等
                logger.warning(f"LLMClient 初始化失败，实体抽取禁用: {str(e)[:120]}")
                self._llm = None

    @property
    def available(self) -> bool:
        return self._llm is not None

    def extract(self, text: str, ontology: Optional[dict] = None) -> dict:
        """返回 {"entities": [...], "relations": [...]}；失败返回空。"""
        if not self._llm or not (text or "").strip():
            return {"entities": [], "relations": []}
        try:
            data = self._llm.chat_json(
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": self._build_prompt(text, ontology or {})},
                ],
                temperature=0.0,
            )
        except Exception as e:  # noqa: BLE001 - 抽取失败不应中断 episode 落库
            logger.warning(f"实体抽取失败: {str(e)[:160]}")
            return {"entities": [], "relations": []}
        return self._normalize(data)

    @staticmethod
    def _build_prompt(text: str, ontology: dict) -> str:
        lines: list[str] = []
        ent_types = ontology.get("entities") or []
        edge_types = ontology.get("edges") or []
        if ent_types:
            lines.append("允许的实体类型（type 必须取自其中）：")
            for e in ent_types:
                lines.append(f"  - {e.get('name')}: {e.get('description', '')}")
        else:
            lines.append("实体类型不限（自行归纳简洁的英文类型名，如 Person/Company/Policy）。")
        if edge_types:
            lines.append("允许的关系类型：")
            for e in edge_types:
                st = e.get("source_targets") or []
                st_s = "; ".join(f"{x.get('source')}->{x.get('target')}" for x in st)
                tail = f" [{st_s}]" if st_s else ""
                lines.append(f"  - {e.get('name')}: {e.get('description', '')}{tail}")
        else:
            lines.append("关系类型不限（用简洁的英文关系名，如 WORKS_AT/SUPPORTS）。")
        return f"{chr(10).join(lines)}\n\n{_SCHEMA}\n\n文本：\n\"\"\"\n{text}\n\"\"\""

    @staticmethod
    def _normalize(data: Any) -> dict:
        if not isinstance(data, dict):
            return {"entities": [], "relations": []}
        ents = []
        for e in data.get("entities") or []:
            if isinstance(e, dict) and str(e.get("name") or "").strip():
                ents.append({
                    "name": str(e["name"]).strip(),
                    "type": (str(e.get("type") or "").strip() or "Entity"),
                    "summary": str(e.get("summary") or "").strip(),
                })
        rels = []
        for r in data.get("relations") or []:
            if not isinstance(r, dict):
                continue
            src, tgt = str(r.get("source") or "").strip(), str(r.get("target") or "").strip()
            if src and tgt:
                rels.append({
                    "source": src,
                    "target": tgt,
                    "type": (str(r.get("type") or "").strip() or "RELATES"),
                    "fact": str(r.get("fact") or "").strip(),
                })
        return {"entities": ents, "relations": rels}
