"""Cross-encoder reranking for hybrid retrieval results.

PMP Phase 6 deliverable: Cross-encoder reranking.

This module provides a reranker interface that can be implemented with
different backends (local cross-encoder, API-based, etc.). The default
implementation uses a local sentence-transformers cross-encoder for
deterministic, offline testing. An API-based implementation can be
swapped in via configuration.

The reranker takes a question and a list of candidate chunks and returns
them re-ranked by relevance to the question.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, Protocol

from app.rag.retrieval import RetrievedChunk

logger = logging.getLogger(__name__)

#: Maximum number of candidates to rerank (to control latency).
#: Cross-encoders are slower than bi-encoders, so we limit the input.
RERANK_MAX_CANDIDATES: Final[int] = 20

#: Default cross-encoder model for local reranking.
#: This is a small, fast model suitable for CPU inference.
DEFAULT_CROSS_ENCODER_MODEL: Final[str] = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class Reranker(Protocol):
    """Protocol for a reranker that scores (question, chunk) pairs.

    A reranker takes a question and a list of chunks and returns the chunks
    re-ordered by relevance to the question, with scores.
    """

    def rerank(
        self,
        question: str,
        chunks: list[RetrievedChunk],
        top_k: int | None = None,
    ) -> list[tuple[RetrievedChunk, float]]:
        """Re-rank chunks by relevance to the question.

        Args:
            question: the user's question.
            chunks: candidate chunks to rerank.
            top_k: if set, return only the top k chunks.

        Returns:
            List of (chunk, score) tuples, sorted by score descending.
        """
        ...


@dataclass(frozen=True, slots=True)
class RerankResult:
    """Result of reranking.

    Attributes:
        chunks: the reranked chunks (subset of input, reordered).
        scores: the reranker scores corresponding to each chunk.
        model_name: the model used for reranking.
    """

    chunks: list[RetrievedChunk]
    scores: list[float]
    model_name: str


class NoOpReranker:
    """No-op reranker that returns chunks in original order with score 1.0.

    Used when reranking is disabled or unavailable. This keeps the pipeline
    functional without a cross-encoder model.
    """

    model_name: Final[str] = "noop"

    def rerank(
        self,
        question: str,
        chunks: list[RetrievedChunk],
        top_k: int | None = None,
    ) -> list[tuple[RetrievedChunk, float]]:
        result = [(chunk, 1.0) for chunk in chunks]
        if top_k is not None:
            result = result[:top_k]
        return result


class CrossEncoderReranker:
    """Local cross-encoder reranker using sentence-transformers.

    This implementation loads a cross-encoder model locally and uses it to
    score (question, chunk) pairs. It is deterministic and requires no
    external API calls, making it suitable for testing and offline use.

    Note: This requires the `sentence-transformers` and `torch` packages,
    which are not installed by default. If they are not available, the
    reranker falls back to NoOpReranker.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_CROSS_ENCODER_MODEL,
        device: str | None = None,
    ) -> None:
        self.model_name = model_name
        self._model = None
        self._device = device
        self._load_model()

    def _load_model(self) -> None:
        """Load the cross-encoder model.

        If sentence-transformers is not available, logs a warning and
        leaves _model as None (will fall back to no-op).
        """
        try:
            import torch  # type: ignore[import-not-found]
            from sentence_transformers import (  # type: ignore[import-not-found]
                CrossEncoder,
            )

            if self._device is None:
                self._device = "cuda" if torch.cuda.is_available() else "cpu"

            logger.info(
                "loading cross-encoder model %s on %s",
                self.model_name,
                self._device,
            )
            self._model = CrossEncoder(self.model_name, device=self._device)
        except ImportError:
            logger.warning(
                "sentence-transformers not installed; reranking disabled. "
                "Install with: pip install sentence-transformers torch"
            )
            self._model = None
        except Exception:  # noqa: BLE001 - model load failure
            logger.warning(
                "failed to load cross-encoder model %s; reranking disabled",
                self.model_name,
            )
            self._model = None

    def rerank(
        self,
        question: str,
        chunks: list[RetrievedChunk],
        top_k: int | None = None,
    ) -> list[tuple[RetrievedChunk, float]]:
        """Re-rank chunks using the cross-encoder.

        If the model failed to load, falls back to original order with
        uniform scores.
        """
        if self._model is None:
            logger.debug("cross-encoder not available; returning original order")
            result = [(chunk, 1.0) for chunk in chunks]
            if top_k is not None:
                result = result[:top_k]
            return result

        if not chunks:
            return []

        # Limit candidates to control latency
        candidates = chunks[:RERANK_MAX_CANDIDATES]

        # Prepare pairs for the cross-encoder: (question, chunk_text)
        pairs = [(question, chunk.text) for chunk in candidates]

        try:
            scores = self._model.predict(pairs, show_progress_bar=False)
        except Exception:  # noqa: BLE001 - cross-encoder can raise various errors
            logger.warning("cross-encoder prediction failed; returning original order")
            result = [(chunk, 1.0) for chunk in chunks]
            if top_k is not None:
                result = result[:top_k]
            return result

        # Sort by score descending
        scored = list(zip(candidates, scores, strict=True))
        scored.sort(key=lambda x: x[1], reverse=True)

        if top_k is not None:
            scored = scored[:top_k]

        return scored


def get_reranker(
    enabled: bool = True,
    model_name: str | None = None,
) -> Reranker:
    """Factory function to get a reranker instance.

    Args:
        enabled: whether to enable reranking. If False, returns NoOpReranker.
        model_name: optional model name for CrossEncoderReranker.

    Returns:
        A reranker instance.
    """
    if not enabled:
        return NoOpReranker()

    return CrossEncoderReranker(model_name=model_name or DEFAULT_CROSS_ENCODER_MODEL)


def rerank_chunks(
    reranker: Reranker,
    question: str,
    chunks: list[RetrievedChunk],
    top_k: int | None = None,
) -> RerankResult:
    """Convenience function to rerank chunks and return a structured result.

    Args:
        reranker: the reranker to use.
        question: the user's question.
        chunks: candidate chunks to rerank.
        top_k: if set, return only the top k chunks.

    Returns:
        RerankResult with reranked chunks, scores, and model name.
    """
    reranked = reranker.rerank(question, chunks, top_k)
    chunks_out = [chunk for chunk, _ in reranked]
    scores = [score for _, score in reranked]
    model_name = getattr(reranker, "model_name", "unknown")

    return RerankResult(
        chunks=chunks_out,
        scores=scores,
        model_name=model_name,
    )