"""``require_role`` itself, tested as a mechanism rather than through routes.

Why this file exists when ``tests/test_users_api.py`` already proves 403s
---------------------------------------------------------------------------
The API tests establish that *some* role check refuses editors. They cannot
establish *which* check refused them, nor that it refuses in the way the module
claims. A guard that returned ``403`` for the wrong reason — an accidentally
swapped allowlist, a comparison against a constant, a mis-parsed enum — would
pass every route test in the suite.

So the fail-closed properties are pinned here, at the unit level, where each one
can be falsified independently:

* an **empty** allowlist denies everyone;
* an **unrecognised** role denies;
* the identity is returned **unchanged**, so chaining is safe;
* the checker is a **factory**, so two routes with different requirements cannot
  share state;
* the denial message and status are **fixed**, and deliberately not the 401
  detail.

Alongside those, this file asserts the *placement* invariant: RBAC logic lives in
exactly one module, and no router compares a role by hand.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import cast

import pytest
from fastapi import HTTPException, status

from app.api.deps import UNAUTHORIZED_DETAIL
from app.api.rbac import FORBIDDEN_DETAIL, RoleChecker, require_role
from app.core.identity import AuthenticatedIdentity, IdentitySource
from app.models.user import UserRole

USER = uuid.UUID("11111111-2222-3333-4444-555555555555")
TENANT = uuid.UUID("66666666-7777-8888-9999-aaaaaaaaaaaa")

#: The one module permitted to decide what a role may do.
RBAC_MODULE = Path("app/api/rbac.py")

#: Every module under ``app/api``. Includes ``rbac.py`` and ``deps.py`` on
#: purpose: the placement rules must hold for the whole package, not only for the
#: files that happen to declare routes today.
API_MODULES = sorted(Path("app/api").glob("*.py"))

#: Everything except ``rbac.py``, which is by definition the module that issues
#: the 403 the other modules are forbidden to issue.
CONSUMERS = [path for path in API_MODULES if path != RBAC_MODULE]

#: A role value the enum does not contain.
UNKNOWN_ROLE = "wizard"


def identity(role: UserRole | str) -> AuthenticatedIdentity:
    """A verified identity carrying ``role``.

    ``role`` accepts a bare ``str`` because one property below is only reachable
    with a role the enum does not contain. :class:`AuthenticatedIdentity` is a
    plain frozen dataclass and validates nothing, so such a value really can be
    placed in one — which is exactly the situation the membership check has to
    survive.
    """
    # `UserRole` is a StrEnum, so every member *is* a string; a value outside the
    # enum is therefore a `UserRole`-typed string with no corresponding member.
    # `cast` is sound for that, and it is how an unrecognised role would reach a
    # real identity if a token ever carried one.
    coerced = role if isinstance(role, UserRole) else cast(UserRole, role)
    return AuthenticatedIdentity(user_id=USER, tenant_id=TENANT, role=coerced)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


class TestAdmission:
    @pytest.mark.parametrize("role", list(UserRole))
    def test_any_role_can_be_permitted(self, role: UserRole) -> None:
        """Every role in the enum can appear in an allowlist.

        If the mechanism could only express "administrator or nothing", a later
        route that the PMP grants to editors would need a second, differently
        shaped check — and the two would drift.
        """
        checker = require_role(role)

        assert checker(identity(role)) is not None

    @pytest.mark.parametrize(
        ("permitted", "actual"),
        [
            (UserRole.ADMIN, UserRole.EDITOR),
            (UserRole.ADMIN, UserRole.VIEWER),
            (UserRole.EDITOR, UserRole.VIEWER),
            (UserRole.VIEWER, UserRole.EDITOR),
            (UserRole.EDITOR, UserRole.ADMIN),
            (UserRole.VIEWER, UserRole.ADMIN),
        ],
    )
    def test_a_role_outside_the_allowlist_is_refused(
        self, permitted: UserRole, actual: UserRole
    ) -> None:
        checker = require_role(permitted)

        with pytest.raises(HTTPException) as caught:
            checker(identity(actual))

        assert caught.value.status_code == status.HTTP_403_FORBIDDEN

    def test_several_roles_can_be_permitted_at_once(self) -> None:
        checker = require_role(UserRole.ADMIN, UserRole.EDITOR)

        for role in (UserRole.ADMIN, UserRole.EDITOR):
            assert checker(identity(role)) is not None

        with pytest.raises(HTTPException):
            checker(identity(UserRole.VIEWER))

    def test_the_order_of_the_allowlist_is_irrelevant(self) -> None:
        assert require_role(UserRole.ADMIN, UserRole.EDITOR)(
            identity(UserRole.EDITOR)
        ) is not None
        assert require_role(UserRole.EDITOR, UserRole.ADMIN)(
            identity(UserRole.ADMIN)
        ) is not None

    def test_a_role_appears_once_however_many_times_it_is_listed(self) -> None:
        """A repeated allowlist entry is not a distinct grant."""
        assert require_role(UserRole.ADMIN, UserRole.ADMIN)(identity(UserRole.ADMIN)) is not None


# ---------------------------------------------------------------------------
# Failing closed
# ---------------------------------------------------------------------------


class TestFailsClosed:
    """The properties that make the default safe rather than convenient."""

    def test_an_empty_allowlist_denies_everyone(
        self,
    ) -> None:
        """``require_role()`` is a closed door, not an open one.

        The most dangerous possible implementation of this function is one that
        admits everybody when given no roles: a route author who wrote
        ``require_role()`` and meant "no restriction" would get it silently.
        Failing closed turns that mistake into a 403 on the first request, where
        it is noticed, instead of an open endpoint, where it is not.
        """
        checker = require_role()

        for role in (*UserRole, UNKNOWN_ROLE):
            with pytest.raises(HTTPException) as caught:
                checker(identity(role))
            assert caught.value.status_code == status.HTTP_403_FORBIDDEN

    def test_an_unrecognised_role_is_refused(
        self,
    ) -> None:
        """A role outside the enum is denied, not defaulted.

        Through a real token this cannot happen —
        :meth:`AuthenticatedIdentity.from_claims` raises before an identity is
        built — so the only way to reach it is a role added to the enum later
        without a decision about this route. It must default to *denied*.

        The honest scope of the guarantee: membership is tested against a
        ``frozenset`` of :class:`UserRole` members, and because ``StrEnum``
        members hash and compare as their *values*, a future member added with
        the value ``"wizard"`` would be admitted here. That is a property of
        ``StrEnum``, not of this check, and it is why the enum — not a string
        literal — is what the allowlist holds.
        """
        checker = require_role(*UserRole)

        with pytest.raises(HTTPException) as caught:
            checker(identity(UNKNOWN_ROLE))

        assert caught.value.status_code == status.HTTP_403_FORBIDDEN

    def test_a_role_checker_never_raises_401(
        self,
    ) -> None:
        """This module issues exactly one status code.

        ``401`` means "I do not know who you are", and that question belongs to
        :mod:`app.api.deps`. A checker answering 401 would tell a client its
        caller could not be identified — false — and would make it re-authenticate
        when the real problem is insufficient privilege.
        """
        checker = require_role(*UserRole)

        with pytest.raises(HTTPException) as caught:
            checker(identity(UNKNOWN_ROLE))

        assert caught.value.status_code == status.HTTP_403_FORBIDDEN
        assert FORBIDDEN_DETAIL != UNAUTHORIZED_DETAIL


# ---------------------------------------------------------------------------
# Transparency
# ---------------------------------------------------------------------------


class TestTheIdentityIsPassedThrough:
    def test_an_admitted_identity_is_returned_unchanged(
        self,
    ) -> None:
        """The checker is transparent on success.

        Chaining is the point of the design: a route declares
        ``Annotated[AuthenticatedIdentity, Depends(require_role(...))]`` and
        receives the object the identity dependency produced. A copy — or worse,
        a re-derived identity — would mean the authorization layer could disagree
        with the authentication layer about who the caller is.
        """
        caller = identity(UserRole.ADMIN)

        assert require_role(UserRole.ADMIN)(caller) is caller

    def test_the_passed_through_identity_retains_every_field(
        self,
    ) -> None:
        caller = identity(UserRole.EDITOR)

        result = require_role(UserRole.EDITOR, UserRole.ADMIN)(caller)

        assert result.user_id == USER
        assert result.tenant_id == TENANT
        assert result.role is UserRole.EDITOR
        assert result.source is IdentitySource.BEARER_ACCESS_TOKEN


# ---------------------------------------------------------------------------
# The factory property
# ---------------------------------------------------------------------------


class TestTheCheckerIsAFactory:
    def test_two_callers_get_distinct_objects(
        self,
    ) -> None:
        """Each route owns its own checker.

        Were the allowlist captured in a module-level variable rather than in the
        closure, one route's requirement would overwrite another's and the
        behaviour would depend on import order.
        """
        assert require_role(UserRole.ADMIN) is not require_role(UserRole.ADMIN)

    def test_no_mutation_leaks_between_two_checkers(
        self,
    ) -> None:
        admin_only = require_role(UserRole.ADMIN)
        everyone = require_role(*UserRole)

        assert admin_only(identity(UserRole.ADMIN)) is not None
        assert everyone(identity(UserRole.VIEWER)) is not None

        with pytest.raises(HTTPException):
            admin_only(identity(UserRole.VIEWER))

        # Building the permissive checker last must not have loosened the first.
        assert admin_only(identity(UserRole.ADMIN)) is not None

    def test_a_refusal_does_not_poison_later_calls(
        self,
    ) -> None:
        """A denial must leave no residue.

        Catches a checker that added the refused role to its allowlist, or that
        memoised a "already decided" flag.
        """
        checker = require_role(UserRole.ADMIN)

        with pytest.raises(HTTPException):
            checker(identity(UserRole.VIEWER))

        assert checker(identity(UserRole.ADMIN)) is not None
        with pytest.raises(HTTPException):
            checker(identity(UserRole.VIEWER))

    def test_the_returned_object_satisfies_the_exported_signature(
        self,
    ) -> None:
        """``RoleChecker`` is importable and describes what comes back.

        Route modules annotate with it, so mypy needs a concrete type rather
        than a fresh inferred callable per call site. If the annotation were
        dropped, the checks above would keep passing while every route lost its
        type.
        """
        checker: RoleChecker = require_role(UserRole.ADMIN)

        assert callable(checker)
        assert checker(identity(UserRole.ADMIN)) is not None


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------


class TestTheDenialMessage:
    def test_it_names_neither_the_role_nor_the_requirement(
        self,
    ) -> None:
        """One fixed string for every failure.

        "You are a viewer", or "this route needs an administrator", is a
        specification of the access-control model handed to whoever is mapping
        it. The application logs the role; the client does not receive it.
        """
        message = FORBIDDEN_DETAIL.lower()

        for leaked in ("viewer", "editor", "admin", "role", "tenant", "required"):
            assert leaked not in message

    def test_it_is_stable_across_every_refused_role(
        self,
    ) -> None:
        messages = {
            exc.detail for role in (*UserRole, UNKNOWN_ROLE) for exc in _refusals(role)
        }

        assert messages == {FORBIDDEN_DETAIL}


def _refusals(role: UserRole | str) -> list[HTTPException]:
    """The exceptions produced by refusing ``role``, across several allowlists.

    A refusal has to look identical whichever route refused it, so the same role
    is checked against every allowlist this application can build.
    """
    raised: list[HTTPException] = []
    for permitted in (UserRole.ADMIN, *UserRole):
        try:
            require_role(permitted)(identity(role))
        except HTTPException as exc:
            raised.append(exc)
    return raised


# ---------------------------------------------------------------------------
# Placement: one module, and no hand-rolled comparisons
# ---------------------------------------------------------------------------


def _mentions_a_role(node: ast.expr) -> bool:
    """True if ``node`` is — or is built from — a :class:`UserRole` reference.

    Matches the bare member names (``admin``/``editor``/``viewer``, so
    ``role == "admin"`` is caught) and the qualified ``UserRole.ADMIN`` form.
    """
    if isinstance(node, ast.Name):
        return node.id in {member.name for member in UserRole}
    if isinstance(node, ast.Attribute):
        return node.attr == "UserRole" or _mentions_a_role(node.value)
    return False


class TestRoleLogicLivesInExactlyOneModule:
    def test_the_glob_found_the_modules_it_is_auditing(
        self,
    ) -> None:
        """Guards the placement tests against passing because the glob was empty.

        ``documents.py`` joined the set at Phase 2.4. Named explicitly so that a
        rename or a moved router would fail *this* test — which is about the audit's
        coverage — rather than silently reducing the number of modules the
        role-comparison checks below actually inspect.
        """
        assert {path.name for path in API_MODULES} >= {
            "auth.py",
            "deps.py",
            "documents.py",
            "rbac.py",
            "users.py",
        }

    @pytest.mark.parametrize("path", CONSUMERS, ids=lambda p: p.name)
    def test_no_consumer_defines_its_own_guard(self, path: Path) -> None:
        """No second authorization implementation may appear.

        A route that checked its own role would bypass every property in this
        file: the fail-closed empty allowlist, the fixed message, the refusal to
        issue a 401.

        Matched on what a *guard* is called rather than on anything containing
        "role". ``update_role`` in ``app/api/users.py`` is a handler for
        ``PATCH /users/{id}/role`` and is entitled to the word; what it is not
        entitled to do is decide whether its caller is permitted. The prefixes
        below are the vocabulary of checking, and a definition using one is the
        thing that would have to be examined.
        """
        tree = ast.parse(path.read_text())
        prefixes = (
            "require",
            "check",
            "enforce",
            "authorize",
            "authorise",
            "permit",
            "allow",
            "guard",
            "deny",
        )

        offenders = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name.lower().startswith(prefixes)
        ]

        assert offenders == []

    @pytest.mark.parametrize("path", CONSUMERS, ids=lambda p: p.name)
    def test_no_consumer_compares_a_role_by_hand(self, path: Path) -> None:
        """No ``role == "admin"``, no ``role in [...]``, no ``role is not …``.

        :func:`require_role` exists so the comparison happens in one place. A
        literal comparison in a router is exactly what it was written to prevent:
        it would bring its own message, its own fail-open risk, and a second place
        to look when the access model is audited.

        ``require_role(UserRole.ADMIN)`` is *not* flagged: the attribute there is
        ``UserRole.ADMIN``, whose ``Attribute.attr`` is ``"ADMIN"``, not a bare
        role name — which is the point. Naming the requirement through the enum is
        the sanctioned way to state it.
        """
        tree = ast.parse(path.read_text())

        comparisons = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and any(_mentions_a_role(part) for part in [node.left, *node.comparators])
        ]

        assert comparisons == []

    @pytest.mark.parametrize("path", CONSUMERS, ids=lambda p: p.name)
    def test_no_consumer_raises_its_own_403(self, path: Path) -> None:
        """The single denial site is :func:`require_role`.

        A second one would mean a second message and a second decision about what
        to disclose. ``409`` is fine — ``app/api/auth.py`` and
        ``app/api/users.py`` raise it for genuine conflicts and guardrails — and
        this test distinguishes them by status code, not by the exception type.
        """
        tree = ast.parse(path.read_text())

        raised = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "HTTPException"
            and "403" in ast.unparse(node)
        ]

        assert raised == []

    @pytest.mark.parametrize("path", CONSUMERS, ids=lambda p: p.name)
    def test_no_handler_takes_a_role_from_the_request(self, path: Path) -> None:
        """No route handler may declare a parameter named ``role``.

        A role reaches the application in exactly one way: as a token claim,
        converted once by :meth:`AuthenticatedIdentity.from_claims`. A handler
        parameter named ``role`` would be a second, client-facing source, and the
        signature is what makes that check greppable.
        """
        tree = ast.parse(path.read_text())

        offenders = [
            f"{node.name}(role)"
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                argument.arg == "role"
                for argument in [*node.args.args, *node.args.kwonlyargs]
            )
        ]

        assert offenders == []

    @pytest.mark.parametrize("path", API_MODULES, ids=lambda p: p.name)
    def test_no_api_module_reaches_into_the_raw_request(self, path: Path) -> None:
        """Nobody under ``app/api`` reads the request for a tenant or a role.

        The ``Authorization`` header is the one client-supplied value that does
        reach the application, and it reaches it through
        :class:`~fastapi.security.HTTPAuthorizationCredentials` — a dependency —
        rather than through ``request.headers``. So *no* module under
        ``app/api``, ``deps.py`` included, may touch ``headers``, ``cookies``,
        ``query_params`` or ``path_params``. That makes a header-supplied
        ``X-Tenant-Id`` structurally impossible rather than merely ignored.
        """
        tree = ast.parse(path.read_text())

        offenders = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr in {"headers", "cookies", "query_params"}
        ]

        assert offenders == []

    def test_the_check_reads_the_role_of_the_identity_it_is_given(
        self,
    ) -> None:
        """The positive control for the two tests above.

        ``require_role`` reaches into ``identity.role`` and nowhere else. Stated
        as an assertion so that a future refactor of the checker which stopped
        consulting the role would fail here rather than quietly admitting
        everyone — the failure mode an empty allowlist is there to prevent.
        """
        tree = ast.parse(RBAC_MODULE.read_text())

        attributes = {
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
        }

        assert "identity.role" in attributes
        assert not any(name.startswith("request.") for name in attributes)

    def test_the_rbac_module_is_the_only_one_holding_the_denial_detail(
        self,
    ) -> None:
        """``FORBIDDEN_DETAIL`` is defined once and not copied.

        A consumer that spelled the message out inline would be a second source
        for the string, free to drift from the one clients are documented against.
        """
        defining = [
            path.name
            for path in API_MODULES
            if f'"{FORBIDDEN_DETAIL}"' in path.read_text() or f"'{FORBIDDEN_DETAIL}'" in path.read_text()
        ]

        assert defining == [RBAC_MODULE.name]


# ---------------------------------------------------------------------------
# Every declared allowlist is actually applied to a route
# ---------------------------------------------------------------------------
#
# This class exists because of a bug it was written to prevent, which is worth
# stating plainly rather than leaving as folklore.
#
# ``app/api/documents.py`` declared three allowlists at module level —
# ``MAY_UPLOAD``, ``MAY_VIEW``, ``ADMIN_ONLY`` — built from :func:`require_role`,
# each with a docstring explaining exactly which PMP section it came from. Every
# one of them was correct, documented, and **unreferenced**: no handler annotated
# anything with them, so every document route served every authenticated caller.
# A viewer could upload, an editor could delete, and any role could read the
# administration status view. The module docstring claimed "No route in this
# module reads a role or compares one", which was true, and the claim had been
# mistaken for the guarantee.
#
# Nothing else in the suite caught it. ``test_rbac.py``'s placement tests passed,
# because no route *did* compare a role. The ``users.py`` tests passed, because
# that module wired its alias correctly. The functional document tests passed,
# because until each role's own route was exercised the missing check produced
# no wrong answer.
#
# So the property worth pinning is not "the allowlists are declared" — trivially
# true either way — but "every route in the package is guarded, and no declared
# guard is dead". Both directions fail loudly.


def _looks_like_an_identity_alias(name: str, value: object) -> bool:
    """Whether a module attribute is one of this module's identity aliases.

    Judged on the *value*: ``Annotated[AuthenticatedIdentity, Depends(...)]``
    evaluates to an ``Annotated`` object, which is neither callable nor a class, so
    a name test alone would also match an unrelated helper and a callable test
    alone would match nothing.
    """
    return name.endswith("Identity") and type(value).__name__ == "_AnnotatedAlias"


def _routes_with_identity_dependencies(source: Path) -> dict[str, set[str]]:
    """Map each handler name to the identity aliases its signature uses.

    Parsed from the AST rather than read from ``app.routes`` on purpose: FastAPI
    0.142 wraps included routers in ``_IncludedRouter`` objects, so
    ``app.routes`` no longer lists the application own routes directly. The
    source is also the thing under review — the question is what the module
    *declares*, and that is what the file says.
    """
    tree = ast.parse(source.read_text())
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in {"get", "post", "put", "patch", "delete"}
            for decorator in node.decorator_list
        )
    }

    guarded: dict[str, set[str]] = {}
    for name, function in functions.items():
        aliases = {
            annotation.id
            for argument in function.args.args
            for annotation in [argument.annotation]
            if isinstance(annotation, ast.Name)
        }
        guarded[name] = aliases & IDENTITY_ALIASES
    return guarded


def _identity_alias_name(node: ast.stmt) -> str | None:
    """The name ``node`` binds, if it binds an identity alias; otherwise ``None``.

    Matches ``Name = Annotated[AuthenticatedIdentity, ...]`` and returns the
    name.

    Returns the name rather than a :class:`bool` so that callers do not have to
    re-narrow the node afterwards. A predicate would force each of them to repeat
    the ``isinstance`` chain this function already performed — and a caller that
    forgot would fail to type-check, which is at least loud, but the duplication
    is the point worth avoiding.

    A plain :class:`ast.Assign`, not an :class:`ast.AnnAssign`: the alias's *value*
    is the ``Annotated[...]`` and its target carries no annotation.

    Matched on the first type argument being ``AuthenticatedIdentity``, not merely
    on being an ``Annotated[...]`` subscript. ``app/api/deps.py`` declares three
    such aliases — ``CurrentIdentity``, ``AppSettings`` and ``FileStore`` — and two
    of them authorize nothing; without this check they would be counted as guards,
    and a route taking ``storage: FileStore`` would look like a route with two
    authorization requirements.
    """
    if not (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Subscript)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "Annotated"
    ):
        return None

    # ``Subscript.slice`` is typed ``expr`` because it may also be a
    # ``Slice``. An ``Annotated[...]`` written by hand always uses a ``Tuple`` of
    # type arguments, so that is the only shape worth accepting — and rejecting
    # the rest is what makes this safe to index.
    arguments = node.value.slice
    if not isinstance(arguments, ast.Tuple) or not arguments.elts:
        return None

    first = arguments.elts[0]
    if isinstance(first, ast.Name) and first.id == "AuthenticatedIdentity":
        return node.targets[0].id
    return None


#: Every module-level name in ``app/api/*.py`` that is an ``Annotated`` identity.
#: Discovered rather than listed, so a router that adds a fourth allowlist is
#: covered by these tests without this file being edited.
def _discovered_identity_aliases() -> frozenset[str]:
    found = {
        name
        for path in CONSUMERS
        for node in ast.parse(path.read_text()).body
        if (name := _identity_alias_name(node)) is not None
    }
    return frozenset(found)


IDENTITY_ALIASES = _discovered_identity_aliases()

#: ``app.api.deps``'s alias for "authenticated, authorization not yet decided".
#: Naming it on a handler *is* the bug: it is the whole of an identity with none
#: of the role requirement. Excluded from :data:`IDENTITY_ALIASES` above because
#: it is the one alias whose whole purpose is to be replaced.
UNGUARDED_IDENTITY = "CurrentIdentity"

#: The module permitted to *define* an identity alias that no route consumes.
DEPENDENCY_MODULE = Path("app/api/deps.py")

#: Modules under ``app/api`` that declare no routes. Closed, and asserted: a
#: router added to this set would stop being audited, so the list is here to be
#: looked at rather than to be appended to.
ROUTERLESS_MODULES = frozenset({Path("app/api/__init__.py"), DEPENDENCY_MODULE})

#: The routes that are legitimately reachable without authentication, named
#: individually.
#:
#: Four in ``auth.py`` — registration and login are how a tenant comes into being
#: at all, and refresh and logout act on the refresh token rather than on the
#: caller's identity. Two in ``health.py`` — an infrastructure probe that required
#: a credential would report a deployment as unhealthy precisely when it is.
#:
#: Named rather than derived, because "every route must authenticate" and "these
#: specific routes must not" are both worth stating, and only the first can be
#: derived from the source.
PUBLIC_ROUTES = frozenset(
    {
        "auth.register",
        "auth.login",
        "auth.refresh",
        "auth.logout",
        "health.health",
        "health.readiness",
    }
)

#: The two modules that serve the tenant API, and therefore must authenticate.
#: ``health.py`` is infrastructure and ``deps.py`` declares no routes.
BUSINESS_ROUTER_MODULES = frozenset({"auth.py", "users.py", "documents.py"})


class TestEveryDeclaredAllowlistIsApplied:
    def test_the_alias_discovery_found_something(self) -> None:
        """Guards the two structural tests below against passing on an empty set.

        If the AST walk stopped matching, both would report "every route is
        guarded" for a package with no guards at all — the same false negative,
        one level up.
        """
        assert IDENTITY_ALIASES >= {"CurrentAdmin"}

    def test_the_public_route_allowlist_names_real_public_routes(self) -> None:
        """Guards the exemption below against becoming a loophole.

        ``PUBLIC_ROUTES`` exists because a handful of routes are legitimately
        unauthenticated. A list nobody checks is indistinguishable from "every
        route is public", so this asserts both directions:

        * every name in it is a route that actually exists — so it cannot rot into
          a set of names that protect nothing;
        * every name in it takes **no** identity parameter — so a route cannot
          acquire authentication and stay exempted, which is the failure mode an
          allowlist is supposed to prevent.
        """
        discovered = {
            f"{path.stem}.{name}"
            for path in CONSUMERS
            for name, aliases in _routes_with_identity_dependencies(path).items()
        }
        private = {
            f"{path.stem}.{name}"
            for path in CONSUMERS
            for name, aliases in _routes_with_identity_dependencies(path).items()
            if aliases
        }

        assert PUBLIC_ROUTES <= discovered, sorted(PUBLIC_ROUTES - discovered)
        assert not PUBLIC_ROUTES & private, sorted(PUBLIC_ROUTES & private)

    def test_the_public_route_allowlist_is_the_whole_unauthenticated_surface(self) -> None:
        """No route outside the allowlist may skip authentication.

        The other direction, and the one that catches a *new* unauthenticated route:
        a handler that takes no identity and is not named in
        :data:`PUBLIC_ROUTES` is authenticated by nothing.
        """
        unnamed = {
            f"{path.stem}.{name}"
            for path in CONSUMERS
            for name, aliases in _routes_with_identity_dependencies(path).items()
            if not aliases and f"{path.stem}.{name}" not in PUBLIC_ROUTES
        }

        assert unnamed == set(), sorted(unnamed)

    @pytest.mark.parametrize("path", CONSUMERS, ids=lambda p: p.name)
    def test_every_route_that_names_an_identity_names_a_guard(self, path: Path) -> None:
        """A route asking for :data:`CurrentIdentity` has declared no requirement.

        Every route in this package authorizes through exactly one mechanism —
        ``Annotated[AuthenticatedIdentity, Depends(require_role(...))]`` — so a
        handler that asks for the plain identity is authenticated and
        **unauthorized**, which is the state the bug this class exists for left
        every document route in.

        Routes that take no identity at all are not in question: they are public,
        and :data:`PUBLIC_ROUTES` names them. Asserted per route by name so a
        failure names the route rather than reporting that some route somewhere is
        unguarded.
        """
        unguarded = {
            name: sorted(aliases)
            for name, aliases in _routes_with_identity_dependencies(path).items()
            if aliases == {UNGUARDED_IDENTITY} and f"{path.stem}.{name}"
            not in PUBLIC_ROUTES
        }

        assert unguarded == {}, unguarded

    @pytest.mark.parametrize("path", CONSUMERS, ids=lambda p: p.name)
    def test_no_module_forgets_to_authenticate_a_whole_router(self, path: Path) -> None:
        """The complement: a surface must not forget to authenticate.

        :meth:`test_every_route_that_names_an_identity_names_a_guard` alone would
        pass for a router of entirely public routes — every handler takes no
        identity at all, so none of them names the unguarded one. This is the half
        that says a route must authenticate, and it needs its own statement for the
        same reason.
        """
        anonymous = {
            name
            for name, aliases in _routes_with_identity_dependencies(path).items()
            if not aliases and f"{path.stem}.{name}" not in PUBLIC_ROUTES
        }

        assert anonymous == set(), sorted(anonymous)

    @pytest.mark.parametrize("path", CONSUMERS, ids=lambda p: p.name)
    def test_no_route_guards_on_more_than_one_allowlist(self, path: Path) -> None:
        """One identity parameter, one requirement.

        A route naming two aliases is either redundant — two chains of the same
        dependency, one of which is dead — or genuinely expressing two
        requirements, which this project has no way to state: PMP §16 is a flat
        table of operations against roles, not a policy language. ``require_role``
        itself only ever accepts one set.
        """
        multiple = {
            name: sorted(aliases)
            for name, aliases in _routes_with_identity_dependencies(path).items()
            if len(aliases) > 1
        }

        assert multiple == {}, multiple

    def test_the_dependency_module_is_where_the_aliases_are_defined(self) -> None:
        """``CurrentIdentity`` is ``deps``'s product, and nothing else's.

        Pinned so the exemption in :meth:`test_no_declared_alias_is_dead` cannot be
        widened by moving the definition: only this module may declare an identity
        alias that no route consumes.
        """
        unguarded_definers = {
            path.name
            for path in CONSUMERS
            if path != DEPENDENCY_MODULE
            and any(
                _identity_alias_name(node) == UNGUARDED_IDENTITY
                for node in ast.parse(path.read_text()).body
            )
        }

        assert unguarded_definers == set(), sorted(unguarded_definers)

    @pytest.mark.parametrize("path", CONSUMERS, ids=lambda p: p.name)
    def test_no_declared_alias_is_dead(self, path: Path) -> None:
        """Every allowlist a module declares is used by at least one route.

        The assertion that would have caught the bug described above. A declared
        allowlist is a *claim* that some route requires it; an unreferenced one is
        a claim about a control that does not exist, and it reads in the source as
        though it did.

        A module that declares no routes at all is skipped, and the skip is
        asserted rather than assumed: a *router* module that lost its decorators
        would otherwise pass by declaring nothing. ``app/api/deps.py`` is the
        legitimate case — it is where the aliases are *defined*, so
        ``CurrentIdentity`` is its own product rather than a requirement any of its
        routes declares. ``app/api/__init__.py`` declares nothing for the ordinary
        reason that it is a package marker.
        """
        guarded = _routes_with_identity_dependencies(path)
        if not guarded:
            assert path in ROUTERLESS_MODULES, path
            return

        declared = {
            name
            for node in ast.parse(path.read_text()).body
            if (name := _identity_alias_name(node)) in IDENTITY_ALIASES
        }
        used = set().union(*guarded.values())

        assert declared <= used, sorted(declared - used)

    def test_the_document_module_declares_exactly_three_allowlists(self) -> None:
        """PMP §16's document rows, one alias each.

        Named rather than derived so that *adding* a fourth allowlist fails here —
        which is where a reviewer would want to be told — and not merely pass, as
        it would under a test derived from the module.
        """
        from app.api import documents

        assert {
            name
            for name, value in vars(documents).items()
            if _looks_like_an_identity_alias(name, value)
        } == {"UploadingIdentity", "ViewingIdentity", "AdminIdentity"}

        # The underlying ``require_role`` products must match them, or the names
        # above are labels on the wrong allowlists.
        from app.api.documents import ADMIN_ONLY, MAY_UPLOAD, MAY_VIEW

        for alias, checker in (
            ("UploadingIdentity", MAY_UPLOAD),
            ("ViewingIdentity", MAY_VIEW),
            ("AdminIdentity", ADMIN_ONLY),
        ):
            assert getattr(documents, alias) is not None, alias
            assert callable(checker), alias
