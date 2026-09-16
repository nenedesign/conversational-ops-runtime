"""
Conversational Operations Runtime — End-to-End Client Example

Demonstrates the full managed run loop from the perspective of an external
client: create a run, send a message, let the agent investigate and propose
a correction, handle human approval, wait for the command worker to dispatch,
resume the run, and retrieve the audit trail.

No SDK — raw HTTP with the requests library.

Prerequisites:
  pip install requests

Usage:
  API_KEY=dev-api-key-001 python3 client_example.py
"""

from __future__ import annotations

import os
import time
import uuid

import requests

BASE_URL = os.environ.get("RUNTIME_URL", "http://localhost:8080")
API_KEY = os.environ.get("API_KEY", "")

if not API_KEY:
    raise SystemExit("Set API_KEY environment variable before running.")

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
})


def ikey(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def poll_command(command_id: str, timeout: int = 60) -> dict:
    """Poll GET /v1/commands/{id} until the command reaches a terminal state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = session.get(f"{BASE_URL}/v1/commands/{command_id}")
        r.raise_for_status()
        cmd = r.json()
        if cmd["status"] in ("succeeded", "failed", "unknown"):
            return cmd
        time.sleep(2)
    raise TimeoutError(f"Command {command_id} did not reach terminal state within {timeout}s")


# ──────────────────────────────────────────────
# Step 1: Create a run
# ──────────────────────────────────────────────
print("Step 1 — Creating run...")
r = session.post(
    f"{BASE_URL}/v1/runs",
    json={"agent_id": "payroll-detective"},
)
r.raise_for_status()
run = r.json()
run_id = run["run_id"]
print(f"  run_id: {run_id}")
print(f"  status: {run['status']}")


# ──────────────────────────────────────────────
# Step 2: Send a message
# ──────────────────────────────────────────────
# The agent investigates EMP-4412 using a read-only tool (no approval needed),
# detects a misclassification, then calls prepare_classification_correction
# (approval required). The run pauses and returns status: awaiting_approval.
# The approval_id is included in the message response — no polling required.
# ──────────────────────────────────────────────
print("\nStep 2 — Sending message...")
r = session.post(
    f"{BASE_URL}/v1/runs/{run_id}/messages",
    json={"content": "Investigate the payroll record for employee EMP-4412 and prepare any necessary corrections."},
)
r.raise_for_status()
msg = r.json()
print(f"  status: {msg['status']}")
if msg.get("message"):
    print(f"  reply:  {msg['message']['content'][:200]}...")

assert msg["status"] == "awaiting_approval", f"Expected awaiting_approval, got {msg['status']}"
approval_id = msg["pending_approval"]["approval_id"]
risk_level   = msg["pending_approval"]["risk_level"]
print(f"  approval_id: {approval_id}")
print(f"  risk_level:  {risk_level}")


# ──────────────────────────────────────────────
# Step 3: Retrieve the approval
# ──────────────────────────────────────────────
# The approval record includes the current proposal version — required when
# submitting the approve decision to detect stale approvals.
# ──────────────────────────────────────────────
print("\nStep 3 — Retrieving approval...")
r = session.get(f"{BASE_URL}/v1/approvals/{approval_id}")
r.raise_for_status()
approval = r.json()
proposal_version = approval["current_proposal_version"]
print(f"  risk_level:        {approval['risk_level']}")
print(f"  proposal_version:  {proposal_version}")
print(f"  requires_approval: {approval['requires_approval']}")


# ──────────────────────────────────────────────
# Step 4: Claim the approval
# ──────────────────────────────────────────────
# Claiming prevents two reviewers from approving simultaneously.
# Optional in low-volume environments; required for multi-reviewer queues.
# ──────────────────────────────────────────────
print("\nStep 4 — Claiming approval...")
r = session.post(
    f"{BASE_URL}/v1/approvals/{approval_id}/claim",
    json={},
    headers={"Idempotency-Key": ikey(f"claim-{approval_id}")},
)
r.raise_for_status()
print(f"  claimed_by: {r.json().get('claimed_by')}")


# ──────────────────────────────────────────────
# Step 5: Human approves the proposal
# ──────────────────────────────────────────────
# proposal_version must match current_proposal_version from Step 3.
# A version mismatch means the proposal was revised while you were reviewing —
# the runtime rejects stale approvals and forces a re-review.
# ──────────────────────────────────────────────
print("\nStep 5 — Approving proposal...")
r = session.post(
    f"{BASE_URL}/v1/approvals/{approval_id}/approve",
    json={
        "proposal_version": proposal_version,
        "approver_note": "Reviewed cited records. Reclassification justified.",
    },
    headers={"Idempotency-Key": ikey(f"approve-{approval_id}-v{proposal_version}")},
)
r.raise_for_status()
approve_result = r.json()
command_id = approve_result["command_id"]
print(f"  approval status: {approve_result['status']}")
print(f"  command_id:      {command_id}")


# ──────────────────────────────────────────────
# Step 6: Wait for the command worker to dispatch
# ──────────────────────────────────────────────
# After approval, the command worker polls the outbox (every 2 seconds,
# FOR UPDATE SKIP LOCKED) and dispatches the commit to the provider adapter.
# Dispatch typically completes within 250ms–2s of being claimed.
# ──────────────────────────────────────────────
print("\nStep 6 — Waiting for command worker to dispatch...")
cmd = poll_command(command_id)
print(f"  command status:       {cmd['status']}")
print(f"  downstream_reference: {cmd['downstream_reference']}")


# ──────────────────────────────────────────────
# Step 7: Resume the run
# ──────────────────────────────────────────────
# POST .../messages with no content (or an empty body) signals a resume.
# The runtime detects that the pending approval's command succeeded,
# feeds the provider reference back to Claude, and returns the final summary.
# ──────────────────────────────────────────────
print("\nStep 7 — Resuming run...")
r = session.post(
    f"{BASE_URL}/v1/runs/{run_id}/messages",
    json={},
)
r.raise_for_status()
final = r.json()
print(f"  status: {final['status']}")
if final.get("message"):
    print(f"  reply:  {final['message']['content'][:300]}...")


# ──────────────────────────────────────────────
# Step 8: Retrieve the full audit event log
# ──────────────────────────────────────────────
# Returns a flat list of RunEventResponse objects ordered by occurred_at.
# A full governed run produces 11 events: tool_call.received through run.completed.
# ──────────────────────────────────────────────
print("\nStep 8 — Fetching audit event log...")
r = session.get(f"{BASE_URL}/v1/runs/{run_id}/events")
r.raise_for_status()
events = r.json()  # list[RunEventResponse]
print(f"  {len(events)} events:")
for i, event in enumerate(events, 1):
    print(f"    [{i:02d}] {event['type']:<40}  producer={event['producer']}")

print("\nDone.")
