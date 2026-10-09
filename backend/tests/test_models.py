"""Tests for the Phase 2 Step 1 models: Tenant, User, and tenant isolation.

How these tests are run, and what that does and does not prove
-------------------------------------------------------------
Model behaviour is exercised against a real, file-backed **SQLite** database
with foreign keys enforced (see the ``sqlite_engine`` fixture). That is genuine
SQL: uniqueness, foreign keys and CHECK constraints are actually enforced by the
engine, and failures surface as ``IntegrityError`` rather than as Python-level
assertions about what the database "should" do.

It is still SQLite. The one behaviour that differs materially from production is
UUID storage — SQLite renders it ``CHAR(32)``, PostgreSQL renders it native
``UUID`` — so assertions about UUID round-tripping prove the ORM contract, not
PostgreSQL's type. Everything else about tenant isolation is dialect-independent
and is verified here for real.

PostgreSQL-specific verification, including UUID handling and ``ON DELETE
RESTRICT``, lives in ``tests/test_postgres_integration.py`` and is skipped
unless an integration DSN is configured. That gap is real and is not closed by
anything in this file.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Delete,
    ForeignKeyConstraint,
    Insert,
    UniqueConstraint,
    Update,
    inspect,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    MODEL_TABLES,
    RefreshToken,
    Tenant,
    TenantScopedMixin,
    User,
    UserRole,
)
from app.models.user import USER_EMAIL_UNIQUE_COLUMNS
from tests.conftest import (
    constraint_names,
    require,
    table_of,
    unique_constraints,
)

#: Reused across tests where the address is irrelevant.
ALICE = "alice@example.com"


def assert_constraint_violated(
    session: Session,
    statement: object,
    *,
    expect_in_message: str,
) -> None:
    """Assert a statement is refused, whether it fails on execute or on commit.

    A constraint can surface at either point depending on the statement: SQLAlchemy
    flushes pending ORM state at commit time, but a Core ``insert`` or ``delete``
    reaches the database immediately. Wrapping only one of them produces a test
    that fails with a confusing traceback instead of passing.

    ``expect_in_message`` is matched against the driver message, not a SQLAlchemy
    exception name, because the two dialects word these differently — PostgreSQL
    reports ``duplicate key value violates unique constraint "uq_tenants_slug"``
    while SQLite reports ``UNIQUE constraint failed: tenants.slug``. The caller
    therefore asserts on something both dialects include.
    """
    with pytest.raises(IntegrityError) as excinfo:
        # Either call may raise; both must be inside the guard.
        if isinstance(statement, (Insert, Delete, Update)):
            session.execute(statement)  # type: ignore[arg-type]
        session.commit()

    message = str(excinfo.value)
    assert expect_in_message in message, (
        f"expected {expect_in_message!r} in the error message, got: {message}"
    )


def aware(moment: datetime) -> datetime:
    """Return ``moment`` as an aware datetime, assuming UTC when naive.

    SQLite has no timestamp-with-time-zone type, so a ``DateTime(timezone=True)``
    column comes back **naive** from SQLite even though the same column on
    PostgreSQL is genuinely ``TIMESTAMPTZ`` and comes back aware. Attaching UTC
    here keeps the arithmetic in the timestamp tests valid on both dialects. It
    does not make SQLite timezone-aware — that remains unverified until the
    PostgreSQL integration suite runs, and those tests assert the raw value
    rather than going through this helper.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def make_tenant(session: Session, *, name: str = "Acme", slug: str = "acme") -> Tenant:
    """Persist and return a tenant."""
    tenant = Tenant(name=name, slug=slug)
    session.add(tenant)
    session.commit()
    return tenant


def make_user(
    session: Session,
    tenant: Tenant,
    *,
    email: str = ALICE,
    role: UserRole = UserRole.VIEWER,
    password_hash: str = "$argon2id$v=19$m=65536,t=3,p=4$placeholder",
) -> User:
    """Persist and return a user belonging to ``tenant``."""
    user = User(
        tenant_id=tenant.id,
        email=email,
        password_hash=password_hash,
        role=role,
    )
    session.add(user)
    session.commit()
    return user


@pytest.fixture
def session(model_session: Session) -> Session:
    """Alias for the shared ``model_session`` fixture.

    Named for readability at the call sites in this module; the implementation
    lives in ``conftest`` so the model and isolation suites share one definition.
    """
    return model_session


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------


class TestTenant:
    def test_creates_with_a_uuid_primary_key(self, session: Session) -> None:
        tenant = make_tenant(session)

        assert isinstance(tenant.id, uuid.UUID)
        assert tenant.id is not None

    def test_two_tenants_get_distinct_ids(self, session: Session) -> None:
        first = make_tenant(session, slug="one")
        second = make_tenant(session, slug="two")

        assert first.id != second.id

    def test_id_is_generated_without_being_supplied(self, session: Session) -> None:
        """The default must fire, so no caller has to remember to set an id."""
        tenant = make_tenant(session)

        assert tenant.id is not None

    def test_name_and_slug_persist(self, session: Session) -> None:
        make_tenant(session, name="Acme Corporation", slug="acme-corp")

        stored = session.execute(select(Tenant)).scalar_one()
        assert stored.name == "Acme Corporation"
        assert stored.slug == "acme-corp"

    @pytest.mark.parametrize("missing", ["name", "slug"])
    def test_name_and_slug_are_required(self, session: Session, missing: str) -> None:
        values: dict[str, object] = {"name": "Acme", "slug": "acme"}
        del values[missing]

        session.add(Tenant(**values))

        with pytest.raises(IntegrityError):
            session.commit()

    def test_slug_is_globally_unique(self, session: Session) -> None:
        """Two tenants cannot share a slug, because a slug is the URL identity."""
        make_tenant(session, slug="shared")

        session.add(Tenant(name="Other", slug="shared"))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_slug_constraint_names_the_column(self, session: Session) -> None:
        """The error must identify which column was rejected.

        SQLite words this as ``UNIQUE constraint failed: tenants.slug`` and
        PostgreSQL as ``duplicate key value violates unique constraint
        "uq_tenants_slug"``; both name the column, so that is what is asserted.
        The constraint's deterministic *name* is asserted separately against the
        metadata, where it is exact on both dialects.
        """
        make_tenant(session, slug="dup")

        session.add(Tenant(name="Other", slug="dup"))
        with pytest.raises(IntegrityError, match="slug"):
            session.commit()

    def test_timestamps_are_set_by_the_database(self, session: Session) -> None:
        tenant = make_tenant(session)

        assert isinstance(tenant.created_at, datetime)
        assert isinstance(tenant.updated_at, datetime)

    def test_timestamp_columns_are_declared_timezone_aware(self) -> None:
        """The declaration is what makes PostgreSQL store ``TIMESTAMPTZ``.

        Checked against the column type rather than a loaded value: SQLite has no
        timezone-preserving datetime type, so a value read back from SQLite is
        always naive regardless of this declaration. Asserting ``timezone=True``
        here is the assertion that is actually dialect-independent.
        """
        for column in (table_of(Tenant).c.created_at, table_of(Tenant).c.updated_at):
            # `Column.type` is a `TypeEngine`, but a datetime column is always a
            # `DateTime`, which is where `timezone` lives.
            assert isinstance(column.type, DateTime)
            assert column.type.timezone is True

    def test_timestamp_values_are_not_naive_on_postgresql(
        self, session: Session
    ) -> None:
        """A loaded timestamp is aware whenever the dialect preserves zones.

        Passes on PostgreSQL and is skipped on SQLite, where the value is
        unavoidably naive. Marked ``xfail`` rather than skipped so it is visibly
        pending instead of silently absent.
        """
        if session.get_bind().dialect.name != "postgresql":
            pytest.xfail(
                "SQLite does not preserve timezone information; this is covered by "
                "tests/test_postgres_integration.py"
            )

        tenant = make_tenant(session)

        assert tenant.created_at.tzinfo is not None
        assert tenant.updated_at.tzinfo is not None

    def test_updated_at_advances_when_the_row_changes(self, session: Session) -> None:
        """``onupdate`` must emit a fresh timestamp, not freeze the original."""
        tenant = make_tenant(session)

        # SQLite's CURRENT_TIMESTAMP has one-second resolution, so an immediate
        # update can land in the same second. Backdating the row first is what
        # makes this deterministic rather than intermittently equal.
        tenant.updated_at = datetime.now(timezone.utc) - timedelta(days=1)
        session.commit()
        stale = aware(tenant.updated_at)

        tenant.name = "Acme Renamed"
        session.commit()

        assert aware(tenant.updated_at) > stale

    def test_updated_at_carries_an_onupdate_expression(self) -> None:
        """Asserted on the model, because the value round-trip is dialect-timed."""
        assert table_of(Tenant).c.updated_at.onupdate is not None
        assert table_of(User).c.updated_at.onupdate is not None

    def test_users_relationship_starts_empty(self, session: Session) -> None:
        tenant = make_tenant(session)

        assert tenant.users == []

    def test_tenant_without_users_can_be_deleted(self, session: Session) -> None:
        tenant = make_tenant(session)

        session.delete(tenant)
        session.commit()

        assert session.execute(select(Tenant)).scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


class TestUser:
    def test_creates_with_a_uuid_primary_key(self, session: Session) -> None:
        user = make_user(session, make_tenant(session))

        assert isinstance(user.id, uuid.UUID)

    def test_two_users_get_distinct_ids(self, session: Session) -> None:
        tenant = make_tenant(session)
        first = make_user(session, tenant, email="a@example.com")
        second = make_user(session, tenant, email="b@example.com")

        assert first.id != second.id

    def test_tenant_id_is_required(self, session: Session) -> None:
        """A user with no tenant must be unrepresentable, not merely unusual."""
        user = User(email=ALICE, password_hash="hash")
        session.add(user)

        with pytest.raises(IntegrityError):
            session.commit()

    def test_tenant_id_is_set_automatically_from_the_relationship(
        self, session: Session
    ) -> None:
        tenant = make_tenant(session)
        user = User(email=ALICE, password_hash="hash", tenant=tenant)
        session.add(user)
        session.commit()

        assert user.tenant_id == tenant.id

    def test_tenant_relationship_resolves(self, session: Session) -> None:
        tenant = make_tenant(session)
        user = make_user(session, tenant)

        assert user.tenant is tenant
        assert user in tenant.users

    def test_email_and_password_hash_persist(self, session: Session) -> None:
        tenant = make_tenant(session)
        make_user(session, tenant, email=ALICE, password_hash="$argon2id$stored")

        stored = session.execute(select(User)).scalar_one()
        assert stored.email == ALICE
        assert stored.password_hash == "$argon2id$stored"

    @pytest.mark.parametrize("missing", ["email", "password_hash"])
    def test_email_and_password_hash_are_required(
        self, session: Session, missing: str
    ) -> None:
        """NOT NULL is what is asserted here.

        Absence of a value is rejected by the database. An *empty string* is not:
        ``''`` is a value, and whether ``email = ''`` should be rejected is a
        validation decision belonging to the application boundary that Phase 2
        Step 2 will add. Asserting it here would encode a rule the schema does not
        actually have.
        """
        tenant = make_tenant(session)
        values: dict[str, object] = {"email": ALICE, "password_hash": "hash"}
        del values[missing]

        session.add(User(tenant_id=tenant.id, **values))

        with pytest.raises(IntegrityError):
            session.commit()

    def test_empty_email_is_not_rejected_by_the_database(
        self, session: Session
    ) -> None:
        """Documents the boundary deliberately rather than asserting a wish.

        If a future step adds NOT NULL-with-empty-string semantics, this test is
        the place that must change — silently, the constraint would pass unnoticed.
        """
        tenant = make_tenant(session)
        user = make_user(session, tenant, email="")

        assert user.email == ""

    def test_password_hash_is_stored_opaquely(self, session: Session) -> None:
        """Nothing in this phase may transform the stored value.

        Asserting equality is how a future accidental in-place hashing step gets
        caught: it would make the stored value differ from what was assigned.
        """
        tenant = make_tenant(session)
        raw = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$hash"

        user = make_user(session, tenant, password_hash=raw)
        session.expire_all()

        assert require(session.get(User, user.id)).password_hash == raw

    def test_role_defaults_to_viewer(self, session: Session) -> None:
        user = make_user(session, make_tenant(session))

        assert user.role is UserRole.VIEWER

    def test_role_defaults_to_viewer_at_the_database_level(
        self, session: Session
    ) -> None:
        """The server default must hold for a row written by raw SQL too."""
        tenant = make_tenant(session)
        session.execute(
            table_of(User).insert().values(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                email="raw@example.com",
                password_hash="hash",
            )
        )
        session.commit()

        stored = session.execute(select(User)).scalar_one()
        assert stored.role == UserRole.VIEWER

    @pytest.mark.parametrize("role", list(UserRole))
    def test_every_defined_role_is_accepted(
        self, session: Session, role: UserRole
    ) -> None:
        tenant = make_tenant(session)
        user = make_user(session, tenant, email=f"{role.value}@example.com", role=role)

        assert user.role is role

    def test_the_three_pmp_roles_are_exactly_these(self) -> None:
        """PMP §6 defines three roles; adding one is a deliberate act."""
        assert {role.value for role in UserRole} == {"admin", "editor", "viewer"}

    def test_an_undefined_role_is_rejected_by_the_database(
        self, session: Session
    ) -> None:
        """The CHECK constraint must hold against raw SQL, not just the enum.

        This is the assertion that a ``UserRole`` member is genuinely the only way
        to set a valid role: without the constraint, any string would persist and
        a typo in one service would become an unreachable role at runtime.
        """
        tenant = make_tenant(session)

        assert_constraint_violated(
            session,
            table_of(User).insert().values(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                email="bad@example.com",
                password_hash="hash",
                role="superuser",
            ),
            expect_in_message="role",
        )

    def test_role_enum_values_are_the_lowercase_strings(self) -> None:
        """A member name of ``ADMIN`` must store as ``admin``, not ``ADMIN``."""
        assert UserRole.ADMIN.value == "admin"
        assert UserRole.ADMIN.name != UserRole.ADMIN.value

    def test_is_active_defaults_to_true(self, session: Session) -> None:
        assert make_user(session, make_tenant(session)).is_active is True

    def test_is_active_can_be_set_false(self, session: Session) -> None:
        tenant = make_tenant(session)
        user = User(tenant_id=tenant.id, email=ALICE, password_hash="hash", is_active=False)
        session.add(user)
        session.commit()

        assert user.is_active is False

    def test_is_active_cannot_be_null(self, session: Session) -> None:
        """A NULL here would make ``if user.is_active`` mean "unknown"."""
        tenant = make_tenant(session)

        assert_constraint_violated(
            session,
            table_of(User).insert().values(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                email="null@example.com",
                password_hash="hash",
                is_active=None,
            ),
            expect_in_message="is_active",
        )

    def test_timestamps_are_set(self, session: Session) -> None:
        user = make_user(session, make_tenant(session))

        assert isinstance(user.created_at, datetime)
        assert isinstance(user.updated_at, datetime)

    def test_updated_at_advances_when_the_row_changes(self, session: Session) -> None:
        user = make_user(session, make_tenant(session))
        # Backdate, then change the row: SQLite's one-second clock resolution
        # would otherwise let an immediate update compare equal.
        user.updated_at = datetime.now(timezone.utc) - timedelta(days=1)
        session.commit()
        stale = aware(user.updated_at)

        user.is_active = False
        session.commit()

        assert aware(user.updated_at) > stale

    def test_deleting_a_user_does_not_delete_the_tenant(self, session: Session) -> None:
        """Cascade must point one way only: tenant to users, never back."""
        tenant = make_tenant(session)
        user = make_user(session, tenant)

        session.delete(user)
        session.commit()

        assert session.get(Tenant, tenant.id) is not None
        assert session.get(User, user.id) is None

    def test_deleting_a_tenant_with_users_is_refused(self, session: Session) -> None:
        """``ON DELETE RESTRICT`` must stop a tenant disappearing with its users.

        Issued as raw SQL, bypassing the ORM's ``delete-orphan`` cascade, because
        the guarantee under test is the database's: a direct ``DELETE`` against a
        tenant that still has users must fail rather than take them with it.
        """
        tenant = make_tenant(session)
        make_user(session, tenant)

        assert_constraint_violated(
            session,
            table_of(Tenant).delete().where(table_of(Tenant).c.id == tenant.id),
            expect_in_message="FOREIGN KEY",
        )

    def test_the_tenant_survives_a_refused_delete(self, session: Session) -> None:
        """The refusal must leave the database untouched, not half-applied."""
        tenant = make_tenant(session)
        make_user(session, tenant)

        with pytest.raises(IntegrityError):
            session.rollback()
            session.execute(
                table_of(Tenant).delete().where(table_of(Tenant).c.id == tenant.id)
            )
            session.commit()
        session.rollback()

        assert session.get(Tenant, tenant.id) is not None
        assert len(session.execute(select(User)).scalars().all()) == 1

    def test_deleting_a_tenant_through_the_orm_removes_its_users(
        self, session: Session
    ) -> None:
        """The ORM path is explicit, so it is allowed to cascade.

        This is the deliberate counterpart to the previous test: the database
        refuses a raw ``DELETE``, but a caller who asks the ORM to remove the
        users first gets the intended behaviour.
        """
        tenant = make_tenant(session)
        user = make_user(session, tenant)

        session.delete(tenant)
        session.commit()

        assert session.get(User, user.id) is None


# ---------------------------------------------------------------------------
# Multi-tenancy — the mandatory isolation cases
# ---------------------------------------------------------------------------


class TestMultiTenancy:
    def test_the_same_email_works_in_two_different_tenants(self, session: Session) -> None:
        """The defining multi-tenancy property.

        PMP §12 requires tenants to be independent. A global ``UNIQUE(email)``
        would make this fail, so this test is the one that would catch that
        mistake.
        """
        tenant_a = make_tenant(session, name="Acme", slug="acme")
        tenant_b = make_tenant(session, name="Globex", slug="globex")

        alice_a = make_user(session, tenant_a, email=ALICE)
        alice_b = make_user(session, tenant_b, email=ALICE)

        assert alice_a.id != alice_b.id
        assert alice_a.tenant_id == tenant_a.id
        assert alice_b.tenant_id == tenant_b.id

    def test_the_same_email_fails_twice_within_one_tenant(self, session: Session) -> None:
        """The other half of the rule, and the reason the constraint exists."""
        tenant = make_tenant(session)
        make_user(session, tenant, email=ALICE)

        session.add(User(tenant_id=tenant.id, email=ALICE, password_hash="other"))

        with pytest.raises(IntegrityError):
            session.commit()

    def test_the_rejected_duplicate_names_both_columns(self, session: Session) -> None:
        """The error must show the composite, not just ``email``.

        A message naming only ``email`` would be indistinguishable from the global
        uniqueness rule this project deliberately does not have.
        """
        tenant = make_tenant(session)
        make_user(session, tenant, email=ALICE)

        session.add(User(tenant_id=tenant.id, email=ALICE, password_hash="other"))
        with pytest.raises(IntegrityError) as excinfo:
            session.commit()

        message = str(excinfo.value)
        assert "email" in message
        assert "tenant_id" in message

    def test_many_tenants_may_share_one_address(self, session: Session) -> None:
        tenants = [
            make_tenant(session, name=f"T{index}", slug=f"t{index}") for index in range(5)
        ]
        for tenant in tenants:
            make_user(session, tenant, email=ALICE)

        assert session.execute(select(User)).scalars().all() is not None
        assert len(tenants) == 5

    def test_users_are_reachable_from_their_own_tenant_only(
        self, session: Session
    ) -> None:
        tenant_a = make_tenant(session, slug="a")
        tenant_b = make_tenant(session, slug="b")
        make_user(session, tenant_a, email="a@example.com")
        make_user(session, tenant_b, email="b@example.com")

        session.expire_all()

        assert [user.email for user in tenant_a.users] == ["a@example.com"]
        assert [user.email for user in tenant_b.users] == ["b@example.com"]

    def test_user_belongs_to_exactly_one_tenant(self, session: Session) -> None:
        tenant = make_tenant(session)
        user = make_user(session, tenant)

        assert user.tenant.id == tenant.id

    def test_user_cannot_reference_a_nonexistent_tenant(self, session: Session) -> None:
        """Foreign keys are enabled on this engine, so this is enforced.

        Asserting on ``FOREIGN KEY`` rather than the constraint name, because that
        is the wording both dialects use; the constraint's deterministic name is
        asserted against the metadata elsewhere.
        """
        session.add(
            User(
                tenant_id=uuid.uuid4(),
                email="orphan@example.com",
                password_hash="hash",
            )
        )

        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            session.commit()

    def test_deleting_a_tenant_orphans_nothing_because_it_is_refused(
        self, session: Session
    ) -> None:
        """No user may end up pointing at a tenant that is gone."""
        tenant = make_tenant(session)
        user = make_user(session, tenant)

        with pytest.raises(IntegrityError):
            session.rollback()
            session.execute(
                table_of(Tenant).delete().where(table_of(Tenant).c.id == tenant.id)
            )
            session.commit()
        session.rollback()

        session.expire_all()
        assert require(session.get(User, user.id)).tenant_id == tenant.id

    def test_removing_a_user_from_the_collection_deletes_it(self, session: Session) -> None:
        tenant = make_tenant(session)
        user = make_user(session, tenant)

        tenant.users.remove(user)
        session.commit()

        assert session.get(User, user.id) is None

    def test_a_detached_tenant_cannot_orphan_its_users(self, session: Session) -> None:
        """Clearing the collection deletes the rows rather than losing the link."""
        tenant = make_tenant(session)
        user = make_user(session, tenant)

        tenant.users.clear()
        session.commit()

        assert session.get(User, user.id) is None
        assert session.get(Tenant, tenant.id) is not None


# ---------------------------------------------------------------------------
# Structural guarantees
# ---------------------------------------------------------------------------


class TestTenantScopingContract:
    def test_user_inherits_the_tenant_scoping_mixin(self) -> None:
        assert issubclass(User, TenantScopedMixin)

    def test_tenant_does_not_inherit_the_mixin(self) -> None:
        """A tenant cannot be scoped to a tenant; it is the scope."""
        assert not issubclass(Tenant, TenantScopedMixin)

    def test_tenant_id_is_not_nullable(self) -> None:
        assert table_of(User).c.tenant_id.nullable is False

    def test_tenant_id_is_indexed(self) -> None:
        assert table_of(User).c.tenant_id.index is True

    def test_tenant_id_has_no_python_side_default(self) -> None:
        """A default would allow a row to exist without a named tenant."""
        assert table_of(User).c.tenant_id.default is None

    def test_tenant_id_has_no_server_side_default(self) -> None:
        assert table_of(User).c.tenant_id.server_default is None

    def test_tenant_id_foreign_key_targets_tenants_id(self) -> None:
        foreign_keys = list(table_of(User).c.tenant_id.foreign_keys)

        assert len(foreign_keys) == 1
        assert foreign_keys[0].target_fullname == "tenants.id"

    def test_tenant_foreign_key_uses_restrict_not_cascade(self) -> None:
        foreign_key = next(iter(table_of(User).c.tenant_id.foreign_keys))

        assert foreign_key.ondelete == "RESTRICT"

    def test_every_user_column_is_not_nullable(self) -> None:
        nullable = [c.name for c in table_of(User).c if c.nullable]

        assert nullable == []

    def test_every_tenant_column_is_not_nullable(self) -> None:
        nullable = [c.name for c in table_of(Tenant).c if c.nullable]

        assert nullable == []

    def test_no_table_has_a_global_email_constraint(self) -> None:
        """A unique constraint on email alone would break PMP §12 isolation."""
        email_indexes = [
            index
            for table in table_of(User).metadata.tables.values()
            for index in table.indexes
            if [c.name for c in index.columns] == ["email"] and index.unique
        ]

        assert email_indexes == []

    def test_email_uniqueness_is_declared_on_tenant_and_email(self) -> None:
        declared = {
            tuple(constraint.columns.keys())
            for constraint in unique_constraints(table_of(User))
        }

        assert USER_EMAIL_UNIQUE_COLUMNS in declared

    def test_tenant_id_leads_the_email_unique_index(self) -> None:
        """A composite index is only email-searchable if email leads it."""
        column_sets = {
            tuple(index.columns.keys())
            for index in table_of(User).indexes
            if index.unique
        }
        column_sets |= {
            tuple(constraint.columns.keys())
            for constraint in unique_constraints(table_of(User))
        }

        assert ("tenant_id", "email") in column_sets


class TestGeneratedConstraintNames:
    """Names come from the shared naming convention, not from string literals."""

    def test_primary_key_names(self) -> None:
        assert table_of(Tenant).primary_key.name == "pk_tenants"
        assert table_of(User).primary_key.name == "pk_users"

    def test_unique_constraint_names(self) -> None:
        names = constraint_names(table_of(Tenant), UniqueConstraint)
        names |= constraint_names(table_of(User), UniqueConstraint)

        assert names == {"uq_tenants_slug", "uq_users_tenant_id_email"}

    def test_foreign_key_name(self) -> None:
        names = constraint_names(table_of(User), ForeignKeyConstraint)

        assert names == {"fk_users_tenant_id_tenants"}

    def test_check_constraint_name(self) -> None:
        names = constraint_names(table_of(User), CheckConstraint)

        assert names == {"ck_users_user_role"}

    def test_index_names(self) -> None:
        names = {index.name for index in table_of(User).indexes}

        assert names == {"ix_users_email", "ix_users_tenant_id"}


class TestMetadataSurface:
    def test_metadata_contains_exactly_the_declared_tables(self) -> None:
        """The closed set of tables is :data:`MODEL_TABLES`, and nothing else.

        Written against the exported constant rather than a literal list, so that
        adding a table in a later step is a deliberate edit to
        ``app.models.MODEL_TABLES`` — the one place that enumerates them — and
        this test follows automatically. A literal here would need updating in two
        files, and the second one would be the one people forget.

        Phase 2 Step 1 asserted ``{"tenants", "users"}`` literally because only
        those two existed. Step 2 adds ``refresh_tokens`` (PMP §14) and Phase 2.4
        adds ``documents`` (PMP §14). Phase 6 adds ``document_chunks`` and
        Phase 7 adds ``query_logs`` and ``audit_logs`` (PMP §22).
        """
        from app.db.base import Base

        assert set(Base.metadata.tables) == MODEL_TABLES
        assert MODEL_TABLES == {
            "tenants",
            "users",
            "refresh_tokens",
            "documents",
            "document_chunks",
            "query_logs",
            "audit_logs",
        }

    def test_no_future_phase_table_exists(self) -> None:
        """Roles and permissions tables belong to later phases.

        ``refresh_tokens`` was in this list at Step 1 and ``documents`` at
        Phase 2.4 and ``query_logs`` at Phase 6; all three are no longer,
        because PMP §14 puts the first two in Phase 2 and PMP §22 puts
        ``query_logs`` in Phase 7 — all are now implemented. They are
        deliberately *kept* in the statement of what this test still refuses,
        so the sentence records how far the project has come rather than being
        quietly shortened each time a table is added.
        """
        from app.db.base import Base

        for future in ("roles", "permissions"):
            assert future not in Base.metadata.tables

    def test_the_package_export_matches_the_metadata(self) -> None:
        from app.db.base import Base

        assert {table.name for table in table_of(Tenant).metadata.tables.values()} == set(
            MODEL_TABLES
        )
        assert set(Base.metadata.tables) == set(MODEL_TABLES)

    def test_declared_table_names(self) -> None:
        assert Tenant.__tablename__ == "tenants"
        assert User.__tablename__ == "users"
        assert RefreshToken.__tablename__ == "refresh_tokens"


class TestImportPurity:
    def test_importing_the_models_creates_no_database_connection(self) -> None:
        """Declaring a model must not touch a database.

        Asserted on the two things that would prove a connection: the ``psycopg``
        driver being imported, and the module-level engine cache being populated.
        Run in a subprocess so no earlier test's engine can make this pass for
        the wrong reason.

        ``app.db.engine`` and ``app.db.health`` *are* imported, because
        ``app/db/__init__.py`` re-exports them — that is Phase 1 behaviour and is
        not a connection. Importing a module that can connect is not the same as
        connecting, which is why the engine cache is the assertion that matters.
        """
        import subprocess
        import sys
        from pathlib import Path

        backend_root = Path(__file__).resolve().parents[1]
        script = (
            "import sys; import app.models; "
            "from app.db import engine; "
            "assert 'psycopg' not in sys.modules, 'psycopg imported'; "
            "assert 'alembic' not in sys.modules, 'alembic imported'; "
            "assert engine._engine is None, 'an engine was built on import'; "
            "print('clean')"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=backend_root,
            check=True,
        )

        assert result.stdout.strip() == "clean"

    def test_reloading_the_models_does_not_create_an_engine(self) -> None:
        """A reload exercises the import path again, against a cleared cache.

        If a model module ever began building an engine, this would catch it: the
        cache would be populated after the reload. Reads the module's private
        ``_engine`` rather than a public accessor because ``get_engine()``
        *builds* an engine when asked, so calling it to inspect the cache would
        prove nothing.
        """
        import importlib

        from app.db import engine as engine_module

        engine_module.reset_engine()
        try:
            importlib.reload(importlib.import_module("app.models"))
            assert engine_module._engine is None
        finally:
            engine_module.reset_engine()

    def test_get_engine_is_a_plain_function_not_a_cache(self) -> None:
        """Guards the assumption the previous test rests on.

        ``get_engine`` uses a module-level ``_engine`` rather than ``lru_cache``,
        which is why the reload test reads ``_engine`` directly. If that changed,
        this fails and points at the test that needs updating.
        """
        from app.db.engine import get_engine

        assert not hasattr(get_engine, "cache_info")

    def test_the_migration_does_not_embed_credentials(self) -> None:
        from tests.conftest import ALEMBIC_VERSIONS

        for revision in ALEMBIC_VERSIONS.glob("*.py"):
            source = revision.read_text(encoding="utf-8")
            assert "password" not in source.lower().replace("password_hash", "")
            assert "postgres://" not in source
            assert "postgresql://" not in source


# ---------------------------------------------------------------------------
# Schema shape, read back from the database rather than from the model
# ---------------------------------------------------------------------------


class TestMaterialisedSchema:
    """Reflection-based checks.

    The tests above assert against the ORM's view of the schema. These read the
    schema back out of the database, which is the only way to catch a mismatch
    between what the models declare and what the engine was actually told.
    """

    def test_exactly_the_declared_tables_exist_after_create_all(
        self, session: Session
    ) -> None:
        """Read back out of the database, not out of the ORM.

        Asserting against ``MODEL_TABLES`` rather than a literal list keeps this
        and ``test_metadata_contains_exactly_the_declared_tables`` stating one
        fact between them: the materialised schema and the declared schema are
        the same closed set.
        """
        assert sorted(inspect(session.get_bind()).get_table_names()) == sorted(
            MODEL_TABLES
        )

    def test_unique_constraint_exists_in_the_database(
        self, session: Session
    ) -> None:
        constraints = inspect(session.get_bind()).get_unique_constraints("users")

        assert any(
            tuple(c["column_names"]) == ("tenant_id", "email") for c in constraints
        )

    def test_foreign_key_exists_in_the_database(self, session: Session) -> None:
        foreign_keys = inspect(session.get_bind()).get_foreign_keys("users")

        assert len(foreign_keys) == 1
        assert foreign_keys[0]["referred_table"] == "tenants"
        assert foreign_keys[0]["constrained_columns"] == ["tenant_id"]

    def test_indexes_exist_in_the_database(self, session: Session) -> None:
        indexes = {
            index["name"] for index in inspect(session.get_bind()).get_indexes("users")
        }

        assert "ix_users_tenant_id" in indexes
        assert "ix_users_email" in indexes

    def test_uuid_round_trips_through_the_database(self, session: Session) -> None:
        """The ORM must hand back a ``UUID``, not a string or bytes.

        Note the caveat: on SQLite this proves the ORM contract. Native PostgreSQL
        ``UUID`` handling is unverified here — see the PostgreSQL integration
        suite.
        """
        tenant = make_tenant(session)
        user = make_user(session, tenant)
        session.expire_all()

        assert isinstance(require(session.get(User, user.id)).id, uuid.UUID)

    def test_no_model_is_attached_to_a_private_metadata(self) -> None:
        """A model on a private ``MetaData`` would be invisible to autogenerate.

        Such a model would simply be omitted from every migration, so the check
        is that every registered table belongs to the one shared registry.
        """
        from app.db.base import Base

        orphans = [
            name
            for name, table in Base.metadata.tables.items()
            if table.metadata is not Base.metadata
        ]

        assert orphans == []

    def test_table_arguments_include_exactly_the_expected_constraints(
        self, session: Session
    ) -> None:
        names = {c.name for c in table_of(User).constraints}

        assert {
            "pk_users",
            "uq_users_tenant_id_email",
            "fk_users_tenant_id_tenants",
            "ck_users_user_role",
        } <= names

    def test_server_default_for_created_at_is_the_database_clock(
        self, session: Session
    ) -> None:
        """The default is SQL-side, so it holds for raw SQL inserts too."""
        tenant_id = uuid.uuid4()

        session.execute(
            table_of(Tenant).insert().values(id=tenant_id, name="Raw", slug="raw")
        )
        session.commit()

        stored = require(session.get(Tenant, tenant_id))

        assert stored.created_at is not None
        assert aware(stored.created_at) <= aware(datetime.now(timezone.utc))
        assert (
            aware(datetime.now(timezone.utc)) - aware(stored.created_at)
        ).total_seconds() < 60
