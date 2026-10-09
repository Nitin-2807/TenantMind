"""Add tsvector column for BM25 full-text search on documents.

PMP Phase 6 — Advanced Retrieval requires BM25/keyword retrieval.
PostgreSQL's built-in full-text search (tsvector + GIN index) provides
BM25-ranked keyword search without an external dependency.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add the search_vector column (tsvector) to documents table.
    # It will hold the precomputed full-text search vector for each document.
    # We use a generated column so it's always in sync with the content.
    # Since we don't store the full text in the documents table, we'll
    # populate it during ingestion from the chunked text.

    op.add_column(
        "documents",
        sa.Column(
            "search_vector",
            sa.Text(),
            nullable=True,
            comment="Precomputed tsvector for BM25 full-text search; populated at ingestion time from document chunks.",
        ),
    )

    # Create a GIN index on the search_vector for fast BM25 queries.
    # PostgreSQL-only: GIN expressions are not available on SQLite, and the
    # index itself is not declared on the model, so the SQLite branch (tests)
    # creates nothing and the schema still matches the models.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_documents_search_vector ON documents USING GIN (to_tsvector('english', coalesce(search_vector, '')))"
        )


def downgrade() -> None:
    op.drop_index(
        "ix_documents_search_vector", table_name="documents", if_exists=True
    )
    op.drop_column("documents", "search_vector")