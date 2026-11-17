# Nexa AI Part 3 Android Integration Contract

This document is the backend contract for a future Android client. No Android files were modified in Part 2. The Part 1 notification JSON schema was not available in this workspace; map its actual stable ID/sender/body/timestamp into the request below only after inspecting that project. Do not send more notification data than required.

## Base URL and version

- Local development: `http://<backend-host>:8000`
- Production: `https://<your-api-host>` (HTTPS is required). Processing routes require a valid per-user bearer token in production; keep ingress restrictions as defense in depth and do not ship a shared permanent token in the APK.
- API prefix/version: `/api/v1`
- OpenAPI JSON: `/openapi.json`; interactive docs: `/docs` (disable or protect documentation in deployments if policy requires).

Android emulator localhost mapping may differ from a physical device. A physical phone must reach a deployed/reachable host; production must use HTTPS.

## Authentication

In development, `POST /api/v1/messages/process` and `POST /api/v1/messages/process/batch` allow requests without a bearer token; these use the shared `local-development` account. If a bearer token is supplied, it must be valid and its mapped account is used. In production, all message-processing and retrieval endpoints require:

`Authorization: Bearer <per-user-token>`

The backend maps each configured token to an account scope. Provision tokens securely per user through a suitable authentication/token-issuance system. The backend currently has **no login/token-issuance endpoint**; static token mappings are a bootstrap mechanism, not a production mobile identity solution. Never embed one shared permanent bearer token in the app.

## Submit one message

`POST /api/v1/messages/process` (`Content-Type: application/json`)

```json
{
  "message_id": "stable-part1-message-id",
  "conversation_id": "opaque-stable-contact-conversation-id",
  "sender_name": "Asha",
  "original_message": "Could you review the proposal by 3 PM?",
  "received_at": "2026-01-15T10:00:00Z",
  "response_language": "same as message"
}
```

Fields:

- `message_id`: stable source-event ID, non-whitespace, 1–200 chars.
- `conversation_id`: optional for backward compatibility. For new requests, provide a stable, opaque ID for one WhatsApp conversation. It must remain stable across app restarts, differ between contacts, and never be a display name or a per-message random value.
- Identity for identified messages is `(authenticated user, conversation_id, message_id)`. The same message ID can occur in different conversations. Identified conversation history is isolated by authenticated user and conversation.
- If `conversation_id` is omitted, the request uses the legacy isolated path. It remains compatible with existing clients, does not share context with identified conversations, and receives no backend-derived history. Existing caller-supplied `conversation_context` remains supported only on this legacy path.
- `sender_name`: 1–120 chars.
- `original_message`: 1–5000 chars.
- `received_at`: optional ISO-8601 datetime **with a timezone offset**. The backend normalizes it to UTC. Naïve timestamps are rejected; Android should send the notification-posted timestamp with its actual offset, not the backend receipt time.
- `conversation_context`: optional for legacy requests, max 10 entries with `sender_name` (1–120 chars) and `text` (max 4000 chars). Omit it when `conversation_id` is supplied: the backend builds bounded context only from prior completed incoming messages stored for that exact user/conversation.
- `response_language`: optional preference, max 40 chars. This controls reply language preference; `detected_language` reports the classifier's language estimate.
- Unknown request fields are rejected.

`NEXA_CONVERSATION_CONTEXT_LIMIT` controls the maximum number of prior completed incoming messages added to an identified conversation's AI context (default 10, maximum 10). These records are ordered by backend creation time and message ID. The backend does not know whether suggested responses were actually sent, so they are not added as conversation turns. The backend does not send WhatsApp replies; Android owns send eligibility, sending, and delivery status.

## Response

```json
{
  "message_id": "stable-part1-message-id",
  "conversation_id": "opaque-stable-contact-conversation-id",
  "sender_name": "Asha",
  "original_message": "Could you review the proposal by 3 PM?",
  "classification": "IMPORTANT",
  "priority_score": 0.82,
  "explanation": "A time-sensitive review was requested.",
  "detected_language": "English",
  "requires_response": true,
  "suggested_response": "I’ll review it before 3 PM.",
  "processing_status": "COMPLETED",
  "processing_error": null,
  "received_at": "2026-01-15T10:00:00Z",
  "backend_received_at": "2026-01-15T10:01:00Z",
  "processing_started_at": "2026-01-15T10:01:00Z",
  "ai_response_generated_at": "2026-01-15T10:01:02Z",
  "processing_duration_ms": 2000,
  "created_at": "2026-01-15T10:01:00Z",
  "updated_at": "2026-01-15T10:01:03Z",
  "processed_at": "2026-01-15T10:01:03Z"
}
```

- `classification`: `IMPORTANT` or `NORMAL`. `URGENT` is not currently a backend enum value; clients should retain the existing classification contract and use `priority_score` as the additional ranking signal.
- `priority_score`: numeric 0–1 (higher means greater priority; use as a ranking signal, not a calibrated probability).
- `detected_language`: model-provided short language label (for example English, Telugu, Telugu-English mixed, or unknown); it is not independently verified language identification.
- `suggested_response`: empty if no reply is needed. It is never sent automatically.
- `processing_status`: `PROCESSING`, `COMPLETED`, or `FAILED` in returned API records. `PENDING` is a stored/future state.
- `received_at` is the Android-provided source event timestamp; `backend_received_at` is the first backend claim time. `processing_started_at` is the latest processing attempt start, `processed_at` is completion time, and `processing_duration_ms` is the duration of that completed attempt. `ai_response_generated_at` is null when no reply generation was requested.

## Idempotency and retrieval

- Same user + same `conversation_id` + same `message_id` returns the stored result without another LLM call once completed. A duplicate while work is active may return `PROCESSING`.
- Reusing an ID with changed sender/body in the same scope returns HTTP 409. Same IDs in different identified conversations are independent.
- Legacy requests without `conversation_id` retain per-user/message-ID idempotency and do not receive persisted conversation history.
- Another authenticated user can use the same `message_id` independently.
- List: `GET /api/v1/messages?limit=50&offset=0` (limit 1–100).
- Detail: `GET /api/v1/messages/{message_id}?conversation_id=<id>` for identified messages; omit the query parameter for legacy messages.
- Retry failed record: `POST /api/v1/messages/{message_id}/retry?conversation_id=<id>` for identified messages; omit the query parameter for legacy messages.

## Batch submission

`POST /api/v1/messages/process/batch` with `{"messages":[<request>, ...]}`. The configured maximum is `NEXA_BATCH_SIZE_LIMIT` (default 25); each item consumes a rate-limit unit. Results contain one result per item.

## Health, errors, timeouts, retry

- `GET /api/v1/health`: liveness.
- `GET /api/v1/ready`: database readiness only; generic 503 if unavailable.
- `401`: missing/invalid bearer token in production on processing and protected endpoints; invalid or malformed supplied tokens return 401 in all environments. Missing Authorization on a development processing POST uses `local-development`.
- `413`: batch over configured limit.
- `409`: message ID reused with changed content, retry budget exhausted, or processing lease lost.
- `422`: invalid request payload (sanitized, does not echo message content).
- `429`: caller rate limit or Groq throttling; follow `Retry-After` when supplied.
- `502`: non-retryable upstream/provider or structured-output failure.
- `503`: transient upstream/service failure; follow `Retry-After` when supplied.
- `500`: generic backend failure; no internals are returned.

Client guidance: use a finite HTTP connect/read timeout appropriate to the backend's provider timeout (`NEXA_LLM_TIMEOUT_SECONDS`, default 30 sec) plus processing overhead. Retry only transient 429/503 responses, obey `Retry-After`, and use bounded exponential backoff with jitter. Do not automatically retry 4xx validation/auth errors. Backend per-message attempts are bounded by `NEXA_RETRY_MAX_ATTEMPTS`; duplicates remain idempotent. Persist completed output locally to support offline viewing. Do not retry indefinitely.
