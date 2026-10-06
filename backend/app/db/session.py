import time
from typing import Any

from opentelemetry.trace import Status, StatusCode
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.telemetry import DEPENDENCY_LATENCY, tracer


def make_database(settings: Settings):
    options: dict[str, Any] = {"pool_pre_ping": True, "hide_parameters": True}
    if settings.database_url.startswith("postgresql"):
        options.update(pool_size=10, max_overflow=10, pool_timeout=10)
    engine = create_async_engine(settings.database_url, **options)

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def begin_query(_connection, _cursor, statement, _parameters, context, _many):
        operation = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "UNKNOWN"
        context._mars_started = time.monotonic()
        context._mars_operation = operation
        context._mars_span = tracer.start_span(
            "db.query", attributes={"db.system": engine.dialect.name, "db.operation.name": operation}
        )

    def finish_query(context, failed=False):
        span = getattr(context, "_mars_span", None)
        if span is not None:
            if failed:
                span.set_status(Status(StatusCode.ERROR))
            span.end()
            context._mars_span = None
            DEPENDENCY_LATENCY.labels(engine.dialect.name, context._mars_operation).observe(
                time.monotonic() - context._mars_started
            )

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def end_query(_connection, _cursor, _statement, _parameters, context, _many):
        finish_query(context)

    @event.listens_for(engine.sync_engine, "handle_error")
    def query_error(context):
        if context.execution_context is not None:
            finish_query(context.execution_context, failed=True)

    if settings.database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def sqlite_foreign_keys(connection, _record):
            connection.isolation_level = None
            connection.execute("PRAGMA foreign_keys=ON")

        @event.listens_for(engine.sync_engine, "begin")
        def sqlite_transaction(connection):
            # SQLite has no row locks. Serialize its test transactions so the
            # repository's read/modify/write operations retain their semantics.
            # Production uses PostgreSQL row locks and SKIP LOCKED instead.
            connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine, async_sessionmaker(engine, expire_on_commit=False)
