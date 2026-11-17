# Nexa AI Backend Production Deployment Guide

**Status: guidance only. Nothing here has been deployed or verified in a live cloud environment.** Production readiness is not claimed. Keep Groq and PostgreSQL credentials in a platform secret manager; never commit them or place them in Android source.

**Authentication requirement:** Processing routes permit anonymous requests only when `NEXA_ENVIRONMENT=development`; those requests share the `local-development` account scope. In production, processing and protected message routes require a valid configured per-user Bearer token and reject missing, malformed, or invalid credentials. Keep ingress restrictions and rate limits as defense in depth. Do not ship a shared permanent bearer token in the Android application.

## Required production configuration

Run from the backend package, with a supported Python runtime and dependencies installed from `requirements-prod.txt`:

- `NEXA_ENVIRONMENT=production`
- `NEXA_DATABASE_URL=postgresql+psycopg://.../DATABASE?sslmode=verify-full` (TLS-enabled provider URL; do not use SQLite in production).
- `NEXA_LLM_BASE_URL=https://api.groq.com/openai/v1`
- `NEXA_LLM_MODEL=<provider-supported-model>`
- `NEXA_LLM_API_KEY=<Groq key from secret manager>`
- `NEXA_ACCESS_TOKENS_JSON=<secret-managed token-to-user mapping>` for current static-token bootstrap only.
- Tune rate limit (`NEXA_RATE_LIMIT_REQUESTS`, `NEXA_RATE_LIMIT_WINDOW_SECONDS`), retry limits/backoff, leases, and retention for service capacity and policy.

Configuration validates production secrets, PostgreSQL and TLS mode, provider URL/model, and numeric limits. Configuration errors are sanitized. Do not copy real credentials into `.env.example`.

## Startup and schema migration

Recommended controlled release sequence:

1. Provision the PostgreSQL database with TLS, least-privilege role(s), network restrictions, encrypted backups and monitoring.
2. Load secrets into the deployment environment without logging them.
3. Run the additive Alembic migration once as a controlled release step: `python -m app.tools.migrate_schema`.
4. Verify `/api/v1/ready` and `/api/v1/health` from the private deployment network.
5. Configure ingress/firewall controls and verify production bearer authentication on processing routes, then start the ASGI service bound to a private interface behind the TLS-terminating proxy, for example `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers <capacity-reviewed-count>`.
6. Configure request/body limits, upstream timeouts, access logging that omits Authorization and request bodies, metrics/alerts, and graceful shutdown. Do not proceed if the processing paths are externally reachable.

The app currently also applies Alembic migrations during lifespan startup (serialized with a PostgreSQL advisory lock). Coordinating migration as a release step is still recommended; do not run destructive DDL.

## Security and operations checklist

- Rotate credentials exposed during development; revoke old Groq and bearer credentials.
- Prefer a real user authentication/token lifecycle before shipping Android. The current static bearer map has no login, expiry, or automated revocation endpoint.
- Keep API TLS mandatory; restrict CORS at an ingress if a browser client is introduced. Native Android does not rely on browser CORS. This backend currently does not configure a CORS allowlist or trusted-host middleware.
- Ensure reverse proxy redacts Authorization, cookies, URL query secrets, and message bodies from logs/traces.
- Restrict database role to application schema/data operations. Restrict PostgreSQL network access and require certificate-validated TLS (`verify-full` preferred where supported).
- Define privacy retention, data deletion, legal hold and backup expiration policies. The app purges stale message records; it does not encrypt message fields itself or make backups.
- Use provider-supported limits and quotas; observe 429/5xx rates and cost. Current DB-backed rate limits are shared only by processes connected to the same PostgreSQL database.
- Test backup restoration and disaster recovery before launch. Capture schema version, request/error rates, queue/lease backlog indicators, DB pool saturation, and provider health without logging private message content.
- Perform SAST/dependency vulnerability review, secret scanning, load testing, failover testing, and external security review; these have not been done here.

## Deployment blockers / evidence gaps

- No cloud provider, deployment manifests, TLS ingress, secret-manager integration, CI/CD workflow, or production backup/restore test exists in this workspace.
- No production identity/token issuance flow; static bearer credentials are not an adequate final mobile auth design.
- No real Groq call was performed during this work because replacement credentials were unavailable/unsafe to use.
- Neon PostgreSQL tests require a test-only DSN and explicit owner approval because they create isolated test schemas. No production data migration or deployment is authorized by this guide.
- The Android project was not available; Android processing trigger and end-to-end contract compatibility remain unverified.

Do not label the service production-ready until these blockers are addressed and deployment, security, provider, PostgreSQL, backup/restore, and Android integration checks are evidenced.
