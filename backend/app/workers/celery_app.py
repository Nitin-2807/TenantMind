"""The Celery application boundary (PMP §22 Phase 5 Step 1).

This is the whole of the background-processing foundation: a configured Celery
application, ready to be handed to a worker process::

    ../.venv/bin/celery -A app.workers.celery_app worker -I app.workers.ingestion --loglevel=INFO

What exists here
----------------

* A Celery application wired to the project's configured Redis, used as both
  the broker and the result backend.
* A JSON-only serialization policy, stated explicitly rather than inherited.

What deliberately does **not** exist here
----------------------------------------

* **One task, registered by import, never at build time.** The ingestion task
  ``tenantmind.ingest_document`` lives in :mod:`app.workers.ingestion` and is
  registered on :data:`celery_app` by importing that module — which is why the
  worker command above carries ``-I app.workers.ingestion``. The task module is
  deliberately NOT imported here: this module is the import-free foundation,
  and the tests below assert that importing only this module stays cheap and
  pulls in no web framework, database or pipeline code.
* **No FastAPI import, no route.** Nothing in this module may import
  ``app.api`` or ``fastapi``. The worker is a separate OS process with its own
  interpreter; importing the web application into it would drag the whole
  route tree, and therefore the whole request-time dependency graph, into a
  process that only ever runs background work.
* **No database connection at import time.** Importing this module must not
  open a socket, resolve a hostname, or construct a SQLAlchemy engine. A
  worker that cannot be imported for a liveness check without a live database
  is a worker whose failure modes are indistinguishable from a real fault.
* **No credentials in configuration or arguments.** The broker password reaches
  this module only through :attr:`Settings.redis_url`, which is a
  :class:`~pydantic.SecretStr` precisely so it cannot be logged by accident.

Why one Redis URL serves both roles
-----------------------------------

A broker and a result backend are conceptually different things, and the
obvious design is two settings. That is deliberately rejected. Two settings
mean a deployment can enqueue onto broker A and read results from backend B,
and the symptom of getting that wrong is every task appearing to hang
forever — with nothing in the logs to say why. Celery keys the two
completely separately inside one Redis (``celery-task-meta-*`` versus the
queue keys), so sharing a database is safe, and one setting makes the
mismatch unrepresentable.

The result backend is not speculative. Without one a task still runs, but its
return value is discarded and :meth:`~celery.result.AsyncResult.get` can never
return, so a smoke test cannot prove the round trip at all.
"""

from __future__ import annotations

from celery import Celery  # type: ignore[import-untyped]

from app.core.config import Settings, settings

#: The Celery application name. Appears in the worker's startup banner, in
#: ``celery -A app.workers.celery_app inspect ping`` output and in every task
#: log line, so it is a constant rather than a constructor argument: a name
#: that varies per environment makes worker logs impossible to correlate.
APP_NAME = "tenantmind"

#: The only serialisation this worker accepts, on every channel.
#:
#: Celery's defaults happen to be JSON today, but that is a property of *a
#: library version*, not of this project, and the failure it guards against is
#: arbitrary code execution: a broker message is untrusted input, and Celery's
#: pickle serialiser reconstructs objects from it. Anything able to write to
#: the Redis key would then run code inside the worker as the deployment's own
#: user. JSON cannot do that, so it is stated here and asserted by a test
#: rather than trusted to a default.
JSON = "json"

#: Serialisers used to *write* task messages and results.
TASK_SERIALIZER = JSON
RESULT_SERIALIZER = JSON

#: Serialisers this worker will *accept*, both for incoming messages and for
#: reading results back. A one-element list, and it must stay one element:
#: adding a non-JSON entry here is the change that would turn the reasoning
#: above into decoration.
ACCEPTED_CONTENT = [JSON]


def build_celery_app(app_settings: Settings | None = None) -> Celery:
    """Build a Celery application from TenantMind's configuration.

    A factory, not a bare module-level ``Celery(...)`` call, for the same
    reason :func:`app.main.create_app` is a factory: tests need to build an
    application against an isolated :class:`~app.core.config.Settings` and
    prove the broker genuinely follows configuration rather than matching by
    coincidence. Tests that use it register their own tasks on the result
    before starting a worker. The production instance at the bottom of this
    module carries its task only once a running worker imports
    :mod:`app.workers.ingestion` — ``-I`` on the command line is the one way
    the shipped worker ever receives work.

    No connection is opened here. Celery connects lazily, on the first publish
    or consume, so building the application — and therefore importing this
    module — is a pure configuration operation.

    Args:
        app_settings: configuration to build from. Defaults to the canonical
            :data:`app.core.config.settings` singleton.

    Returns:
        A configured, unconnected :class:`~celery.Celery` application.
    """
    config = settings if app_settings is None else app_settings

    # `.get_secret_value()` at exactly this point and nowhere else. The value is
    # read once, handed to Celery, and not retained by this module — so a
    # traceback frame or a `dir()` of this module cannot leak it.
    broker_url = config.redis_url.get_secret_value()

    application = Celery(APP_NAME, broker=broker_url, backend=broker_url)

    application.conf.update(
        task_serializer=TASK_SERIALIZER,
        result_serializer=RESULT_SERIALIZER,
        accept_content=list(ACCEPTED_CONTENT),
        # The whole project stores and reports UTC. Celery's defaults are UTC
        # today and `enable_utc` is implied, but a worker that formats task
        # timestamps in the host's local zone would make an ingestion audit
        # trail disagree with the `documents` rows it is reconstructing, and
        # that is not a discrepancy worth discovering from a log.
        timezone="UTC",
        enable_utc=True,
    )

    return application


#: The application a worker process runs.
#:
#: Deliberately built at import time and deliberately *not* started here. The
#: name is what Celery's ``-A app.workers.celery_app`` resolves, and importing
#: this module is a safe no-op apart from reading configuration. It is also
#: deliberately taskless at import: the worker registers
#: ``tenantmind.ingest_document`` by additionally importing
#: :mod:`app.workers.ingestion` (``-I app.workers.ingestion``), keeping this
#: module free of the web framework, the database and the pipeline code.
celery_app: Celery = build_celery_app()