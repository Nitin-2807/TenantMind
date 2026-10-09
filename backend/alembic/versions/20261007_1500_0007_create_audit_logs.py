"""Create audit_logs table for audit trail.

PMP Phase 7: audit logs.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-07

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
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
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=True),
        sa.Column(
            "resource_id",
            sa.Uuid(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "outcome",
            sa.String(16),
            nullable=False,
            server_default="success",
        ),
        sa.Column(
            "details",
            sa.JSON(none_as_null=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'denied', 'error')",
            name="ck_audit_logs_outcome",
        ),
    )

    # Composite indexes for tenant-scoped queries
    op.create_index(
        "ix_audit_logs_tenant_created",
        "audit_logs",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_audit_logs_tenant_user_created",
        "audit_logs",
        ["tenant_id", "user_id", "created_at"],
    )
    op.create_index(
        "ix_audit_logs_tenant_action_created",
        "audit_logs",
        ["tenant_id", "action", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_tenant_action_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_tenant_user_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_tenant_created", table_name="audit_logs")
    op.drop_table("audit_logs")