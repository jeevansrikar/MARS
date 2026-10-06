# Deployment

This repository contains deployment configuration, not evidence of a successful
production deployment. Read `verification.md` before using real traffic.

## Local Docker stack

From the repository root, with Docker Engine and Compose v2 installed:

```powershell
Copy-Item .env.example .env
# Edit .env: choose a local database password, real provider keys, and metrics token.
docker compose config --quiet
docker compose build
docker compose up -d
docker compose exec api python -m app.manage create-user --email you@example.com
```

Open `http://localhost:3000`. The migration service must finish successfully before
API/workers start. Check `docker compose ps`, `/ready`, and `docker compose logs worker`.
Missing provider keys keep readiness unhealthy and research fails explicitly.
The backend image provisions the pinned public ONNX model during its build; allow
build-time access to the model registry and budget for its assets. Runtime inference
uses only local assets. For a non-container install, from `backend/` run
`..\.venv\Scripts\python.exe -m app.providers.embeddings --download` before starting
workers. Run the same command without `--download` to validate the local model.
Database and Redis host ports bind only to loopback. Use URL-safe database passwords
or percent-encode them correctly in the DSN. Never expose local Compose directly.

## Production topology

Use a TLS load balancer/reverse proxy in front of the frontend container; its `/api`
proxy reaches the separate FastAPI service. Use managed private PostgreSQL and Redis
with encryption, backups and access controls. The worker is an independent process
pool. No Kubernetes or object storage is required for bounded HTML/text research.

1. Run the mandatory CI jobs on the exact release commit. Run staging provider,
   crash recovery and load acceptance checks described in `verification.md`.
2. Build/publish both images. The release workflow runs only after the quality
   workflow succeeds on a main-branch push; configure the GitHub `production`
   environment's protection rules. It publishes images; it does not silently choose
   or access a cloud account. Record image **digests**, not mutable tags.
3. Provision a managed PostgreSQL database with pgvector and a Redis instance. Limit their firewall
   rules to API/workers. Configure TLS in provider connection URLs/options.
   Migration 0002 creates the vector extension; arrange extension privileges or
   preinstall it with the database administrator. Give the application only required DML permissions after migration setup;
   run Alembic with a separate DDL role in stricter deployments.
4. Have a secret manager render a mode-0600 environment file on the deployment host.
   Required values: ENVIRONMENT=production, DATABASE_URL, REDIS_URL, explicit HTTPS
   ALLOWED_ORIGINS, ANTHROPIC_API_KEY, SEARCH_API_KEY and a random 32+ character
   METRICS_TOKEN. Model choices and budgets remain configurable.
5. Back up the database and rehearse restore on an isolated instance. Set the image
   and secret-file variables, then run the migration once and start services:

```sh
export BACKEND_IMAGE='registry.example/mars-backend@sha256:YOUR_REVIEWED_DIGEST'
export FRONTEND_IMAGE='registry.example/mars-frontend@sha256:YOUR_REVIEWED_DIGEST'
export MARS_SECRET_ENV_FILE='/etc/mars/runtime.env'
docker compose -f deploy/compose.production.yml config --quiet
docker compose -f deploy/compose.production.yml run --rm migrate
docker compose -f deploy/compose.production.yml up -d
docker compose -f deploy/compose.production.yml exec api python -m app.manage create-user --email admin@example.com
```

6. Route HTTPS to loopback port 8080, preserving same-origin `/api` access. Disable
   ingress buffering for SSE, permit >30-second idle timeouts, enforce a 16 KiB
   request-body limit, and configure trusted proxy/edge IP limiting. Set HSTS there.
7. Probe `/ready` privately; log in through HTTPS and verify Secure cookies. Run one
   real research question, close the browser, reopen history, inspect evidence,
   exercise cancellation and verify operator metrics/traces before shifting traffic.
8. Scale workers with `--scale worker=N`; adjust worker concurrency only after load
   measurement. Scale API separately behind the proxy. Sum every process's DB pool
   limit (10 + 10 overflow) against the database connection budget. Session/auth,
   checkpoints, quotas and events are external to the processes.

For a CDN-hosted SPA, proxy `/api` under the same site. Do not put API/provider secrets
in `VITE_*`. Cross-origin API URLs are supported for controlled development, but
production SameSite cookies assume a same-site deployment.

Automatic cloud rollout is deliberately not configured without a chosen host/account
and credentials. Publishing is gated; target deployment steps above are explicit.

## Organization provisioning

Run from `backend/`, or prefix the Python commands with `docker compose exec api`:

```powershell
..\.venv\Scripts\python.exe -m app.manage create-org --name "Research team"
# Use the UUID printed by create-org:
..\.venv\Scripts\python.exe -m app.manage add-member --email you@example.com --organization ORGANIZATION_UUID
..\.venv\Scripts\python.exe -m app.manage remove-member --email you@example.com --organization ORGANIZATION_UUID
```

There is no public organization-administration endpoint. The browser lists current
memberships and requires an explicit audience choice before sharing findings.
Membership removal prevents future shared reads/writes; it does not recall data
previously delivered. Removing an account also removes its authored shared memory.
