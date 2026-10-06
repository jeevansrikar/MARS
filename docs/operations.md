# Operations and recovery

## Monitoring

Scrape API `/metrics` with the operator bearer token on a private route. Scrape each
worker's port 9101 privately; it has no public authentication layer and must not be
published. Run one API process per container for accurate process-local metrics.
Aggregate counters across replicas; take the **maximum**, not sum, of the durable
job-state gauges repeated by each worker. Standard Python/process collectors supply
process metrics where supported. Deploy host/container, managed DB and Redis
exporters for CPU, memory, connection counts and server latency.

Metrics cover HTTP status/latency, active streams, active worker tasks, durable job
counts, job duration/outcomes, LLM attempts/errors/latency/provider-reported tokens,
and research source/claim counts/coverage ratios. Model cost uses operator-supplied
USD-per-million-token `MODEL_PRICES` pairs `[input,output]`; missing prices yield no
estimate, never a fabricated one. Provider invoices remain authoritative; failed
requests may incur charges without returning usage.

Set OTEL_EXPORTER_OTLP_ENDPOINT to export HTTP, worker, agent, search/fetch, LLM,
SQL query and Redis command spans. W3C trace context and request IDs are persisted
on job creation and extracted by the worker. HTTP spans use route templates; SQL
and Redis spans record operation names, never query strings, statements, keys,
values or exception messages. Dependency latency histograms supplement traces.
Redis pipeline internals are not individually instrumented. In-process span tests
verify private query/error redaction; an external collector is still unverified.
Do not enable automatic HTTP payload/header capture alongside these safe spans.

Alert on sustained readiness failures, growing queued jobs, worker heartbeat keys
expiring (`mars:worker:*`, 15 seconds), old active leases, rising provider timeouts/
429s and failure rate. Budget/provider errors need different response from code
errors. Use job IDs, request IDs and trace IDs in structured JSON logs; exception
stack locations omit sensitive exception messages/locals.

## Budgets and retention

Defaults: 2 active jobs/user, 20 submissions/day/user, 32 LLM attempts, 4 search
calls, 8 sources, 1 MB/source, 24,000 normalized characters/source, 2,048 output
tokens/call, 96,000 input bytes/call, 900 seconds/job, 3 worker attempts and 2 debate
rounds. Attempts consume the same durable budgets. Input bytes bound context size;
they are not falsely reported as exact token usage. Tune model prices and quotas
against a staging dataset before raising limits.

Memory expires after 90 days by default. Run `python -m app.manage prune` daily using
your existing scheduler to remove expired memory and sessions. Search cache lasts
60 seconds for fresh/news queries and 900 seconds otherwise; source text 60 seconds;
embedding vectors 24 hours; Redis event Streams one day. PostgreSQL events/results remain until job/user removal
or an operator-defined retention policy. Size/back up that storage accordingly.

## Failure recovery

| Failure | Behavior / recovery |
|---|---|
| Browser/network disconnect | Job continues. Reopen history; snapshot + event replay restore progress. |
| API restart | Durable jobs remain. Browser reconnects; workers run independently. |
| Worker process dies | Heartbeat stops. After lease expiry another worker resumes completed checkpoints; incomplete provider calls may repeat. |
| Local embedding model missing | Worker startup fails before claiming jobs. Provision the pinned assets and validate locally; no fabricated vector fallback exists. |
| Redis unavailable | New authenticated calls fail closed at the limiter. Running jobs retain PostgreSQL checkpoints; search caches/notifications degrade. Outbox retries when Redis returns. |
| Redis data lost | Jobs/results/events survive in PostgreSQL; notification polling and database event replay recover. Short-lived quotas/cache counters reset. |
| PostgreSQL unavailable | Readiness fails. No unsafe checkpoint/claim writes; current workers eventually lose leases. Restore service, then workers reconcile. |
| Provider 429/5xx/timeout | Async bounded retries with jitter; then bounded job retry. Nonretryable errors fail without repeated billing. |
| Unsupported source/unsafe URL | Source is rejected. If none remain, job fails NO_EVIDENCE. |

## Backups, restore and rollback

Use managed PostgreSQL continuous backups/PITR with encrypted storage and a
documented recovery objective; choose retention according to actual privacy policy.
For local exercises, `pg_dump -Fc` and `pg_restore --no-owner` against a **new isolated
database** provide a portable restore path. Never validate restore by overwriting
the only live database. Test user ownership, results, events and migration revision
on the restored instance, then rehearse worker lease recovery. Redis persistence is
useful but is not a research-state backup.

Before migration: record `alembic current`, back up, and review generated SQL. The
initial migration creates an empty schema; subsequent revisions add vectors and
organization scope. `downgrade base` destroys v2 tables and must never
be used as a casual production rollback. Prefer forward fixes and compatible
application rollback. Stop workers before incompatible schema changes. Roll back
to the previous reviewed image digests only if the schema is compatible; otherwise
restore/PITR to an isolated database and perform a controlled cutover.

Rotate provider/metrics keys in the secret manager and restart API/workers. Rotate
database/Redis credentials with overlapping validity if supported. Reset one user
with `python -m app.manage reset-password --email ...`; this revokes their sessions.
`delete-user` is destructive and removes durable v2 jobs, results, memory and events
through foreign keys. Short-lived caches and retained backups expire under their
separate retention policies.
