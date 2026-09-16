# Conversational Operations Runtime

A self-hostable, API-first control plane for governed AI agent execution.

The model proposes. The runtime decides.

---

## The problem

AI agents that take actions in business systems (payroll, booking, HR, ERP) need a governance layer between the model and the provider. Without it, the model writes directly to production data, approvals are bolted on as ad-hoc logic, and audit trails are afterthoughts.

Most teams build this governance layer from scratch, inside the agent, at each integration point. The logic is duplicated, the audit trail is incomplete, and the human approval flow is specific to one workflow.

This runtime is the governance layer, built once, separate from the model.

---

## Design

```
Client / Agent                Runtime                      Provider
─────────────────    ─────────────────────────────    ─────────────────
                     ┌───────────────────────────┐
model tool call  →   │  argument validation       │
                     │  policy gate               │
                     │  proposal creation         │
                     │  human approval            │ →  commit_correction()
                     │  authorization recheck     │ ←  provider_reference
                     │  idempotent dispatch       │
                     │  unknown-outcome handling  │
                     │  reconciliation            │
                     │  append-only audit log     │
                     └───────────────────────────┘
```

The model proposes an action by calling a tool. The runtime validates the arguments, evaluates the policy, creates a structured proposal for human review, and only creates a command record after a human approves. The command is dispatched to the provider using a stable idempotency key. If the outcome is uncertain (timeout, connection reset), the runtime enters a known reconciliation state rather than retrying blindly.

This is model-neutral. Any agent that can call an HTTP endpoint can use this runtime.

---

## Six resources

| Resource | Who creates it | Meaning |
|----------|---------------|---------|
| `tool_call` | Model (via agent) | A proposed action. Unvalidated at creation. |
| `proposal` | Action Service | Structured, reviewable description of the intended action. |
| `proposal_version` | Action Service | Immutable snapshot of one version of a proposal. Append-only. |
| `approval` | Action Service | A human decision record. |
| `command` | Action Service | An authorized side-effect instruction. Created only after `authorization.passed`. |
| `command_attempt` | Command Worker | One attempt to dispatch a command to the provider. |

A command record does not exist until a human has approved the proposal and the runtime has rechecked that the underlying resource has not changed since prepare. The model cannot authorize its own actions.

---

## Golden event sequence

The primary workflow: model proposal through run completion.

```
run.started
run.message_received
run.tool_proposed
proposal.drafted
policy.evaluated
run.approval_required        ← run pauses here
approval.claimed
approval.approved
authorization.rechecked      ← stale detection happens here
authorization.passed
command.created              ← command only exists after this point
command.dispatched
command.succeeded
run.resumed
run.completed
```

Failure branches (stale approval, unknown outcome, approval expired, approval revised) are in [`spec/golden_sequence.json`](spec/golden_sequence.json).

---

## What this is not

- Not a model SDK. Bring your own agent.
- Not an agent graph framework. Orchestration lives in your agent.
- Not a workflow engine. No DAGs, no cron.
- Not an approval inbox product. The approval console is a thin API client.
- Not an enterprise automation suite. One governed-action loop, done correctly.

---

## Phase 0 artifacts (this repo)

Phase 0 is the executable contract: everything needed to evaluate the design, write a conformance test, or build a client before the server exists.

| Artifact | Purpose |
|----------|---------|
| [`spec/openapi.yaml`](spec/openapi.yaml) | Full API specification (1,924 lines, OpenAPI 3.1) |
| [`spec/schemas/`](spec/schemas/) | JSON Schema Draft 2020-12 for all 6 resources |
| [`spec/events/catalog.yaml`](spec/events/catalog.yaml) | All 20 webhook events with delivery semantics |
| [`spec/golden_sequence.json`](spec/golden_sequence.json) | Primary event sequence + 4 failure branches |
| [`spec/state_machines.md`](spec/state_machines.md) | State transition diagrams for all resources |
| [`spec/error_model.md`](spec/error_model.md) | 13 error codes, conventions, client guidance |
| [`spec/idempotency.md`](spec/idempotency.md) | Key format, window tiers, provider enforcement |
| [`spec/auth_model.md`](spec/auth_model.md) | Tenant isolation, scope definitions, identity flow |
| [`spec/versioning.md`](spec/versioning.md) | Compatibility guarantees, deprecation policy |
| [`spec/threat_model.md`](spec/threat_model.md) | Injection vectors, authorization boundaries |
| [`spec/tool_contracts/payroll.json`](spec/tool_contracts/payroll.json) | Tool argument schemas for the payroll domain |
| [`migrations/001_initial.sql`](migrations/001_initial.sql) | Full Postgres schema with ownership annotations |
| [`adapters/adapter_interface.py`](adapters/adapter_interface.py) | Python Protocol definition + simulated provider |
| [`conformance/test_core_endpoints.py`](conformance/test_core_endpoints.py) | pytest conformance suite (golden sequence integration test) |
| [`client_example.py`](client_example.py) | Minimal client: create run through inspect replay |
| [`spec/examples/curl_example.sh`](spec/examples/curl_example.sh) | End-to-end curl walkthrough |

---

## Quick look: adapter interface

Implement this Protocol to connect the runtime to any provider:

```python
class PayrollProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    async def prepare_correction(
        self, request: PrepareCorrectionRequest
    ) -> CorrectionProposal:
        # Returns structured proposal. No side effects.
        # Safe to call multiple times (stale approval recalculation).
        ...

    async def commit_correction(
        self, request: CommitCorrectionRequest, idempotency_key: str
    ) -> CommitResult:
        # CommitResult.status: "committed" | "rejected" | "duplicate" | "unknown"
        # Return "unknown" on timeout. Do not raise. The runtime reconciles.
        ...

    async def reconcile_correction(
        self, request: ReconcileRequest
    ) -> ReconciliationResult:
        # Called when command.status is "unknown". Not part of normal execution.
        # Query the provider by idempotency_key to resolve the outcome.
        ...
```

Provider capability tiers are declared at startup. Tier 1 (no idempotency): high-risk irreversible actions unavailable by default. Tier 3 (full idempotency + reconciliation): automated reconciliation on unknown outcome.

---

## Quick look: minimal client

```python
from conversational_ops import ConversationalOps   # Phase 1 SDK

client = ConversationalOps(base_url="http://localhost:8080", api_key="...")

run = client.runs.create(agent="payroll-detective")

response = client.runs.send_message(
    run.id,
    "Investigate the payroll anomaly for EMP-4412.",
    idempotency_key=f"msg-{run.id}-001",
)
# 202 Accepted, status: awaiting_approval

if response.status == "awaiting_approval":
    approval = client.approvals.get(response.pending_approval.approval_id)
    client.approvals.claim(approval.id)
    client.approvals.approve(
        approval.id,
        proposal_version=approval.current_version,
    )
```

See [`client_example.py`](client_example.py) for the full walkthrough (raw HTTP, no SDK required).

---

## Status

**Phase 0 (this repo): complete.** Spec, schemas, event catalog, database migration, adapter interface, conformance tests, curl example.

**Phase 1 (in progress):** Agent API, Action Service, Command Worker, PostgreSQL, Claude Sonnet 4.6 as model, SimulatedPayrollProvider, minimal approval console. Exit criteria: one full governed-action loop end-to-end, audit events matching the golden sequence, inspect replay without side effects.

---

## License

Apache 2.0. See [LICENSE](LICENSE).

---

**Neville Ko** — AI Product Manager and Builder  
[Portfolio](https://www.fromus.ca) · [LinkedIn](https://www.linkedin.com/in/nevilleko/)
