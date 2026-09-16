"""
Conversational Operations Runtime — Conformance Test Suite

Validates the live runtime against its own contract. Two tiers:

  Unit-style (no INTEGRATION flag): verify HTTP status codes, response shapes,
  and error envelopes against the running server. No Anthropic API calls.
  Run with:
      BASE_URL=http://localhost:8080 API_KEY=dev-api-key-001 pytest conformance/

  Integration (INTEGRATION=1): end-to-end workflow tests that call Claude
  Sonnet 4.6. Requires ANTHROPIC_API_KEY in the runtime's .env.
  Run with:
      BASE_URL=http://localhost:8080 API_KEY=dev-api-key-001 INTEGRATION=1 pytest conformance/

Endpoints under test match Phase 0-4 implementation. Endpoints planned but
not yet implemented are marked skip with a note.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest
import requests

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")
API_KEY  = os.getenv("API_KEY", "dev-api-key-001")
HEADERS  = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

AGENT_A      = "payroll-agent-a"
AGENT_B      = "border-agent-a"
TEST_EMPLOYEE = "EMP-4412"


def idem() -> str:
    return f"test-{uuid.uuid4().hex[:12]}"


def post(path: str, body: dict, *, idempotency_key: str | None = None) -> requests.Response:
    headers = {**HEADERS}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return requests.post(f"{BASE_URL}{path}", json=body, headers=headers)


def get(path: str) -> requests.Response:
    return requests.get(f"{BASE_URL}{path}", headers=HEADERS)


def assert_error_envelope(r: requests.Response, expected_code: str) -> None:
    data = r.json()
    assert "error" in data, f"Response missing 'error' key: {data}"
    err = data["error"]
    assert "code" in err and "message" in err and "request_id" in err
    assert err["code"] == expected_code, f"Expected '{expected_code}', got '{err['code']}'"


def create_run(agent_id: str = AGENT_A) -> str:
    r = post("/v1/runs", {"agent_id": agent_id})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


# ─────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────

class TestHealth:
    def test_health_returns_ok(self):
        r = get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# ─────────────────────────────────────────────
# Runs — POST /v1/runs
# ─────────────────────────────────────────────

class TestCreateRun:
    def test_creates_run(self):
        r = post("/v1/runs", {"agent_id": AGENT_A})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "run_id" in data
        assert "tenant_id" in data
        assert data["status"] == "created"
        assert data["agent_id"] == AGENT_A

    def test_requires_agent_id(self):
        r = post("/v1/runs", {})
        assert r.status_code == 422

    def test_optional_agent_version(self):
        r = post("/v1/runs", {"agent_id": AGENT_A, "agent_version": "1.2.0"})
        assert r.status_code == 200
        assert r.json()["agent_version"] == "1.2.0"

    def test_tenant_from_credential_not_body(self):
        """Runtime must ignore tenant_id in the request body."""
        r = post("/v1/runs", {"agent_id": AGENT_A, "tenant_id": "injected"})
        if r.status_code == 200:
            assert r.json()["tenant_id"] != "injected"


# ─────────────────────────────────────────────
# Runs — GET /v1/runs/{run_id}
# ─────────────────────────────────────────────

class TestGetRun:
    def test_returns_run(self):
        run_id = create_run()
        r = get(f"/v1/runs/{run_id}")
        assert r.status_code == 200
        assert r.json()["run_id"] == run_id

    def test_not_found(self):
        r = get("/v1/runs/nonexistent-run-id")
        assert r.status_code == 404
        assert_error_envelope(r, "not_found")


# ─────────────────────────────────────────────
# Runs — POST /v1/runs/{run_id}/cancel
# ─────────────────────────────────────────────

class TestCancelRun:
    def test_cancel_returns_cancelled(self):
        run_id = create_run()
        r = post(f"/v1/runs/{run_id}/cancel", {})
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    def test_cannot_cancel_already_cancelled(self):
        run_id = create_run()
        post(f"/v1/runs/{run_id}/cancel", {})
        r = post(f"/v1/runs/{run_id}/cancel", {})
        assert r.status_code == 409
        assert_error_envelope(r, "run_terminal")

    def test_cancel_not_found(self):
        r = post("/v1/runs/nonexistent/cancel", {})
        assert r.status_code == 404


# ─────────────────────────────────────────────
# Runs — GET /v1/runs/{run_id}/events
# ─────────────────────────────────────────────

class TestRunEvents:
    def test_returns_list_for_new_run(self):
        run_id = create_run()
        r = get(f"/v1/runs/{run_id}/events")
        assert r.status_code == 200
        events = r.json()
        assert isinstance(events, list)

    def test_events_have_required_fields(self):
        run_id = create_run()
        r = get(f"/v1/runs/{run_id}/events")
        assert r.status_code == 200
        events = r.json()
        if events:
            ev = events[0]
            for field in ("event_id", "run_id", "type", "occurred_at", "producer", "causation_id"):
                assert field in ev, f"Event missing field: {field}"

    def test_not_found_for_unknown_run(self):
        r = get("/v1/runs/nonexistent/events")
        assert r.status_code == 404


# ─────────────────────────────────────────────
# Runs — GET /v1/runs (list) — not yet implemented
# ─────────────────────────────────────────────

class TestListRuns:
    @pytest.mark.skip(reason="GET /v1/runs not yet implemented")
    def test_returns_paginated_list(self):
        pass


# ─────────────────────────────────────────────
# Runs — replay — not yet implemented
# ─────────────────────────────────────────────

class TestReplayRun:
    @pytest.mark.skip(reason="POST /v1/runs/{id}/replay not yet implemented")
    def test_inspect_mode(self):
        pass


# ─────────────────────────────────────────────
# Tool calls
# ─────────────────────────────────────────────

class TestToolCallSubmission:
    def _valid_body(self) -> dict:
        return {
            "action": {
                "tool_name": "prepare_classification_correction",
                "arguments": {
                    "employee_id": TEST_EMPLOYEE,
                    "correction_type": "misclassification_contractor_to_employee",
                    "evidence_ids": ["ev-001"],
                },
            },
            "context": {"actor_id": "test-actor"},
        }

    def test_submit_returns_approval_required(self):
        r = post("/v1/tool-calls", self._valid_body(),
                 idempotency_key=idem())
        assert r.status_code == 200, r.text
        data = r.json()
        assert "tool_call_id" in data
        assert data["status"] == "approval_required"
        assert data["next_action"] == "await_approval"
        assert data["approval"]["risk_level"] == "high"

    def test_unknown_tool_rejected(self):
        body = {
            "action": {"tool_name": "nonexistent_tool", "arguments": {}},
            "context": {"actor_id": "test-actor"},
        }
        r = post("/v1/tool-calls", body, idempotency_key=idem())
        assert r.status_code == 400
        assert_error_envelope(r, "invalid_tool_request")

    def test_invalid_arguments_rejected(self):
        body = {
            "action": {
                "tool_name": "prepare_classification_correction",
                "arguments": {
                    "employee_id": TEST_EMPLOYEE,
                    "correction_type": "not_a_valid_enum_value",
                    "evidence_ids": ["ev-001"],
                },
            },
            "context": {"actor_id": "test-actor"},
        }
        r = post("/v1/tool-calls", body, idempotency_key=idem())
        assert r.status_code == 400
        assert_error_envelope(r, "invalid_tool_request")

    def test_idempotency_replay(self):
        key = idem()
        r1 = post("/v1/tool-calls", self._valid_body(), idempotency_key=key)
        r2 = post("/v1/tool-calls", self._valid_body(), idempotency_key=key)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["tool_call_id"] == r2.json()["tool_call_id"]


# ─────────────────────────────────────────────
# Proposals
# ─────────────────────────────────────────────

class TestProposals:
    def _create_proposal(self) -> tuple[str, str]:
        """Returns (tool_call_id, proposal_id)."""
        r = post("/v1/tool-calls", {
            "action": {
                "tool_name": "prepare_classification_correction",
                "arguments": {
                    "employee_id": TEST_EMPLOYEE,
                    "correction_type": "misclassification_contractor_to_employee",
                    "evidence_ids": ["ev-001"],
                },
            },
            "context": {"actor_id": "test-actor"},
        }, idempotency_key=idem())
        assert r.status_code == 200
        data = r.json()
        return data["tool_call_id"], data["proposal"]["proposal_id"]

    def test_get_proposal(self):
        _, proposal_id = self._create_proposal()
        r = get(f"/v1/proposals/{proposal_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["proposal_id"] == proposal_id
        assert "status" in data

    def test_get_proposal_versions(self):
        _, proposal_id = self._create_proposal()
        r = get(f"/v1/proposals/{proposal_id}/versions")
        assert r.status_code == 200
        versions = r.json()
        assert isinstance(versions, list)
        assert len(versions) >= 1

    def test_get_specific_version(self):
        _, proposal_id = self._create_proposal()
        r = get(f"/v1/proposals/{proposal_id}/versions/1")
        assert r.status_code == 200
        data = r.json()
        assert data["version"] == 1
        assert "risk_level" in data

    def test_not_found(self):
        r = get("/v1/proposals/nonexistent-id")
        assert r.status_code == 404
        assert_error_envelope(r, "not_found")


# ─────────────────────────────────────────────
# Approvals
# ─────────────────────────────────────────────

class TestApprovals:
    def _create_approval(self) -> tuple[str, str]:
        """Returns (approval_id, proposal_id)."""
        r = post("/v1/tool-calls", {
            "action": {
                "tool_name": "prepare_classification_correction",
                "arguments": {
                    "employee_id": TEST_EMPLOYEE,
                    "correction_type": "misclassification_contractor_to_employee",
                    "evidence_ids": ["ev-001"],
                },
            },
            "context": {"actor_id": "test-actor"},
        }, idempotency_key=idem())
        data = r.json()
        return data["approval"]["approval_id"], data["proposal"]["proposal_id"]

    def test_get_approval(self):
        approval_id, _ = self._create_approval()
        r = get(f"/v1/approvals/{approval_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["approval_id"] == approval_id
        assert data["status"] == "pending"
        assert "risk_level" in data
        assert "current_proposal_version" in data

    def test_claim_approval(self):
        approval_id, _ = self._create_approval()
        r = post(f"/v1/approvals/{approval_id}/claim", {},
                 idempotency_key=idem())
        assert r.status_code == 200

    def test_stale_approval_rejected(self):
        """Approving with wrong proposal_version must be rejected."""
        approval_id, _ = self._create_approval()
        r = post(f"/v1/approvals/{approval_id}/approve",
                 {"proposal_version": 999, "approver_note": "stale"},
                 idempotency_key=idem())
        assert r.status_code == 409
        assert_error_envelope(r, "stale_approval")

    def test_reject_approval(self):
        approval_id, _ = self._create_approval()
        r = post(f"/v1/approvals/{approval_id}/reject",
                 {"proposal_version": 1, "reason": "not warranted"},
                 idempotency_key=idem())
        assert r.status_code == 200

    def test_not_found(self):
        r = get("/v1/approvals/nonexistent-id")
        assert r.status_code == 404

    @pytest.mark.skip(reason="GET /v1/approvals list not yet implemented")
    def test_list_approvals(self):
        pass


# ─────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────

class TestCommands:
    def test_list_commands(self):
        r = get("/v1/commands")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_list_commands_filter_by_status(self):
        r = get("/v1/commands?status=succeeded")
        assert r.status_code == 200

    def test_get_command_not_found(self):
        r = get("/v1/commands/nonexistent-id")
        assert r.status_code == 404
        assert_error_envelope(r, "not_found")

    def test_reconcile_only_valid_on_unknown(self):
        """Reconcile on a non-unknown command must return 409."""
        # Create a command via the normal approval path, then attempt reconcile
        r_tc = post("/v1/tool-calls", {
            "action": {
                "tool_name": "prepare_classification_correction",
                "arguments": {
                    "employee_id": TEST_EMPLOYEE,
                    "correction_type": "misclassification_contractor_to_employee",
                    "evidence_ids": ["ev-001"],
                },
            },
            "context": {"actor_id": "test-actor"},
        }, idempotency_key=idem())
        approval_id = r_tc.json()["approval"]["approval_id"]
        # Approve to create command at status: authorized
        r_approve = post(f"/v1/approvals/{approval_id}/approve",
                         {"proposal_version": 1},
                         idempotency_key=idem())
        assert r_approve.status_code == 200
        command_id = r_approve.json()["command_id"]
        # Wait briefly for worker to dispatch
        time.sleep(4)
        r_cmd = get(f"/v1/commands/{command_id}")
        assert r_cmd.status_code == 200
        cmd_status = r_cmd.json()["status"]
        if cmd_status != "unknown":
            r = post(f"/v1/commands/{command_id}/reconcile", {},
                     idempotency_key=idem())
            assert r.status_code == 409
            assert_error_envelope(r, "not_reconcilable")

    def test_attempts_endpoint(self):
        """GET /v1/commands/{id}/attempts returns a list."""
        # Get any command that exists
        cmds = get("/v1/commands").json()
        if cmds:
            command_id = cmds[0]["command_id"]
            r = get(f"/v1/commands/{command_id}/attempts")
            assert r.status_code == 200
            assert isinstance(r.json(), list)


# ─────────────────────────────────────────────
# Handoffs
# ─────────────────────────────────────────────

class TestHandoffs:
    def test_get_handoff_not_found(self):
        r = get("/v1/handoffs/nonexistent-id")
        assert r.status_code == 404
        assert_error_envelope(r, "not_found")

    def test_handoff_on_terminal_run_rejected(self):
        run_id = create_run()
        post(f"/v1/runs/{run_id}/cancel", {})
        r = post(f"/v1/runs/{run_id}/handoffs", {
            "to_agent": AGENT_B,
            "reason": "test",
        })
        assert r.status_code == 409
        assert_error_envelope(r, "run_terminal")

    def test_handoff_creates_target_run(self):
        run_id = create_run()
        r = post(f"/v1/runs/{run_id}/handoffs", {
            "to_agent": AGENT_B,
            "reason": "conformance test handoff",
            "context_package": {
                "structured_facts": {"employee_id": TEST_EMPLOYEE, "jurisdiction": "AR"},
            },
        })
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["from_agent"] == AGENT_A
        assert data["to_agent"] == AGENT_B
        assert data["target_run_id"] is not None
        assert data["status"] == "active"

        # Source run should be completed
        source = get(f"/v1/runs/{run_id}").json()
        assert source["status"] == "completed"

        # Target run should be created
        target = get(f"/v1/runs/{data['target_run_id']}").json()
        assert target["status"] == "created"
        assert target["agent_id"] == AGENT_B

    def test_get_handoff(self):
        run_id = create_run()
        created = post(f"/v1/runs/{run_id}/handoffs", {
            "to_agent": AGENT_B,
            "reason": "conformance test",
        }).json()
        handoff_id = created["handoff_id"]

        r = get(f"/v1/handoffs/{handoff_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["handoff_id"] == handoff_id
        assert data["from_agent"] == AGENT_A
        assert data["to_agent"] == AGENT_B


# ─────────────────────────────────────────────
# Spec artifacts
# ─────────────────────────────────────────────

class TestSpecArtifacts:
    SPEC_DIR = Path(__file__).parent.parent / "spec"

    def test_openapi_exists(self):
        assert (self.SPEC_DIR / "openapi.yaml").exists()

    def test_golden_sequence_is_valid_json(self):
        path = self.SPEC_DIR / "golden_sequence.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "sequence" in data
        assert len(data["sequence"]) > 0

    def test_tool_contracts_load(self):
        """All JSON files in spec/tool_contracts/ must be valid and have a 'tools' key."""
        contracts_dir = self.SPEC_DIR / "tool_contracts"
        assert contracts_dir.exists()
        files = list(contracts_dir.glob("*.json"))
        assert len(files) >= 2, "Expected at least payroll.json and handoff.json"
        for f in files:
            data = json.loads(f.read_text())
            assert "tools" in data, f"{f.name} missing 'tools' key"
            assert len(data["tools"]) > 0


# ─────────────────────────────────────────────
# Integration — Phase 2: command worker
# ─────────────────────────────────────────────

class TestPhase2CommandWorker:
    """Requires INTEGRATION=1 — no Anthropic API calls, but runs the live worker."""

    @pytest.mark.skipif(not os.getenv("INTEGRATION"), reason="Set INTEGRATION=1")
    def test_worker_dispatches_after_approval(self):
        """Full loop: submit tool call → approve → worker dispatches → command.succeeded."""
        key = idem()
        r = post("/v1/tool-calls", {
            "action": {
                "tool_name": "prepare_classification_correction",
                "arguments": {
                    "employee_id": TEST_EMPLOYEE,
                    "correction_type": "misclassification_contractor_to_employee",
                    "evidence_ids": ["ev-integration-001"],
                },
            },
            "context": {"actor_id": "integration-test"},
        }, idempotency_key=key)
        assert r.status_code == 200
        approval_id = r.json()["approval"]["approval_id"]

        # Approve
        approve_r = post(f"/v1/approvals/{approval_id}/approve",
                         {"proposal_version": 1, "approver_note": "integration test"},
                         idempotency_key=idem())
        assert approve_r.status_code == 200
        command_id = approve_r.json()["command_id"]

        # Poll command until terminal
        for _ in range(15):
            time.sleep(2)
            cmd = get(f"/v1/commands/{command_id}").json()
            if cmd["status"] in ("succeeded", "failed", "unknown"):
                break

        assert cmd["status"] == "succeeded", f"Command did not succeed: {cmd}"
        assert cmd["downstream_reference"] is not None

        # Verify attempt record
        attempts = get(f"/v1/commands/{command_id}/attempts").json()
        assert len(attempts) >= 1
        assert attempts[0]["status"] == "succeeded"


# ─────────────────────────────────────────────
# Integration — Phase 3: managed run loop
# ─────────────────────────────────────────────

class TestPhase3ManagedRun:
    """Requires INTEGRATION=1 and ANTHROPIC_API_KEY in runtime .env."""

    @pytest.mark.skipif(not os.getenv("INTEGRATION"), reason="Set INTEGRATION=1")
    def test_full_managed_run_loop(self):
        """Create run → send message → await approval → approve → resume → completed."""
        run_id = create_run(AGENT_A)

        # Send investigation message
        msg_r = post(f"/v1/runs/{run_id}/messages",
                     {"content": f"Investigate {TEST_EMPLOYEE} for compliance issues."})
        assert msg_r.status_code == 200, msg_r.text
        msg = msg_r.json()
        assert msg["status"] == "awaiting_approval"
        assert msg["pending_approval"] is not None
        approval_id = msg["pending_approval"]["approval_id"]

        # Get proposal version
        approval = get(f"/v1/approvals/{approval_id}").json()
        version = approval["current_proposal_version"]

        # Approve
        approve_r = post(f"/v1/approvals/{approval_id}/approve",
                         {"proposal_version": version, "approver_note": "integration test"},
                         idempotency_key=idem())
        assert approve_r.status_code == 200
        command_id = approve_r.json()["command_id"]

        # Wait for worker
        for _ in range(15):
            time.sleep(2)
            if get(f"/v1/commands/{command_id}").json()["status"] == "succeeded":
                break

        # Resume
        resume_r = post(f"/v1/runs/{run_id}/messages", {})
        assert resume_r.status_code == 200
        final = resume_r.json()
        assert final["status"] == "completed"
        assert final["message"] is not None

        # Verify audit trail
        events = get(f"/v1/runs/{run_id}/events").json()
        event_types = [e["type"] for e in events]
        for expected in ("tool_call.received", "proposal.created", "approval.required",
                         "command.dispatched", "command.succeeded", "run.completed"):
            assert expected in event_types, f"Missing audit event: {expected}"


# ─────────────────────────────────────────────
# Integration — Phase 4: multi-agent handoff
# ─────────────────────────────────────────────

class TestPhase4AgentHandoff:
    """Requires INTEGRATION=1 and ANTHROPIC_API_KEY in runtime .env."""

    @pytest.mark.skipif(not os.getenv("INTEGRATION"), reason="Set INTEGRATION=1")
    def test_agent_handoff_from_managed_run(self):
        """payroll-agent-a detects withholding issue and hands off to border-agent-a."""
        run_id = create_run(AGENT_A)

        msg_r = post(f"/v1/runs/{run_id}/messages", {
            "content": (
                f"Investigate {TEST_EMPLOYEE} for compliance issues. "
                "If you detect a withholding obligation requiring cross-border expertise, "
                "hand off to the appropriate specialist agent."
            ),
        })
        assert msg_r.status_code == 200, msg_r.text
        final = msg_r.json()

        # Run may complete immediately after handoff
        assert final["status"] == "completed"
        assert final["handoff"] is not None
        handoff_id   = final["handoff"]["handoff_id"]
        target_run_id = final["handoff"]["target_run_id"]
        assert final["handoff"]["to_agent"] == AGENT_B

        # Verify handoff record
        handoff = get(f"/v1/handoffs/{handoff_id}").json()
        assert handoff["from_agent"] == AGENT_A
        assert handoff["to_agent"] == AGENT_B
        assert handoff["target_run_id"] == target_run_id
        assert handoff["status"] == "active"

        # Verify source run is completed
        source = get(f"/v1/runs/{run_id}").json()
        assert source["status"] == "completed"

        # Verify target run was created with border-agent-a
        target = get(f"/v1/runs/{target_run_id}").json()
        assert target["agent_id"] == AGENT_B

        # Send first message to border-agent-a — it already has context pre-loaded
        border_r = post(f"/v1/runs/{target_run_id}/messages", {
            "content": "What is the withholding exposure and what must be resolved before reclassification?",
        })
        assert border_r.status_code == 200, border_r.text
        border_final = border_r.json()
        assert border_final["status"] in ("completed", "awaiting_approval")
        assert border_final["message"] is not None

        # Audit trail on source run must include handoff events
        events = get(f"/v1/runs/{run_id}/events").json()
        event_types = [e["type"] for e in events]
        assert "run.handoff_initiated" in event_types
        assert "run.completed" in event_types
