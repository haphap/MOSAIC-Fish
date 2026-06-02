"""Embedding 客户端（OpenAI 格式 /embeddings）。

替代 Zep Cloud 内置的云端 embedding。沿用项目「可配置 OpenAI 格式 API」的思路，
默认复用 LLM 的 key/base_url（见 Config.EMBEDDING_*）。不引入 Ollama / 本地推理。
"""

from __future__ import annotations

from typing import Optional

from openai import OpenAI

from ..config import Config
from ..utils.logger import get_logger

logger = get_logger('mirofish.embedder')

_MAX_CHARS = 8000  # 单条文本截断，避免超长输入


class Embedder:
    """按需调用 OpenAI 格式 embedding API；未配置 key 时优雅降级（返回 None）。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.model = model or Config.EMBEDDING_MODEL_NAME
        key = api_key or Config.EMBEDDING_API_KEY
        url = base_url or Config.EMBEDDING_BASE_URL
        self._client: Optional[OpenAI] = OpenAI(api_key=key, base_url=url) if key else None
        if self._client is None:
            logger.warning("EMBEDDING_API_KEY 未配置，向量检索将不可用（仅全文检索）")

    @property
    def available(self) -> bool:
        return self._client is not None

    def embed(self, text: str) -> Optional[list[float]]:
        """单条文本 → 向量；失败返回 None（调用方应能降级到全文检索）。"""
        if not self._client or not text:
            return None
        try:
            resp = self._client.embeddings.create(model=self.model, input=text[:_MAX_CHARS])
            return list(resp.data[0].embedding)
        except Exception as e:  # noqa: BLE001 - 降级而非中断
            logger.warning(f"embedding 失败: {str(e)[:120]}")
            return None

    def embed_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        """批量 embedding；整体失败时回退为逐条。"""
        if not self._client or not texts:
            return [None] * len(texts)
        try:
            resp = self._client.embeddings.create(
                model=self.model, input=[t[:_MAX_CHARS] for t in texts]
            )
            ordered = sorted(resp.data, key=lambda d: d.index)
            return [list(d.embedding) for d in ordered]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"批量 embedding 失败，回退逐条: {str(e)[:120]}")
            return [self.embed(t) for t in texts]
