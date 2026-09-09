"""External embedder client (OpenAI-compatible /embeddings API).

Works with any OpenAI-compatible endpoint: OpenAI, Azure (with compatible path),
Ollama (/v1), Jina, Together, local vLLM/TEI, etc.
"""

from __future__ import annotations

import logging
from typing import Sequence

import httpx

from .config import EmbedderConfig

log = logging.getLogger(__name__)


class EmbedderError(RuntimeError):
    pass


class Embedder:
    def __init__(self, cfg: EmbedderConfig, timeout: float = 120.0):
        self.cfg = cfg
        self.client = httpx.Client(
            base_url=cfg.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {},
            timeout=timeout,
        )

    def _post(self, payload: dict) -> dict:
        try:
            r = self.client.post("/embeddings", json=payload)
        except httpx.HTTPError as e:
            raise EmbedderError(f"network error calling embedder: {e}") from e
        if r.status_code != 200:
            raise EmbedderError(
                f"embedder returned HTTP {r.status_code}: {r.text[:500]}"
            )
        return r.json()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns vectors in the same order."""
        if not texts:
            return []
        payload = {"model": self.cfg.model, "input": list(texts)}
        data = self._post(payload)
        # OpenAI format: data[] sorted by index
        items = sorted(data["data"], key=lambda d: d["index"])
        vectors = [d["embedding"] for d in items]
        if len(vectors) != len(texts):
            raise EmbedderError(
                f"embedder returned {len(vectors)} vectors for {len(texts)} texts"
            )
        return vectors

    def embed_batched(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed with batching per cfg.batch_size."""
        out: list[list[float]] = []
        for i in range(0, len(texts), self.cfg.batch_size):
            chunk = [t[: self.cfg.max_text_chars] for t in texts[i : i + self.cfg.batch_size]]
            out.extend(self.embed(chunk))
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text[: self.cfg.max_text_chars]])[0]

    def close(self) -> None:
        self.client.close()
