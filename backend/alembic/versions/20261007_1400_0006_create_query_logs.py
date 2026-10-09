"""Create query_logs table for query history and feedback.

PMP Phase 7: query history + feedback.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "query_logs",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("grounded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("context_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="success",
        ),
        sa.Column("error_kind", sa.String(32), nullable=True),
        sa.Column(
            "citations",
            sa.JSON(none_as_null=True),
            nullable=True,
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("feedback", sa.String(8), nullable=True),
        sa.Column(
            "feedback_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('success', 'error')",
            name="ck_query_logs_status",
        ),
        sa.CheckConstraint(
            "feedback IS NULL OR feedback IN ('up', 'down')",
            name="ck_query_logs_feedback",
        ),
        sa.CheckConstraint(
            "error_kind IS NULL OR error_kind IN ('rejected', 'retrieval', 'generation', 'rate_limited')",
            name="ck_query_logs_error_kind",
        ),
    )

    # Composite indexes for tenant-scoped queries
    op.create_index(
        "ix_query_logs_tenant_created",
        "query_logs",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_query_logs_tenant_user_created",
        "query_logs",
        ["tenant_id", "user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_query_logs_tenant_user_created", table_name="query_logs")
    op.drop_index("ix_query_logs_tenant_created", table_name="query_logs")
    op.drop_table("query_logs")