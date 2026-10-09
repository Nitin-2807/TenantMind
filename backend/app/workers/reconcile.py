"""Reconciliation CLI: re-enqueue stale processing documents.

Run as:

    python -m app.workers.reconcile [--threshold-seconds N]

This is the manual reconciliation entry point for Step 4B. No Celery Beat,
no scheduler — an operator (or cron) invokes it when they choose.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.db.engine import get_engine
from app.db.session import build_session_factory, session_scope
from app.models.document import Document, DocumentStatus
from app.workers.ingestion import reconcile_stale_documents

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-enqueue documents stuck in 'processing' status"
    )
    parser.add_argument(
        "--threshold-seconds",
        type=int,
        default=None,
        help=(
            "Override the stale threshold (default: settings.ingestion_stale_threshold_seconds)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List stale documents without re-enqueueing",
    )
    args = parser.parse_args()

    try:
        threshold = args.threshold_seconds or settings.ingestion_stale_threshold_seconds
        factory = build_session_factory(get_engine(settings))

        if args.dry_run:
            # Dry run: just query and report
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=threshold)

            with session_scope(factory) as session:
                stale = session.scalars(
                    select(Document.id, Document.updated_at).where(
                        Document.status == DocumentStatus.PROCESSING,
                        Document.updated_at < cutoff,
                    )
                ).all()

            if stale:
                print(f"Found {len(stale)} stale document(s):")
                for doc_id, updated_at in stale:
                    print(f"  {doc_id}  (updated_at={updated_at.isoformat()})")
            else:
                print("No stale documents found.")
            return 0

        requeued = reconcile_stale_documents(factory, threshold_seconds=threshold)
        print(f"Re-enqueued {requeued} document(s)")
        return 0

    except Exception:  # pragma: no cover - CLI error path
        logger.exception("Reconciliation failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())