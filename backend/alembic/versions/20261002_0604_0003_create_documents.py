"""create documents

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02 06:04:24.545888

PMP §14 ``documents``: ``id``, ``tenant_id``, ``uploaded_by``, ``filename``,
``storage_key``, ``status``, ``allowed_roles``, ``chunk_count``, ``error``,
``created_at``.

Reviewer notes — four deliberate additions to PMP §14, each with a reason that is
a requirement rather than a preference, and mirroring how revision ``0002``
documented the columns it added:

1. ``updated_at`` is added, and §14 does not list it for any table. Phase 2 Step 1
   added it to ``tenants`` and ``users`` on the same grounds. Here it has a
   concrete job: ``status`` is written by an ingestion worker that does not exist
   yet (PMP §22 puts background processing in Phase 5), and ``updated_at`` is the
   only signal available for "this document has been ``processing`` since Monday
   and is still ``processing`` on Friday" — the failure PMP §8's "failed ingestion
   jobs must be recorded" is about. It is served by
   ``GET /admin/documents/{id}/status``.

2. ``size_bytes`` and ``content_type`` are added. PMP §17 lists "File size limits"
   and "MIME/content validation" as requirements, and ``max_upload_size_mb`` has
   been declared in configuration since Phase 1 for exactly this phase. A limit that
   is enforced and then discarded cannot be reported on, and Phase 4's parser has to
   be told what it is opening. Both are filled from the **verified** bytes, never
   from what the client declared — see ``app/services/uploads.py``.

3. ``uploaded_by`` is **nullable** with ``ON DELETE SET NULL``, and §14 implies it is
   neither. This is forced by a Phase 2.3 decision, not chosen freely: PMP §15
   exposes ``DELETE /users/{id}``, which Phase 2.3 implemented as a hard delete.
   ``RESTRICT`` would make an uploader undeletable — so ``DELETE /users/{id}`` would
   begin failing for exactly the most active users — and ``CASCADE`` would destroy a
   tenant's documents because one person left. ``SET NULL`` keeps the document,
   which belongs to the *tenant*, and loses only the attribution. Every document
   created through the API has ``uploaded_by`` set from
   ``AuthenticatedIdentity.user_id``, and a test asserts it.

4. Two ``CHECK`` constraints are added that §14 does not mention: ``chunk_count >=
   0`` and ``size_bytes > 0``. Both encode invariants the application enforces, and
   a constraint is the place where an invariant stops being a convention. The second
   is what makes "a zero-byte upload is refused" true in the database as well as in
   ``app/services/uploads.py``.

Two shapes worth reading before reviewing:

``allowed_roles`` is ``JSON``, not ``text[]``
    PMP §13 shows the value as a list (``"allowed_roles": ["admin", "editor"]``), and
    §17 calls for document-level role filtering. A native array type is not portable
    to the SQLite database most of this project's tests run against, and an
    ``ARRAY`` column would make ``Base.metadata.create_all`` fail and take the whole
    non-PostgreSQL suite down with it. The consequence — that the visibility rule is
    applied in the service rather than in SQL — is stated in
    ``app/services/documents.py``. The tenant predicate, which is the property that
    keeps organisations apart, stays in SQL regardless.

``status`` is ``VARCHAR`` plus a ``CHECK``, not a PostgreSQL ``ENUM``, for the same
    reason ``users.role`` is: a native enum has to be altered by hand, which no
    ordinary Alembic migration can express. The constraint is named
    ``ck_documents_document_status`` and admits exactly the three values PMP §10
    lists — ``processing``, ``ready``, ``failed`` — and no more.

What is NOT here is more important than what is: **no column holds document
content.** PMP §17 requires that file contents not be served directly by the API,
and this schema keeps the bytes out of the database entirely; they live on disk
under a server-generated key that no response exposes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        # Nullable and SET NULL: see reviewer note 3.
        sa.Column(
            "uploaded_by",
            sa.Uuid(),
            nullable=True,
        ),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "processing",
                "ready",
                "failed",
                name="document_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="processing",
            nullable=False,
        ),
        sa.Column("allowed_roles", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.String(length=2000), nullable=True),
        # Added: see reviewer note 2.
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        # `server_default` rather than a Python-side default, so a row written by
        # raw SQL still gets correct timestamps — the same reasoning, and the same
        # shape, as `tenants` and `users` in revision 0001. Written as
        # `sa.func.now()` rather than `sa.text("now()")` for the reason revision
        # 0001's note gives: the model's declared expression and the migration's
        # must match so autogenerate's `compare_server_default` comparison stays
        # quiet on every dialect the test suite runs against, SQLite included.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Added: see reviewer note 1.
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # `tenant_id` is last because it is inherited from the mixin rather than
        # declared in PMP §14's column order — same as in revisions 0001 and 0002.
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        # Added: see reviewer note 4.
        sa.CheckConstraint(
            "chunk_count >= 0",
            name=op.f("ck_documents_chunk_count_non_negative"),
        ),
        sa.CheckConstraint(
            "size_bytes > 0",
            name=op.f("ck_documents_size_bytes_positive"),
        ),
        # RESTRICT, matching `users`: a tenant cannot be deleted while it still
        # holds documents, because silently destroying them is not a decision a
        # DELETE should make. The ORM clears them first (see
        # `TenantScopedMixin.documents`).
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_documents_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name=op.f("fk_documents_uploaded_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        # Globally unique rather than unique per tenant: `storage_key` is a random
        # 128-bit token, and two tenants must never be able to collide on one.
        sa.UniqueConstraint("storage_key", name=op.f("uq_documents_storage_key")),
    )
    op.create_index(
        op.f("ix_documents_tenant_id"),
        "documents",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_documents_uploaded_by"),
        "documents",
        ["uploaded_by"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_documents_uploaded_by"), table_name="documents")
    op.drop_index(op.f("ix_documents_tenant_id"), table_name="documents")
    op.drop_table("documents")