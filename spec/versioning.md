# API Versioning

## Version identifier

The current API version is `2026-09-01`. Every response includes the `api_version` field in the response body and the `X-API-Version` header.

```http
X-API-Version: 2026-09-01
```

---

## Version negotiation

Clients may request a specific version by sending the `Accept-Version` header:

```http
Accept-Version: 2026-09-01
```

If the requested version is no longer supported, the runtime returns `400 validation_error` with `details.reason: "unsupported_version"`.

If no version is specified, the runtime uses the current stable version. New deployments default to the latest version.

---

## Compatibility guarantees

Within a version, the runtime guarantees:

- **No removals.** Fields and endpoints are never removed within a version.
- **No breaking changes.** Enum values, required fields, and response shapes are frozen.
- **Additive changes only.** New optional fields may be added. Clients must ignore unknown fields.

A new version is required for:
- Removing a field or endpoint
- Changing the type or semantics of an existing field
- Changing required/optional status of a field
- Removing or renaming an enum value
- Changing error codes or response status codes

---

## Deprecation

Deprecated fields and endpoints are marked in the OpenAPI spec with `deprecated: true` and a `x-deprecation-date` extension. Deprecation notices are communicated at least 90 days before removal.

The runtime logs a warning when a client uses a deprecated endpoint or field. The warning includes the `request_id` so the caller can trace which requests need updating.

---

## Audit events and version

Every audit event includes the `api_version` at the time the event was emitted. This allows replaying a historical run to reconstruct the state as it was seen by the original client, even if the API has evolved since.

---

## Webhook versioning

Webhook events include `api_version` in the envelope. Tenants may pin their webhook delivery to a specific version to avoid breaking changes in their event handlers. Webhook version pinning is configured in the tenant's webhook settings.

---

## Version timeline

| Version | Status | EOL |
|---------|--------|-----|
| `2026-09-01` | Current | — |
