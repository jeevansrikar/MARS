# Verification record — 2026-10-06

This is a hardening implementation with local verification, **not a production
readiness certification**. The requested production definition of done has not
been established. Results below distinguish tests from unavailable infrastructure.

| Check | Actual result |
|---|---|
| Backend suite | 202 passed, 30 skipped, 47.15 seconds; RUN_EMBEDDING_TESTS=1 |
| Python lint | Ruff passed across app/tests/migrations |
| Python typing | mypy passed, 50 application files |
| Application imports | All 49 discovered modules imported successfully |
| Frontend typing/lint/build | Passed; Vite production build succeeded |
| Frontend unit tests | 4 passed |
| Browser tests | 6 passed in installed Microsoft Edge, 15.1 seconds; controlled API routes |
| Real local embedding inference | Passed: pinned ONNX assets, paraphrase ranking, workflow retrieval and memory persistence |
| Python dependency scan | pip-audit: no known vulnerabilities found |
| JavaScript dependency scan | npm audit: 0 vulnerabilities |
| SQLite migrations | Upgrade → downgrade → upgrade passed within backend suite |
| PostgreSQL migration SQL | All three revisions generated offline (242 lines); not executed against PostgreSQL |
| Compose/CI YAML | Four configuration files parsed; Docker/nginx validation awaits container gate |
| Actual API process | Uvicorn started; `/live` 200, `/ready` 503 without dependencies, unauthenticated research 401 |
| Docker builds/containers | Not run: no Docker executable/daemon on this machine |
| Real PostgreSQL/pgvector/Redis | Not run: no services/executables available; WSL not installed |
| Live Anthropic/Tavily research | Not run: no configured test credentials used |
| Production load/latency/cost | Not measured; no invented p50/p95/p99 or baseline comparison |
| External telemetry/backup restore/cloud rollout | Not exercised |

The 30 skips are 29 legacy PostgreSQL/pgvector memory cases and one PostgreSQL
concurrent idempotency/claim case. Local durability tests use SQLite with serialized
transactions and fakeredis with Lua support. They do not establish PostgreSQL row
locking, real Redis recovery, or behavior under multi-host crashes.

The refreshed npm scan identified a high-severity source-map-js advisory; the
lockfile now resolves patched version 1.2.2 and the subsequent scan reports zero
vulnerabilities. Typecheck, lint, unit tests and production build passed after the
update. A six-worker Edge rerun timed out on initial navigation; all six tests
passed with one worker, now the configured default. No test assertion was relaxed.
The final nginx review removed location-level header overrides so security headers
are inherited alongside route-specific cache policies. CI now asserts the served
HTML headers after container startup; that container check was not run locally.

Backend tests cover ownership, idempotency conflicts, quotas/budgets, authentication,
CSRF/body limits, exact quotes/forged citations, unsafe DNS/redirects, oversized
sources, provider timeout/429/5xx and nonretryable errors, incremental streaming,
search failures, cancellation of active calls, lease fencing/checkpoint recovery,
event replay after Redis stream deletion, simulated browser disconnect, memory
isolation, explicit organization sharing/membership revocation, dependency failure
readiness, private trace/server-error redaction, and a complete controlled-provider workflow.
The workflow fixture is explicitly synthetic test evidence; production never uses it.

Browser tests cover submit→checkpoint display, refresh without cancellation,
explicit cancellation, ambiguous request retries using the same key, and pending
submission recovery after refresh, early evidence/belief display, and explicit
organization audience selection. A screenshot was inspected for layout. These
tests are not live provider end-to-end acceptance.

## Required staging acceptance

Use disposable accounts/data and known spend limits:

1. Run CI with real PostgreSQL+pgvector and Redis; require **zero unexpected skips**.
   Exercise Alembic upgrade/downgrade on disposable databases and `alembic check`.
   CI provisions the pinned model and sets RUN_EMBEDDING_TESTS=1.
2. Build both images, start Compose, provision a user and check dependency readiness.
3. Use real Anthropic/Tavily credentials; inspect fetched content and every citation
   against a small human-reviewed question set, including conflicting and missing
   evidence. Record coverage, citation accuracy and failure cases separately.
4. Close the browser during a run; reopen/replay. Kill a worker after a checkpoint,
   start a replacement, and verify completed stages are reused. Observe fenced stale
   writes. Restart API; pause/restart Redis and PostgreSQL in the disposable stack.
5. Run `scripts/load_test.py` with staging-only tokens and appropriate quotas. Measure
   actual API p50/p95/p99, failure rate and throughput; correlate worker queue delay,
   job duration, LLM latency/tokens and provider billing. The included probe measures
   admission only; build representative multi-user traffic for a capacity decision.
6. Verify TLS/cookies, ingress/egress policy, metrics/traces, backup restore and image
   rollback against the actual hosting setup. Do not label deployment production-ready
   before these gates pass.

## Remaining functional limits

- Primary retrieval uses BM25, real local dense embeddings, multi-query RRF and LLM
  reranking. Filtered production pgvector query performance and ANN index choices
  need a representative corpus; local vector tests do not establish database performance.
- Session/user/organization memory and membership revocation are implemented and
  tested with SQLite. Production locking and concurrent membership changes need
  the real-service gate. Organization jobs/results remain private to their author;
  only supported findings/evidence are shared, and administration is operator-only.
- Source quality/calibrated accuracy and ownership-level source independence are
  not established. Registrable-domain/content counts are descriptive proxies only.
- Belief evolution uses durable graph checkpoints and live findings. Assessments
  are model judgments with provenance checks, not calibrated factual confidence.
- Safe HTTP/worker/provider/SQL/Redis-command spans are implemented. Redis pipeline
  internals, billing reconciliation, host metrics and collector integration need
  deployment work. Redaction tests use an in-process exporter.
- Publishing is gated by CI; cloud-specific automatic rollout is not configured
  without a deployment target. The production Compose configuration is supplied.

These are stated explicitly so preservation of a library or a configuration file
is not mistaken for a verified runtime feature.
