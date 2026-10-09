"""Add document_chunks table for per-chunk BM25 full-text search.

PMP Phase 6 requires BM25/keyword retrieval with proper citations.
This requires storing the chunk text and tsvector per chunk.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create document_chunks table for per-chunk BM25 search
    op.create_table(
        "document_chunks",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "document_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "search_vector",
            sa.Text(),
            nullable=False,
            comment="Precomputed tsvector for BM25 search",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
    )

    # Create composite index for tenant-scoped queries
    op.create_index(
        "ix_document_chunks_tenant_document",
        "document_chunks",
        ["tenant_id", "document_id"],
    )

    # Create GIN index on search_vector for fast BM25 queries.
    # PostgreSQL-only: GIN expressions are not available on SQLite, and the
    # index is not declared on the model, so the SQLite branch creates
    # nothing and the schema still matches the models.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_document_chunks_search_vector "
            "ON document_chunks USING GIN (to_tsvector('english', search_vector))"
        )


def downgrade() -> None:
    op.drop_index(
        "ix_document_chunks_search_vector", table_name="document_chunks", if_exists=True
    )
    op.drop_index("ix_document_chunks_tenant_document", table_name="document_chunks")
    op.drop_table("document_chunks")