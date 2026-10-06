# Troubleshooting

| Symptom | Check |
|---|---|
| ImportError for `app` | Run from `backend/` with the new root `.venv`; the old bundled `backend/venv` belongs to another machine. |
| `/live` 200, `/ready` 503 | Check PostgreSQL/schema, Redis, and required provider configuration. Use operator `/health`; apply Alembic migrations. |
| Job remains queued | Start `python -m app.workers.run`; inspect worker logs and expiring Redis heartbeat keys. Check DB locks/connection capacity. |
| Login succeeds but requests are unauthorized | Use same-origin frontend/API, HTTPS in production, and credentials-included requests. Do not put cookies in localStorage. |
| Mutation rejected 403 | Supply `X-MARS-Client: 1`; Origin must exactly match ALLOWED_ORIGINS, including scheme/port. |
| 409 on submit | Same idempotency key with different content. Reuse only for an identical ambiguous request. |
| 429 on submit/stream | Active/daily quota, endpoint/IP limiter or four-stream cap; close stale tabs or wait for TTL. |
| SSE does not update through proxy | Disable proxy buffering/caching and use read timeouts longer than heartbeat interval. Inspect Last-Event-ID and owner credentials. |
| NO_EVIDENCE | Check Tavily credentials, external connectivity, source HTML/text support and safety rejections. No fake fallback is provided. |
| PROVIDER_REJECTED | Validate Anthropic key, configured model access, request limits and provider account. Never paste full keys into logs/issues. |
| EMBEDDING_UNAVAILABLE | Provision pinned assets from backend with `python -m app.providers.embeddings --download`; verify EMBEDDING_CACHE_DIR and revision match the deployment. Workers warm the model before accepting jobs. |
| pgvector migration permission error | Use a DDL role authorized to install vector, or have the database administrator preinstall the extension. Apply all three migrations. |
| Organization scope rejected or empty | Confirm membership with the operator; scope requires an organization UUID. Shared queries exclude private memory, and revoked membership blocks reads/final writes. |
| INVALID_PROVIDER_OUTPUT / OUTPUT_TRUNCATED | Review stage/model and bounded output budget. Structured-output failures are surfaced, not parsed into invented claims. |
| Worker crashes repeatedly | Inspect safe stack locations/error type, lease age and durable attempt/call budgets. Completed stages remain reusable. |
| Unit tests skip PostgreSQL cases | Set TEST_DATABASE_URL for a disposable PostgreSQL+pgvector DB and TEST_REDIS_URL for disposable Redis. Tests create/drop their own tables. |
| Docker command missing | Install/run Docker Engine or Docker Desktop on the host; configuration files alone do not verify containers. |

The old PDF, original SQL and legacy-library narrative comments are historical.
Use the current architecture and verification report for actual runtime capabilities.
