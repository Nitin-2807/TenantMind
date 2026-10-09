"""Deterministic lexical-hash embedding provider for the Phase 8 benchmark.

Why it exists
-------------
A real bi-encoder (Gemini ``text-embedding-004`` or a local
``sentence-transformers`` model) is **not available in this environment** — there
is no Gemini key and ``torch``/``sentence-transformers`` are not installed. The
production embedding provider therefore cannot be measured. Fabricating numbers
for it is exactly what the phase brief forbids (§20, §34).

What this stands in for, and how it is labelled
-----------------------------------------------
Instead of fabricating, the benchmark embeds with a deterministic
feature-hashing provider: text is tokenised, each content word is hashed to a
fixed dimension bucket with a sign bit, weighted by term frequency, and the
result is L2-normalised. Cosine similarity between two texts therefore rises
with shared content vocabulary — a genuine, reproducible lexical signal that
exercises the **real** dense pipeline (embedding boundary, Qdrant cosine
search, RRF fusion, payload filtering) end to end.

It is a stand-in, not a production model:

* the model name is ``lexical-hash-eval-v1`` and the report states it;
* it has no synonymy or word-order understanding, so its dense retrieval
  quality is expected to be modest and lower-bound-like;
* dense-vs-BM25 comparisons in the report carry this caveat explicitly.

All vectors are pure functions of the input text, so two runs embed identically
and the benchmark is reproducible (§13 of the phase brief).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from app.rag.embeddings import EmbeddingProvider

#: Distinctive model name so no reader can mistake this provider's output for a
#: production embedding model's.
MODEL_NAME: str = "lexical-hash-eval-v1"

#: Small fixed stopword set, identical treatment as :mod:`evaluation.metrics`.
_STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "i", "in", "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were", "will", "with", "would", "can", "could", "do", "does", "did", "not", "no", "yes", "what", "which", "who", "whom", "how", "when", "where", "why"]
)

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in _TOKEN.findall(text.lower())
        if token not in _STOPWORDS and len(token) > 1
    ]


def lexical_hash_vector(text: str, *, dimensions: int) -> list[float]:
    """A deterministic, L2-normalised feature-hashing vector for ``text``.

    Each content token maps deterministically to one dimension (plus a sign bit
    drawn from the digest) and contributes its term frequency. The vector is
    normalised to unit length, matching the Cosine collection the store
    creates. A text with no content tokens yields the uniform direction
    ``1/sqrt(dim)`` so the embedding call never produces a zero vector.
    """
    weights: list[float] = [0.0] * dimensions
    counts: dict[int, float] = {}
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        # Term-frequency within the hashed bucket: a token family appearing
        # twice contributes twice its signed weight.
        counts[index] = counts.get(index, 0.0) + sign
    for index, weight in counts.items():
        weights[index] = weight
    norm = math.sqrt(sum(weight * weight for weight in weights))
    if norm == 0.0:
        unit = 1.0 / math.sqrt(dimensions)
        return [unit] * dimensions
    return [weight / norm for weight in weights]


class LexicalHashEmbeddings(EmbeddingProvider):
    """An :class:`~app.rag.embeddings.EmbeddingProvider` over
    :func:`lexical_hash_vector`. Deterministic and fully offline."""

    def __init__(self, *, dimensions: int = 512) -> None:
        self._dimensions = dimensions
        self._closed = False

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return MODEL_NAME

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed each text; the width check is inherited from the ABC."""
        return [lexical_hash_vector(text, dimensions=self._dimensions) for text in texts]

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed