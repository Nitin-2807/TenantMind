"""Declarative base and shared metadata for all SQLAlchemy models.

This module exists so that Alembic autogenerate and the application agree on a
single :class:`~sqlalchemy.MetaData` object. Every future model in
``app/models/`` must inherit from :class:`Base` rather than creating its own
declarative base; a second base would produce a second metadata registry, and
Alembic would then silently miss those tables.

Scope note: Step 4 established the foundation alone. Phase 2 Step 1 has since added
the first two models, ``tenants`` and ``users`` (PMP §14), and revision ``0001``
owns their schema. This module still defines no tables itself: models live in
``app/models`` and must be imported explicitly for them to register here.
Document, query-log and refresh-token models belong to later steps.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: Explicit, deterministic constraint naming.
#:
#: Without this, SQLAlchemy auto-names unnamed constraints using a hash of the
#: table and column set, which makes Alembic autogenerate emit spurious
#: drop-and-recreate churn whenever an unrelated column changes. Naming
#: conventions give migrations stable, reviewable constraint names.
#:
#: The trailing underscores are the conventional placeholders Alembic
#: documents for the naming convention, and are relied on by the migrations
#: generated in Phase 2.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every TenantMind ORM model.

    Inheriting from this class is mandatory for all models. It binds the model
    registry to :data:`metadata`, which is the object Alembic compares the live
    database against.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
