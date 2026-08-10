"""Embedding backends for the optional vector-retrieval path (stage B4).

A backend turns text into a fixed-dimension float vector. The default backend
is :class:`DisabledEmbedding`, which reports ``available() == False`` so the
pipeline never builds or queries a vector index and simply falls back to
FTS5-only. A real backend (an OpenAI-compatible ``/embeddings`` endpoint) is
opt-in via configuration; when it is present the retrieval layer enables hybrid
search.

No synthetic / hash embedding is provided on purpose: feeding a non-semantic
vector into the recall metrics would make hybrid retrieval look better than it
is and hide regressions. Tests that exercise the vector code path inject their
own clearly-labelled *test-only* stub backend; production code never does.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

from upgradelens.db.vector import EmbeddingBackend, VectorIndexUnavailable


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    """Connection details for an OpenAI-compatible embeddings endpoint."""

    base_url: str
    model: str
    api_key: str = ""
    dimension: int = 0  # 0 = read the dimension from the API response


class DisabledEmbedding:
    """No embedding model wired up -- vector retrieval stays off."""

    model: str = ""
    dimension: int = 0

    def available(self) -> bool:
        return False

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None


class OpenAICompatibleEmbedding:
    """Embed texts via an OpenAI-compatible ``/embeddings`` endpoint.

    Uses only the standard library so no extra dependency is required. The
    endpoint must accept ``{"input": [...], "model": ...}`` and return
    ``{"data": [{"embedding": [...]}, ...]}``.
    """

    def __init__(self, config: OpenAICompatibleConfig) -> None:
        self.model = config.model
        self.dimension = config.dimension
        self._base_url = config.base_url.rstrip("/")
        self._api_key = config.api_key
        # DashScope / OpenAI-compatible `/embeddings` caps the number of input
        # texts per request (text-embedding-v3 rejects >10 with HTTP 400). Batching
        # keeps a large `rebuild` working on any compliant endpoint.
        self._batch_size = 8
        self._probe()

    def _probe(self) -> None:
        # Liveness + dimension discovery so a misconfigured backend fails loudly
        # at construction time rather than mid-retrieval.
        out = self.embed(["probe"])
        if out is None or not out:
            raise VectorIndexUnavailable("embedding endpoint returned no vectors")

    def available(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors = self._embed_batch(batch)
            if vectors is None:
                return None
            out.extend(vectors)
        if self.dimension == 0 and out:
            self.dimension = len(out[0])
        return out

    def _embed_batch(self, batch: list[str]) -> list[list[float]] | None:
        payload = json.dumps({"input": batch, "model": self.model}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # network / endpoint failure
            raise VectorIndexUnavailable(f"embedding request failed: {exc}") from exc
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            return None
        return [d["embedding"] for d in data]


def embedding_from_config(
    *,
    enabled: bool,
    base_url: str,
    model: str,
    api_key: str,
    dimension: int = 0,
) -> EmbeddingBackend:
    """Build the configured backend, or a :class:`DisabledEmbedding` when off.

    A backend that cannot be reached at construction time degrades to disabled
    rather than raising, so a flaky embedding endpoint never breaks retrieval.
    """
    if not enabled or not base_url or not model:
        return DisabledEmbedding()
    try:
        return OpenAICompatibleEmbedding(
            OpenAICompatibleConfig(
                base_url=base_url, model=model, api_key=api_key, dimension=dimension
            )
        )
    except VectorIndexUnavailable:
        return DisabledEmbedding()
