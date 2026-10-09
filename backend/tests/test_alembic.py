"""Tests for the Alembic migration foundation.

The live command (``alembic upgrade head``) needs a reachable PostgreSQL
server, which the ordinary unit suite deliberately does not require. These
tests therefore verify the *configuration* — that it loads, resolves its DSN
from application settings, exposes the right metadata, and embeds no
credential — and they exercise Alembic's own machinery against an in-process
SQLite database, which needs no server.

Coverage gap, stated plainly: no test here runs a real migration against
PostgreSQL. That becomes possible in Phase 2, when there is a first migration
to run, and must then be an explicitly isolated integration test that skips
only when PostgreSQL is genuinely unreachable.
"""

from __future__ import annotations

import configparser
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Final
from unittest import mock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect

from alembic import command
from app.core import config as config_module
from app.db.base import Base
from app.models import MODEL_TABLES
from tests.conftest import (
    ALEMBIC_ENV,
    ALEMBIC_INI,
    ALEMBIC_MAKO,
    ALEMBIC_VERSIONS,
    BACKEND_ROOT,
    make_settings,
)

#: A DSN containing '%', which is ConfigParser's interpolation character. If
#: env.py did not escape it, the resolved URL would be corrupted.
PERCENT_DSN = "postgresql+psycopg://u:p%40ss@localhost:5432/tenantmind"
# Kept as a name rather than inlined, since the test above reads better with
# the value referenced by symbol.

#: A DSN that resolves without a server, used to satisfy env.py during the
#: import of the module under test.
LOAD_ENV_DSN = "sqlite+pysqlite://"


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def test_alembic_layout_exists() -> None:
    assert ALEMBIC_INI.is_file()
    assert ALEMBIC_ENV.is_file()
    assert ALEMBIC_MAKO.is_file()
    assert ALEMBIC_VERSIONS.is_dir()


def test_script_location_resolves_to_the_alembic_directory() -> None:
    """A relative path would break running Alembic from another directory."""
    config = Config(str(ALEMBIC_INI))
    script_location = config.get_main_option("script_location")

    assert script_location is not None
    assert Path(script_location) == ALEMBIC_INI.parent / "alembic"
    assert Path(script_location).is_dir()


def test_config_loads_without_error() -> None:
    config = Config(str(ALEMBIC_INI))

    assert config.get_main_option("script_location") is not None
    assert config.get_main_option("file_template") is not None


def test_file_template_is_collision_resistant() -> None:
    """Timestamped names keep concurrent branches from colliding."""
    template = Config(str(ALEMBIC_INI)).get_main_option("file_template")

    assert template is not None
    assert "%(rev)s" in template
    assert "%(year)d" in template


# ---------------------------------------------------------------------------
# No secrets are committed
# ---------------------------------------------------------------------------


def test_alembic_ini_ships_with_an_empty_url() -> None:
    text_content = ALEMBIC_INI.read_text(encoding="utf-8")

    assert re.search(
        r"^sqlalchemy\.url\s*=\s*$", text_content, re.MULTILINE
    ), "sqlalchemy.url must be present and empty"


def test_ini_url_is_empty_when_parsed() -> None:
    parser = configparser.ConfigParser()
    parser.read(ALEMBIC_INI, encoding="utf-8")

    assert parser.get("alembic", "sqlalchemy.url", fallback="").strip() == ""


def test_no_migration_file_contains_something_shaped_like_a_dsn() -> None:
    pattern = re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s\"']*@")

    for path in (ALEMBIC_INI, ALEMBIC_ENV, ALEMBIC_MAKO):
        assert not pattern.search(path.read_text(encoding="utf-8")), (
            f"{path} contains something shaped like a DSN"
        )


def test_env_py_resolves_the_dsn_from_application_settings() -> None:
    """The DSN must come from Settings, never from the ini file."""
    source = ALEMBIC_ENV.read_text(encoding="utf-8")

    assert "from app.core.config import settings" in source
    assert "resolve_dsn" in source
    assert not re.search(r"postgres(?:ql)?(?:\+\w+)?://[^\s\"']*@", source)


def test_ini_documents_why_the_url_is_empty() -> None:
    """A future maintainer must not 'fix' the empty url by pasting a DSN."""
    text_content = ALEMBIC_INI.read_text(encoding="utf-8").lower()

    assert "no credentials" in text_content or "empty" in text_content
    assert "do not put a dsn here" in text_content


# ---------------------------------------------------------------------------
# env.py: metadata hook and URL resolution
# ---------------------------------------------------------------------------


def test_env_exposes_the_application_metadata() -> None:
    """Alembic must autogenerate against the application's own registry.

    Pointing it at a separate ``MetaData`` would make it silently miss every
    table, so identity is asserted rather than mere equivalence.
    """
    env = load_env_module()

    assert env.target_metadata is Base.metadata


def test_env_writes_the_settings_dsn_into_the_config() -> None:
    """The DSN must reach Alembic, and the committed empty url must not win."""
    env = load_env_module()

    env.config.set_main_option("sqlalchemy.url", "stale-value-from-ini")
    env._configure_url()

    stored = env.config.get_main_option("sqlalchemy.url")
    assert stored is not None
    assert stored == LOAD_ENV_DSN


def test_env_upgrades_a_bare_postgresql_dsn(monkeypatch) -> None:
    """env.py goes through resolve_dsn, so the psycopg2 default is corrected."""
    env = load_env_module("postgresql://u:p@localhost:5432/tenantmind")

    env._configure_url()

    stored = env.config.get_main_option("sqlalchemy.url")
    assert stored is not None
    assert stored.startswith("postgresql+psycopg://")


def test_env_escapes_percent_signs_in_the_dsn() -> None:
    """A password containing '%' must survive ConfigParser interpolation.

    ``ConfigParser`` treats '%' as an interpolation character, so an unescaped
    percent in a password would corrupt the DSN.
    """
    env = load_env_module(PERCENT_DSN)

    env._configure_url()

    stored = env.config.get_main_option("sqlalchemy.url")
    assert stored is not None
    # Read back through ConfigParser, which is where an unescaped '%' breaks.
    assert "p%40ss" in stored


def test_env_raises_a_clear_error_when_unconfigured(monkeypatch) -> None:
    """A missing DSN must read as configuration, not as a connection fault.

    The module is loaded with a working DSN — the import itself calls
    ``_configure_url`` — and the settings are then swapped for an unconfigured
    object, which is the state a deployment with no ``DATABASE_URL`` is in.
    """
    env = load_env_module()
    monkeypatch.setattr(env, "settings", make_settings(database_url=None))

    from app.db.engine import DatabaseNotConfiguredError

    with pytest.raises(DatabaseNotConfiguredError) as excinfo:
        env._configure_url()

    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "Alembic" in message


def test_env_patches_onto_the_sys_path() -> None:
    """The app package is not installed, so env.py must add its own root."""
    source = ALEMBIC_ENV.read_text(encoding="utf-8")

    assert "sys.path" in source
    assert "BACKEND_ROOT" in source


def test_alembic_cli_reports_a_missing_dsn_clearly() -> None:
    """Running the real CLI with no DSN must not produce an ImportError."""
    result = run_alembic(["current"], database_url="")

    assert result.returncode != 0
    assert "DATABASE_URL" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


# ---------------------------------------------------------------------------
# Revision graph shape
# ---------------------------------------------------------------------------

#: The revisions the project owns, as a concrete file set rather than a glob.
#: Named rather than discovered so that adding a later migration fails these
#: assertions loudly instead of silently changing what they describe.
#:
#: The set is ordered from base to head below via :data:`REVISION_CHAIN`; here it
#: is only a membership test.
BASE_REVISION = "0001"
#: Revision ``0002`` created ``refresh_tokens``.
SECOND_REVISION = "0002"
#: Revision ``0003`` created ``documents``.
THIRD_REVISION = "0003"
#: Revision ``0004`` added the search vector to ``documents`` (Phase 6).
FOURTH_REVISION = "0004"
#: Revision ``0005`` created ``document_chunks`` (Phase 6).
FIFTH_REVISION = "0005"
#: Revision ``0006`` created ``query_logs`` (Phase 7).
SIXTH_REVISION = "0006"
#: Revision ``0007`` created ``audit_logs``; it is the head (Phase 7).
HEAD_REVISION = "0007"
REAL_REVISIONS = {
    ALEMBIC_VERSIONS / f"20261001_1056_{BASE_REVISION}_create_tenants_and_users.py",
    ALEMBIC_VERSIONS
    / f"20261001_1130_{SECOND_REVISION}_create_refresh_tokens.py",
    ALEMBIC_VERSIONS / f"20261002_0604_{THIRD_REVISION}_create_documents.py",
    ALEMBIC_VERSIONS
    / f"20261007_1200_{FOURTH_REVISION}_add_document_search_vector.py",
    ALEMBIC_VERSIONS / f"20261007_1300_{FIFTH_REVISION}_add_document_chunks.py",
    ALEMBIC_VERSIONS / f"20261007_1400_{SIXTH_REVISION}_create_query_logs.py",
    ALEMBIC_VERSIONS / f"20261007_1500_{HEAD_REVISION}_create_audit_logs.py",
}

#: ``(revision, down_revision)`` in application order. This is the chain the
#: migrations must actually declare, as distinct from the set of files present.
REVISION_CHAIN: Final[tuple[tuple[str, str | None], ...]] = (
    (BASE_REVISION, None),
    (SECOND_REVISION, BASE_REVISION),
    (THIRD_REVISION, SECOND_REVISION),
    (FOURTH_REVISION, THIRD_REVISION),
    (FIFTH_REVISION, FOURTH_REVISION),
    (SIXTH_REVISION, FIFTH_REVISION),
    (HEAD_REVISION, SIXTH_REVISION),
)

#: Retained as an alias for the many assertions below that phrase themselves in
#: terms of the first migration. Phase 2 Step 2 made the chain two revisions long,
#: so "first" is now a position rather than the whole story.
FIRST_REVISION = BASE_REVISION


def test_exactly_the_declared_migrations_exist() -> None:
    """The versions folder holds precisely the revisions this project declares.

    Phase 1 asserted the folder was empty, to prove no table had been
    fabricated. Step 1 asserted exactly one revision. The property worth keeping
    is the one that survives both: the folder holds precisely the revisions the
    project declares, so a stray or hand-written migration cannot join the graph
    unnoticed.
    """
    assert set(ALEMBIC_VERSIONS.glob("*.py")) == REAL_REVISIONS

    for revision in REAL_REVISIONS:
        assert revision.is_file()
        assert revision.name.endswith(".py")

    # The filename embeds the revision identifier, and so does the file's own
    # `revision = ...`. Ordering the files by name must therefore reproduce the
    # declared chain order exactly, with each identifier appearing once.
    identifiers = [
        revision.name.split("_")[2] for revision in sorted(REAL_REVISIONS)
    ]
    assert identifiers == [revision for revision, _ in REVISION_CHAIN]
    assert len(set(identifiers)) == len(identifiers), (
        "a revision identifier appears in more than one filename"
    )


def test_script_directory_reports_a_single_rooted_head() -> None:
    """One linear chain: a single head, rooted at base, with no branch to merge.

    Two heads would mean two migrations claim the same parent and a deployment
    would need an explicit merge step.
    """
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))

    assert script.get_heads() == [HEAD_REVISION]
    assert script.get_current_head() == HEAD_REVISION
    assert script.get_base() == BASE_REVISION


def test_script_template_emits_typed_revisions() -> None:
    """The template must satisfy the mypy gate that runs over the repo."""
    template = ALEMBIC_MAKO.read_text(encoding="utf-8")

    assert "def upgrade() -> None:" in template
    assert "def downgrade() -> None:" in template
    assert "from __future__ import annotations" in template
    assert "${message}" in template


# ---------------------------------------------------------------------------
# Alembic machinery, offline and online
# ---------------------------------------------------------------------------


def test_offline_mode_needs_no_database(tmp_path: Path) -> None:
    """``--sql`` generates SQL with no DBAPI connection at all."""
    result = run_alembic(
        ["upgrade", "head", "--sql"],
        database_url=f"sqlite+pysqlite:///{tmp_path / 'offline.db'}",
    )

    assert result.returncode == 0, result.stderr
    assert "Generating static SQL" in result.stderr


def test_offline_mode_targets_postgresql_for_a_postgres_dsn() -> None:
    """The generated SQL must be PostgreSQL, not SQLite."""
    result = run_alembic(
        ["upgrade", "head", "--sql"],
        database_url="postgresql+psycopg://u:p@localhost:5432/tenantmind",
    )

    assert result.returncode == 0, result.stderr
    assert "postgresql" in result.stderr.lower()


#: Alembic creates and maintains this table itself. It is bookkeeping, not an
#: application table, so it is excluded when asserting that Step 4 introduced
#: no schema.
ALEMBIC_BOOKKEEPING_TABLE = "alembic_version"


def application_tables(engine: Engine) -> list[str]:
    """Return the tables Alembic did not create for its own bookkeeping."""
    return [
        name
        for name in inspect(engine).get_table_names()
        if name != ALEMBIC_BOOKKEEPING_TABLE
    ]


def test_online_upgrade_runs_against_a_live_database(
    sqlite_engine: Engine, configured_database
) -> None:
    """A full online migration cycle, with no PostgreSQL server involved.

    ``upgrade head`` applies the real chain and creates exactly the tables
    :data:`app.models.MODEL_TABLES` declares. Alembic's own ``alembic_version``
    table is expected and is excluded from the assertion.

    Asserted against ``MODEL_TABLES`` rather than a literal list so that this and
    ``tests/test_models.py`` state one fact between them: the migrated schema and
    the declared schema are the same closed set.

    This exercises genuine DDL through the real migration. It is still SQLite, so
    it verifies the migration is executable and complete, not that PostgreSQL
    accepts it — see ``tests/test_postgres_integration.py`` for that gap, which
    remains open.
    """
    configured_database(str(sqlite_engine.url))

    command.upgrade(Config(str(ALEMBIC_INI)), "head")

    assert sorted(application_tables(sqlite_engine)) == sorted(MODEL_TABLES)


def test_online_current_runs_against_a_live_database(
    sqlite_engine: Engine, configured_database
) -> None:
    configured_database(str(sqlite_engine.url))

    command.current(Config(str(ALEMBIC_INI)))


def test_autogenerate_finds_nothing_to_do(
    sqlite_engine: Engine, configured_database
) -> None:
    """Models and the migrated schema must already agree.

    This is the drift check. Phase 1 ran it against empty metadata and an empty
    database, which proved there was no spurious output. Phase 2 Step 1 upgrades
    the database with the real migration first, so any operation autogenerate
    emits now describes genuine drift between a model and the schema it claims
    to own — a column, index or constraint that exists in one and not the other.

    The generated file is inspected rather than a Script object, because
    Alembic's in-memory ``upgrade_ops`` attribute is not part of its public
    API and is not available on ``Script`` in 1.20. The file is the artifact a
    developer actually reviews, so it is also the more meaningful thing to
    assert on.

    Only the real migration is deleted, never every file in ``versions/``: the
    blanket ``unlink`` this replaced would have deleted revision 0001 as well,
    destroying the migration on a failed assertion.
    """
    configured_database(str(sqlite_engine.url))
    config = Config(str(ALEMBIC_INI))

    real_revisions = set(ALEMBIC_VERSIONS.glob("*.py"))
    assert real_revisions, "expected the Phase 2 migrations to exist"
    assert set(real_revisions) == REAL_REVISIONS, (
        f"expected exactly {sorted(REAL_REVISIONS)}, found {sorted(real_revisions)}"
    )

    command.upgrade(config, "head")
    assert sorted(application_tables(sqlite_engine)) == sorted(MODEL_TABLES)

    generated: set[Path] = set()
    try:
        command.revision(config, autogenerate=True, message="probe")

        generated = set(ALEMBIC_VERSIONS.glob("*.py")) - real_revisions
        assert len(generated) == 1, (
            f"expected exactly one generated revision, found {generated}"
        )
        source = next(iter(generated)).read_text(encoding="utf-8")

        assert "op." not in source, (
            "autogenerate detected drift between the models and the migrated "
            f"schema:\n{source}"
        )
        for operation in (
            "op.create_table",
            "op.drop_table",
            "op.add_column",
            "op.drop_column",
            "op.create_index",
            "op.drop_index",
            "op.create_unique_constraint",
            "op.create_foreign_key",
        ):
            assert operation not in source, (
                f"autogenerate proposed {operation} against a schema that already "
                "matches the models"
            )
    finally:
        for path in generated:
            path.unlink()

    assert set(ALEMBIC_VERSIONS.glob("*.py")) == real_revisions


def test_autogenerate_would_catch_a_real_model_change(
    sqlite_engine: Engine, configured_database, tmp_path: Path
) -> None:
    """Proves the drift check above is not passing vacuously.

    A copy of the model package with one column's width changed is imported in a
    subprocess, and autogenerate is pointed at the unmodified database. The
    expectation is that a difference *is* reported.

    Without this, "autogenerate found nothing" and "autogenerate is broken" would
    be indistinguishable from the outside, and the first test would be worthless.

    The edit is applied to a **copy** under ``tmp_path``, never to the repository,
    so no test can leave a modified model behind.
    """
    # The database must be at head first: autogenerate refuses to run against a
    # database that is behind its script directory, which is a different failure
    # from the one being tested.
    configured_database(str(sqlite_engine.url))
    command.upgrade(Config(str(ALEMBIC_INI)), "head")

    package_copy = tmp_path / "tenantmind_drift_app"
    shutil.copytree(BACKEND_ROOT / "app", package_copy / "app")
    shutil.copytree(BACKEND_ROOT / "alembic", package_copy / "alembic")
    shutil.copy2(ALEMBIC_INI, package_copy / "alembic.ini")

    user_module = package_copy / "app" / "models" / "user.py"
    original = user_module.read_text(encoding="utf-8")
    assert "sa.String(USER_PASSWORD_HASH_MAX_LENGTH)" in original
    user_module.write_text(
        original.replace(
            "sa.String(USER_PASSWORD_HASH_MAX_LENGTH)", "sa.String(200)"
        ),
        encoding="utf-8",
    )

    script = (
        "import sys, os;"
        f"sys.path.insert(0, {str(package_copy)!r});"
        "import app.models;"
        "from alembic.config import Config;"
        "from alembic import command;"
        f"cfg = Config({str(package_copy / 'alembic.ini')!r});"
        "cfg.set_main_option('script_location',"
        f" {str(package_copy / 'alembic')!r});"
        "command.revision(cfg, autogenerate=True, message='probe',"
        " rev_id='driftprobe');"
        "print('done')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=package_copy,
        check=False,
        env={**os.environ, "DATABASE_URL": str(sqlite_engine.url).replace("%", "%%")},
    )

    generated = sorted((package_copy / "alembic" / "versions").glob("*driftprobe*.py"))
    try:
        assert result.returncode == 0, result.stderr
        assert "done" in result.stdout
        assert len(generated) == 1, (
            f"expected the modified model to produce one revision, found {generated}"
        )

        source = generated[0].read_text(encoding="utf-8")
        assert "op." in source, (
            "autogenerate emitted no operations despite a deliberate column-width "
            f"change, so the drift check cannot detect drift:\n{source}"
        )
        assert "password_hash" in source
    finally:
        for path in generated:
            path.unlink()

    # The real model and the real migrations directory must be untouched.
    real_user = BACKEND_ROOT / "app" / "models" / "user.py"
    assert "sa.String(USER_PASSWORD_HASH_MAX_LENGTH)" in real_user.read_text(
        encoding="utf-8"
    )
    assert set(ALEMBIC_VERSIONS.glob("*.py")) == REAL_REVISIONS


# ---------------------------------------------------------------------------
# Importing the application must not touch Alembic or a database
# ---------------------------------------------------------------------------


def test_importing_the_application_runs_no_migrations() -> None:
    """Importing ``app.main`` must not reach Alembic, and must open no connection.

    The Alembic half of this is unchanged since Phase 1 and is the load-bearing
    one: ``alembic/env.py`` and the migration modules must never be imported by
    application code, because importing ``env.py`` resolves the database URL and
    comparing metadata against a live schema, which turns a failed import into a
    failed database connection and a started server into a migration run.

    The database half was originally written as ``'app.db' not in sys.modules``,
    which asserted the same concern by proxy: a Phase 1 application had no routes
    that needed a session, so it could be imported with no database layer at all.
    Phase 2.3 adds database-backed routes, so ``app.main`` must now import
    ``app.db.session`` to wire them, and the proxy is no longer the property that
    matters.

    What actually matters is stronger and is asserted instead: importing the
    application must not *open* a connection. The engine is created lazily by
    :func:`app.db.engine.get_engine` on first use, so
    :func:`app.db.engine.is_engine_configured` is still ``False`` after a full
    import. A server can therefore boot with the database down and report
    unhealthy, rather than refusing to start.
    """
    result = run_python(
        "import sys;"
        "from app.main import app;"
        "assert 'alembic' not in sys.modules, 'alembic was imported';"
        "from app.db.engine import is_engine_configured;"
        "assert not is_engine_configured(), 'an engine was built at import time';"
        "print('clean')"
    )

    assert result.stdout.strip() == "clean"


def test_importing_app_db_does_not_import_alembic() -> None:
    """Alembic is a deployment tool; the application must not depend on it."""
    result = run_python("import app.db, sys; print('alembic' in sys.modules)")

    assert result.stdout.strip() == "False"


def test_alembic_env_module_is_never_imported_by_the_application() -> None:
    """``alembic/env.py`` runs migrations at import, so nothing may import it.

    Checked by path rather than by module name: a substring match on "env"
    would also hit unrelated modules such as ``dotenv`` and ``pydantic.v1``.
    """
    result = run_python(
        "import sys\n"
        "import app.db, app.main\n"
        "loaded = [\n"
        "    name\n"
        "    for name, module in sys.modules.items()\n"
        "    if str(getattr(module, '__file__', '')).endswith('alembic/env.py')\n"
        "]\n"
        "print(loaded)\n"
    )

    assert result.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_env_module(dsn: str = LOAD_ENV_DSN) -> types.ModuleType:
    """Import ``alembic/env.py`` as a module without running a real migration.

    Two things must be neutralised for the import to succeed on a machine with
    no database:

    * ``alembic.context`` is an Alembic proxy that only exists during a CLI
      run. It is replaced with a stub, so the module-level dispatch takes the
      offline branch and the resulting calls are recorded rather than
      executed.
    * ``app.core.config.settings`` is a module-level singleton, so it is
      pointed at a DSN that resolves without a server. The stub records the
      attempted configuration, which is what makes the import side-effect-free.
    """
    stub_context = types.SimpleNamespace(
        config=Config(str(ALEMBIC_INI)),
        is_offline_mode=mock.MagicMock(return_value=True),
        configure=mock.MagicMock(),
        begin_transaction=mock.MagicMock(),
        run_migrations=mock.MagicMock(),
    )

    spec = importlib.util.spec_from_file_location("tenantmind_alembic_env", ALEMBIC_ENV)
    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    with (
        mock.patch("alembic.context", stub_context),
        mock.patch.object(
            config_module, "settings", make_settings(database_url=dsn)
        ),
    ):
        spec.loader.exec_module(module)

    module.stub_context = stub_context  # type: ignore[attr-defined]
    return module


def run_alembic(
    args: list[str], *, database_url: str
) -> subprocess.CompletedProcess[str]:
    """Invoke the Alembic CLI from ``backend/`` with an explicit DSN."""
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    environment.pop("PYTHONPATH", None)

    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        env=environment,
        check=False,
    )


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run a snippet in a fresh interpreter rooted at ``backend/``."""
    environment = os.environ.copy()
    # Guarantee a clean, unconfigured environment for import-time assertions.
    environment.pop("DATABASE_URL", None)
    environment.pop("PYTHONPATH", None)

    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        env=environment,
        check=True,
    )
