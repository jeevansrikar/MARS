import json
import logging
import sys

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.telemetry import SafeServerErrors, safe_span
from app.db.models import Job


async def test_trace_context_reaches_durable_job_without_private_payload(system):
    _, client, repo, _, _, _ = system
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    try:
        response = await client.post(
            "/api/v1/research",
            headers={"Idempotency-Key": "tracing-001"},
            json={"query": "Sensitive research question"},
        )
        async with repo.sessions() as db:
            job = await db.get(Job, response.json()["job_id"])
            assert job.trace_context["traceparent"]
            assert job.trace_context["request_id"]
        with pytest.raises(ValueError):
            with safe_span("unsafe_exception"):
                raise ValueError("private-provider-error-body")
        spans = exporter.get_finished_spans()
        assert any(s.name == "db.query" for s in spans)
        assert any(s.name == "http.request" for s in spans)
        serialized = json.dumps([s.to_json() for s in spans])
        assert "Sensitive research question" not in serialized
        assert "private-provider-error-body" not in serialized
    finally:
        exporter.clear()
        exporter.shutdown()


def test_server_error_logging_cannot_echo_private_exception_text():
    try:
        raise ValueError("private SQL parameters and provider response")
    except ValueError:
        record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "ASGI exception", (), sys.exc_info())
    assert SafeServerErrors().filter(record)
    rendered = logging.Formatter().format(record)
    assert "ValueError" in rendered and "test_server_error_logging" in rendered
    assert "private SQL" not in rendered and "provider response" not in rendered
