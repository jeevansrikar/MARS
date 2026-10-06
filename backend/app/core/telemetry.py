import logging
import traceback
from contextlib import contextmanager

import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter("mars_http_requests", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("mars_http_seconds", "HTTP latency", ["route"])
LLM_REQUESTS = Counter("mars_llm_requests", "LLM requests", ["model", "status"])
LLM_TOKENS = Counter("mars_llm_tokens", "Provider reported tokens", ["model", "direction"])
LLM_LATENCY = Histogram("mars_llm_seconds", "LLM latency", ["model"])
WORKERS = Gauge("mars_active_worker_tasks", "Local active worker jobs")
STREAMS = Gauge("mars_active_streams", "Local SSE connections")
JOBS = Counter("mars_job_outcomes", "Worker outcomes", ["status"])
JOB_DURATION = Histogram("mars_job_seconds", "Job duration")
QUEUE = Gauge("mars_jobs_current", "Durable job counts", ["status"])
QUALITY = Histogram("mars_research_ratio", "Measured claim ratios", ["metric"], buckets=[0, 0.25, 0.5, 0.75, 0.9, 1])
RESEARCH_COUNTS = Histogram(
    "mars_research_items", "Sources and claims per job", ["kind"], buckets=[0, 1, 2, 4, 8, 16, 32]
)
ESTIMATED_COST = Counter("mars_llm_estimated_usd", "Estimated cost using operator-configured prices", ["model"])
tracer = trace.get_tracer("mars")
DEPENDENCY_LATENCY = Histogram("mars_dependency_seconds", "Database/Redis operation latency", ["system", "operation"])


@contextmanager
def safe_span(name: str, **kwargs):
    with tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False, **kwargs) as span:
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", type(exc).__name__)
            raise


def safe_stack(error: BaseException) -> list[str]:
    # File/function/line only: exception strings and local variables can contain private text.
    return [f"{frame.filename}:{frame.lineno}:{frame.name}" for frame in traceback.extract_tb(error.__traceback__)]


class SafeServerErrors(logging.Filter):
    def filter(self, record):
        if record.exc_info:
            error = record.exc_info[1]
            record.msg = "ASGI failure: %s; locations=%s"
            record.args = (type(error).__name__, safe_stack(error) if error else [])
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def redact_and_trace(_logger, _method, event):
    for key in (
        "query",
        "original_query",
        "content",
        "output",
        "existing_claim",
        "new_claim",
        "error",
        "api_key",
        "password",
        "authorization",
        "topics",
    ):
        if key in event:
            event[key] = "[redacted]"
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event["trace_id"] = format(context.trace_id, "032x")
    return event


def setup_telemetry(settings, service: str):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Uvicorn otherwise logs raw query strings and re-raised exception messages,
    # even after our application handler has returned a sanitized 500 response.
    logging.getLogger("uvicorn.access").disabled = True
    server_logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(item, SafeServerErrors) for item in server_logger.filters):
        server_logger.addFilter(SafeServerErrors())

    def runtime_context(_logger, _method, event):
        event.setdefault("service", service)
        event.setdefault("environment", settings.environment)
        return event

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            runtime_context,
            redact_and_trace,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ]
    )
    structlog.contextvars.bind_contextvars(service=service, environment=settings.environment)
    # Network libraries can log URLs and provider bodies; keep those out of standard logs.
    for name in ("httpx", "httpx2", "httpcore", "anthropic", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider(
            resource=Resource.create({"service.name": service, "deployment.environment": settings.environment})
        )
        if settings.otel_exporter_otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
            if not endpoint.endswith("/v1/traces"):
                endpoint += "/v1/traces"
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
