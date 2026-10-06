# API v1

Interactive contracts: `/docs`; machine contract: `/openapi.json`.

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/v1/auth/login` | Email/password; returns user and HttpOnly session cookie |
| GET | `/api/v1/auth/me` | Current authenticated user |
| GET | `/api/v1/auth/organizations` | Organizations the caller currently belongs to |
| POST | `/api/v1/auth/logout` | Revokes all sessions for this user |
| POST | `/api/v1/research` | Commit job; 202 snapshot; required `Idempotency-Key` (8–128 characters) |
| GET | `/api/v1/research` | Owner history; `before` epoch cursor, `limit` 1–100 |
| GET | `/api/v1/research/{job_id}` | Owner snapshot, checkpoints and final result |
| GET | `/api/v1/research/{job_id}/events` | SSE replay after `Last-Event-ID` or `after` |
| POST | `/api/v1/research/{job_id}/cancel` | Request explicit cancellation; idempotent |
| GET | `/api/v1/memory` | Visible memory; optional `query`, `session_id`, `organization_id`; sparse/dense fused query ranking |
| DELETE | `/api/v1/memory/{memory_id}` | Forget owner memory, 204 |
| GET | `/live` | Process liveness |
| GET | `/ready` | Database/schema, Redis and provider configuration; 503 on failure |
| GET | `/health` | Operator-only dependency diagnostics |
| GET | `/metrics` | Operator-only Prometheus metrics |

Mutation requests require `X-MARS-Client: 1`. Browser origins must match the explicit
allowlist. Authentication uses an opaque cookie; controlled nonbrowser clients may
send the same token as `Authorization: Bearer ...`. Never put credentials in URLs.
Operator endpoints require `Authorization: Bearer <METRICS_TOKEN>`.

Submit body: `{"query":"Your question","session_id":null,"memory_scope":"user","organization_id":null}`.
`memory_scope` is `session`, `user` (default), or `organization`. Organization scope
requires an organization UUID and current membership; other scopes reject an
organization ID. A provided session UUID must already belong to the caller.
Organization memory queries select only that organization's shared findings and
require current membership in the underlying visibility query. Default memory
queries return private user memory, plus matching session memory when requested.
Memory entries include `scope`, `organization_id` and `can_delete`.
The server derives
ownership from authentication and rejects a client `user_id`. Reusing an idempotency
key with different content returns 409; with the same content returns the original
job. Quotas return 429. Missing/foreign jobs both return 404.

Statuses: queued → running → completed/completed_with_warnings/failed; cancellation
moves through cancelling → cancelled. Transient failures can return a job to queued
with bounded backoff. New submissions are necessary to retry an exhausted terminal
job; use a new idempotency key deliberately.

Each SSE event has event_id, sequence, job_id, type, agent, timestamp and payload.
Kinds include job_queued, job_started, job_retrying, agent_started, agent_completed,
agent_failed, agent_cancelled,
job_cancelling, job_completed, job_cancelled and job_failed. `agent` identifies the
planner, retrieval, reranker, researchers, critic, skeptic, debate round, synthesizer
or verifier, plus dense retrieval, evidence-fusion, conflict-graph and belief
checkpoints. Heartbeats are SSE comments. Invalid/future cursors return 400.
Four concurrent streams per user are allowed. Credentials are rechecked during
streaming; sign-out/expiry closes subscriptions. Streams never cancel jobs.

Errors use `{"code":"...","message":"safe description","request_id":"..."}`.
Provider bodies, SQL parameters and stack exception strings are not returned.
Validation errors deliberately avoid echoing submitted secrets or private queries.

Readiness validates credential presence, not billable upstream connectivity. A
configured but invalid provider key will fail a research job with a typed error.
