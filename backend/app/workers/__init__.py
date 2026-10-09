"""Background processing (PMP §22 Phase 5).

The Celery application lives in :mod:`app.workers.celery_app`, and nothing is
re-exported here on purpose.

Re-exporting ``celery_app`` from this package would shadow the submodule of the
same name — ``app.workers.celery_app`` would then be ambiguous between the
module and the :class:`~celery.Celery` instance, and which one a caller got
would depend on import order. One name, one thing.
"""