"""
Conversational Operations Runtime — Minimal Client Example

Demonstrates the primary governed-action loop from the perspective of an
external developer connecting their agent to the runtime. No SDK — raw
HTTP with the requests library.

Prerequisites:
  pip install requests

Usage:
  API_KEY=your-api-key python3 client_example.py

This example:
  1. Creates a run
  2. Sends a message triggering the payroll detective agent
  3. Polls until the run reaches approval_required
  4. Approves the proposal
  5. Polls until the run completes
  6. Retrieves the audit event log
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


def idempotency_key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def poll_run(run_id: str, until_status: set[str], timeout: int = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = session.get(f"{BASE_URL}/v1/runs/{run_id}")
        resp.raise_for_status()
        run = resp.json()
        if run["status"] in until_status:
            return run
        time.sleep(1)
    raise TimeoutError(f"Run {run_id} did not reach {until_status} within {timeout}s")


# ──────────────────────────────────────────────
# Step 1: Create a run
# ──────────────────────────────────────────────
print("→ Creating run...")
resp = session.post(
    f"{BASE_URL}/v1/runs",
    json={"agent_id": "payroll-detective"},
    headers={"Idempotency-Key": idempotency_key("run")},
)
resp.raise_for_status()
run = resp.json()
run_id = run["run_id"]
print(f"  run_id: {run_id}")
print(f"  status: {run['status']}")


# ──────────────────────────────────────────────
# Step 2: Send a message
# ──────────────────────────────────────────────
print("\n→ Sending message...")
msg_key = idempotency_key("msg")
resp = session.post(
    f"{BASE_URL}/v1/runs/{run_id}/messages",
    json={"content": "Investigate the payroll anomaly for EMP-4412."},
    headers={"Idempotency-Key": msg_key},
)
resp.raise_for_status()
msg_response = resp.json()
print(f"  status: {msg_response['status']}")  # 202 Accepted → awaiting_approval


# ──────────────────────────────────────────────
# Step 3: Poll until approval is required
# ──────────────────────────────────────────────
print("\n→ Waiting for approval_required...")
run = poll_run(run_id, until_status={"approval_required", "completed", "failed"})
print(f"  status: {run['status']}")

if run["status"] != "approval_required":
    print(f"  Run ended with status: {run['status']} — no approval needed.")
    exit(0)


# ──────────────────────────────────────────────
# Step 4: Retrieve the pending approval
# ──────────────────────────────────────────────
print("\n→ Retrieving pending approval...")
approval_id = run["pending_approval"]["approval_id"]
resp = session.get(f"{BASE_URL}/v1/approvals/{approval_id}")
resp.raise_for_status()
approval = resp.json()
proposal_version = approval["current_proposal_version"]
print(f"  approval_id: {approval_id}")
print(f"  proposal_version: {proposal_version}")
print(f"  risk_level: {approval['risk_level']}")


# ──────────────────────────────────────────────
# Step 5: Claim the approval
# ──────────────────────────────────────────────
print("\n→ Claiming approval...")
resp = session.post(
    f"{BASE_URL}/v1/approvals/{approval_id}/claim",
    json={},
    headers={"Idempotency-Key": idempotency_key(f"claim-{approval_id}")},
)
resp.raise_for_status()
print(f"  claimed: {resp.json().get('claimed_by')}")


# ──────────────────────────────────────────────
# Step 6: Approve
# ──────────────────────────────────────────────
print("\n→ Approving proposal...")
resp = session.post(
    f"{BASE_URL}/v1/approvals/{approval_id}/approve",
    json={
        "proposal_version": proposal_version,
        "approver_note": "Reviewed cited records.",
    },
    headers={"Idempotency-Key": idempotency_key(f"approve-{approval_id}-v{proposal_version}")},
)
resp.raise_for_status()
print(f"  result: {resp.json().get('status')}")


# ──────────────────────────────────────────────
# Step 7: Poll until completed
# ──────────────────────────────────────────────
print("\n→ Waiting for run to complete...")
run = poll_run(run_id, until_status={"completed", "failed", "cancelled"}, timeout=120)
print(f"  final status: {run['status']}")
if run["status"] == "completed":
    print(f"  summary: {run.get('summary', '(none)')}")


# ──────────────────────────────────────────────
# Step 8: Retrieve the full audit event log
# ──────────────────────────────────────────────
print("\n→ Fetching audit event log...")
resp = session.get(f"{BASE_URL}/v1/runs/{run_id}/events", params={"limit": 50})
resp.raise_for_status()
events = resp.json()["events"]
print(f"  {len(events)} events recorded:")
for event in events:
    print(f"    [{event['sequence']:02d}] {event['type']}")


# ──────────────────────────────────────────────
# Step 9: Replay in inspect mode (no side effects)
# ──────────────────────────────────────────────
print("\n→ Replaying in inspect mode...")
resp = session.post(
    f"{BASE_URL}/v1/runs/{run_id}/replay",
    json={"mode": "inspect"},
    headers={"Idempotency-Key": idempotency_key(f"replay-{run_id}-inspect")},
)
resp.raise_for_status()
replay = resp.json()
print(f"  replayed_run_id: {replay['run_id']}")
print(f"  mode: {replay['mode']}")

print("\nDone.")
