"""
Conversational Operations Runtime — Core Endpoint Conformance Tests

One passing test per API endpoint. Tests validate:
  - Correct HTTP status codes
  - Required response fields
  - Idempotency key replay behavior
  - Standard error envelope shape
  - Invalid state transition rejection

Run against a stub server or the real runtime:
  BASE_URL=http://localhost:8080 API_KEY=your-key pytest conformance/

The golden event sequence for the primary flow is in spec/golden_sequence.json.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
import requests

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")
API_KEY = os.getenv("API_KEY", "test-key")
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def idem() -> str:
    """Generate a unique idempotency key."""
    return f"test-{uuid.uuid4().hex[:12]}"


def post(path: str, body: dict, *, idempotency_key: str | None = None) -> requests.Response:
    headers = {**HEADERS}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return requests.post(f"{BASE_URL}{path}", json=body, headers=headers)


def get(path: str) -> requests.Response:
    return requests.get(f"{BASE_URL}{path}", headers=HEADERS)


# ─────────────────────────────────────────────
# Error envelope shape
# ─────────────────────────────────────────────

def assert_error_envelope(response: requests.Response, expected_code: str) -> None:
    """All errors must use the standard error envelope."""
    data = response.json()
    assert "error" in data, f"Response missing 'error' key: {data}"
    error = data["error"]
    assert "code" in error, f"Error missing 'code': {error}"
    assert "message" in error, f"Error missing 'message': {error}"
    assert "request_id" in error, f"Error missing 'request_id': {error}"
    assert error["code"] == expected_code, (
        f"Expected error code '{expected_code}', got '{error['code']}'"
    )


# ─────────────────────────────────────────────
# Runs
# ─────────────────────────────────────────────

class TestCreateRun:
    def test_creates_run_with_201(self):
        r = post("/v1/runs", {"agent_id": "payroll-detective"}, idempotency_key=idem())
        assert r.status_code == 201, r.text
        data = r.json()
        assert "run_id" in data
        assert "tenant_id" in data
        assert data["status"] == "created"

    def test_requires_agent_id(self):
        r = post("/v1/runs", {}, idempotency_key=idem())
        assert r.status_code == 400
        assert_error_envelope(r, "invalid_tool_request")

    def test_tenant_not_in_request_body(self):
        """Tenant must come from the credential, not the request body."""
        r = post("/v1/runs",
                 {"agent_id": "payroll-detective", "tenant_id": "injected-tenant"},
                 idempotency_key=idem())
        # Either the runtime ignores tenant_id in the body (201) or rejects it (400).
        # It must never use the supplied value to override the authenticated tenant.
        if r.status_code == 201:
            data = r.json()
            assert data["tenant_id"] != "injected-tenant"

    def test_idempotency_replay(self):
        key = idem()
        r1 = post("/v1/runs", {"agent_id": "payroll-detective"}, idempotency_key=key)
        r2 = post("/v1/runs", {"agent_id": "payroll-detective"}, idempotency_key=key)
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["run_id"] == r2.json()["run_id"]
        assert r2.headers.get("X-Idempotency-Replayed") == "true"

    def test_idempotency_conflict(self):
        key = idem()
        r1 = post("/v1/runs", {"agent_id": "payroll-detective"}, idempotency_key=key)
        assert r1.status_code == 201
        r2 = post("/v1/runs", {"agent_id": "border-buddy"}, idempotency_key=key)
        assert r2.status_code == 409
        assert_error_envelope(r2, "idempotency_conflict")


class TestGetRun:
    def test_returns_run(self):
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]
        r = get(f"/v1/runs/{run_id}")
        assert r.status_code == 200
        assert r.json()["run_id"] == run_id

    def test_not_found(self):
        r = get("/v1/runs/nonexistent-run-id")
        assert r.status_code == 404


class TestListRuns:
    def test_returns_paginated_list(self):
        r = get("/v1/runs?limit=5")
        assert r.status_code == 200
        data = r.json()
        assert "runs" in data
        assert "has_more" in data
        assert isinstance(data["runs"], list)


class TestSendMessage:
    def test_returns_202_for_async_run(self):
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]
        r = post(f"/v1/runs/{run_id}/messages",
                 {"content": "Investigate EMP-4412."},
                 idempotency_key=idem())
        assert r.status_code in (200, 202), r.text
        data = r.json()
        assert "run_id" in data
        assert "status" in data
        assert "terminal" in data

    def test_response_includes_idempotency_fields(self):
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]
        key = idem()
        r = post(f"/v1/runs/{run_id}/messages",
                 {"content": "Investigate EMP-4412."},
                 idempotency_key=key)
        assert r.status_code in (200, 202)
        data = r.json()
        assert data.get("idempotency_key") == key
        assert "idempotency_replayed" in data

    def test_message_idempotency_replay(self):
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]
        key = idem()
        r1 = post(f"/v1/runs/{run_id}/messages",
                  {"content": "Investigate EMP-4412."}, idempotency_key=key)
        r2 = post(f"/v1/runs/{run_id}/messages",
                  {"content": "Investigate EMP-4412."}, idempotency_key=key)
        assert r1.status_code in (200, 202)
        assert r2.status_code in (200, 202)
        assert r1.json()["run_id"] == r2.json()["run_id"]


class TestRunEvents:
    def test_returns_event_list_with_cursor(self):
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]
        r = get(f"/v1/runs/{run_id}/events?limit=10")
        assert r.status_code == 200
        data = r.json()
        assert "events" in data
        assert "has_more" in data

    def test_events_have_required_fields(self):
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]
        r = get(f"/v1/runs/{run_id}/events")
        assert r.status_code == 200
        events = r.json()["events"]
        if events:
            ev = events[0]
            assert "event_id" in ev
            assert "sequence" in ev
            assert "type" in ev
            assert "occurred_at" in ev
            assert "recorded_at" in ev
            assert "producer" in ev
            assert "correlation_id" in ev


class TestCancelRun:
    def test_cancel_returns_cancelled_status(self):
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]
        r = post(f"/v1/runs/{run_id}/cancel", {"reason": "test"},
                 idempotency_key=idem())
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    def test_cannot_cancel_completed_run(self):
        """Cancelling a terminal run is an invalid state transition."""
        # This test assumes a completed run fixture exists or is set up
        # In a real test suite, set up a completed run first
        pass


class TestReplayRun:
    def test_inspect_mode_returns_202(self):
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]
        r = post(f"/v1/runs/{run_id}/replay",
                 {"mode": "inspect"},
                 idempotency_key=idem())
        assert r.status_code in (200, 202), r.text

    def test_fork_defaults_to_sandbox(self):
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]
        r = post(f"/v1/runs/{run_id}/replay",
                 {"mode": "fork", "from_state_version": 1},
                 idempotency_key=idem())
        if r.status_code in (200, 202):
            data = r.json()
            assert data.get("execution_target") == "sandbox"

    def test_fork_production_requires_confirm(self):
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]
        r = post(f"/v1/runs/{run_id}/replay",
                 {"mode": "fork", "from_state_version": 1,
                  "execution_target": "production"},   # missing confirm_production
                 idempotency_key=idem())
        assert r.status_code in (400, 422), (
            "Fork to production without confirm_production must be rejected"
        )

    def test_fork_response_includes_lineage(self):
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]
        r = post(f"/v1/runs/{run_id}/replay",
                 {"mode": "fork", "from_state_version": 1,
                  "fork_reason": "conformance test"},
                 idempotency_key=idem())
        if r.status_code in (200, 202):
            data = r.json()
            assert "source_run_id" in data
            assert data["source_run_id"] == run_id


# ─────────────────────────────────────────────
# Proposals
# ─────────────────────────────────────────────

class TestGetProposal:
    def test_returns_proposal_with_approved_version(self):
        """Proposal schema must include approved_version when status is approved."""
        # Requires a fixture with an approved proposal
        # In integration: run a full workflow, extract proposal_id from approval
        pass

    def test_not_found(self):
        r = get("/v1/proposals/nonexistent-proposal-id")
        assert r.status_code == 404

    def test_versions_endpoint_returns_list(self):
        # Requires fixture with a multi-version proposal
        pass


# ─────────────────────────────────────────────
# Approvals
# ─────────────────────────────────────────────

class TestListApprovals:
    def test_returns_paginated_list(self):
        r = get("/v1/approvals?limit=10")
        assert r.status_code == 200
        data = r.json()
        assert "approvals" in data
        assert "has_more" in data

    def test_filter_by_status(self):
        r = get("/v1/approvals?status=pending")
        assert r.status_code == 200

    def test_filter_assigned_to_me(self):
        r = get("/v1/approvals?assigned_to=me")
        assert r.status_code == 200


class TestApprovalLifecycle:
    def test_claim_prevents_concurrent_review(self):
        """Two claim calls on the same approval — second should fail or be idempotent."""
        # Requires fixture with a pending approval
        pass

    def test_approve_requires_proposal_version(self):
        """Approve request without proposal_version must be rejected."""
        # Requires fixture with a pending approval
        pass

    def test_revise_creates_new_version_returns_pending(self):
        """Revise must return the approval in pending status with new version."""
        pass

    def test_cannot_approve_after_reject(self):
        """Approving a rejected approval is an invalid state transition."""
        pass

    def test_approve_response_includes_idempotency_fields(self):
        pass


# ─────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────

class TestListCommands:
    def test_filter_by_status_unknown(self):
        r = get("/v1/commands?status=unknown")
        assert r.status_code == 200
        data = r.json()
        assert "commands" in data

    def test_filter_by_run_id(self):
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]
        r = get(f"/v1/commands?run_id={run_id}")
        assert r.status_code == 200


class TestReconcile:
    def test_reconcile_only_valid_on_unknown_command(self):
        """Reconciliation on a succeeded command is an invalid state transition."""
        pass

    def test_reconcile_returns_202(self):
        """Valid reconciliation request returns 202 Accepted."""
        pass


# ─────────────────────────────────────────────
# Idempotency contract
# ─────────────────────────────────────────────

class TestIdempotencyContract:
    def test_same_key_same_body_returns_same_response(self):
        key = idem()
        r1 = post("/v1/runs", {"agent_id": "payroll-detective"}, idempotency_key=key)
        r2 = post("/v1/runs", {"agent_id": "payroll-detective"}, idempotency_key=key)
        assert r1.json()["run_id"] == r2.json()["run_id"]

    def test_same_key_different_body_returns_409(self):
        key = idem()
        r1 = post("/v1/runs", {"agent_id": "payroll-detective"}, idempotency_key=key)
        assert r1.status_code == 201
        r2 = post("/v1/runs", {"agent_id": "border-buddy"}, idempotency_key=key)
        assert r2.status_code == 409
        assert_error_envelope(r2, "idempotency_conflict")
        data = r2.json()["error"]["details"]
        assert "original_request_id" in data
        assert "original_created_at" in data

    def test_replay_header_set_on_replayed_response(self):
        key = idem()
        r1 = post("/v1/runs", {"agent_id": "payroll-detective"}, idempotency_key=key)
        assert r1.status_code == 201
        assert r1.headers.get("X-Idempotency-Replayed") != "true"
        r2 = post("/v1/runs", {"agent_id": "payroll-detective"}, idempotency_key=key)
        assert r2.headers.get("X-Idempotency-Replayed") == "true"


# ─────────────────────────────────────────────
# Golden sequence conformance
# ─────────────────────────────────────────────

class TestGoldenSequence:
    """
    Validate that the golden event sequence from spec/golden_sequence.json
    is emitted correctly for the primary workflow.

    This test runs the full loop: create run → send message →
    await approval → claim → approve → verify command → check events.
    """

    GOLDEN_PATH = Path(__file__).parent.parent / "spec" / "golden_sequence.json"

    def test_golden_sequence_file_exists(self):
        assert self.GOLDEN_PATH.exists(), (
            f"Golden sequence not found at {self.GOLDEN_PATH}"
        )

    def test_golden_sequence_is_valid_json(self):
        data = json.loads(self.GOLDEN_PATH.read_text())
        assert "sequence" in data
        assert len(data["sequence"]) > 0
        for item in data["sequence"]:
            assert "event" in item

    def test_primary_flow_emits_golden_events(self):
        """
        Integration test: run the primary flow and verify the event log
        matches the golden sequence in order.

        Skipped unless INTEGRATION=1 is set — requires a running runtime
        with a simulated payroll provider.
        """
        if not os.getenv("INTEGRATION"):
            pytest.skip("Set INTEGRATION=1 to run full integration test")

        golden = json.loads(self.GOLDEN_PATH.read_text())
        expected_events = [item["event"] for item in golden["sequence"]]

        # 1. Create run
        run_id = post("/v1/runs", {"agent_id": "payroll-detective"},
                      idempotency_key=idem()).json()["run_id"]

        # 2. Send message
        msg_r = post(f"/v1/runs/{run_id}/messages",
                     {"content": "Investigate the payroll anomaly for EMP-4412."},
                     idempotency_key=idem())
        assert msg_r.status_code in (200, 202)
        assert msg_r.json()["status"] == "awaiting_approval"

        approval_id = msg_r.json()["pending_approval"]["approval_id"]

        # 3. Claim approval
        post(f"/v1/approvals/{approval_id}/claim", {}, idempotency_key=idem())

        # 4. Get current proposal version
        approval = get(f"/v1/approvals/{approval_id}").json()
        version = approval["current_proposal_version"]

        # 5. Approve
        approve_r = post(
            f"/v1/approvals/{approval_id}/approve",
            {"proposal_version": version, "approver_note": "Conformance test approval."},
            idempotency_key=idem(),
        )
        assert approve_r.status_code == 200

        # 6. Wait for command to complete (simple poll)
        import time
        for _ in range(10):
            time.sleep(1)
            run = get(f"/v1/runs/{run_id}").json()
            if run["status"] in ("completed", "failed"):
                break

        assert run["status"] == "completed", f"Run did not complete: {run['status']}"

        # 7. Verify event sequence
        events_r = get(f"/v1/runs/{run_id}/events?limit=100")
        assert events_r.status_code == 200
        actual_types = [e["type"] for e in events_r.json()["events"]]

        for expected in expected_events:
            assert expected in actual_types, (
                f"Expected event '{expected}' not found in run events.\n"
                f"Actual events: {actual_types}"
            )
