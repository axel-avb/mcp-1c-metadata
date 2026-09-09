"""External reranker client (Cohere/Jina-compatible /rerank API).

Both Cohere and Jina expose the same wire format:
    POST {endpoint}
    { "model": "...", "query": "...", "documents": ["...", ...], "top_n": N }
    -> { "results": [ {"index": 0, "relevance_score": 0.93}, ... ] }

If `endpoint` is empty (not configured) or the call fails, `rerank()` falls back
to returning the original order unchanged (graceful degradation).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

import httpx

from .config import RerankerConfig

log = logging.getLogger(__name__)


@dataclass
class RerankResult:
    index: int          # position in the original `documents` list
    score: float


class Reranker:
    def __init__(self, cfg: RerankerConfig):
        self.cfg = cfg
        self._client: httpx.Client | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.endpoint)

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.cfg.api_key:
                # Jina uses Authorization: Bearer, Cohere uses Authorization: Bearer too
                headers["Authorization"] = f"Bearer {self.cfg.api_key}"
            self._client = httpx.Client(headers=headers, timeout=self.cfg.timeout)
        return self._client

    def rerank(
        self, query: str, documents: Sequence[str], top_n: int | None = None
    ) -> list[RerankResult]:
        """Rerank documents against the query. Returns ranked (index, score).

        Falls back to original order if reranker is disabled or fails.
        """
        n = len(documents)
        if n == 0:
            return []
        want = top_n if top_n is not None else self.cfg.top_n
        want = max(1, min(want, n))

        if not self.enabled:
            log.debug("reranker disabled, returning original order")
            return [RerankResult(i, 0.0) for i in range(n)]

        payload: dict = {"query": query, "documents": list(documents), "top_n": want}
        if self.cfg.model:
            payload["model"] = self.cfg.model

        try:
            r = self._get_client().post(self.cfg.endpoint.rstrip("/"), json=payload)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("reranker failed (%s); falling back to original order", e)
            return [RerankResult(i, 0.0) for i in range(n)]

        results: list[RerankResult] = []
        for item in data.get("results", []):
            idx = item.get("index")
            score = float(item.get("relevance_score", 0.0))
            if idx is not None and 0 <= idx < n:
                results.append(RerankResult(idx, score))
        # keep only the top `want`
        results.sort(key=lambda x: x.score, reverse=True)
        results = results[:want]
        if not results:
            log.warning("reranker returned no results; falling back to original order")
            return [RerankResult(i, 0.0) for i in range(n)]
        return results

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
