# State Machines

State transitions for each of the six primary resources. All transitions are recorded as audit events.

---

## Run

```
created
  → active          (run.started)
  → approval_required  (run.approval_required — model proposed a high-risk action)
  → awaiting_user   (run.awaiting_user — model needs clarification)
  → escalated       (run.escalated — beyond agent authority)

active
  → approval_required
  → awaiting_user
  → escalated
  → completed       (run.completed)
  → failed          (run.failed)
  → cancelled       (run.cancelled)

approval_required
  → active          (run.resumed — after command.succeeded)
  → failed
  → cancelled

awaiting_user
  → active          (after user message received)
  → cancelled

escalated
  → (terminal — human specialist takes over; run is closed)

completed  (terminal)
failed     (terminal)
cancelled  (terminal)
```

---

## Proposal

```
drafted
  → pending_approval   (approval created, requires_approval: true)
  → approved           (policy gate passed, no approval required — policy_outcome: approved)
  → rejected           (policy gate blocked — policy_outcome: rejected)

pending_approval
  → approved           (approval.approved decision recorded)
  → rejected           (approval.rejected decision recorded)
  → cancelled          (run cancelled)

approved   (terminal within proposal lifecycle — command.created follows)
rejected   (terminal)
cancelled  (terminal)
```

Revision does not change the proposal's status — it creates a new `proposal_version` record and resets the associated approval to `pending`.

---

## Proposal version

Proposal versions are immutable. There is no state machine — each version is a point-in-time snapshot created either by the prepare stage or by a human revision.

---

## Approval

```
pending
  → claimed            (reviewer called /claim)
  → approved           (no-claim direct approval — allowed by policy)
  → rejected
  → revised            (reviewer submitted a revision — new proposal_version created, approval returns to pending)
  → expired            (expires_at passed without a decision)
  → cancelled          (run cancelled)

claimed
  → approved           (reviewer called /approve)
  → rejected           (reviewer called /reject)
  → revised            (reviewer called /revise)
  → pending            (/release called — claim dropped)
  → expired
  → cancelled

revised → pending      (immediately — revision resets the approval)

approved   (terminal — triggers authorization.rechecked, then authorization.passed or stale detection)
rejected   (terminal)
expired    (terminal — proposal is recalculated, new approval created)
cancelled  (terminal)
```

---

## Command

Command is only created after `authorization.passed`. It does not exist before that point.

```
created
  → dispatched         (command worker sent request to provider)

dispatched
  → succeeded          (provider confirmed committed — command.succeeded)
  → failed             (provider rejected — command.failed; will not retry)
  → unknown            (timeout, connection reset — command.unknown)

unknown
  → (reconciliation in progress — do not retry)
  → succeeded          (reconciliation.completed with resolution: succeeded)
  → failed             (reconciliation.completed with resolution: failed)
  → unknown            (reconciliation.completed with resolution: still_unknown — manual intervention required)

succeeded  (terminal)
failed     (terminal)
cancelled  (terminal — only from created state, before dispatch)
```

---

## Handoff

```
created
  → completed          (receiving agent confirmed active — handoff.completed)
  → failed             (receiving agent could not accept — run transitions to failed or escalated)
```

---

## State machine invariants

1. A command can only be created from the `authorization.passed` event. There is no path from `proposal.drafted` or `approval.pending` to `command.created`.

2. A run can only reach `completed` after all associated commands reach a terminal state (`succeeded`, `failed`, or `cancelled`).

3. A run can be cancelled from any non-terminal state. Cancellation propagates to open commands (sets `cancelled` if not yet `dispatched`) and open approvals.

4. A stale approval is not a state — it is a transition trigger. When `authorization.rechecked` detects that the resource version has changed since `prepare`, the approval returns to `pending` with a new proposal version. There is no `stale` status on the approval record.

5. Proposal versions are immutable. Revision creates a new version record; the old one is preserved in the audit trail.
