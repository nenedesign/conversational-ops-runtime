from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


# ── Requests ──────────────────────────────────

class ToolCallAction(BaseModel):
    tool_name: str
    arguments: dict[str, Any]


class ToolCallAgent(BaseModel):
    id: str
    version: str | None = None


class ToolCallContext(BaseModel):
    actor_id: str | None = None
    run_id: str | None = None
    conversation_id: str | None = None


class ToolCallMetadata(BaseModel):
    model_provider: str | None = None
    model_id: str | None = None
    tool_schema_version: str | None = None


class ToolCallSubmissionRequest(BaseModel):
    agent: ToolCallAgent | None = None
    action: ToolCallAction
    context: ToolCallContext | None = None
    metadata: ToolCallMetadata | None = None


class ApproveRequest(BaseModel):
    proposal_version: int
    approver_note: str | None = None


class RejectRequest(BaseModel):
    proposal_version: int
    reason: str


# ── Response fragments ─────────────────────────

class ProposalRef(BaseModel):
    proposal_id: str
    current_version: int
    status: str


class ApprovalRef(BaseModel):
    approval_id: str
    status: str
    risk_level: str
    expires_at: datetime | None = None


class CommandRef(BaseModel):
    command_id: str
    status: str


# ── Top-level responses ────────────────────────

class ToolCallSubmissionResponse(BaseModel):
    tool_call_id: str
    status: Literal["proposal_created", "approval_required", "rejected", "command_created", "unknown"]
    proposal: ProposalRef | None = None
    approval: ApprovalRef | None = None
    command: CommandRef | None = None
    rejection_reason: str | None = None
    next_action: Literal["await_approval", "await_command", "complete", "error"]


class ProposalVersionResponse(BaseModel):
    version_id: str
    proposal_id: str
    version: int
    content: dict[str, Any]
    evidence_ids: list[str]
    risk_level: str
    resource_version: int | None
    policy_version: str | None
    uncertainty_statement: str | None
    created_by: str
    created_by_type: str
    reason: str | None
    created_at: datetime


class ProposalResponse(BaseModel):
    proposal_id: str
    run_id: str | None
    tenant_id: str
    tool_call_id: str
    status: str
    current_version: int
    approved_version: int | None
    approval_id: str | None
    created_at: datetime
    updated_at: datetime


class ApprovalResponse(BaseModel):
    approval_id: str
    proposal_id: str
    run_id: str | None
    tenant_id: str
    status: str
    current_proposal_version: int
    decided_proposal_version: int | None
    risk_level: str
    requires_approval: bool
    claimed_by: str | None
    claimed_at: datetime | None
    approver_id: str | None
    approver_note: str | None
    decided_at: datetime | None
    expires_at: datetime | None
    resulting_command_id: str | None
    created_at: datetime
    updated_at: datetime


class CommandResponse(BaseModel):
    command_id: str
    run_id: str | None
    tenant_id: str
    tool_call_id: str | None
    proposal_id: str | None
    proposal_version: int
    approval_id: str | None
    actor_id: str
    tool_name: str
    idempotency_key: str
    status: str
    downstream_reference: str | None
    created_at: datetime
    authorized_at: datetime | None
    dispatched_at: datetime | None
    completed_at: datetime | None


class ApproveResponse(BaseModel):
    approval_id: str
    status: str
    command_id: str
    message: str


class CommandAttemptResponse(BaseModel):
    attempt_id: str
    command_id: str
    attempt_number: int
    worker_id: str | None
    status: str
    provider_reference: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class CreateRunRequest(BaseModel):
    agent_id: str
    agent_version: str | None = None
    system_prompt: str | None = None


class RunResponse(BaseModel):
    run_id: str
    tenant_id: str
    status: str
    agent_id: str
    agent_version: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class RunMessageRequest(BaseModel):
    content: str | None = None  # None or empty = resume after approval


class RunMessageResponse(BaseModel):
    run_id: str
    status: str
    message: dict[str, Any] | None = None
    pending_approval: dict[str, Any] | None = None
    handoff: dict[str, Any] | None = None


class CreateHandoffRequest(BaseModel):
    to_agent: str
    handoff_type: str = "internal_handoff"
    reason: str
    context_package: dict[str, Any] | None = None


class HandoffResponse(BaseModel):
    handoff_id: str
    run_id: str
    target_run_id: str | None
    from_agent: str
    to_agent: str
    handoff_type: str
    reason: str | None
    status: str
    created_at: datetime
    completed_at: datetime | None = None


class RunEventResponse(BaseModel):
    event_id: str
    run_id: str
    type: str
    occurred_at: datetime
    producer: str
    causation_id: str
    data: dict[str, Any]


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
