"""The shared annotated field types (PMP §8, PMP §14).

Why a file for types that are a few lines each
----------------------------------------------
These types are the only place this project states what a slug, an email or a
password *is*. If the definition drifts — the floor moves, folding stops, the
pattern loosens — every endpoint changes behaviour at once, and a route test
would show it as an unrelated failure.

So the properties are asserted directly, including the ones that are *supposed*
to be absent:

* **both directions** of normalisation, which is what keeps
  ``POST /auth/register-tenant`` and ``POST /auth/login`` in agreement;
* the password floor applies to creation and **not** to verification;
* the length ceilings that bound the work Argon2 can be asked to do.

Deliberately untested
---------------------
No test asserts that a valid address is deliverable, or an invalid one
undeliverable. See :mod:`app.schemas.fields` for why ``email-validator`` is not
installed: a DNS lookup on an unauthenticated endpoint is a worse trade than a
conservative pattern, and the pattern's limits are documented rather than probed.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.models.tenant import TENANT_SLUG_MAX_LENGTH
from app.models.user import USER_EMAIL_MAX_LENGTH
from app.schemas.fields import (
    EMAIL_PATTERN,
    NEW_PASSWORD_MIN_LENGTH,
    PASSWORD_MAX_LENGTH,
    TENANT_SLUG_PATTERN,
    EmailAddress,
    NewPassword,
    Password,
    TenantName,
    TenantSlug,
)

#: A syntactically valid address in a domain that has no meaning. Nothing here
#: resolves it, which is the point.
EXAMPLE_EMAIL = "alice@acme.test"

#: The same address written three ways that must all land on one stored value.
SPELLINGS_OF_ONE_ADDRESS = (
    "alice@acme.test",
    "Alice@Acme.test",
    " ALICE@ACME.TEST ",
)

#: Three spellings of one slug.
SPELLINGS_OF_ONE_SLUG = ("acme", "Acme", "  acme  ")


class _Slug(BaseModel):
    value: TenantSlug


class _Email(BaseModel):
    value: EmailAddress


class _TenantName(BaseModel):
    value: TenantName


class _NewPassword(BaseModel):
    value: NewPassword


class _PresentedPassword(BaseModel):
    value: Password


# ---------------------------------------------------------------------------
# Tenant slug
# ---------------------------------------------------------------------------


class TestTenantSlug:
    @pytest.mark.parametrize(
        "slug",
        ["acme", "a", "acme-corp", "a1-b2", "9", "x" * TENANT_SLUG_MAX_LENGTH],
    )
    def test_a_permitted_slug_is_accepted(self, slug: str) -> None:
        assert _Slug(value=slug).value == slug.lower()

    @pytest.mark.parametrize(
        "candidate",
        [
            "",
            " ",
            "-acme",
            "acme-",
            "ac--me",
            "acme_corp",
            "acme corp",
            "acme.corp",
            "acme/corp",
            "acmé",
            "acme!",
            "acme\ncorp",
        ],
    )
    def test_a_malformed_slug_is_refused(self, candidate: str) -> None:
        """Lower case, dash-separated, non-empty segments.

        The shape is a URL path segment. ``_`` and ``.`` are legal there and
        excluded here so a slug can never collide with a future
        ``/users/{id}``-style route or be mistaken for a file extension.
        """
        with pytest.raises(ValidationError):
            _Slug(value=candidate)

    def test_it_is_folded_to_lower_case(self) -> None:
        assert _Slug(value="Acme").value == "acme"

    def test_every_spelling_of_one_slug_folds_to_the_same_value(self) -> None:
        """Case and padding must not create two tenants out of one name.

        This is the write-side half of the property; the login-side half is
        asserted in ``tests/test_auth_api.py``.
        """
        folded = {_Slug(value=spelling).value for spelling in SPELLINGS_OF_ONE_SLUG}

        assert len(folded) == 1

    def test_it_is_still_empty_after_stripping(self) -> None:
        """Trimming happens *before* the length check.

        So a field sent as ``"   "`` is refused as too short rather than accepted
        on its padding.
        """
        with pytest.raises(ValidationError):
            _Slug(value="   ")

    def test_a_slug_longer_than_the_column_is_refused(self) -> None:
        """Refused, not truncated.

        Truncation would map two distinct slugs onto one row, and the loser would
        receive a ``409`` for a name they never typed.
        """
        with pytest.raises(ValidationError):
            _Slug(value="x" * (TENANT_SLUG_MAX_LENGTH + 1))

    def test_the_pattern_is_anchored_at_both_ends(self) -> None:
        """Asserted against the literal pattern, not through a model.

        ``re.search`` with an unanchored pattern would accept ``"!!acme!!"``, and
        this is where that would be noticed rather than at the first endpoint that
        depends on it.
        """
        assert re.search(TENANT_SLUG_PATTERN, "acme")
        assert re.search(TENANT_SLUG_PATTERN, "!!acme!!") is None
        assert re.search(TENANT_SLUG_PATTERN, "acme!!") is None


# ---------------------------------------------------------------------------
# Email address
# ---------------------------------------------------------------------------


class TestEmailAddress:
    @pytest.mark.parametrize(
        "address",
        [
            "a@b.co",
            EXAMPLE_EMAIL,
            "alice.smith@acme.test",
            "alice+tag@acme.test",
            "alice@sub.domain.acme.test",
            "a1@b2.io",
            "x!#$%&'*+/=?^_`{|}~@acme.test",
            "first-last@acme.test",
        ],
    )
    def test_a_permitted_address_is_accepted(self, address: str) -> None:
        assert _Email(value=address).value == address.lower()

    @pytest.mark.parametrize(
        "candidate",
        [
            "",
            "alice",
            "alice@",
            "@acme.test",
            "alice@acme",
            "alice@acme.t",
            "alice@.test",
            "alice@acme.",
            "alice@@acme.test",
            "alice acme@acme.test",
            "alice@acme test",
            "alice@acme,test",
            "ali<ce>@acme.test",
            '"odd name"@acme.test',
            "alice@[192.0.2.1]",
            f"{'a' * 65}@acme.test",
        ],
    )
    def test_a_malformed_address_is_refused(self, candidate: str) -> None:
        """A deliberately conservative shape, not RFC 5322.

        Every exclusion here is *legal* in RFC 5322 and unusable as a login
        identity: quoted local parts, IP-literal domains, a single-character TLD,
        a comment character. See :mod:`app.schemas.fields` for why the reference
        implementation is not installed instead.
        """
        with pytest.raises(ValidationError):
            _Email(value=candidate)

    def test_every_spelling_of_one_address_folds_to_the_same_value(self) -> None:
        """Email is case-insensitive in practice, and must be one row per address.

        ``UNIQUE (tenant_id, email)`` is only case-insensitive if the value is
        folded *before* it is written — and the lookup at login must fold
        identically, or the account would exist and be unreachable behind a
        "wrong password" error.
        """
        folded = {
            _Email(value=spelling).value for spelling in SPELLINGS_OF_ONE_ADDRESS
        }

        assert folded == {EXAMPLE_EMAIL}

    def test_a_pasted_address_is_accepted(self) -> None:
        """A trailing newline is the common case, not an edge case."""

        assert _Email(value=f"  {EXAMPLE_EMAIL}\n").value == EXAMPLE_EMAIL

    def test_the_shortest_permitted_address_is_accepted(self) -> None:
        """One-character local part, one-character first label, two-letter TLD.

        ``min_length = 3`` alone would admit ``a@b``, which is syntactically
        shaped but is not an address anybody can receive mail at.
        """
        assert _Email(value="a@b.co").value == "a@b.co"

    def test_an_address_below_the_minimum_length_is_refused(self) -> None:
        for candidate in ("a@", "a@b", "ab@"):
            with pytest.raises(ValidationError):
                _Email(value=candidate)

    def test_an_address_longer_than_the_column_is_refused(self) -> None:
        """The widest accepted address is the widest the column stores.

        ``max_length`` is imported from the model rather than restated, so the two
        cannot drift. This is the guard on that import.
        """
        with pytest.raises(ValidationError):
            _Email(value=f"{'a' * USER_EMAIL_MAX_LENGTH}@acme.test")

    def test_the_pattern_is_anchored_at_both_ends(self) -> None:
        assert re.search(EMAIL_PATTERN, EXAMPLE_EMAIL)
        assert re.search(EMAIL_PATTERN, f"prefix {EXAMPLE_EMAIL}") is None
        assert re.search(EMAIL_PATTERN, f"{EXAMPLE_EMAIL} suffix") is None

    def test_the_top_level_domain_must_be_alphabetic(self) -> None:
        """``alice@acme.t3st`` is refused.

        Stated on its own because it is the rule that would look arbitrary in a
        review: a numeric TLD is not served by the root zone, so accepting it
        would admit addresses that cannot receive mail.
        """
        with pytest.raises(ValidationError):
            _Email(value="alice@acme.t3st")

    def test_the_local_part_is_capped_at_its_real_length(self) -> None:
        """64 characters, per the address specification.

        At 65 the whole address is refused even though the *column* would have
        room, which is the difference between validating an address and merely
        bounding a string.
        """
        assert _Email(value=f"{'a' * 64}@acme.test").value

        with pytest.raises(ValidationError):
            _Email(value=f"{'a' * 65}@acme.test")


# ---------------------------------------------------------------------------
# Passwords: creation
# ---------------------------------------------------------------------------


class TestPasswordCreation:
    def test_the_floor_is_twelve_characters(self) -> None:
        """NIST SP 800-63B's recommendation.

        A floor is the one rule that cannot be argued with: without it, ``a`` is a
        valid password.
        """
        assert NEW_PASSWORD_MIN_LENGTH == 12

    def test_a_password_at_the_floor_is_accepted(self) -> None:
        chosen = "x" * NEW_PASSWORD_MIN_LENGTH

        assert _NewPassword(value=chosen).value == chosen

    def test_a_password_below_the_floor_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _NewPassword(value="x" * (NEW_PASSWORD_MIN_LENGTH - 1))

    @pytest.mark.parametrize(
        "password",
        [
            "x" * NEW_PASSWORD_MIN_LENGTH,
            "correct horse battery staple",
            "aaaaaaaaaaaaaaaa",
            "123456789012",
            "!@#$%^&*()_+{}",
            "  spaces are characters  ",
            "éèê unicode is allowed",
            "a\tb\nc\rd and then some",
        ],
    )
    def test_no_composition_rule_is_applied(self, password: str) -> None:
        """Only length. No character classes, no mixed case, no digits.

        NIST advises *against* composition rules: they produce predictable
        patterns (``Password1!``) rather than strong ones. Each shape is listed
        explicitly because a test that only checked the floor would still pass
        against an implementation that added a class requirement later.
        """
        assert _NewPassword(value=password).value == password

    def test_the_ceiling_matches_the_documented_one(self) -> None:
        assert PASSWORD_MAX_LENGTH == 128

    def test_a_password_at_the_ceiling_is_accepted(self) -> None:
        chosen = "x" * PASSWORD_MAX_LENGTH

        assert _NewPassword(value=chosen).value == chosen

    def test_a_password_beyond_the_ceiling_is_refused(self) -> None:
        """The ceiling bounds the work Argon2 can be asked to do.

        Argon2 is deliberately expensive and this field is reachable before
        authentication. An unbounded password field would let an anonymous caller
        submit megabytes per request, each of which the server would then hash.
        """
        with pytest.raises(ValidationError):
            _NewPassword(value="x" * (PASSWORD_MAX_LENGTH + 1))

    def test_no_password_is_normalised(self) -> None:
        """Passwords are compared byte for byte and must not be folded.

        Trimming or lower-casing would silently change a chosen secret, so
        ``"a b "`` and ``"a b"`` remain different credentials.
        """
        chosen = f"  {EXAMPLE_EMAIL}  "

        assert _NewPassword(value=chosen).value == chosen


class TestPasswordVerification:
    def test_a_password_below_the_creation_floor_is_still_presented(
        self,
    ) -> None:
        """Login must not apply a *creation* policy to a *verification* path.

        A stored password that no longer satisfies today's policy is still a
        stored password. The honest answer to presenting it is "credentials
        invalid", which the service gives as ``401``. Answering ``422`` here would
        claim the request was malformed — a different, and misleading, statement
        about a credential that is probably correct.
        """
        assert _PresentedPassword(value="short").value == "short"

    def test_an_empty_password_is_refused(self) -> None:
        """Below one character there is nothing to compare against."""

        with pytest.raises(ValidationError):
            _PresentedPassword(value="")

    def test_the_ceiling_is_still_enforced(self) -> None:
        """The cost bound is not a creation policy; it binds every caller.

        This is the asymmetry stated the other way round: the floor is a policy
        about creating credentials, the ceiling is a resource bound, and only the
        first is dropped on the verification path.
        """
        assert _PresentedPassword(value="x" * PASSWORD_MAX_LENGTH).value is not None

        with pytest.raises(ValidationError):
            _PresentedPassword(value="x" * (PASSWORD_MAX_LENGTH + 1))

    def test_the_two_types_differ_only_in_their_floor(self) -> None:
        """One type silently applying the other's policy would be the bug.

        The ceiling is shared, so a change to it moves both; the floor exists on
        exactly one of them. This pair of tests is what makes
        ``NewPassword`` and ``Password`` two types rather than one type with a
        flag.
        """
        short = "x" * (NEW_PASSWORD_MIN_LENGTH - 1)

        with pytest.raises(ValidationError):
            _NewPassword(value=short)

        assert _PresentedPassword(value=short).value == short


# ---------------------------------------------------------------------------
# Tenant name
# ---------------------------------------------------------------------------


class TestTenantName:
    def test_surrounding_whitespace_is_stripped(self) -> None:
        """A display name, so it is stored as typed apart from padding."""

        assert _TenantName(value="  Acme Corporation  ").value == "Acme Corporation"

    def test_case_is_preserved(self) -> None:
        """Unlike a slug, a name is *not* folded.

        Two tenants may legitimately be called ``Acme`` and ``ACME``; their slugs
        are what must be unique, and folding the name would make the two
        indistinguishable in the interface without making them the same tenant.
        """
        assert _TenantName(value="AcMe").value == "AcMe"

    def test_an_empty_name_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _TenantName(value="   ")

    def test_a_name_at_the_minimum_length_is_accepted(self) -> None:
        assert _TenantName(value="a").value == "a"


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


class TestTheTypesCompose:
    def test_every_failure_is_reported_in_one_pass(self) -> None:
        """A client is not sent away once per bad field."""

        class _Signup(BaseModel):
            name: TenantName
            slug: TenantSlug
            email: EmailAddress
            password: NewPassword

        with pytest.raises(ValidationError) as caught:
            _Signup(name="", slug="Nope!", email="nope", password="short")

        assert {error["loc"][0] for error in caught.value.errors()} == {
            "name",
            "slug",
            "email",
            "password",
        }

    @pytest.mark.parametrize(
        ("annotation", "value"),
        [
            (EmailAddress, EXAMPLE_EMAIL),
            (TenantSlug, "acme"),
            (TenantName, "Acme"),
            (NewPassword, "x" * NEW_PASSWORD_MIN_LENGTH),
            (Password, "x"),
        ],
        ids=lambda a: str(a).replace("typing.", ""),
    )
    def test_each_type_validates_standalone(
        self, annotation: Any, value: str
    ) -> None:
        """Every shared type works with a bare :class:`TypeAdapter`.

        An unresolvable forward reference or a missing core schema would raise
        here rather than in whichever endpoint happens to use the type first.
        """
        adapter: TypeAdapter[Any] = TypeAdapter(annotation)

        assert adapter.validate_python(value) is not None

    @pytest.mark.parametrize(
        "annotation",
        [EmailAddress, TenantSlug, TenantName, NewPassword, Password],
        ids=lambda a: str(a).replace("typing.", ""),
    )
    def test_each_type_refuses_a_non_string(self, annotation: Any) -> None:
        """The normalisers pass non-strings through untouched.

        That is deliberate: it leaves Pydantic to report "input should be a valid
        string", which is more useful than a vaguer complaint raised by a
        normaliser that did not recognise its input.
        """
        with pytest.raises(ValidationError) as caught:
            TypeAdapter(annotation).validate_python(12345)

        assert "string" in str(caught.value)

    @pytest.mark.parametrize(
        ("annotation", "value"),
        [
            (EmailAddress, EXAMPLE_EMAIL),
            (TenantSlug, "acme"),
            (TenantName, "Acme"),
            (NewPassword, "x" * NEW_PASSWORD_MIN_LENGTH),
            (Password, "x"),
        ],
        ids=lambda a: str(a).replace("typing.", ""),
    )
    def test_each_type_normalises_to_a_plain_string(
        self, annotation: Any, value: str
    ) -> None:
        """The result is a ``str``, never an enum or a wrapper.

        A validated model field that came back as anything other than ``str``
        would store a value the database column has to convert, and that
        conversion is where case handling tends to get lost.
        """
        assert type(TypeAdapter(annotation).validate_python(value)) is str
