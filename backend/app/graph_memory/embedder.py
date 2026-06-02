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
_MAX_BATCH = 10    # dashscope text-embedding-v* 兼容模式单次最多 10 条


class Embedder:
    """按需调用 OpenAI 格式 embedding API；未配置 key 时优雅降级（返回 None）。

    传 ``dimensions``：text-embedding-3-*/v4 等支持自定义维度，必须显式请求才能
    与 Neo4j 向量索引维度（Config.EMBEDDING_DIMENSIONS）对齐（如 text-embedding-v4
    默认 1024，需请求 1536）。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
    ) -> None:
        self.model = model or Config.EMBEDDING_MODEL_NAME
        self.dimensions = dimensions if dimensions is not None else Config.EMBEDDING_DIMENSIONS
        key = api_key or Config.EMBEDDING_API_KEY
        url = base_url or Config.EMBEDDING_BASE_URL
        self._client: Optional[OpenAI] = OpenAI(api_key=key, base_url=url) if key else None
        if self._client is None:
            logger.warning("EMBEDDING_API_KEY 未配置，向量检索将不可用（仅全文检索）")

    @property
    def available(self) -> bool:
        return self._client is not None

    def _create(self, inputs: list[str]):
        kwargs: dict = {"model": self.model, "input": inputs}
        if self.dimensions and self.dimensions > 0:
            kwargs["dimensions"] = self.dimensions
        return self._client.embeddings.create(**kwargs)

    def embed(self, text: str) -> Optional[list[float]]:
        """单条文本 → 向量；失败返回 None（调用方应能降级到全文检索）。"""
        if not self._client or not text:
            return None
        try:
            resp = self._create([text[:_MAX_CHARS]])
            return list(resp.data[0].embedding)
        except Exception as e:  # noqa: BLE001 - 降级而非中断
            logger.warning(f"embedding 失败: {str(e)[:120]}")
            return None

    def embed_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        """批量 embedding（自动分片 ≤ _MAX_BATCH）；分片失败时回退为逐条。"""
        if not self._client or not texts:
            return [None] * len(texts)
        out: list[Optional[list[float]]] = []
        for i in range(0, len(texts), _MAX_BATCH):
            chunk = texts[i:i + _MAX_BATCH]
            try:
                resp = self._create([t[:_MAX_CHARS] for t in chunk])
                ordered = sorted(resp.data, key=lambda d: d.index)
                out.extend(list(d.embedding) for d in ordered)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"批量 embedding 分片失败，回退逐条: {str(e)[:120]}")
                out.extend(self.embed(t) for t in chunk)
        return out
