# Security boundaries

Accounts are provisioned by the administrator CLI. Argon2id hashes passwords;
high-entropy opaque session tokens are stored only as SHA-256 digests. Production
cookies are Secure, HttpOnly and SameSite=Strict. Password reset and logout revoke
sessions. Use a same-origin frontend/API reverse proxy; cross-site cookie setups
are intentionally unsupported. No public registration or user-supplied identity.

Every job, event, result and cancellation is scoped to the authenticated owner.
Memory access checks owner/session or explicit organization scope and current
membership before lexical or vector ranking. Organization context excludes private
memories, and final shared writes recheck membership under a database lock. Only
the author can delete a shared finding. Organization administration is operator-only.
All production writes use ORM parameters. Worker lease
fences prevent stale writes. Redis enforces user/endpoint, authenticated-IP and
login IP/account limits plus connection caps; PostgreSQL enforces active-job and
daily quotas. Redis failure fails new authenticated requests closed. Backend
request bodies are capped at 16 KiB; nginx enforces the same boundary.

The public source fetcher rejects credentials, private/loopback/link-local/metadata
IP targets, unsafe protocols/ports and unsafe redirects. Its connector validates
the exact DNS addresses it uses, preventing a second DNS lookup from bypassing
checks. Downloads have time/size limits; compressed and unsupported documents are
rejected. This release reads HTML/text, not arbitrary file uploads or executable
documents. Add network-level egress restrictions as a second boundary.

Retrieved text, user input and memories stay in the model's untrusted user content.
System instructions prohibit following embedded commands and require supplied IDs
and quotations. Agents have no shell or arbitrary action tools. This reduces prompt
injection impact but is not a proof against it: misleading sources or model errors
can still influence claims. Deterministic quote checks and the separate verifier
are complementary, not a claim that prompt injection has been eliminated.

React renders text rather than arbitrary HTML; source links allow only HTTP(S)
and use noopener/noreferrer. Nginx sends CSP and anti-sniffing headers. CORS permits
only configured origins. API mutation headers plus origin checks defend against
cookie CSRF. Configure TLS/HSTS at your trusted ingress. The bundled API disables
untrusted proxy-header parsing; behind multiple proxies configure a reviewed trusted
proxy chain and edge per-IP limits rather than trusting arbitrary forwarded headers.

Secrets come from environment/secret management, never frontend Vite variables.
Application logs omit raw provider errors, private source bodies, passwords and query contents;
internal stack locations omit exception messages and locals. Limit operator metrics,
database, Redis and worker metrics to private networks. Search/source caches are
owner-scoped and short-lived. Deleting a user removes durable v2 data; caches expire
within 15 minutes for search/source content and 24 hours for embedding vectors.
Deletion does not synchronously purge those caches; no deleted account can access
them. Backup retention is governed by the operator's policy. Local ONNX inference
does not send memory/query text to an embedding API. Research agents still send
bounded query, evidence and relevant hypotheses to the configured LLM provider.
Default Uvicorn/nginx access logging is disabled to avoid recording memory-search
query strings. Restrict and review ingress/nginx error-log retention separately:
infrastructure errors can include request URLs even with access logging disabled.

Dependencies have pinned direct versions and automated pip-audit/npm audit gates.
Image scans run separately in CI. A clean dependency scan is not a penetration
test. Test authentication, authorization, DNS/redirect handling and network policy
in the target environment before admitting real users.
