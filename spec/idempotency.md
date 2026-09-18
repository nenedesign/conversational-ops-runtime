# Idempotency

All mutation endpoints require an `Idempotency-Key` header. The runtime deduplicates on this key within the window for the operation type.

---

## How it works

1. Client sends `Idempotency-Key: <uuid>` with any `POST`, `PUT`, or `PATCH` request.
2. Runtime checks the `idempotency_keys` table.
   - First time seen: process normally, store the key and the response status + body.
   - Seen before, same parameters: return the stored response. Set `X-Idempotency-Replayed: true`.
   - Seen before, different parameters: return `409 idempotency_conflict`.
3. After the window expires, the key is removed from the deduplication store. Reusing an expired key starts fresh.

---

## Key format

Use UUIDs (v4). Keys must be:
- Globally unique within the tenant
- Stable across retry attempts for the same logical operation
- Different for semantically different requests (different approvals, different runs, different proposal versions)

Bad: `approve-{approval_id}` (not unique if the approval is revised and re-approved)  
Good: `approve-{approval_id}-v{proposal_version}` (scoped to the exact version being approved)

---

## Window tiers

| Operation type | Window | Notes |
|---------------|--------|-------|
| General mutations (`POST /runs`, `POST /messages`, etc.) | 24 hours | Standard API window |
| Commands (`commit_correction`, provider dispatch) | 7 days | Longer to cover provider reconciliation windows |
| Approval decisions | Run-scoped | Key unique within a run; no expiry until run reaches terminal state |
| Provider-side keys | Provider-declared | The adapter's `max_idempotency_window_hours` governs how long the provider will honor deduplication. The runtime does not retry after this window without reconciling first. |

---

## Idempotency vs. replay

These are different things:

- **Idempotency** prevents duplicate effects on mutation endpoints. It is a client-facing protocol.
- **Replay** (`POST /v1/runs/{run_id}/replay`) re-executes a run against its recorded event log. It uses its own separate idempotency key for the replay request itself.

A replay in `inspect` mode will not trigger provider calls and does not require the provider to support idempotency. A replay in `fork` or `simulate` mode does use provider idempotency if provider calls are made.

---

## Response headers

| Header | Meaning |
|--------|---------|
| `X-Idempotency-Replayed: true` | Response is a replay of a stored result |
| `X-Request-ID: <id>` | Present on all responses (including replayed). Use for tracing. |

---

## `409 idempotency_conflict`

Returned when the same key is received with different request parameters. This is a programming error, not a transient failure. Do not retry.

```json
{
  "error": {
    "code": "idempotency_conflict",
    "message": "Idempotency-Key was already used with different parameters.",
    "request_id": "req_01j9x4c7n8mfv3kp",
    "details": {
      "original_request_id": "req_01j9x0001xyzabc",
      "received_at": "2026-09-01T12:00:00Z"
    }
  }
}
```

---

## Provider idempotency enforcement

The runtime enforces provider-side idempotency via the command `idempotency_key` field. This key is:

- Generated once when the command is created
- Passed to `commit_correction(request, idempotency_key=...)`
- Stable across all retry attempts for that command
- Never reused for a different command

If `commit_correction` returns `CommitResult(status="duplicate")`, the runtime records this as a successful deduplication and does not treat it as an error. The prior `provider_reference` is preserved.

If the idempotency window has expired (beyond `max_idempotency_window_hours`), the runtime will not retry automatically. It will trigger reconciliation to determine whether the operation committed before the window closed.
