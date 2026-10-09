"""Reusable annotated field types shared by more than one request schema.

Why this module exists
----------------------
Three of this project's request fields — the tenant slug, the email address and
the password — appear on more than one endpoint. When a field appears twice, the
temptation is to spell its validation out twice, and that is exactly how
``POST /users/invite`` ends up accepting an email shape that ``POST /auth/login``
then fails to match. Every such field is therefore declared once, here, and
imported by the schemas that need it.

The normalisation decision is the load-bearing part
---------------------------------------------------
:data:`EmailAddress` and :data:`TenantSlug` both **normalise** their input
(strip surrounding whitespace, lower-case it) before validating it, and the
models store exactly what the schema produced.

That has to be true in *both* directions, or the system breaks in a way that is
very hard to see:

* the value written at registration/invitation time, and
* the value looked up at login time

must be produced by the same rule. If registration stored ``Alice@Acme.com``
while login folded the lookup key to ``alice@acme.com``, the account would exist
and be unreachable, and the only symptom would be a "wrong password" error on a
correct password. Folding both sides to one canonical form is what makes email
case-insensitive *and* keeps the two endpoints in agreement. The corollary is
that a user cannot register both ``Alice@x.com`` and ``alice@x.com`` as two
accounts in one tenant — the composite unique constraint refuses the second, and
that is the intended outcome, not a bug.

Deliberately not a full RFC 5322 implementation
-----------------------------------------------
The pattern below is a *conservative* address shape, not a complete grammar. It
rejects quoted local parts (``"odd name"@example.com``), IP-literal domains
(``user@[192.0.2.1]``) and comments — all legal in RFC 5322 and all unusable in
practice as a login identity.

That is a choice, and the alternative was rejected deliberately:

* Hand-rolling a permissive pattern is what produces a boundary that accepts
  addresses no mail server would deliver to.
* The other honest option is the ``email-validator`` package, which is the
  reference implementation. It is **not** installed, for two reasons. First, its
  normal Pydantic integration (``EmailStr``) performs a live **DNS MX lookup**,
  which would put a network call and a measurable latency spike on an
  unauthenticated endpoint, and would make a DNS resolver an oracle for whether
  an address was submitted. Second, it brings ``dnspython`` — a DNS library —
  into a codebase that has no other reason to speak DNS. The syntax-only mode
  avoids the lookup but still requires the dependency, which for a single
  regular expression is not a trade worth making.

If internationalised or otherwise exotic addresses are ever required, the
migration is to add ``email-validator`` and use it in
``check_deliverability=False`` mode. That is recorded as a deferred item rather
than pre-installed.

Password policy
---------------
The PMP states one thing about passwords (PMP §8, "Passwords must never be
stored in plaintext") and defines no composition rules. This module adds only a
length floor and a ceiling, and nothing else:

* :data:`NewPassword` enforces the floor. Twelve characters is the NIST SP
  800-63B recommendation, and a floor is the one rule that cannot be argued
  with: without it, ``a`` is a valid password.
* No character-class requirements. NIST explicitly advises against them, because
  they produce predictable patterns (``Password1!``) rather than strong ones.
* The ceiling exists for a mechanical reason: it bounds the input handed to
  Argon2, which is deliberately expensive. An unbounded password field would let
  an unauthenticated caller submit megabytes per request.

:data:`Password` is the *verification* counterpart and deliberately enforces only
the ceiling. Login must not reject a request for failing a policy that governs
creating credentials — a stored password that no longer satisfies today's policy
is still a stored password, and the honest answer to presenting it is
"credentials invalid", not "malformed request". Two types with two clearly
different names is preferable to one type silently applying a creation policy to
a verification path.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator, StringConstraints

from app.models.tenant import TENANT_NAME_MAX_LENGTH, TENANT_SLUG_MAX_LENGTH
from app.models.user import USER_EMAIL_MAX_LENGTH

#: Minimum length of a newly created password. See the module docstring.
NEW_PASSWORD_MIN_LENGTH = 12

#: Maximum length of any password, new or presented.
#:
#: Argon2 is fed the whole string, so this bounds the per-request work an
#: unauthenticated caller can cause. 128 characters is far beyond any passphrase
#: a person would choose, and the column that stores the resulting hash is only
#: 255 characters wide regardless.
PASSWORD_MAX_LENGTH = 128

#: A deliberately conservative email address shape: a local part of up to 64
#: non-space characters, an ``@``, one or more domain labels, and a final
#: alphabetic top-level domain of at least two characters.
#:
#: See the module docstring for what this deliberately does not accept.
EMAIL_PATTERN = (
    r"^[^\s@,;:<>()\[\]\\\"]{1,64}"
    r"@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,63}$"
)

#: A lower-case, dash-separated URL slug: ``acme``, ``acme-corp``, ``a1-b2``.
#:
#: The model documents that normalisation and validation belong at the
#: application boundary, so this is that boundary. No reserved-word list is
#: applied because the PMP defines none, and inventing one would reject slugs the
#: product never said were unavailable.
TENANT_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


def _strip(value: Any) -> Any:
    """Trim surrounding whitespace from a string, passing anything else through.

    Non-string input is returned untouched so that Pydantic reports its own
    "input should be a valid string" error rather than this function reporting
    something vaguer. Trimming happens *before* length validation, so a field
    sent as ``"   "`` is rejected as too short instead of passing on its padding.
    """
    if isinstance(value, str):
        return value.strip()
    return value


def _fold_case(value: Any) -> Any:
    """Trim and lower-case a string, passing anything else through.

    Case folding makes email addresses and tenant slugs case-insensitive, which
    matters for two different reasons: a human who types ``Acme`` in the slug
    box at login should not be locked out of a tenant registered as ``acme``,
    and ``Alice@x.com`` / ``alice@x.com`` must not become two accounts in one
    tenant. See the module docstring for why both the write and the lookup path
    must use this same function.
    """
    if isinstance(value, str):
        return value.strip().lower()
    return value


#: A tenant's display name, as sent by a client registering a tenant.
TenantName = Annotated[
    str,
    BeforeValidator(_strip),
    StringConstraints(min_length=1, max_length=TENANT_NAME_MAX_LENGTH),
]

#: A tenant's URL slug. Folded to lower case; see :data:`_fold_case`.
TenantSlug = Annotated[
    str,
    BeforeValidator(_fold_case),
    StringConstraints(
        min_length=1,
        max_length=TENANT_SLUG_MAX_LENGTH,
        pattern=TENANT_SLUG_PATTERN,
    ),
]

#: An email address. Folded to lower case; see :data:`_fold_case`.
#:
#: ``max_length`` is imported from the model rather than restated, so the widest
#: address the boundary accepts is exactly the widest the column stores. A
#: longer value is refused instead of being silently truncated, which would turn
#: two different addresses into one row.
EmailAddress = Annotated[
    str,
    BeforeValidator(_fold_case),
    StringConstraints(
        min_length=3,
        max_length=USER_EMAIL_MAX_LENGTH,
        pattern=EMAIL_PATTERN,
    ),
]

#: A password being *created*. Enforces the length floor.
NewPassword = Annotated[
    str,
    StringConstraints(min_length=NEW_PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH),
]

#: A password being *verified*. Enforces only the ceiling — see the module
#: docstring for why a verification path must not apply a creation policy.
Password = Annotated[
    str,
    StringConstraints(min_length=1, max_length=PASSWORD_MAX_LENGTH),
]
