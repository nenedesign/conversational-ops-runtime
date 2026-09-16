# Error Model

All 4xx and 5xx responses use the same envelope:

```json
{
  "error": {
    "code": "validation_error",
    "message": "proposal_version is required",
    "request_id": "req_01j9x4c7n8mfv3kp",
    "details": {
      "field": "proposal_version"
    }
  }
}
```

`request_id` is returned in every response (including 2xx) via the `X-Request-ID` header. Use this when reporting issues or correlating with audit logs.

---

## Error codes

| Code | HTTP status | Meaning |
|------|------------|---------|
| `validation_error` | 400 | Request body or parameters failed schema validation. `details.field` identifies the failing field. |
| `not_found` | 404 | The requested resource does not exist in this tenant's scope. |
| `unauthorized` | 401 | API key missing, expired, or malformed. |
| `forbidden` | 403 | API key is valid but the caller lacks permission for this action. |
| `conflict` | 409 | General resource conflict (e.g. approving a run that is already completed). |
| `idempotency_conflict` | 409 | Same `Idempotency-Key` was sent with different parameters. See [idempotency.md](idempotency.md). |
| `stale_approval` | 409 | The resource changed after the proposal was prepared. The proposal has been recalculated. Re-read the approval and start the review again. |
| `approval_expired` | 409 | The approval window elapsed before a decision was made. The proposal has been recalculated. Re-read the approval and start the review again. |
| `invalid_state_transition` | 409 | The requested state change is not allowed from the current state. `details.current_status` and `details.attempted_transition` are provided. |
| `provider_error` | 502 | The provider returned an unexpected error. May be transient. |
| `unknown_outcome` | 202 or event | Provider outcome is uncertain after timeout or connection reset. The command enters `unknown` status. Do not retry manually. The runtime will reconcile. Subscribe to `reconciliation.completed` to know when it is safe to proceed. |
| `rate_limit_exceeded` | 429 | Too many requests. `Retry-After` header is set. |
| `internal_error` | 500 | Unexpected runtime error. The `request_id` is recorded in system logs. |

---

## Operational errors vs. provider rejections

`provider_error` and `unknown_outcome` are infrastructure-level problems. They are distinct from a provider *rejecting* a valid request (which results in `command.status = "failed"` and a `command.failed` event, not an HTTP error).

A `command.failed` event means the provider understood the request and refused it. `provider_error` means the provider or network behaved unexpectedly.

---

## Details field conventions

The `details` object is code-specific. Documented conventions:

| Code | Fields |
|------|--------|
| `validation_error` | `field`, `reason` |
| `idempotency_conflict` | `original_request_id`, `received_at` |
| `stale_approval` | `approval_id`, `proposal_id`, `new_version` |
| `approval_expired` | `approval_id`, `expired_at` |
| `invalid_state_transition` | `resource_type`, `resource_id`, `current_status`, `attempted_transition` |
| `rate_limit_exceeded` | `limit`, `window`, `retry_after_seconds` |

---

## Client guidance

- Treat `idempotency_conflict` as a programming error, not a transient failure. The client sent the same key with different parameters, which is a bug.
- Treat `stale_approval` and `approval_expired` as expected flows. Re-read the approval and restart the review; the runtime has already recalculated the proposal.
- Treat `unknown_outcome` as a durable state, not an error. Subscribe to `reconciliation.completed` before deciding whether to retry or escalate.
- `internal_error` is always safe to retry with exponential backoff. Include the `request_id` when reporting.
