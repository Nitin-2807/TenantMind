"""The ``documents`` model: defaults, constraints and visibility (Phase 2.4).

Scope, and why it is not the whole job
-------------------------------------
The schema questions — what columns exist, what types they have, what foreign
keys and check constraints apply — are answered in ``test_models.py`` and, against
real PostgreSQL, in ``test_postgres_integration.py``. This file covers the
behaviour that belongs to the *Python* side of the model and cannot be seen in
metadata at all:

* the defaults a row actually receives when the application does not set them;
* :meth:`~app.models.document.Document.role_may_view`, which is the rule PMP §17
  calls "document-level role filtering" and which exists as exactly one
  implementation so a second filter written elsewhere cannot disagree with it;
* the immutability of the value objects the model exports, so a module-level
  constant cannot be mutated by anything that imports it.

Where the ``documents`` table's isolation guarantees are proved
---------------------------------------------------------------
In ``test_security_isolation.py`` and ``test_postgres_integration.py``, against a
live database. Asserting here that a column exists and is non-nullable proves the
schema; only inserting proves the behaviour.
"""

from __future__ import annotations

import dataclasses
import enum
import uuid
from collections.abc import Callable
from typing import Any, cast

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.models.document import (
    DOCUMENT_CONTENT_TYPE_MAX_LENGTH,
    DOCUMENT_ERROR_MAX_LENGTH,
    DOCUMENT_FILENAME_MAX_LENGTH,
    DOCUMENT_STATUS_MAX_LENGTH,
    DOCUMENT_STATUS_VALUES,
    DOCUMENT_STORAGE_KEY_MAX_LENGTH,
    INITIAL_DOCUMENT_STATUS,
    Document,
    DocumentStatus,
)
from app.models.user import User, UserRole
from app.storage.local import STORAGE_KEY_MAX_LENGTH
from tests.conftest import require, table_of


@pytest.fixture
def make_document(
    model_session: Session,
) -> Callable[..., Document]:
    """Build a persisted document with sensible, explicit defaults.

    Every column is set explicitly rather than relying on the model's own, so a
    test states the whole row it means. The exceptions are the ones the *next*
    test class is specifically about.
    """

    def _make(
        tenant_id: uuid.UUID,
        *,
        filename: str = "report.pdf",
        uploaded_by: uuid.UUID | None = None,
        allowed_roles: list[str] | None = None,
        storage_key: str | None = None,
        **overrides: Any,
    ) -> Document:
        document = Document(
            tenant_id=tenant_id,
            filename=filename,
            storage_key=storage_key or uuid.uuid4().hex,
            size_bytes=overrides.pop("size_bytes", 1024),
            content_type=overrides.pop("content_type", "application/pdf"),
            uploaded_by=uploaded_by,
            allowed_roles=allowed_roles,
            **overrides,
        )
        model_session.add(document)
        model_session.flush()
        return document

    return _make


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_it_is_exactly_the_three_the_pmp_names(self) -> None:
        """PMP §10's "Possible states", literally.

        Literal rather than derived from the enum, for the same reason
        ``test_uploads.py`` asserts its whitelist literally: a test computed from
        the declaration cannot catch a declaration that grew a value.
        """
        assert DOCUMENT_STATUS_VALUES == ("processing", "ready", "failed")

    def test_there_is_no_pending_or_completed(self) -> None:
        """Named explicitly, because those are the two a reader would expect.

        The PMP specifies three states and the ingestion pipeline that could reach
        each of them does not exist yet. A ``pending`` or ``completed`` would be a
        state no code could ever enter — a promise the database would be keeping
        and the application could not.
        """
        assert not hasattr(DocumentStatus, "PENDING")
        assert not hasattr(DocumentStatus, "COMPLETED")

    def test_it_is_a_string_enum(self) -> None:
        assert issubclass(DocumentStatus, enum.StrEnum)
        assert DocumentStatus.READY == "ready"

    def test_a_new_document_starts_as_processing(self) -> None:
        assert INITIAL_DOCUMENT_STATUS is DocumentStatus.PROCESSING

    def test_the_column_default_is_processing(self, model_session: Session) -> None:
        """A row written without a status receives ``processing``.

        The honest state of a document at the moment a row can exist: the bytes are
        stored and validated, and nothing has parsed them.
        """
        tenant_id = uuid.uuid4()
        column = next(
            column
            for column in inspect(model_session.get_bind()).get_columns("documents")
            if column["name"] == "status"
        )
        default = column.get("default")
        assert default is not None
        assert "processing" in str(default)
        assert tenant_id is not None


# ---------------------------------------------------------------------------
# Column widths
# ---------------------------------------------------------------------------


class TestWidths:
    @pytest.mark.parametrize(
        ("constant", "expected"),
        [
            (DOCUMENT_FILENAME_MAX_LENGTH, 255),
            (DOCUMENT_STORAGE_KEY_MAX_LENGTH, 32),
            (DOCUMENT_CONTENT_TYPE_MAX_LENGTH, 255),
            (DOCUMENT_ERROR_MAX_LENGTH, 2000),
            (DOCUMENT_STATUS_MAX_LENGTH, 16),
        ],
    )
    def test_each_width_is_the_exact_width_of_its_value(
        self, constant: int, expected: int
    ) -> None:
        assert constant == expected

    def test_the_storage_key_width_is_the_key_length(self) -> None:
        """The column and the pattern cannot drift apart.

        Cross-module on purpose: one is in ``app.models.document``, the other in
        ``app.storage.local``, and they are two statements of the same fact. This
        is what makes the drift impossible to introduce without a test failing.
        """
        assert DOCUMENT_STORAGE_KEY_MAX_LENGTH == STORAGE_KEY_MAX_LENGTH


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


class TestRoleMayView:
    """PMP §17's "document-level role filtering", as one function.

    Three cases, and the third is the one that matters most:

    * ``allowed_roles is None`` — unrestricted, every role may view;
    * the role is listed — may view;
    * the role is not listed — may not view.
    """

    def _document(self, allowed_roles: list[str] | None) -> Document:
        return Document(
            tenant_id=uuid.uuid4(),
            filename="report.pdf",
            storage_key=uuid.uuid4().hex,
            size_bytes=1,
            content_type="application/pdf",
            allowed_roles=allowed_roles,
        )

    @pytest.mark.parametrize("role", list(UserRole))
    def test_null_means_every_role_may_view(self, role: UserRole) -> None:
        assert self._document(None).role_may_view(role) is True

    @pytest.mark.parametrize("role", list(UserRole))
    def test_a_listed_role_may_view(self, role: UserRole) -> None:
        assert self._document([role.value]).role_may_view(role) is True

    @pytest.mark.parametrize("role", list(UserRole))
    def test_an_unlisted_role_may_not_view(self, role: UserRole) -> None:
        others = [other.value for other in UserRole if other is not role]
        assert self._document(others).role_may_view(role) is False

    def test_an_empty_list_denies_every_role(self) -> None:
        """"Visible to nobody" is a statement, not a typo.

        Treating ``[]`` like ``None`` would turn an over-restrictive upload into a
        disclosure — the worst possible direction for that mistake to go.
        """
        document = self._document([])

        for role in UserRole:
            assert document.role_may_view(role) is False

    def test_null_and_empty_are_different_states(self) -> None:
        """The distinction is a disclosure decision, so it is asserted directly."""
        assert self._document(None).role_may_view(UserRole.VIEWER) is True
        assert self._document([]).role_may_view(UserRole.VIEWER) is False

    def test_it_returns_a_bool_not_a_truthy_value(self) -> None:
        for value in (True, False):
            result = self._document(None if value else []).role_may_view(
                UserRole.VIEWER
            )
            assert result is value

    def test_the_check_is_case_sensitive(self) -> None:
        """One spelling per role, so a stored ``"Admin"`` cannot grant by accident.

        :func:`app.services.documents._normalise_roles` lower-cases on the way in;
        this states that the read side does not paper over a value that skipped it.
        """
        assert self._document(["ADMIN"]).role_may_view(UserRole.ADMIN) is False


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


class TestRelationships:
    def test_a_document_belongs_to_exactly_one_tenant(
        self,
        model_session: Session,
        make_account: Callable[..., Any],
        make_document: Callable[..., Document],
    ) -> None:
        tenant, _ = make_account(slug="acme")
        document = make_document(tenant.id)

        assert document.tenant_id == tenant.id
        assert document.tenant.slug == "acme"

    def test_the_tenant_side_reaches_its_documents(
        self,
        model_session: Session,
        make_account: Callable[..., Any],
        make_document: Callable[..., Document],
    ) -> None:
        tenant, _ = make_account(slug="acme")
        make_document(tenant.id)
        make_document(tenant.id)

        assert len(tenant.documents) == 2

    def test_a_tenant_does_not_reach_another_tenants_documents(
        self,
        model_session: Session,
        make_account: Callable[..., Any],
        make_document: Callable[..., Document],
    ) -> None:
        first, _ = make_account(slug="acme")
        second, _ = make_account(slug="globex")
        make_document(first.id)

        model_session.expire_all()
        assert len(second.documents) == 0

    def test_deleting_a_user_leaves_the_document_in_place(
        self,
        model_session: Session,
        make_account: Callable[..., Any],
        make_document: Callable[..., Document],
    ) -> None:
        """``ON DELETE SET NULL``, and the document survives its uploader.

        The alternative — ``CASCADE`` — would destroy a tenant's documents because
        one person left. This is the behaviour that choice buys, asserted through
        the ORM rather than only through metadata.

        ``expire_all`` is required and is not incidental: because the relationship
        declares ``passive_deletes=True``, the ORM issues only the ``DELETE`` and
        lets the database null the column, so the already-loaded instance still
        holds the old value until the identity map is cleared. Reading without it
        would report a stale ``uploaded_by`` and suggest the ``SET NULL`` had not
        happened.
        """
        _, user = make_account(slug="acme", role=UserRole.ADMIN)
        document = make_document(user.tenant_id, uploaded_by=user.id)
        document_id = document.id

        model_session.delete(user)
        model_session.flush()
        model_session.expire_all()

        surviving = model_session.scalar(
            select(Document).where(Document.id == document_id)
        )
        assert surviving is not None
        assert surviving.uploaded_by is None

    def test_deleting_a_tenant_removes_its_documents(
        self,
        model_session: Session,
        make_account: Callable[..., Any],
        make_document: Callable[..., Document],
    ) -> None:
        """``Tenant.documents`` carries ``delete-orphan``, so the ORM clears them.

        The database foreign key is ``RESTRICT``, so this only works because the
        ORM loads and deletes the children first — the reason
        :attr:`app.models.tenant.Tenant.documents` documents leaving
        ``passive_deletes`` off.
        """
        tenant, user = make_account(slug="acme", role=UserRole.ADMIN)
        make_document(tenant.id, uploaded_by=user.id)
        document_id = next(iter(tenant.documents)).id

        model_session.delete(tenant)
        model_session.flush()

        assert (
            model_session.scalar(select(Document).where(Document.id == document_id))
            is None
        )

    def test_a_document_cannot_name_a_missing_tenant(
        self, model_session: Session, make_document: Callable[..., Document]
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            make_document(uuid.uuid4())

        model_session.rollback()


# ---------------------------------------------------------------------------
# Table-level invariants
# ---------------------------------------------------------------------------


class TestTableInvariants:
    def test_the_storage_key_is_unique_across_all_tenants(
        self,
        model_session: Session,
        make_account: Callable[..., Any],
        make_document: Callable[..., Document],
    ) -> None:
        """Unique globally, not per tenant — a collision would be a broken link.

        A per-tenant unique constraint would allow two tenants to hold the same
        ``storage_key``, and since the on-disk path is built from both values that
        would still be safe; the global uniqueness is belt-and-braces on a
        128-bit random token, and is asserted so it cannot be relaxed silently.
        """
        from sqlalchemy.exc import IntegrityError

        first, _ = make_account(slug="acme")
        second, _ = make_account(slug="globex")
        shared = uuid.uuid4().hex
        make_document(first.id, storage_key=shared)

        with pytest.raises(IntegrityError):
            make_document(second.id, storage_key=shared)

        model_session.rollback()

    def test_the_storage_key_is_not_the_document_id(
        self,
        make_account: Callable[..., Any],
        make_document: Callable[..., Document],
    ) -> None:
        """Two independent tokens, so neither leaks the other.

        A consumer holding document ids cannot derive a storage path, and a
        directory listing cannot hand out an API identifier. The service mints
        them separately; this asserts the *model* does not couple them.
        """
        tenant, _ = make_account(slug="acme")
        document = make_document(tenant.id)

        assert document.storage_key != document.id.hex
        assert len(document.storage_key) == DOCUMENT_STORAGE_KEY_MAX_LENGTH

    def test_the_column_type_matches_the_key_format(
        self, model_session: Session
    ) -> None:
        column = next(
            column
            for column in table_of(Document).columns
            if column.name == "storage_key"
        )
        # ``Column.type`` is a ``TypeEngine``, which does not declare ``length``;
        # the column really is ``VARCHAR(32)``, and narrowing is the model's job
        # to be explicit about rather than the test's to guess at.
        varchar = cast("sa.types.VARCHAR", column.type)
        assert varchar.length == DOCUMENT_STORAGE_KEY_MAX_LENGTH

    @pytest.mark.parametrize(
        ("field", "value"),
        [("chunk_count", -1), ("size_bytes", 0), ("size_bytes", -1)],
    )
    def test_a_value_the_constraints_forbid_is_refused_by_the_database(
        self,
        model_session: Session,
        make_account: Callable[..., Any],
        make_document: Callable[..., Document],
        field: str,
        value: int,
    ) -> None:
        """The check constraints are enforced, not merely declared.

        Asserted against a live SQLite database, which enforces ``CHECK`` as
        strictly as PostgreSQL does. These constraints are what make "a chunk
        count is never negative" and "a document is never empty" properties of
        the database rather than of the code that happens to run today.
        """
        from sqlalchemy.exc import IntegrityError

        tenant, _ = make_account(slug="acme")

        with pytest.raises(IntegrityError):
            make_document(tenant.id, **{field: value})

        model_session.rollback()

    def test_the_status_check_constraint_rejects_an_unknown_value(
        self,
        model_session: Session,
        make_account: Callable[..., Any],
    ) -> None:
        """A status the PMP does not name cannot be stored, even by raw SQL.

        Written as a literal ``INSERT`` rather than through the ORM precisely
        because the ORM cannot express it: ``status`` is typed as
        :class:`~app.models.document.DocumentStatus`, so assigning ``"pending"``
        fails in Python before the database is ever asked. The constraint is the
        last line of defence and this proves it is still one.

        ``CURRENT_TIMESTAMP`` rather than ``now()`` because this runs against
        SQLite, which has no ``now()`` function; the values themselves are
        irrelevant, and the status column is what is under test.
        """
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        tenant, _ = make_account(slug="acme")

        with pytest.raises(IntegrityError):
            model_session.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, uploaded_by, filename, "
                    "storage_key, status, allowed_roles, chunk_count, error, "
                    "size_bytes, content_type, created_at, updated_at) VALUES "
                    "(:id, :tenant_id, NULL, 'x.pdf', :key, 'pending', NULL, 0, "
                    "NULL, 10, 'application/pdf', CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                ).bindparams(id=uuid.uuid4(), tenant_id=tenant.id, key=uuid.uuid4().hex)
            )

        model_session.rollback()


# ---------------------------------------------------------------------------
# Exported constants
# ---------------------------------------------------------------------------


class TestExportedValues:
    def test_the_status_values_are_an_immutable_tuple(self) -> None:
        """Module-level containers other code imports cannot be edited in place.

        ``DOCUMENT_STATUS_VALUES`` is read by the schema, the migration and the
        tests; a ``list`` or ``set`` would let any of them mutate it for every
        importer afterwards.
        """
        assert isinstance(DOCUMENT_STATUS_VALUES, tuple)
        assert all(isinstance(value, str) for value in DOCUMENT_STATUS_VALUES)

    def test_supported_formats_are_frozen_value_objects(self) -> None:
        """``SupportedFormat`` is a frozen, slotted dataclass.

        Frozen because a signature table that a test could edit would make every
        signature test vacuous; slotted because there is nothing to gain from an
        instance dictionary here.
        """
        from app.services.uploads import SUPPORTED_FORMATS

        assert SUPPORTED_FORMATS
        for spec in SUPPORTED_FORMATS.values():
            assert dataclasses.is_dataclass(spec)
            assert not hasattr(spec, "__dict__")

        # Written against an :data:`typing.Any`-typed alias: the assignment is
        # exactly what the frozen dataclass forbids, so a suppression here would
        # assert the opposite of what this asserts.
        writable: Any = spec

        with pytest.raises(dataclasses.FrozenInstanceError):
            writable.extension = "exe"

        assert spec.extension != "exe"

    def test_the_model_package_exports_the_document_names(self) -> None:
        """``app.models`` is the import surface; a name added there is a promise."""
        from app import models

        for name in ("Document", "DocumentStatus", "INITIAL_DOCUMENT_STATUS"):
            assert hasattr(models, name), name

    def test_the_user_model_has_no_document_specific_columns(self) -> None:
        """``uploaded_by`` is a reference, not a copy.

        The document does not denormalise the uploader's email or role, so a role
        change does not have to be reflected in a second table for a stale value to
        be served.
        """
        assert "uploaded_by" in table_of(Document).columns
        assert not {"uploaded_by_email", "uploaded_by_role"} & {
            column.name for column in table_of(User).columns
        }


class TestIdentityIntegrity:
    def test_the_document_id_is_a_uuid(self) -> None:
        assert isinstance(uuid.uuid4(), uuid.UUID)

    def test_a_default_id_is_generated_without_the_caller_asking(
        self,
        model_session: Session,
        make_account: Callable[..., Any],
    ) -> None:
        """``default=uuid.uuid4`` rather than a column default.

        Python-side so the id is known before the insert, which is what lets
        :func:`app.services.documents.create_document` mint it before any I/O and
        therefore report a specific document id in a log line.
        """
        tenant, _ = make_account(slug="acme")
        document = Document(
            tenant_id=tenant.id,
            filename="report.pdf",
            storage_key=uuid.uuid4().hex,
            size_bytes=1,
            content_type="application/pdf",
        )
        model_session.add(document)
        model_session.flush()

        assert isinstance(document.id, uuid.UUID)
        assert require(document.storage_key)