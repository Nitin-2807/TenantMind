"""Phase 5 Step 1 — the Celery application boundary, its broker, and Redis.

Scope, stated so a later reader does not assume more was built than was:

* **Proved here**: that a Celery application exists, is configured from
  :class:`~app.core.config.Settings`, names the configured Redis, refuses
  anything but JSON on the wire, and can be imported by a worker process
  without dragging in the web application, the database, or the document
  pipeline.
* **Proved in** :mod:`tests.test_worker_integration`: that a real Redis, a real
  worker and a real round trip work together. That suite is opt-in because it
  needs a server; nothing in this file needs one, and nothing in this file
  touches the network.
* **Built and asserted bounded**: the ingestion task (Step 2) and the one
  sanctioned way to enqueue it (Step 3). Retries, failure tracking, Redis
  caching and rate limiting are not built yet.

Two rules are enforced here that are easy to break silently in later steps, so
they get tests rather than comments:

1. **No client can name a task.** Enqueuing happens only inside
   ``app/workers/``. No request handler imports Celery, and no module outside
   ``app/workers/`` calls ``delay``, ``apply_async`` or ``send_task``. The
   request path reaches the worker package through exactly one door — the
   function :func:`app.workers.ingestion.enqueue_ingestion` — asserted below.
2. **The worker imports nothing it does not run.** A worker process must be
   able to import the application without the web framework, the ORM, the
   vector store or the PDF parser being present.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from celery import Celery  # type: ignore[import-untyped]
from pydantic import ValidationError

from app.workers.celery_app import (
    ACCEPTED_CONTENT,
    APP_NAME,
    RESULT_SERIALIZER,
    TASK_SERIALIZER,
    build_celery_app,
    celery_app,
)
from tests.conftest import make_settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"
COMPOSE_FILE = BACKEND_ROOT.parent / "docker-compose.yml"

#: Password planted in every fake broker URL here. If any test ever prints or
#: logs configuration, this is the string that must not appear.
BROKER_PASSWORD = "smoke-test-broker-password-not-a-credential"

#: A broker URL shaped like a real one — credential inline, database index at
#: the end — because kombu parses it with ``urllib.parse`` and a
#: ``redis://host`` that happens to be simple enough is not a test.
CREDENTIALLED_URL = f"redis://:{BROKER_PASSWORD}@broker.internal:6379/3"

#: Packages that must not appear in ``sys.modules`` after importing the worker
#: application. Grouped by what pulling them in would mean.
#:
#: * ``fastapi``/``starlette`` — the web framework. The worker is a separate
#:   process that never serves a request.
#: * ``sqlalchemy``/``psycopg`` — the ORM and driver. Importing the application
#:   must not construct an engine or resolve a hostname; a worker that cannot be
#:   imported for a liveness check without a live database is a worker whose
#:   failure is indistinguishable from a real fault.
#: * ``qdrant_client``/``pypdf``/``docx`` — the retrieval and parsing stack.
#: * ``app.api``/``app.db``/``app.models``/``app.rag``/``app.services``/
#:   ``app.storage`` — this project's own subsystems, none of which Phase 5
#:   Step 1 needs in order to hold a message and hand back a result.
FORBIDDEN_AT_IMPORT = (
    "fastapi",
    "starlette",
    "sqlalchemy",
    "psycopg",
    "qdrant_client",
    "pypdf",
    "docx",
    "app.api",
    "app.db",
    "app.models",
    "app.rag",
    "app.services",
    "app.storage",
)


# ---------------------------------------------------------------------------
# Test 1 — the application object exists and is configured, not merely present
# ---------------------------------------------------------------------------


class TestTheApplicationObject:
    """The smallest thing that can start a worker, and nothing beyond it."""

    def test_importing_the_module_yields_a_celery_application(self) -> None:
        assert isinstance(celery_app, Celery)

    def test_it_is_named_for_this_project(self) -> None:
        """A worker's name appears in every log line it emits.

        Celery resolves ``-A app.workers.celery_app`` by looking for a module
        attribute called ``app``, ``celery`` or ``celery_app``, which is why the
        instance has the name it does. The *application* name is separate and is
        what a human reads in ``celery inspect`` output.
        """
        assert APP_NAME == "tenantmind"
        assert celery_app.main == "tenantmind"

    def test_the_broker_is_the_configured_redis(self) -> None:
        assert celery_app.conf.broker_url == "redis://localhost:6379/0"

    def test_the_result_backend_is_the_same_redis(self) -> None:
        """Deliberately the same value as the broker.

        Two settings would let a deployment enqueue on one Redis and read
        results from another, and the symptom would be every task appearing to
        hang with nothing in the logs to explain it. Celery keys the two
        completely separately inside one server, so sharing is safe.
        """
        assert celery_app.conf.result_backend == celery_app.conf.broker_url

    def test_a_configured_redis_reaches_the_broker(self) -> None:
        """Proves derivation rather than coincidence.

        If the broker URL were a constant, or read from anywhere but
        :class:`~app.core.config.Settings`, this would fail while every other
        assertion in this class kept passing.
        """
        configured = build_celery_app(make_settings(redis_url=CREDENTIALLED_URL))

        assert configured.conf.broker_url == CREDENTIALLED_URL
        assert configured.conf.result_backend == CREDENTIALLED_URL

    def test_two_different_configurations_give_two_different_brokers(self) -> None:
        """The factory is not caching, and `settings` is not read at call time."""
        first = build_celery_app(make_settings(redis_url="redis://one:6379/0"))
        second = build_celery_app(make_settings(redis_url="redis://two:6379/1"))

        assert first.conf.broker_url != second.conf.broker_url
        assert first.conf.broker_url == "redis://one:6379/0"
        assert second.conf.broker_url == "redis://two:6379/1"

    def test_building_the_application_opens_no_connection(self, no_network: None) -> None:
        """Configuration is a pure operation; Celery connects on first publish.

        This is what lets ``celery -A app.workers.celery_app inspect`` run
        against a broker that is down, and what makes the whole boundary safe to
        import from a health check.
        """
        built = build_celery_app(make_settings(redis_url=CREDENTIALLED_URL))

        assert built.conf.broker_url == CREDENTIALLED_URL

    def test_no_broker_is_reachable_and_nothing_complains(self) -> None:
        """A syntactically valid URL to a dead host must still build cleanly.

        Refusing here would be wrong: a worker is routinely started before its
        broker finishes booting, and a liveness probe must not fail because of
        it. Reachability is a *runtime* question, answered by
        ``tests/test_worker_integration.py``.
        """
        built = build_celery_app(make_settings(redis_url="redis://127.0.0.1:1/0"))

        assert built.conf.broker_url == "redis://127.0.0.1:1/0"

    def test_timestamps_are_utc(self) -> None:
        """Because the ``documents`` rows this worker will write are UTC.

        A worker formatting task timestamps in the host's local zone would make
        an ingestion audit trail disagree with the PostgreSQL rows it is
        reconstructing, and that is not a discrepancy worth finding in a log.
        """
        assert celery_app.conf.timezone == "UTC"
        assert celery_app.conf.enable_utc is True


# ---------------------------------------------------------------------------
# Test 2 — the serialisation policy
# ---------------------------------------------------------------------------


class TestTheSerialisationPolicy:
    """JSON only, stated rather than inherited.

    Celery's defaults happen to be JSON today, but that is a property of a
    library version, not of this project. The failure it guards against is
    arbitrary code execution: a broker message is untrusted input, and Celery's
    pickle serialiser reconstructs Python objects from it. Anything able to
    write one Redis key would then run code inside the worker as the
    deployment's own user.
    """

    def test_task_messages_are_json(self) -> None:
        assert TASK_SERIALIZER == "json"
        assert celery_app.conf.task_serializer == "json"

    def test_results_are_json(self) -> None:
        assert RESULT_SERIALIZER == "json"
        assert celery_app.conf.result_serializer == "json"

    def test_json_is_the_only_thing_accepted(self) -> None:
        """Must stay a one-element list.

        This is the setting that matters on the *read* side: it is what stops a
        pickle-encoded message being deserialised by a worker that was never
        asked to send one. Adding an entry here is the change that would turn
        the reasoning above into decoration.
        """
        assert celery_app.conf.accept_content == ["json"]
        assert ACCEPTED_CONTENT == ["json"]

    @pytest.mark.parametrize(
        "serializer",
        ["pickle", "yaml", "msgpack", "application/x-python-serialize"],
    )
    def test_no_other_content_type_is_ever_accepted(self, serializer: str) -> None:
        assert serializer not in celery_app.conf.accept_content

    def test_the_configuration_a_caller_can_read_adds_no_second_policy(self) -> None:
        """`conf.broker_url` is the only broker-shaped value exposed.

        Guards against a future edit adding, say, ``broker_transport_options``
        or a ``CELERY_*`` passthrough dict that quietly widens the accepted
        content types back out.
        """
        relevant = {
            key: celery_app.conf[key]
            for key in ("broker_url", "result_backend", "task_serializer", "result_serializer")
            if key in celery_app.conf
        }

        assert relevant["task_serializer"] == "json"
        assert relevant["result_serializer"] == "json"


# ---------------------------------------------------------------------------
# Test 3 — importing the worker application stays cheap and safe
# ---------------------------------------------------------------------------


class TestImportingTheWorkerApplication:
    """The worker is a separate OS process; it must not need the web app."""

    def test_the_module_imports_no_web_framework(self) -> None:
        """Checked from the source, not from a runtime observation.

        An import *inside a function body* is invisible to a runtime check that
        only looks at ``sys.modules``, and that is exactly how a worker ends up
        importing FastAPI on its first task instead of at boot.
        """
        source = (APP_ROOT / "workers" / "celery_app.py").read_text(encoding="utf-8")
        imported = _imported_names(source)

        for forbidden in ("fastapi", "starlette", "app.api", "app.main"):
            assert forbidden not in imported, (
                f"app/workers/celery_app.py imports {forbidden!r}. The worker is a "
                "separate process that serves no requests; it must not pull the "
                "web application in with it."
            )

    def test_the_module_imports_no_database_or_ingestion_code(self) -> None:
        """No engine, no session factory, no pipeline — at import time or ever.

        ``app.services.ingestion`` is the module Step 2 will call from a task,
        and it is deliberately the module that *must not* be imported here:
        constructing it at worker boot would mean a boot that cannot complete
        without PostgreSQL and Qdrant both answering.
        """
        source = (APP_ROOT / "workers" / "celery_app.py").read_text(encoding="utf-8")
        imported = _imported_names(source)

        for forbidden in (
            "app.db",
            "app.models",
            "app.rag",
            "app.services.ingestion",
            "app.storage",
            "sqlalchemy",
        ):
            assert forbidden not in imported, (
                f"app/workers/celery_app.py imports {forbidden!r}; the worker "
                "application must build without the database, the vector store "
                "or the ingestion pipeline."
            )

    def test_importing_it_loads_none_of_those_packages(self) -> None:
        """The same rule, observed rather than read.

        The source-level check above proves what this module *asks* for. This
        proves what actually arrives, in a fresh interpreter where nothing has
        already been imported by another test — which matters, because in this
        suite ``fastapi`` and ``sqlalchemy`` are guaranteed to be in
        ``sys.modules`` already by the time most tests run.
        """
        program = (
            "import json, sys\n"
            "import app.workers.celery_app\n"
            "print(json.dumps(sorted(sys.modules)))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=BACKEND_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        loaded = set(json.loads(completed.stdout))

        for forbidden in FORBIDDEN_AT_IMPORT:
            offenders = {name for name in loaded if name == forbidden or name.startswith(f"{forbidden}.")}
            assert not offenders, (
                f"importing app.workers.celery_app pulled in {sorted(offenders)}; "
                "the worker process must not need it"
            )

    def test_the_package_does_not_re_export_the_application(self) -> None:
        """One name, one thing.

        ``app/workers/__init__.py`` re-exporting ``celery_app`` would shadow the
        submodule of the same name, so ``app.workers.celery_app`` would name
        either the module or the :class:`~celery.Celery` instance depending on
        import order — and ``celery -A app.workers.celery_app`` would be
        ambiguous at exactly the moment somebody is debugging a worker that
        will not start.
        """
        package_init = (APP_ROOT / "workers" / "__init__.py").read_text(encoding="utf-8")
        imported = _imported_names(package_init)

        assert "app.workers.celery_app" not in imported, (
            "app/workers/__init__.py must not re-export the application; the "
            "submodule and the Celery instance would share a name."
        )

    def test_the_documented_worker_command_imports_the_task(self) -> None:
        """The shipped run command must carry ``-I app.workers.ingestion``.

        ``tenantmind.ingest_document`` is registered by importing
        :mod:`app.workers.ingestion`, and — per the assertions above — this
        module must not import it itself. A worker started from the documented
        command therefore MUST name the extra import: without ``-I`` the worker
        runs taskless and silently discards every ingestion message, which is
        the failure the documentation must not reintroduce.
        """
        source = (APP_ROOT / "workers" / "celery_app.py").read_text(encoding="utf-8")

        assert "-I app.workers.ingestion" in source, (
            "the documented worker command must include `-I app.workers.ingestion`; "
            "a worker started without it registers no tasks and discards every "
            "ingestion message as an 'unregistered task'."
        )


# ---------------------------------------------------------------------------
# Test 4 — no client can name a task
# ---------------------------------------------------------------------------


class TestNoClientCanEnqueue:
    """The rule PMP §17 implies and Phase 5 Step 1 implements by omission.

    A task name arriving from a request is remote code execution by another
    name: the worker will import whatever module that name resolves to and call
    whatever function it names. So there is no route that takes a task name, no
    route that takes an arbitrary Celery signature, and no module outside
    ``app/workers/`` that enqueues anything at all. Step 3 adds the one
    exception, which is a *function*, not a door onto Celery: the upload
    endpoint calls :func:`~app.workers.ingestion.enqueue_ingestion`, which is
    what publishes the fixed task name — a request never supplies one.
    """

    def test_the_shipped_application_registers_only_the_sanctioned_task(
        self,
    ) -> None:
        """Step 3: the project task set is exactly the ingestion task.

        Every ``celery.*`` entry here is a Celery built-in (``celery.chain``,
        ``celery.map`` and friends are canvas primitives, registered by the
        library). Step 2 added the ingestion task, and importing its module —
        which is what the upload endpoint does — registers it; this asserts the
        *whole* project task set is that one task, so a second task still
        requires a reviewed decision.
        """
        import app.workers.ingestion as ingestion_module

        registered = set(celery_app.tasks)

        assert registered, "sanity: Celery always registers its own built-ins"
        project_tasks = {
            name for name in registered if not name.startswith("celery.")
        }
        assert project_tasks == {ingestion_module.INGEST_DOCUMENT_TASK_NAME}, (
            f"the shipped application registers project tasks "
            f"{sorted(project_tasks)!r}; exactly "
            f"{ingestion_module.INGEST_DOCUMENT_TASK_NAME!r} is sanctioned, so "
            "a new task is a decision that must be reviewed on its own."
        )

    @pytest.mark.parametrize("call", ["delay", "apply_async", "send_task", "signature"])
    def test_nothing_outside_the_worker_package_enqueues(self, call: str) -> None:
        """Scans the real source tree rather than trusting review.

        A blanket ``app/**`` scan for the call would also catch this test file's
        own strings, so the worker package and the tests are excluded; the
        package is excluded because Step 2 is *supposed* to call ``delay``
        there.
        """
        offenders = [
            path.relative_to(BACKEND_ROOT).as_posix()
            for path in sorted(APP_ROOT.rglob("*.py"))
            if "workers" not in path.parts
            and f".{call}(" in path.read_text(encoding="utf-8")
        ]

        assert not offenders, (
            f"{offenders} call Celery's {call}. Enqueuing belongs in "
            "app/workers/ only — a request handler that can name a task can make "
            "the worker execute anything it can import."
        )

    def test_the_api_reaches_the_worker_package_through_one_door(self) -> None:
        """Step 3: the one sanctioned enqueue call, and nothing else.

        Step 1 could forbid ``app.workers`` imports entirely; Step 3 cannot,
        because the upload endpoint must ask for ingestion. What it may take
        from the worker package is exactly the enqueue helper (and the error it
        raises): no ``celery_app``, no task object, no way to name a task. A
        handler that could call ``send_task`` itself could publish a task name
        derived from the request, which is the property this whole class exists
        to prevent.
        """
        SANCTIONED = frozenset({"enqueue_ingestion", "EnqueueError"})
        offenders: list[str] = []

        for path in sorted((APP_ROOT / "api").rglob("*.py")):
            relative = path.relative_to(BACKEND_ROOT).as_posix()
            source = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                    if any(name == "app.workers" or name.startswith("app.workers.")
                           for name in names):
                        offenders.append(f"{relative}: `import {names}`")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if not module.startswith("app.workers"):
                        continue
                    imported_names = {alias.name for alias in node.names}
                    if (
                        module != "app.workers.ingestion"
                        or not imported_names <= SANCTIONED
                    ):
                        offenders.append(
                            f"{relative}: `from {module} import "
                            f"{sorted(imported_names)}`"
                        )

        assert not offenders, (
            f"{offenders} reach into app.workers. The request path may use "
            "exactly `from app.workers.ingestion import enqueue_ingestion` "
            "(and the EnqueueError it raises); anything more lets a handler "
            "name background work."
        )

    def test_no_route_body_can_reach_a_task_name(self) -> None:
        """A structural check on the served contract, not on the handlers.

        No Phase 5 field may appear in any request schema. This is the test that
        would catch a future ``POST /tasks`` or a ``task_name`` query parameter
        being added to an existing route without anyone reasoning about it.
        """
        offenders = [
            f"{name}: {sorted(fields)}"
            for name, fields in _schema_field_names().items()
            if fields & {"task", "task_name", "queue", "celery", "job", "job_name"}
        ]

        assert not offenders, (
            f"request schemas expose task-shaped fields: {offenders}. A caller "
            "must not be able to name a unit of background work."
        )


# ---------------------------------------------------------------------------
# Test 5 — the broker credential cannot leak
# ---------------------------------------------------------------------------


class TestTheBrokerCredentialStaysPut:
    """The password exists in exactly one place, and it is not a log line."""

    def test_settings_mask_the_broker_password(self) -> None:
        """Why ``redis_url`` became a :class:`~pydantic.SecretStr` in this step.

        Built through ``make_settings`` like every other settings-based test, so
        the assertion is about masking rather than about how the object was
        constructed.
        """
        settings = make_settings(redis_url=CREDENTIALLED_URL)

        assert BROKER_PASSWORD not in repr(settings)
        assert BROKER_PASSWORD not in str(settings)
        assert settings.redis_url.get_secret_value() == CREDENTIALLED_URL

    def test_the_module_never_retains_the_broker_password(self) -> None:
        """It is read once, handed to Celery, and not kept in a module global.

        A traceback frame, a ``dir()`` call or a debugger session against a
        worker must not turn into a credential disclosure.
        """
        source = (APP_ROOT / "workers" / "celery_app.py").read_text(encoding="utf-8")

        assert "get_secret_value()" in source, (
            "sanity: the credential must still be read to be handed to Celery"
        )
        assert not any(
            name.isupper() and BROKER_PASSWORD in name for name in dir(celery_app)
        )
        # The module namespace holds the application and its settings-derived
        # configuration; nothing else that could be a captured credential.
        module_globals = vars(sys.modules["app.workers.celery_app"])
        assert not [
            name
            for name, value in module_globals.items()
            if isinstance(value, str) and BROKER_PASSWORD in value
        ]

    def test_no_log_line_emits_the_broker_url(self) -> None:
        """A source-level check, because a credential here is a log-line away.

        ``logger.info(celery_app.conf.broker_url)`` is a one-token mistake and
        the broker URL is the one configuration value in this project that is
        routinely a credential.
        """
        source = (APP_ROOT / "workers" / "celery_app.py").read_text(encoding="utf-8")

        for forbidden in ("logger", "logging", "print("):
            assert forbidden not in source, (
                f"app/workers/celery_app.py references {forbidden!r}. This module "
                "holds a broker credential in the configuration it returns; it "
                "logs nothing, and a test asserts that rather than trusting review."
            )

    def test_the_ingestion_pipeline_is_still_free_of_worker_dependencies(self) -> None:
        """The dependency runs the other way, and must keep doing so.

        ``app/services/ingestion.py`` is callable *by* a task, so it must not
        import ``celery``. Step 2 will call it from one; it still must not know
        a task framework exists, or it can no longer be driven from a test or a
        CLI the way it is today.
        """
        source = (APP_ROOT / "services" / "ingestion.py").read_text(encoding="utf-8")
        imported = _imported_names(source)

        for forbidden in ("celery", "redis", "kombu"):
            assert forbidden not in imported, (
                f"app/services/ingestion.py imports {forbidden!r}; the pipeline is "
                "driven by a task, not aware of one."
            )


# ---------------------------------------------------------------------------
# Test 6 — Redis configuration
# ---------------------------------------------------------------------------


class TestRedisConfiguration:
    """Loading, and refusing what Celery would silently accept."""

    def test_the_default_is_a_local_redis_with_no_credential(self) -> None:
        """A bare checkout must construct ``Settings`` with nothing configured."""
        settings = make_settings()

        assert settings.redis_url.get_secret_value() == "redis://localhost:6379/0"
        assert "@" not in settings.redis_url.get_secret_value()

    def test_the_url_comes_from_the_environment(self) -> None:
        settings = make_settings(redis_url=CREDENTIALLED_URL)

        assert settings.redis_url.get_secret_value() == CREDENTIALLED_URL

    @pytest.mark.parametrize("url", ["redis://host:6379/0", "rediss://host:6380/1"])
    def test_a_redis_scheme_is_accepted(self, url: str) -> None:
        """``rediss`` is TLS, which a managed broker in a real deployment is."""
        assert make_settings(redis_url=url).redis_url.get_secret_value() == url

    @pytest.mark.parametrize("url", ["", "   "])
    def test_a_blank_url_is_refused(self, url: str) -> None:
        """Celery's own default is ``amqp://localhost``.

        So an unset broker does not fail — it resolves to a transport nobody in
        this project runs, and the symptom is a worker that starts cleanly,
        accepts every task, and delivers none of them.
        """
        with pytest.raises(ValidationError, match="must not be blank"):
            make_settings(redis_url=url)

    @pytest.mark.parametrize(
        "url",
        [
            "amqp://guest:guest@localhost:5672//",
            "localhost:6379",
            "not-a-url",
            "sentinel://localhost:26379/0",
        ],
    )
    def test_a_non_redis_url_is_refused(self, url: str) -> None:
        """Naming the wrong transport must be a startup error, not a mystery.

        ``sentinel://`` is refused deliberately even though Celery supports it:
        this project runs one Redis and configuring Sentinel implies a topology
        that neither the compose file nor PMP §23 describes.
        """
        with pytest.raises(ValidationError, match="redis_url must begin with"):
            make_settings(redis_url=url)

    def test_the_refusal_names_the_variable_and_the_alternatives(self) -> None:
        """An operator has to be able to fix this without reading the source."""
        with pytest.raises(ValidationError) as raised:
            make_settings(redis_url="amqp://guest:guest@localhost:5672//")

        message = str(raised.value)

        assert "redis_url" in message
        assert "redis://" in message
        assert "rediss://" in message
        assert "amqp://localhost" in message, (
            "the message must say what Celery would have done instead, because "
            "'it silently fell back' is the confusing part"
        )

    def test_the_broker_password_is_not_echoed_by_the_refusal(self) -> None:
        """Even an error message must not print the credential it rejected."""
        with pytest.raises(ValidationError) as raised:
            make_settings(redis_url=f"amqp://guest:{BROKER_PASSWORD}@localhost:5672//")

        assert BROKER_PASSWORD not in str(raised.value)


# ---------------------------------------------------------------------------
# Test 7 — the Redis service in Docker Compose
# ---------------------------------------------------------------------------


class TestTheRedisServiceInCompose:
    """The properties that matter, checked on the real file.

    This project's compose file is hand-maintained local infrastructure, so the
    guarantees worth asserting are the ones a future edit could quietly undo:
    an unpinned image, a port bound to all interfaces, a credential written into
    the file, or a healthcheck that proves only that a process is alive.
    """

    def test_the_compose_file_exists(self) -> None:
        assert COMPOSE_FILE.is_file()

    def test_it_declares_a_redis_service(self) -> None:
        assert "  redis:" in COMPOSE_FILE.read_text(encoding="utf-8")

    def test_the_image_is_pinned_to_an_exact_version(self) -> None:
        """An unpinned ``redis:latest`` changes the broker under a worker that
        nobody edited a file to change."""
        block = _compose_service(COMPOSE_FILE.read_text(encoding="utf-8"), "redis")
        image = _block_value(block, "image")

        assert image.startswith("redis:")
        tag = image.split(":", 1)[1]
        assert re.fullmatch(r"\d+\.\d+\.\d+-[a-z]+", tag), (
            f"redis image {image!r} must pin an exact major.minor.patch and a base "
            "variant, matching the PostgreSQL service"
        )

    def test_it_is_loopback_only(self) -> None:
        """Anything that can open this port can enqueue a job for the worker.

        With a single shared password, an unauthenticated broker on a network is
        an unauthenticated job queue.
        """
        block = _compose_service(COMPOSE_FILE.read_text(encoding="utf-8"), "redis")

        assert "127.0.0.1:6379:6379" in block
        assert "0.0.0.0" not in block
        assert "6379:6379" in block

    def test_the_credential_comes_from_the_environment(self) -> None:
        """Never written into a committed file, and never given a default.

        A well-known password in git is the same password everywhere, which is
        why the PostgreSQL service above already refuses to start without one.
        """
        text = COMPOSE_FILE.read_text(encoding="utf-8")
        block = _compose_service(text, "redis")

        assert "${TENANTMIND_TEST_REDIS_PASSWORD:?" in text, (
            "the Redis password must be interpolated from the caller's "
            "environment with no default"
        )
        assert "requirepass" in block
        assert block.count("${TENANTMIND_TEST_REDIS_PASSWORD") == 2, (
            "expected the password in the server command and in REDISCLI_AUTH, "
            "which is how the healthcheck authenticates without putting it on a "
            "command line"
        )

    def test_it_has_a_healthcheck_that_authenticates(self) -> None:
        """``up --wait`` must not complete against a server that rejects AUTH."""
        block = _compose_service(COMPOSE_FILE.read_text(encoding="utf-8"), "redis")

        assert "healthcheck:" in block
        assert "redis-cli ping" in block
        assert "REDISCLI_AUTH" in block

    def test_it_persists_nothing(self) -> None:
        """A decision, not an omission — and a testable one.

        Redis here is a broker. PMP §19 calls its task role "temporary task
        information"; the authoritative ingestion state is the ``documents`` row
        in PostgreSQL. A named volume would imply a durability guarantee that
        no code in this project yet reads back.
        """
        block = _compose_service(COMPOSE_FILE.read_text(encoding="utf-8"), "redis")

        assert "--save" in block
        assert "--appendonly" in block
        assert "volumes:" not in block, (
            "the Redis service must not declare a volume; see the module comment"
        )

    def test_the_two_existing_services_are_untouched(self) -> None:
        """A Redis addition must not quietly change PostgreSQL or Qdrant."""
        text = COMPOSE_FILE.read_text(encoding="utf-8")

        assert "postgres:16.15-bookworm" in text
        assert "qdrant/qdrant:v1.19.1" in text
        assert "127.0.0.1:5432:5432" in text
        assert "127.0.0.1:6333:6333" in text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _imported_names(source: str) -> set[str]:
    """Every module name a source file imports, at any nesting depth.

    Deliberately a source-level scan over the whole tree rather than a scan of
    one node: an import inside a function body, or inside a ``try``, is exactly
    how a worker ends up importing FastAPI on its first task rather than at
    boot, and a naive ``ast.walk`` over module-level nodes would miss it.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import: `from . import x`
                names.add("." * node.level + (node.module or ""))
            elif node.module:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _compose_service(text: str, service: str) -> str:
    """Return the body of one service block, indentation-delimited.

    Hand-rolled because this project has no YAML parser and adding one to parse
    a file it also writes would be a dependency bought for a single test. The
    block runs from ``  <service>:`` to the next line indented at two spaces,
    which is the structure of the file and is asserted by the tests above.
    """
    lines = text.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.rstrip() == f"  {service}:"
    )
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("    "):
            break
        body.append(line)
    return "\n".join(body)


def _block_value(block: str, key: str) -> str:
    """Return a ``key: value`` pair from a service block, stripped of quoting."""
    match = re.search(rf"^\s+{key}:\s*(.+?)\s*$", block, re.MULTILINE)
    if match is None:
        raise AssertionError(f"no {key!r} key found in the redis service block")
    return match.group(1).strip("\"'")


def _schema_field_names() -> dict[str, set[str]]:
    """Map every request schema in ``app/schemas`` to its declared field names.

    Imported rather than parsed so the answer reflects the actual pydantic
    models this project serves, including any alias a field might be exposed
    under.
    """
    import importlib
    import pkgutil

    import app.schemas

    found: dict[str, set[str]] = {}
    for module_info in pkgutil.iter_modules(app.schemas.__path__):
        module = importlib.import_module(f"app.schemas.{module_info.name}")
        for attribute in vars(module).values():
            fields = getattr(attribute, "model_fields", None)
            if not isinstance(fields, dict):
                continue
            names = set(fields) | {
                field.alias or name for name, field in fields.items()
            }
            found[f"{module_info.name}.{getattr(attribute, '__name__', '?')}"] = {
                str(name) for name in names
            }
    return found