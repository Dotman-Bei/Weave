"""Vector embeddings for the `Utterance.embedding` field in specification §4.1.

Purpose: semantic similarity *fallback*. Lexical overlap answers "does the
question share words with what is stored"; it cannot answer "does the question
mean the same thing". Asking for a favourite colour with the words "what colour
do I like best" ties, lexically, between the colour fact and an unrelated
"likes long walks" fact. An embedding separates them.

Deliberately optional. Without the model installed every path falls back to
lexical scoring and Weave behaves exactly as before -- the zero-dependency
promise is not traded away for this.

The provider is a small *static* embedding model: a lookup table distilled from
a sentence transformer. No torch, ~30MB, and encoding is a numpy gather rather
than a forward pass, which keeps ingestion in the millisecond range.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Protocol, Sequence

from .config import Settings, get_settings

log = logging.getLogger("weave.embeddings")


class Embedder(Protocol):
    """Encodes text into L2-normalised vectors, so cosine == dot product."""

    name: str
    dim: int

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Similarity of two vectors from :meth:`Embedder.encode`.

    Both are already unit length, so this is a plain dot product; it stays
    tolerant of a non-normalised input rather than silently returning nonsense.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


class StaticEmbedder:
    """model2vec static embeddings."""

    def __init__(self, model_name: str) -> None:
        from model2vec import StaticModel  # optional dependency

        self.name = model_name
        self._model = StaticModel.from_pretrained(model_name)
        self.dim = int(self._model.dim)

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(list(texts))
        out: list[list[float]] = []
        for vector in vectors:
            values = [float(v) for v in vector]
            norm = math.sqrt(sum(v * v for v in values))
            if norm > 0:
                values = [v / norm for v in values]
            # Four decimals keeps the JSON payload of a 256-dim vector under a
            # kilobyte without measurably changing similarity.
            out.append([round(v, 4) for v in values])
        return out


_embedder: Embedder | None = None
_checked = False


def get_embedder(settings: Settings | None = None) -> Embedder | None:
    """Return the configured embedder, or ``None`` when unavailable.

    Resolved once per process. A missing package, an absent model or no network
    on first load are all non-fatal: the caller falls back to lexical scoring.
    """
    global _embedder, _checked
    settings = settings or get_settings()

    if settings.embeddings == "off":
        return None
    if _checked:
        return _embedder

    _checked = True
    try:
        _embedder = StaticEmbedder(settings.embedding_model)
        log.info("embeddings enabled: %s (dim=%d)", _embedder.name, _embedder.dim)
    except Exception as exc:  # pragma: no cover - depends on environment
        log.info("embeddings unavailable, using lexical scoring only: %s", exc)
        _embedder = None
    return _embedder


def embed_one(text: str, settings: Settings | None = None) -> list[float]:
    """Encode a single string, returning ``[]`` when embeddings are off."""
    embedder = get_embedder(settings)
    if embedder is None or not (text or "").strip():
        return []
    try:
        vectors = embedder.encode([text])
    except Exception:  # pragma: no cover
        return []
    return vectors[0] if vectors else []


def embed_many(
    texts: Sequence[str], settings: Settings | None = None
) -> list[list[float]]:
    """Encode a batch, returning empty vectors when embeddings are off."""
    embedder = get_embedder(settings)
    if embedder is None or not texts:
        return [[] for _ in texts]
    try:
        return embedder.encode(list(texts))
    except Exception:  # pragma: no cover
        return [[] for _ in texts]


def reset_embedder() -> None:
    """Test hook."""
    global _embedder, _checked
    _embedder = None
    _checked = False


def describe(settings: Settings | None = None) -> dict[str, Any]:
    embedder = get_embedder(settings)
    if embedder is None:
        return {"enabled": False, "model": None, "dim": 0}
    return {"enabled": True, "model": embedder.name, "dim": embedder.dim}
