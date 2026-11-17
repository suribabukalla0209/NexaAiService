# Nexa AI WhatsApp Intelligence Backend

This standalone FastAPI service validates WhatsApp notification records, uses Groq's OpenAI-compatible API to classify each message and (when needed) draft a reply, and persists the analysis through SQLAlchemy. SQLite remains the local-development default; PostgreSQL is supported with `NEXA_DATABASE_URL`. It does **not** send WhatsApp messages. The workspace did not include the existing Android app or its JSON schema, so the API contract below is an explicit initial contract and must be reconciled with Part 1 before Android integration.

## Requirements and startup (Windows PowerShell)

1. Open a terminal in `backend/`.
2. Create and activate a virtual environment, then install dependencies:

   ```powershell
   py -3.13 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Configure local credentials:

   ```powershell
   Copy-Item .env.example .env
   ```

   Edit `.env` and set `NEXA_LLM_API_KEY` to a Groq API key. Keep it on the backend; never put it in the Android app. The example configures Groq's OpenAI-compatible API URL and model. There is no fake/in-memory production AI fallback. Requests without provider credentials fail safely and are marked `FAILED`; production startup rejects missing credentials.

   Set `NEXA_ACCESS_TOKENS_JSON` to a JSON object mapping a long, unique bearer token to a stable account ID, for example `{"random-high-entropy-token":"android-user-1"}`. Provision a separate token for each account. Processing POST routes allow anonymous access only in development; those requests use the shared `local-development` scope. Production requires valid bearer tokens. Do not commit `.env` or use example credentials. Production deployments must terminate TLS/HTTPS in a trusted reverse proxy and protect the token store.

For production, set `NEXA_ENVIRONMENT=production`. Startup validates the provider URL/model, requires a non-placeholder LLM credential, requires a token map with tokens at least 32 characters long, and rejects non-HTTPS provider URLs. Configuration errors are sanitized before being raised. Other supported controls are `NEXA_RATE_LIMIT_REQUESTS` (default 60), `NEXA_RATE_LIMIT_WINDOW_SECONDS` (default 60), `NEXA_RETRY_MAX_ATTEMPTS` (default 3 per message), `NEXA_RETRY_BASE_DELAY_SECONDS` (default 5), `NEXA_RETRY_MAX_DELAY_SECONDS` (default 300), and `NEXA_MESSAGE_RETENTION_DAYS` (default 90).

For PostgreSQL cloud use, set `NEXA_DATABASE_URL` to a TLS-enabled URL such as `postgresql+psycopg://USER:PASSWORD@HOST:5432/DB?sslmode=require` using a deployment secret store. Production mode requires PostgreSQL and an `sslmode` of `require`, `verify-ca`, or `verify-full`; it rejects SQLite. SQLite remains the local-development default through `NEXA_DATABASE_PATH`.

4. Start from the `backend/` directory:

   ```powershell
   uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

   For local development with reload: `uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`.

   OpenAPI docs are at `/docs`; unauthenticated health check is `/api/v1/health`.

   `GET /api/v1/ready` checks the configured database and returns only `{"status":"ready"}` or a generic 503 response. The health endpoint remains a liveness check.

## API contract

Development permits anonymous processing requests in the shared `local-development` scope. In production, processing and protected message endpoints require a valid configured bearer token; malformed or invalid supplied tokens return 401 in every environment. Health/readiness remain unauthenticated. The backend has no login/token-issuance endpoint; token provisioning is through configured bearer-token mappings. Keep ingress restrictions as defense in depth and do not deploy a shared permanent mobile token.

### Input record

`POST /api/v1/messages/process`

```json
{
  "message_id": "stable-source-id",
  "conversation_id": "opaque-stable-contact-conversation-id",
  "sender_name": "Asha",
  "original_message": "Could you review the proposal by 3 PM?",
  "received_at": "2026-01-15T10:00:00Z",
  "response_language": "same as message"
}
```

`message_id`, `sender_name`, and `original_message` are required. `conversation_id` is optional for existing Android clients; new clients must provide a stable, opaque ID per WhatsApp conversation, never a sender display name or a per-message random value. Identified message identity is `(user_id, conversation_id, message_id)`. Requests without `conversation_id` remain in an isolated legacy path, keep per-user/message-ID idempotency, and do not share persisted history with identified conversations. `received_at` is an optional ISO-8601 datetime with a timezone offset; it is normalized to UTC and naïve input is rejected. Legacy `conversation_context` remains supported only when `conversation_id` is omitted. For identified conversations the backend builds context from prior completed incoming messages in that user/conversation; callers must omit `conversation_context`. `NEXA_CONVERSATION_CONTEXT_LIMIT` controls the maximum history size (default 10, max 10). `response_language` is optional (defaults to `same as message`) and 2–40 characters. Extra/unknown fields are rejected.

On successful HTTP 200 processing, the response follows the existing `MessageResult` model (all fields are present; nullable fields may be `null`):

```json
{
   "message_id": "stable-source-id",
   "conversation_id": "opaque-stable-contact-conversation-id",
   "sender_name": "Asha",
   "original_message": "Could you review the proposal by 3 PM?",
   "classification": "IMPORTANT",
   "priority_score": 0.92,
   "explanation": "The message requests a time-sensitive review.",
   "detected_language": "English",
   "requires_response": true,
   "suggested_response": "I’ll review it by 3 PM.",
   "processing_status": "COMPLETED",
   "processing_error": null,
   "received_at": "2026-01-15T10:00:00Z",
   "backend_received_at": "2026-01-15T10:00:01Z",
   "processing_started_at": "2026-01-15T10:00:01Z",
   "ai_response_generated_at": "2026-01-15T10:00:02Z",
   "processing_duration_ms": 1000,
   "created_at": "2026-01-15T10:00:01Z",
   "updated_at": "2026-01-15T10:00:02Z",
   "processed_at": "2026-01-15T10:00:02Z"
}
```

The values above illustrate the DTO; classification, language, explanation and suggested reply are generated by the configured Groq model. A duplicate in-progress request may return HTTP 200 with `processing_status: "PROCESSING"`; a previously completed duplicate returns its saved result. Language is model-estimated, not independently verified. The backend classification enum remains `IMPORTANT` or `NORMAL`; `URGENT` is not currently represented. `processing_started_at` is the latest attempt start, `processed_at` is completion time, `processing_duration_ms` is that attempt's duration, and `ai_response_generated_at` is null when no reply was generated. The database also supports `PENDING` for future queued processing.

Processing errors use FastAPI's `{"detail":"..."}` format: 401 for missing/invalid authentication in production or invalid/malformed supplied credentials in any environment, 409 for conflicting message IDs or retry/lease conflicts, 422 with `{"detail":"Invalid request payload"}` for invalid JSON fields, 429 for rate limits/cooldowns (with `Retry-After`), 502 for non-retryable provider failures, 503 for retryable provider failures (with `Retry-After`), and a sanitized 500 for unexpected processing errors. Missing Authorization on a development processing request uses the shared `local-development` scope. Provider failures are recorded as `FAILED`; the response does not expose provider details or credentials. Batch requests return `{"results":[...]}`; item processing failures are represented by each result's existing status/error fields where possible.

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/health` | Liveness check (no auth) |
| GET | `/api/v1/ready` | Database readiness check (no auth; generic 503 on failure) |
| POST | `/api/v1/messages/process` | Process one record; returns 409 if an ID is reused for different source content; provider errors return an appropriate safe 429/502/503 and persist `FAILED` |
| POST | `/api/v1/messages/process/batch` | Process a batch sequentially; batch size is limited by `NEXA_BATCH_SIZE_LIMIT`; per-message failures are returned in results |
| GET | `/api/v1/messages?limit=50&offset=0` | List this authenticated user's processed records |
| GET | `/api/v1/messages/{message_id}?conversation_id=<id>` | Get a record for the authenticated user; omit `conversation_id` for legacy records |
| POST | `/api/v1/messages/{message_id}/retry?conversation_id=<id>` | Retry a saved failed record; omit `conversation_id` for legacy records |

Processing is idempotent on `(account scope, conversation_id, message_id)` for identified conversations and `(account scope, message_id)` for legacy requests. Repeated requests with identical sender and source text in that scope return the saved result without another LLM call. Reuse of an ID with changed sender/text in the same scope returns 409. A lease expiry can still cause another AI provider call, but fencing prevents stale workers from replacing the current result. Message bodies, bearer tokens, and provider response bodies are not written to logs.

Part 3 client contract and sample response: [ANDROID_API_INTEGRATION.md](ANDROID_API_INTEGRATION.md). Production setup and outstanding deployment checks: [PRODUCTION_DEPLOYMENT_GUIDE.md](PRODUCTION_DEPLOYMENT_GUIDE.md). Current audit evidence and blockers: [PART2_COMPLETION_REPORT.md](PART2_COMPLETION_REPORT.md).

Requests use per-account fixed-window quotas stored in the selected database. Anonymous processing shares the `local-development` quota. The default is 60 units per 60 seconds; batch requests consume one unit per message. A rejected request returns 429 with `Retry-After`. PostgreSQL deployments share quotas across application instances using the same database; SQLite quotas are only shared by workers using the same database file.

Each `PROCESSING` claim has a random fencing token and a lease. `NEXA_PROCESSING_LEASE_SECONDS` configures the lease duration (default `180` seconds). A heartbeat renews a live worker's lease while classification/reply calls are running. Claims and lease recovery use a SQLite immediate transaction locally and row locks/unique-conflict handling in PostgreSQL; expired or legacy lease-less claims can be reclaimed by a subsequent request. Completion and failure writes require the current token and an unexpired lease, so an expired worker cannot overwrite the replacement worker's result. The Groq/LLM calls are outside database write transactions. There is no background retry loop: recovery happens only when a request is made, and `FAILED` messages remain explicitly retryable. Concurrent duplicates during a live claim receive the current `PROCESSING` record rather than waiting for its final result.

Transient Groq 429/5xx and network/timeout failures are retryable. The API preserves upstream `Retry-After` where provided and returns 429 for throttling or 503 for other transient upstream failures. Per-message attempts are bounded; cooldown grows exponentially and is capped by `NEXA_RETRY_MAX_DELAY_SECONDS`. Other provider 4xx responses and invalid structured output are treated as permanent. There is no automatic retry worker; a caller must retry after the supplied cooldown, and a message is no longer retried after its configured attempt budget is exhausted.

Message retention is configurable by `NEXA_MESSAGE_RETENTION_DAYS` (default 90 days since last update). Expired records are purged at startup and daily; unexpired `PROCESSING` leases are retained. Configure encrypted storage and restricted DB access. SQLite backups must be WAL-aware (online backup API or coordinated WAL snapshot); PostgreSQL backups must use the provider's consistent snapshot/PITR facility. Encrypt backups, control access, define retention/deletion handling, and perform restore drills. The application does not encrypt message contents itself or create backups.

PostgreSQL support is additive; SQLite-to-PostgreSQL copying is a separate operator action and does not modify the SQLite source. PostgreSQL uses pooled SQLAlchemy connections, Alembic startup migrations, row-level claim locks, and database-shared quotas. No public deployment was performed. Cloud manifests, TLS ingress, managed secret store, automated backups, and Android-trigger validation are not included. Do not scale multiple app instances against separate SQLite files; use a single durable local SQLite instance or PostgreSQL for shared cloud state.

## PostgreSQL schema and SQLite data migration

The app runs Alembic migrations during startup. To run migrations explicitly from `backend/` after setting the database environment, use `python -m app.tools.migrate_schema`. Revision `0003_conversations_and_timestamps` preserves existing rows and columns while changing message uniqueness. On SQLite it uses Alembic batch table recreation to replace the primary key; the migration backfills existing rows into per-message legacy identities. Its downgrade is intentionally a no-op because removing the new identity could lose contact separation. Back up the database before upgrading.

The SQLite-to-PostgreSQL migration utility preserves the conversation key when present. For older SQLite schemas it creates the same isolated legacy identity. Historical SQLite datetime values that no longer carry offsets are interpreted as UTC by that utility; review the source data and this explicit compatibility policy before migrating.

Migration from SQLite to PostgreSQL is an explicit operator action. First take and verify an encrypted, WAL-aware SQLite backup, provision an empty TLS-enabled PostgreSQL database, and set `NEXA_DATABASE_URL` through a secret manager. Run a dry run and review the printed record counts (no message content or DSN is printed):

```powershell
python -m app.tools.migrate_sqlite_to_postgres --source .\data\nexa_ai.db --dry-run
python -m app.tools.migrate_sqlite_to_postgres --source .\data\nexa_ai.db
```

The migration utility applies the Alembic schema, copies message and rate-limit rows without overwriting conflicting target keys, validates source message counts and a content digest, and never modifies/deletes the SQLite source. Run it while the app is stopped or source writes are quiesced. Re-run after resolving any integrity conflict; imports are idempotent for matching records. For rollback, stop writes and switch configuration back to the untouched SQLite snapshot only if PostgreSQL has received no new writes. If PostgreSQL has accepted writes, first reconcile/export those writes back into SQLite; this phase does not implement a reverse synchronization utility.

PostgreSQL integration tests use a real server selected by `NEXA_TEST_POSTGRES_URL` and must point to a disposable test database:

```powershell
# Enter the disposable Neon direct (not pooler) URL locally at the secure prompt.
$secret = Read-Host "Disposable Neon test DSN" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try { $env:NEXA_TEST_POSTGRES_URL = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); Remove-Variable secret }
& .\.venv\Scripts\python.exe -m pytest -q -m postgres -rs
```

The test module accepts only the `postgresql+psycopg://` driver, a direct Neon hostname ending in `.neon.tech` (it rejects `-pooler.` hosts), `sslmode=require` or stronger, and the exact database name `nexa_test`. It reads only `NEXA_TEST_POSTGRES_URL`, not `NEXA_DATABASE_URL`, and never prints the URL. Each integration case creates a uniquely named, validated `nexa_test_<uuid>` schema and drops only that schema in a `finally` block after the case completes or fails. `DROP SCHEMA ... CASCADE` can remove only objects inside the exact schema successfully created by that same case (including its Alembic version table and synthetic test tables/rows). The helper never targets `public`, database-level objects, or any schema name not matching its strict generated-name format. If schema creation finds an existing schema, it is not marked owned and is never dropped. If schema cleanup fails after a test failure, an exception group reports both failures; if cleanup alone fails, that cleanup error is raised. On Windows without Docker, install PostgreSQL locally using the official Windows installer, start its local service, and create an isolated database/owner from `psql` connected to the administrative `postgres` database:

```sql
CREATE ROLE nexa_test LOGIN;
```

Then use `\password nexa_test` in `psql` to set a temporary test-only password and create its disposable database:

```sql
CREATE DATABASE nexa_test OWNER nexa_test;
```

Use the resulting local credentials only in the current PowerShell session. URL-encode any reserved characters in the password. The test suite requires the exact database name `nexa_test`; only its own uniquely created schemas are removed after the test. Do not point this variable at production or any database with valuable data. Integration tests skip when the DSN is absent/invalid for test use. SQLite tests do not establish PostgreSQL support.

## Provider configuration

`NEXA_LLM_BASE_URL`, `NEXA_LLM_MODEL`, `NEXA_LLM_API_KEY`, and `NEXA_LLM_TIMEOUT_SECONDS` configure an OpenAI-compatible Chat Completions provider supporting strict JSON Schema response format. Classification and reply drafting use separate structured LLM requests. Provider changes can be isolated in `app/services/llm_provider.py`. Message/context are passed as untrusted data; prompts explicitly prohibit following embedded instructions or performing external actions. Replies are suggestions only.

## Tests

From `backend/` run:

```powershell
pytest -q
```

The test suite uses an explicit deterministic test double for classification, isolated SQLite databases, and a mocked provider transport. Production code has no fake-response path.

## Android integration status / limitations

No Android source, notification JSON sample, or Gradle project was present in the workspace at implementation time. Thus the backend uses the documented DTO above and cannot yet guarantee compatibility with the actual `whatsapp.json`. No Kotlin API/Room/WorkManager/Compose changes, app build, APK, or physical-device validation are included. Before integrating, map the actual Part 1 stable identifier, sender, message body, and timestamp into this DTO; inspect existing local persistence and navigation; configure Retrofit and per-user auth; and implement background sync plus local analysis persistence without changing notification capture. Use the Android emulator's host mapping or a secured HTTPS endpoint as appropriate; never ship a shared production bearer token in the APK.
