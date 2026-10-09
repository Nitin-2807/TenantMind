"""Security tests for tenant isolation.

These are deliberately separate from ``test_models.py`` even where they overlap.
The model tests ask "does the schema behave correctly?"; these ask "is there a way
for a tenant boundary to be crossed?", which is a different question with a
different cost of failure. A cross-tenant leak is the worst bug this system can
have (PMP §24 rates it Critical), so the guarantee is stated as its own tests
rather than left as a side effect of someone else's assertions.

What is genuinely proved here
-----------------------------
* Email uniqueness is per tenant, verified by **inserting** into a live database,
  not by reading metadata. Metadata could say one thing while the materialised
  schema says another.
* ``tenant_id`` has no default at either layer, so a row cannot be written
  without a tenant being named.
* The foreign key is enforced.
* Tenant deletion is refused while users remain.
* No module in the application derives a tenant from request data. This last one
  is proved by **source inspection** over the AST of every application file, not
  by reading — a comment mentioning ``tenant_id`` cannot satisfy it, and neither
  can a string literal.
* Authentication exists in exactly one place: one hashing library, one JWT
  library, one module permitted to import each.

What is not proved here
-----------------------
Nothing here checks a *role*. RBAC is Phase 2 Step 3, and ``test_no_role_
enforcement_logic_exists`` keeps asserting its absence so that step cannot be
silently pre-empted.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Tenant, User, UserRole
from tests.conftest import require, table_of

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"
ALEMBIC_ROOT = BACKEND_ROOT / "alembic"

ALICE = "alice@example.com"

#: Members that carry client-supplied HTTP input. Only meaningful when reached
#: through a request object, which is why the detector keys on the chain's root
#: rather than on these names alone — matching ``url`` or ``path`` anywhere would
#: flag ``engine.url`` and ``sys.path``, which are the opposite of a problem.
REQUEST_SOURCES = frozenset(
    {
        "headers",
        "header",
        "cookies",
        "query_params",
        "path_params",
        "form",
        "json",
        "body",
    }
)

#: Attribute names that can hold an HTTP request. A chain rooted at one of these
#: is client-controlled input.
REQUEST_ROOTS = frozenset({"request", "req", "http_request", "websocket"})

#: Ambient identifiers that would denote a tenant established outside a call.
TENANT_SCOPE_NAMES = frozenset(
    {"tenant", "tenant_id", "current_tenant", "active_tenant", "tenant_context"}
)


def application_python_files() -> list[Path]:
    """Every application source file, excluding tests and caches."""
    return sorted(
        path
        for path in APP_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def tree_of(path: Path) -> ast.Module:
    """Alias for :func:`parse` named for the import-scanning call site.

    ``tree_of(path)`` reads better than ``parse(path)`` inside the loops that walk
    every application file looking for imports, where the *reason* for parsing is
    not the name itself.
    """
    return parse(path)


def declared_requirements(path: Path) -> list[str]:
    """Return the installable requirement lines from ``path``.

    Comments, blanks and lines without an exact ``==`` pin are dropped. This is
    what makes the assertion about *installed* packages rather than about prose:
    ``requirements.txt`` explains why ``python-jose`` was rejected, and that
    explanation must not be mistaken for a dependency on it.
    """
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#") and "==" in line
    ]


def imported_root_modules(tree: ast.Module) -> set[str]:
    """Return the top-level package names imported anywhere in ``tree``.

    Roots only, because that is the granularity a library is installed at:
    ``argon2.low_level`` and ``argon2`` are the same package, and
    ``jwt.exceptions`` and ``jwt`` are the same one. Collapsing to the root is
    what makes "one library has one importer" checkable.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        # Relative imports (``from . import x``) have no module name and are
        # intra-package, so they are not a third-party library.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def dotted_name(node: ast.AST) -> str | None:
    """Return the dotted source name of an attribute chain, if it is simple.

    Returns ``None`` for anything dynamic — a subscript, a call result, a
    comprehension — because those cannot be resolved to a fixed identifier and
    reporting a guess would be worse than reporting nothing.
    """
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def chain_root(node: ast.AST) -> str | None:
    """Return the first identifier of a dotted chain, e.g. ``request`` in
    ``request.headers``."""
    current = node
    while isinstance(current, ast.Attribute):
        current = current.value
    if isinstance(current, ast.Name):
        return current.id
    return None


def matching_sources(tree: ast.Module) -> list[str]:
    """Chains in ``tree`` that read client-supplied input from a request object.

    Syntactic and deliberately narrow: it reports only chains rooted at a request
    object and reaching a member that actually carries input. A comment, a
    docstring, or a parameter named ``tenant_id`` — which is exactly what future
    repositories will require — cannot satisfy or trigger it.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in REQUEST_SOURCES:
            continue
        if chain_root(node) in REQUEST_ROOTS:
            found.add(dotted_name(node) or node.attr)
    return sorted(found)


def matching_environment_reads(tree: ast.Module) -> list[str]:
    """Tenant identifiers read from the process environment.

    Looks specifically at ``os.environ``/``os.getenv`` calls rather than at any
    string that happens to contain ``tenant``. A column-name tuple in a model
    module is a legitimate use of the word and must not be reported.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = dotted_name(node.func) or ""
            root = target.rsplit(".", 1)[-1]
            if root not in ("getenv", "_Environ", "environ") and root != "get":
                continue
            base = target.rsplit(".", 1)[0] if "." in target else ""
            if root == "get" and base not in ("os.environ", "environ"):
                continue
            for argument in node.args:
                if (
                    isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                    and "tenant" in argument.value.lower()
                ):
                    found.add(argument.value)
    return sorted(found)


def module_level_tenant_names(tree: ast.Module) -> list[str]:
    """Module-level bindings that look like ambient tenant state.

    Only exact identifiers in :data:`TENANT_SCOPE_NAMES` count. A substring match
    would flag ``TENANT_NAME_MAX_LENGTH``, which is a column width and not tenant
    scope. Module-level ``UPPER_CASE`` constants are also exempt for the same
    reason.
    """
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Name)
                and target.id.lower() in TENANT_SCOPE_NAMES
                and not target.id.isupper()
            ):
                found.append(f"line {target.lineno}: {target.id}")
    return found


# ---------------------------------------------------------------------------
# No global email uniqueness — proved by inserting, not by reading metadata
# ---------------------------------------------------------------------------


@pytest.fixture
def session(model_session: Session) -> Session:
    """Alias for the shared ``model_session`` fixture; see ``conftest``."""
    return model_session


class TestNoGlobalEmailUniqueness:
    def test_same_email_in_two_tenants_is_accepted(self, session: Session) -> None:
        """The mandatory multi-tenancy case, against a real database.

        If a global ``UNIQUE(email)`` were ever introduced, this fails. Nothing
        else in the suite would necessarily catch it, because a per-tenant unique
        constraint and a global one both look correct in metadata for a single
        tenant.
        """
        tenant_a = Tenant(name="Acme", slug="acme")
        tenant_b = Tenant(name="Globex", slug="globex")
        session.add_all([tenant_a, tenant_b])
        session.commit()

        session.add_all(
            [
                User(
                    tenant_id=tenant_a.id,
                    email=ALICE,
                    password_hash="hash-a",
                    role=UserRole.VIEWER,
                ),
                User(
                    tenant_id=tenant_b.id,
                    email=ALICE,
                    password_hash="hash-b",
                    role=UserRole.ADMIN,
                ),
            ]
        )
        session.commit()

        stored = session.execute(select(User)).scalars().all()
        assert len(stored) == 2
        assert {user.tenant_id for user in stored} == {tenant_a.id, tenant_b.id}

    def test_the_two_users_are_distinct_rows_with_distinct_credentials(
        self, session: Session
    ) -> None:
        """Separate accounts, not one row matched twice.

        This is what makes the shared address safe: the rows are independent, so a
        change to one tenant's user cannot be observed through the other.
        """
        tenant_a = Tenant(name="Acme", slug="acme")
        tenant_b = Tenant(name="Globex", slug="globex")
        session.add_all([tenant_a, tenant_b])
        session.commit()

        user_a = User(tenant_id=tenant_a.id, email=ALICE, password_hash="hash-a")
        user_b = User(tenant_id=tenant_b.id, email=ALICE, password_hash="hash-b")
        session.add_all([user_a, user_b])
        session.commit()

        session.expire_all()
        assert require(session.get(User, user_a.id)).password_hash == "hash-a"
        assert require(session.get(User, user_b.id)).password_hash == "hash-b"

    def test_the_same_address_within_one_tenant_is_refused(
        self, session: Session
    ) -> None:
        """The constraint that makes the allowance above safe."""
        tenant = Tenant(name="Acme", slug="acme")
        session.add(tenant)
        session.commit()

        session.add(User(tenant_id=tenant.id, email=ALICE, password_hash="hash-a"))
        session.commit()
        session.add(User(tenant_id=tenant.id, email=ALICE, password_hash="hash-b"))

        with pytest.raises(IntegrityError):
            session.commit()

    def test_the_materialised_schema_has_no_unique_index_on_email_alone(
        self, session: Session
    ) -> None:
        """Reflection, not the model's own opinion of itself.

        Reading ``Base.metadata`` would only prove what the model asked for. This
        asks the database what it actually built.
        """
        inspector = inspect(session.get_bind())

        # A unique constraint and a unique index are both ways to express global email
        # uniqueness, so both are collected. They are reported by name, since the
        # two reflected types are different dicts.
        offenders = [
            f"index {index['name']}"
            for index in inspector.get_indexes("users")
            if index["unique"] and index["column_names"] == ["email"]
        ]
        offenders += [
            f"constraint {constraint['name']}"
            for constraint in inspector.get_unique_constraints("users")
            if constraint["column_names"] == ["email"]
        ]

        assert offenders == [], (
            f"a unique constraint on email alone would break PMP §12 isolation: "
            f"{offenders}"
        )

    def test_the_materialised_schema_has_the_composite_constraint(
        self, session: Session
    ) -> None:
        inspector = inspect(session.get_bind())

        composite = [
            constraint
            for constraint in inspector.get_unique_constraints("users")
            if tuple(constraint["column_names"]) == ("tenant_id", "email")
        ]

        assert len(composite) == 1


# ---------------------------------------------------------------------------
# tenant_id cannot be omitted or invented
# ---------------------------------------------------------------------------


class TestTenantIdCannotBeOmitted:
    def test_it_is_not_nullable_in_the_database(self, session: Session) -> None:
        column = next(
            column
            for column in inspect(session.get_bind()).get_columns("users")
            if column["name"] == "tenant_id"
        )

        assert column["nullable"] is False

    def test_inserting_without_a_tenant_fails_in_the_database(
        self, session: Session
    ) -> None:
        """A Core insert omitting ``tenant_id`` must be refused by NOT NULL.

        Issued as raw SQL rather than through the ORM, because the ORM would
        supply nothing for the column either — but the failure has to come from the
        database, not from the ORM noticing.
        """
        with pytest.raises(IntegrityError, match="tenant_id"):
            session.execute(
                table_of(User).insert().values(
                    id=uuid.uuid4(),
                    email=ALICE,
                    password_hash="hash",
                    role="viewer",
                )
            )
            session.commit()
        session.rollback()

    @pytest.mark.parametrize(
        "attribute", ["default", "server_default", "onupdate", "server_onupdate"]
    )
    def test_it_carries_no_default_of_any_kind(self, attribute: str) -> None:
        """A default on either side would invent a tenant where none was named."""
        assert getattr(table_of(User).c.tenant_id, attribute) is None

    def test_a_user_pointing_at_a_missing_tenant_fails(self, session: Session) -> None:
        session.add(
            User(
                tenant_id=uuid.uuid4(),
                email=ALICE,
                password_hash="hash",
            )
        )

        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            session.commit()

    def test_the_foreign_key_exists_in_the_database(self, session: Session) -> None:
        foreign_keys = inspect(session.get_bind()).get_foreign_keys("users")

        assert len(foreign_keys) == 1
        assert foreign_keys[0]["referred_table"] == "tenants"
        assert foreign_keys[0]["constrained_columns"] == ["tenant_id"]


class TestTenantDeletionIsRefused:
    def test_a_tenant_with_users_cannot_be_deleted(self, session: Session) -> None:
        tenant = Tenant(name="Acme", slug="acme")
        session.add(tenant)
        session.commit()
        session.add(User(tenant_id=tenant.id, email=ALICE, password_hash="hash"))
        session.commit()

        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            session.rollback()
            session.execute(
                table_of(Tenant).delete().where(table_of(Tenant).c.id == tenant.id)
            )
            session.commit()
        session.rollback()

        assert session.get(Tenant, tenant.id) is not None

    def test_deleting_a_user_never_removes_the_tenant(self, session: Session) -> None:
        tenant = Tenant(name="Acme", slug="acme")
        session.add(tenant)
        session.commit()
        user = User(tenant_id=tenant.id, email=ALICE, password_hash="hash")
        session.add(user)
        session.commit()

        session.delete(user)
        session.commit()

        assert session.get(Tenant, tenant.id) is not None

    def test_the_declared_delete_rule_is_restrict(self) -> None:
        foreign_key = next(iter(table_of(User).c.tenant_id.foreign_keys))

        assert foreign_key.ondelete == "RESTRICT"

    def test_the_user_side_relationship_declares_no_cascade(self) -> None:
        """``User.tenant`` must not cascade, or deleting a user could reach the
        tenant."""
        cascade = User.tenant.property.cascade

        # `CascadeOptions.delete` is falsy when the relationship declares no
        # delete cascade. `delete_orphan` is asserted as well because it would be
        # an equally dangerous way to reach the tenant from the user side.
        assert not cascade.delete
        assert not cascade.delete_orphan

    def test_the_tenant_side_relationship_cascades_toward_users(self) -> None:
        """The cascade is one-directional, and this is the direction it points."""
        cascade = Tenant.users.property.cascade

        assert cascade.delete
        assert cascade.delete_orphan


# ---------------------------------------------------------------------------
# No tenant identity is read from request data — source-level proof
# ---------------------------------------------------------------------------


class TestNoRequestDerivedTenant:
    """AST inspection of every application module.

    These tests read source, not runtime behaviour, because the property being
    asserted is an *absence*: there is no code path to observe. A behavioural test
    would pass trivially in a codebase that simply has no request handling yet.
    """

    def test_no_module_reads_tenant_identity_from_a_request(self) -> None:
        offenders: dict[str, list[str]] = {}

        for path in application_python_files():
            found = matching_sources(parse(path))
            if found:
                offenders[str(path.relative_to(BACKEND_ROOT))] = found

        assert offenders == {}, (
            "tenant identity must come from the authenticated principal, never "
            "from request data (PMP §8, §12). Found request-source access in:\n"
            f"{offenders}"
        )

    def test_no_migration_reads_tenant_identity_from_a_request(self) -> None:
        for path in sorted(ALEMBIC_ROOT.rglob("*.py")):
            assert matching_sources(parse(path)) == [], (
                f"{path} reads from a request source; migrations must not"
            )

    def test_no_module_declares_a_global_tenant_variable(self) -> None:
        """A module-level ``tenant_id`` would be ambient, context-free scope.

        The safe pattern is an explicit parameter, which is a local and so is not
        matched here.
        """
        offenders: list[str] = []

        for path in application_python_files():
            found = module_level_tenant_names(parse(path))
            if found:
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {found}")

        assert offenders == [], (
            "a module-level tenant variable makes tenant scope ambient instead of "
            f"explicit: {offenders}"
        )

    def test_no_module_reads_a_tenant_from_an_environment_variable(self) -> None:
        """``TENANT_ID`` from the environment is not a trusted identity.

        Scoped to actual environment reads, so a column-name tuple such as
        ``("tenant_id", "email")`` in a model module is not mistaken for one.
        """
        offenders: list[str] = []

        for path in application_python_files():
            found = matching_environment_reads(parse(path))
            if found:
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {found}")

        assert offenders == [], (
            f"a tenant identifier must not be read from configuration: {offenders}"
        )

    def test_the_ast_detectors_would_catch_a_violation(self) -> None:
        """Proves the checks above are not vacuous.

        Each detector is run against source that genuinely does the forbidden
        thing, and against look-alike source that must not be flagged. If this
        fails, the detector is broken and every other test in this class asserts
        nothing — either by missing a real violation or by flagging innocent code.
        """
        violating = (
            "tenant_id = request.headers.get('X-Tenant-ID')",
            "tenant_id = request.query_params['tenant_id']",
            "tenant_id = request.cookies.get('tenant')",
            "tenant_id = request.json()['tenant_id']",
            "import os\ntenant_id = os.getenv('TENANT_ID')",
            "import os\ntenant_id = os.environ['TENANT_ID']",
            "tenant_id = TENANT_ID",
        )
        for source in violating:
            tree = ast.parse(source)
            detected = matching_sources(tree) or matching_environment_reads(tree) or (
                bool(module_level_tenant_names(tree))
            )
            assert detected, f"detector missed: {source!r}"

        # Innocent code that a naive substring or attribute matcher would flag.
        innocent = (
            "url = engine.url",
            "path = Path(__file__).parent",
            "session = factory()",
            "COLUMNS = ('tenant_id', 'email')",
            "TENANT_NAME_MAX_LENGTH = 255",
            "def get_documents(tenant_id, session):\n    return session",
        )
        for source in innocent:
            tree = ast.parse(source)
            assert not matching_sources(tree), f"false positive: {source!r}"
            assert not matching_environment_reads(tree), f"false positive: {source!r}"
            assert not module_level_tenant_names(tree), f"false positive: {source!r}"


# ---------------------------------------------------------------------------
# Scope: authentication exists, and it exists exactly once
# ---------------------------------------------------------------------------


class TestAuthenticationCodeShape:
    """Phase 2 Step 2's "one implementation path" requirement.

    Step 2 originally asserted the *absence* of authentication code. Those
    assertions are inverted here rather than deleted, because "no
    authentication exists" and "authentication exists in exactly one place" are
    different claims and only the second one is worth enforcing from here on.

    Concretely, the risk this guards against is not that someone writes auth
    code — that is what this step does — but that they write it a *second*
    time. Two password hashing calls, two JWT libraries, or a second ad-hoc
    role check would each be a silent fork in the one path that matters most,
    and none of them would fail any functional test.
    """

    #: Modules that implement a cryptographic primitive. Exactly one file per
    #: concern is the whole point; a second one is a fork.
    SECURITY_MODULE = APP_ROOT / "core" / "security.py"

    #: The libraries approved in Step 2, mapped to the single module allowed to
    #: import each. ``argon2`` for hashing, ``jwt`` for tokens.
    APPROVED_IMPORTERS: Final[dict[str, str]] = {
        "argon2": "app/core/security.py",
        "jwt": "app/core/security.py",
    }

    #: Considered and rejected during Step 2's dependency selection. Kept here
    #: as a standing prohibition so a later step cannot quietly add one:
    #: ``jose`` has algorithm-negotiation defects, ``passlib`` is unmaintained,
    #: ``bcrypt`` would create a second hashing implementation, and
    #: ``fastapi_users`` is a whole user-management framework that would
    #: implement the endpoint surface explicitly deferred to Step 3.
    FORBIDDEN_LIBRARIES: Final[frozenset[str]] = frozenset(
        {"jose", "passlib", "bcrypt", "fastapi_users", "authlib", "oauthlib"}
    )

    def test_the_authentication_modules_exist(self) -> None:
        """Authentication is implemented, in the modules Step 2 designated."""
        assert self.SECURITY_MODULE.is_file(), "app/core/security.py must exist"
        assert (APP_ROOT / "core" / "identity.py").is_file()
        assert (APP_ROOT / "api" / "deps.py").is_file()
        assert (APP_ROOT / "services" / "auth.py").is_file()

    def test_password_hashing_and_token_handling_share_one_module(self) -> None:
        """No second ``security``-style module.

        A file named ``jwt.py``, ``password.py`` or ``tokens.py`` would be a
        competing implementation path. Their continued absence is asserted
        explicitly so adding one is a deliberate, visible act.
        """
        for name in ("jwt.py", "password.py", "tokens.py", "rbac.py", "auth.py"):
            assert not (APP_ROOT / "core" / name).is_file(), (
                f"app/core/{name} would be a second implementation path for "
                "something app/core/security.py already owns"
            )

    def test_only_the_security_module_imports_a_cryptography_library(self) -> None:
        """One importer per library, not merely one library.

        Weaker than "only ``security.py`` may import ``argon2``" would sound:
        ``jwt`` must be importable from ``deps.py`` only if that module
        verifies a token, which it must not do directly — it delegates to
        :class:`~app.core.identity.AuthenticatedIdentity`. Asserting the exact
        importer per library catches both an extra library and a misplaced one.
        """
        offenders: list[str] = []
        for path in application_python_files():
            relative = path.relative_to(BACKEND_ROOT).as_posix()
            for imported in imported_root_modules(tree_of(path)):
                if imported in self.FORBIDDEN_LIBRARIES:
                    offenders.append(f"{relative}: rejected library {imported!r}")
                elif imported in self.APPROVED_IMPORTERS:
                    expected = self.APPROVED_IMPORTERS[imported]
                    if relative != expected:
                        offenders.append(
                            f"{relative}: imports {imported!r}, which only "
                            f"{expected} may do"
                        )

        assert offenders == [], (
            "authentication must have exactly one implementation path: "
            f"{offenders}"
        )

    def test_the_requirements_declare_exactly_the_approved_pins(self) -> None:
        """Two pinned packages, and nothing else in the auth space.

        Asserted positively rather than by exclusion, so adding a *fourth* auth
        dependency fails here rather than passing an exclusion list that was
        never updated.

        Only requirement *lines* are read. ``requirements.txt`` documents why
        each package was chosen, and that prose legitimately names the
        alternatives that were rejected — asserting on the raw file text would
        flag this project's own reasoning about why ``python-jose`` was not
        used.
        """
        approved = {"argon2-cffi", "pyjwt"}
        declared: dict[str, str] = {}

        for name in ("requirements.txt", "requirements-dev.txt"):
            for line in declared_requirements(BACKEND_ROOT / name):
                package, _, version = line.partition("==")
                declared[package.strip().lower()] = version.strip()

        auth_packages = {
            package
            for package in declared
            if any(
                package == item or package.startswith(f"{item}-")
                for item in (
                    "argon2",
                    "pyjwt",
                    "jose",
                    "passlib",
                    "bcrypt",
                    "authlib",
                    "oauthlib",
                )
            )
        }
        assert auth_packages == approved, (
            f"expected exactly {sorted(approved)} in the requirements, found "
            f"{sorted(auth_packages)}"
        )

        # Pinned exactly, per the file's own stated policy.
        for package in approved:
            assert re.fullmatch(r"\d+(?:\.\d+)+", declared[package]), (
                f"{package} must be pinned to an exact version, found "
                f"{declared[package]!r}"
            )

    def test_the_rejected_auth_libraries_are_not_installed_requirements(
        self,
    ) -> None:
        """The exclusions survive independently of the positive check above."""
        for name in ("requirements.txt", "requirements-dev.txt"):
            for package in declared_requirements(BACKEND_ROOT / name):
                base = package.split("==")[0].strip().lower()
                for forbidden in self.FORBIDDEN_LIBRARIES:
                    assert not (
                        base == forbidden or base.startswith(f"{forbidden}-")
                    ), f"{name} declares rejected package {base!r}"

    def test_role_enforcement_lives_in_exactly_one_module(self) -> None:
        """Authorization has exactly one home: :mod:`app.api.rbac`.

        **Inverted at Phase 2.3, not deleted.** This test previously asserted that
        *no* role-enforcement function existed anywhere, because RBAC was a later
        step. Phase 2.3 is that step, so the absence it asserted is now the
        problem: an authorization check that can be written inline in any router
        is an authorization check that will eventually be written inline in a
        router, inconsistently.

        The replacement asserts the property that actually matters and that the
        original was a proxy for: exactly one application module may define a
        role-enforcement helper, and it is the one dedicated to it. A second
        module growing its own ``has_role``-style function fails this test, which
        is the failure the original test was watching for — just pointing the
        other way.
        """
        authorization_module = "app/api/rbac.py"
        markers = ("require_role", "check_permission", "has_role", "is_admin")

        definitions: dict[str, list[str]] = {}
        for path in application_python_files():
            relative = path.relative_to(BACKEND_ROOT).as_posix()
            tree = parse(path)
            for node in ast.walk(tree):
                # Both node kinds carry `name` and `lineno`; the isinstance
                # narrows the union so mypy sees the attributes.
                if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    continue
                if any(marker in node.name.lower() for marker in markers):
                    definitions.setdefault(relative, []).append(
                        f"{relative}:{node.lineno} {node.name}"
                    )

        assert set(definitions) == {authorization_module}, (
            "role-enforcement logic must exist only in "
            f"{authorization_module}; found it in {sorted(definitions)}"
        )
        assert any(
            name.endswith("require_role") for name in definitions[authorization_module]
        ), f"{authorization_module} defines no require_role factory"



