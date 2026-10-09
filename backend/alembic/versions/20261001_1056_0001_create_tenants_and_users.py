"""create tenants and users

The first real migration: the two tables that everything else in TenantMind will
hang off (PMP §14, PMP §12).

Reviewer notes — three places where this file deliberately departs from
verbatim autogenerate output, because the output was generated against SQLite
and this project targets PostgreSQL:

1. **Timestamp defaults are ``now()``, not ``CURRENT_TIMESTAMP``.** Autogenerate
   rendered SQLite's ``(CURRENT_TIMESTAMP)``. Both work on PostgreSQL, but
   ``now()`` is what :attr:`app.models.tenant.Tenant.created_at` declares via
   ``server_default=func.now()``, and matching the model's declared expression
   keeps autogenerate's ``compare_server_default`` comparison quiet. Written as
   ``sa.func.now()`` rather than ``sa.text("now()")`` so the value stays a
   SQLAlchemy expression instead of an opaque string.

2. **``is_active`` defaults to ``true``, not ``1``.** Same reason:
   ``sa.true()`` renders as ``true`` on PostgreSQL and matches the model.

3. **The role CHECK constraint is declared inline rather than through
   ``sa.Enum(create_constraint=True)``.** Two reasons. Inside a migration there
   is no naming convention in play, so the constraint's implicit name is
   unreliable; naming it explicitly pins it to exactly ``ck_users_user_role``,
   the same name the model produces, which is what stops a later autogenerate
   run from proposing to drop and recreate it. And it is declared *inside*
   ``create_table`` rather than as a separate ``op.create_check_constraint``
   call, because that standalone call emits ``ALTER TABLE ... ADD CONSTRAINT``,
   which SQLite does not support at all — it raises ``NotImplementedError`` and
   aborts the upgrade. An inline constraint is part of the ``CREATE TABLE`` and
   works on both dialects.

Nothing else was changed. The column set, types, widths, nullability, keys,
indexes and the composite unique constraint are all exactly what autogenerate
produced from the models.

Deliberately absent: ``tenants.plan``. PMP §14 lists it, but it is not in the
Phase 2 Step 1 field list, so it is deferred to a later step rather than
smuggled into the first migration. There is no ``documents``, ``query_logs`` or
``refresh_tokens`` table either.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Name of the role CHECK constraint, matching what the shared naming convention
#: produces for the model. Referenced rather than re-spelled in both directions.
ROLE_CHECK_CONSTRAINT = "ck_users_user_role"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenants_slug")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        # `create_constraint=False`: the Enum is only the VARCHAR type here, so
        # that the CHECK below is the single source of truth for the role
        # constraint rather than being duplicated in two places.
        sa.Column(
            "role",
            sa.Enum(
                "admin",
                "editor",
                "viewer",
                name="user_role",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            server_default="viewer",
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        # RESTRICT, not CASCADE: deleting a tenant must fail while it still has
        # users rather than silently removing an organisation's entire user
        # population. See TenantScopedMixin for the reasoning.
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_users_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        # Per-tenant email uniqueness. A bare UNIQUE(email) would stop the same
        # person holding accounts in two different tenants.
        sa.UniqueConstraint(
            "tenant_id", "email", name=op.f("uq_users_tenant_id_email")
        ),
        # Inline, so it lands in the CREATE TABLE. See reviewer note 3.
        sa.CheckConstraint(
            "role IN ('admin', 'editor', 'viewer')",
            name=op.f(ROLE_CHECK_CONSTRAINT),
        ),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=False)
    op.create_index(op.f("ix_users_tenant_id"), "users", ["tenant_id"], unique=False)


def downgrade() -> None:
    # Reverse order of creation. The CHECK goes before the table so it is not
    # left behind on a failure partway through.
    op.drop_index(op.f("ix_users_tenant_id"), table_name="users")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    # No explicit drop for the role CHECK: it was declared inline in
    # `create_table`, so dropping the table removes it. A standalone
    # `op.drop_constraint` would emit ALTER TABLE ... DROP CONSTRAINT, which
    # SQLite again does not support.
    op.drop_table("users")
    op.drop_table("tenants")
