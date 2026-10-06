# Engineering audit

Baseline inspected 2026-10-05. This directory is an extracted archive, not a Git checkout.
All application sources, tests, SQL, frontend components/configuration, README and the
17-page historical PDF were inspected. Python sources parse. Some tool excerpts were
truncated; direct reads established that the source files themselves are intact.

## Dependency map before changes

`frontend/App -> usePipeline -> POST /research/stream -> main.run_pipeline`
owns every Anthropic call inside a StreamingResponse. Calls use a synchronous client
in threads, researchers are sequential, and no search provider is invoked.

`retrieval/{decomposer,hybrid_pipeline} -> app.core.llm_client` (missing)
`memory/{working,retriever,semantic} -> app.core/config/llm_client` (missing)
`api/memory_endpoints -> app.db.session` (missing); router never mounted.
Tests import `app.*`, but that package does not exist. Memory SQL is manual,
ORM metadata is split, semantic memory has no owner, and episodic supersession
can affect other users. No Docker, Alembic, CI, queue, authentication or telemetry.

Frontend has six agent cards and debate/belief/benchmark views. MemoryPanel is unused.
The fetch stream has no abort lifecycle, resume, replay, history or cancellation.
Vite environment declarations and lint/typecheck scripts are missing.
Quality values in main are fabricated from hashes/confidence; comparison baselines
in the frontend are constants without a benchmark dataset.

## Implementation checklist

- Separate API, durable repository, worker and orchestration; retain agent roles.
- PostgreSQL transactions, owner-scoped records, migrations, checkpoint fencing.
- Redis notifications/Streams, database reconciliation, replay and cancellation.
- Credential authentication, ownership, idempotency, distributed limits and quotas.
- Async provider, real search, safe fetch, retrieval and quote validation.
- Claim-level verification and measured coverage; no manufactured accuracy.
- Preserve BM25/RRF/decomposition/reranking and evaluation algorithms.
- Isolate memory and remove heuristic confidence from the public experience.
- Resilient frontend, tests, CI, containers, operations and security documentation.
- Record actual verification results and distinguish service tests from test doubles.

## Environment baseline

Bundled backend/venv targets another machine and cannot execute. A repository-local
.venv uses Python 3.12. Docker, PostgreSQL and Redis executables were not on PATH.
Dependencies were installed into an isolated root .venv; network access is available. No live provider keys
have been inspected or printed. The historical PDF is retained as historical material;
the new README and docs supersede its architecture and benchmark claims.
