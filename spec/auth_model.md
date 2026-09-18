# Auth Model

## Tenant isolation

Every tenant is a separate data boundary. Tenants are identified by API key; the key encodes the tenant scope. There is no `tenant_id` parameter on any client-facing endpoint.

Attempting to access a resource from another tenant returns `404 not_found`, not `403 forbidden`. This prevents tenant enumeration.

---

## API key scopes

Keys are issued per tenant and carry one or more scopes:

| Scope | Permitted operations |
|-------|---------------------|
| `runs:write` | Create runs, send messages, cancel runs |
| `runs:read` | Read runs, run events, replay (inspect mode) |
| `approvals:decide` | Claim, approve, reject, revise approvals |
| `approvals:read` | Read approvals and proposal versions |
| `commands:read` | Read commands and command attempts |
| `replay:fork` | Create sandbox and production forks |
| `admin` | Tenant configuration, user management, policy management |

Most integrations need `runs:write`, `runs:read`, `approvals:decide`, and `approvals:read`. Restrict `replay:fork` and `admin` to internal tooling.

---

## Identity flow

```
Client API key
  → Agent API validates key, resolves tenant_id + user_id
  → Agent API issues authenticated context (tenant_id, user_id, scopes)
  → Action Service reads context from authenticated request
  → Action Service never accepts tenant_id from the request body
```

The Action Service trusts the identity context established by the Agent API. It does not re-authenticate directly against the user store.

---

## User identity in approvals

Approval decisions (`/approve`, `/reject`, `/revise`, `/claim`) require the `approvals:decide` scope. The `approver_id` recorded in the approval record is the user identity from the API key, not a value the client supplies in the request body.

If a key with `approvals:decide` is used in an automated pipeline (rather than by a human approver), this is a misconfiguration. The approval record will reflect the automated identity, which is visible in the audit log.

---

## Human-in-the-loop enforcement

Requiring approval (`requires_approval: true`) means a key with `approvals:decide` must make a `POST /approvals/{id}/approve` call before the command is created. The runtime cannot be configured to automatically approve on behalf of a human.

Bypassing this by granting a service account the `approvals:decide` scope is a tenant-level policy decision. It is recorded in the audit log.

---

## Webhook signatures

Outbound webhooks are signed with HMAC-SHA256 over the raw request body using the tenant's webhook secret. Verify the signature before processing any webhook:

```python
import hashlib, hmac

def verify_webhook(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

The signature is in `X-Webhook-Signature`. The timestamp is in `X-Webhook-Timestamp`. Reject events where the timestamp is more than 300 seconds old.

---

## Tenant configuration

Tenant-level policies (which tool calls require approval, which are blocked, idempotency window overrides, Tier 1 compensating controls) are managed via `PUT /v1/config/policies` with an `admin` key. Policy versions are recorded in `policy_versions` and referenced in every proposal version.

---

## Service-to-service authentication

The Agent API and Action Service communicate over authenticated internal channels. The transport authentication is deployment-specific (mTLS, HMAC header, JWT) and is not part of the public API contract. Each service validates that the request originates from the expected peer before processing.
