"""create refresh_tokens

Revision ID: 0002
Revises: 0001

PMP §14 ``refresh_tokens``: ``id``, ``user_id``, ``token_hash``, ``expires_at``,
``revoked``, ``created_at``.

Reviewer notes — three deliberate departures from PMP §14 and from what
autogenerate would emit, each with a reason that is not "cleaner":

1. **``tenant_id`` is added, and it is not in PMP §14.** Phase 2 Step 1
   established the rule that every table except ``tenants`` is tenant-scoped,
   and the model inherits
   :class:`~app.models.tenant.TenantScopedMixin` rather than re-declaring the
   column. Its foreign key is ``ON DELETE RESTRICT``, matching ``users``: a
   tenant cannot be deleted while it still has credentials outstanding, because
   silently destroying them is not a decision a ``DELETE`` should make.

2. **``jti`` is added.** PMP §14 does not list it, but the token design in
   :mod:`app.core.security` puts a unique ``jti`` in every refresh token and
   stores it in the database so revocation can be addressed by a unique-index
   lookup rather than by scanning and hashing every row. It is ``UNIQUE``.

3. **The ``revoked`` column is a boolean, not a timestamp.** PMP §14 says
   ``revoked``; that is the authoritative state every check reads. No
   ``revoked_at`` is added, because the two would be redundant state that can
   disagree and nothing in this step needs the revocation *time*.

What is NOT here is more important than what is: **no column holds a token.**
``token_hash`` is a 64-character hex SHA-256 digest
(:func:`app.core.security.hash_refresh_token`). A dump of this table yields no
usable credential. This is why ``token_hash`` is ``VARCHAR(64)`` — SHA-256 is
exactly 32 bytes, so 64 hex characters is the exact width of the output, not a
guess.

``user_id`` cascades; ``tenant_id`` restricts. The asymmetry is intentional and
is argued in :mod:`app.models.refresh_token`: a refresh token is a credential
that must die with its user (PMP §15 exposes ``DELETE /users/{id}``), whereas a
tenant is a governance boundary whose tokens are revoked, not cascaded away.

Timestamp and boolean defaults use ``sa.func.now()`` / ``sa.false()`` rather
than SQLite's ``CURRENT_TIMESTAMP`` / ``0``, for the reason recorded in revision
0001: the migration is generated and verified against SQLite but targets
PostgreSQL, and matching the model's declared expressions keeps
``compare_server_default`` in ``alembic/env.py`` quiet.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "revoked",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_refresh_tokens_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("jti", name=op.f("uq_refresh_tokens_jti")),
    )
    # Both indexes come from the model: `tenant_id` from
    # TenantScopedMixin (index=True), `user_id` from the column itself. Created
    # explicitly because Alembic does not create indexes that are declared
    # inline on a column — it emits them as separate operations.
    op.create_index(
        "ix_refresh_tokens_tenant_id",
        "refresh_tokens",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_refresh_tokens_user_id",
        "refresh_tokens",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_tenant_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")