"""
Managed runs — Phase 3.

POST /v1/runs                   Create a run
GET  /v1/runs/{run_id}          Get run status
POST /v1/runs/{run_id}/messages Send a user message OR resume after approval
GET  /v1/runs/{run_id}/events   List audit events for this run
POST /v1/runs/{run_id}/cancel   Cancel a run

Agentic loop:
  - Claude may call multiple tools in one response. All tool_use blocks in a
    single response are processed together and results returned in one message.
  - Low-risk tools (investigate_payroll_anomaly): execute immediately; result
    fed back to Claude; loop continues.
  - High/medium-risk tools (prepare_classification_correction, flag_for_legal_review):
    approval records are created; Claude receives a placeholder result explaining
    that human approval is required; Claude generates a summary response; run
    pauses at status='awaiting_approval'.
  - Resume: client calls POST .../messages with no content after the approval
    has been granted and the command worker has dispatched successfully. The
    runtime adds a continuation message and calls Claude for the final response.
"""

import json
from uuid import uuid4

import anthropic
import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from ..audit import write_audit_event
from ..auth import require_auth
from ..config import settings
from ..db import get_pool
from ..models import (
    CreateRunRequest,
    RunEventResponse,
    RunMessageRequest,
    RunMessageResponse,
    RunResponse,
)
from ..policy import evaluate_policy
from ..provider import PrepareCorrectionRequest, provider as _provider
from ..tool_contracts import _contracts, validate_arguments

router = APIRouter(tags=["runs"])

MAX_TOOL_ROUNDS = 5

AGENT_SYSTEM_PROMPTS: dict[str, str] = {
    "payroll-detective": (
        "You are a payroll compliance detective working for a multinational company. "
        "Your role is to identify payroll anomalies and prepare correction proposals for human review. "
        "When asked to investigate an employee, first call investigate_payroll_anomaly to gather facts. "
        "If you detect a misclassification, call prepare_classification_correction to create a proposal. "
        "Explain your findings and reasoning clearly. "
        "You do not make final decisions — you prepare evidence-backed cases for human review."
    ),
}

DEFAULT_SYSTEM_PROMPT = (
    "You are a governed AI agent. You have access to tools that may require human "
    "approval before they take effect. Use tools when they help accomplish the user's request."
)


# ── Content helpers ───────────────────────────────────────────────────────────

def _serialize_blocks(blocks: list) -> list[dict]:
    result = []
    for block in blocks:
        if block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            result.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
    return result


def _extract_text(blocks: list) -> str:
    return "".join(block.text for block in blocks if block.type == "text")


# ── Claude API ────────────────────────────────────────────────────────────────

def _build_anthropic_tools() -> list[dict]:
    return [
        {
            "name": tool_name,
            "description": contract["description"],
            "input_schema": contract["arguments"],
        }
        for tool_name, contract in _contracts.items()
    ]


async def _call_claude(system_prompt: str, messages: list[dict], api_key: str) -> anthropic.types.Message:
    client = anthropic.AsyncAnthropic(api_key=api_key)
    return await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=system_prompt,
        messages=messages,
        tools=_build_anthropic_tools(),
    )


# ── Tool dispatch ─────────────────────────────────────────────────────────────

async def _execute_read_tool(tool_name: str, tool_input: dict, tenant_id: str) -> str:
    if tool_name == "investigate_payroll_anomaly":
        employee_id = tool_input.get("employee_id", "")
        employee = await _provider.get_employee(employee_id, tenant_id)
        record = await _provider.get_payroll_record(employee_id, tenant_id)
        return json.dumps({
            "employee_id": employee_id,
            "name": employee.name,
            "status": employee.status,
            "contractor_type": employee.contractor_type,
            "jurisdiction": employee.jurisdiction,
            "payroll_period": record.period,
            "gross": record.gross,
            "currency": record.currency,
            "classification": record.classification,
            "resource_version": record.resource_version,
            "compliance_note": (
                "Jurisdiction AR (Argentina): workers employed exclusively by one "
                "company for more than 6 consecutive months must be reclassified "
                "as employees under Law 20744."
            ),
        })
    return f"Tool '{tool_name}' completed."


async def _create_proposal_records(
    conn: asyncpg.Connection,
    tenant_id: str,
    run_id: str,
    actor_id: str,
    tool_name: str,
    tool_input: dict,
) -> dict:
    error = validate_arguments(tool_name, tool_input)
    if error:
        return {"status": "validation_error", "error": error}

    policy = evaluate_policy(tool_name)
    if policy.outcome == "rejected":
        return {"status": "rejected", "error": f"Policy rejected tool '{tool_name}'."}

    prep = PrepareCorrectionRequest(
        employee_id=tool_input.get("employee_id", ""),
        correction_type=tool_input.get("correction_type", ""),
        evidence_ids=tool_input.get("evidence_ids", []),
        requested_by=actor_id,
        tenant_id=tenant_id,
    )
    proposal_content = await _provider.prepare_correction(prep)

    tool_call_id = str(uuid4())
    proposal_id = str(uuid4())
    version_id = str(uuid4())
    approval_id = str(uuid4())

    async with conn.transaction():
        await conn.execute(
            "INSERT INTO tool_calls (tool_call_id, run_id, tenant_id, tool_name, arguments, status) VALUES ($1, $2, $3, $4, $5, 'proposal_created')",
            tool_call_id, run_id, tenant_id, tool_name, json.dumps(tool_input),
        )
        await conn.execute(
            "INSERT INTO proposals (proposal_id, run_id, tenant_id, tool_call_id, status, current_version) VALUES ($1, $2, $3, $4, 'awaiting_approval', 1)",
            proposal_id, run_id, tenant_id, tool_call_id,
        )
        await conn.execute(
            """
            INSERT INTO proposal_versions
                (version_id, proposal_id, run_id, tenant_id, version, content,
                 evidence_ids, risk_level, resource_version, policy_version,
                 uncertainty_statement, created_by, created_by_type, reason)
            VALUES ($1, $2, $3, $4, 1, $5, $6, $7, $8, $9, $10, $11, 'agent', 'initial')
            """,
            version_id, proposal_id, run_id, tenant_id,
            json.dumps(proposal_content.proposal_content),
            proposal_content.evidence_citations,
            policy.risk_level,
            proposal_content.resource_version,
            proposal_content.policy_version,
            proposal_content.uncertainty_statement,
            actor_id,
        )
        await conn.execute(
            "INSERT INTO approvals (approval_id, proposal_id, run_id, tenant_id, current_proposal_version, risk_level, requires_approval, status) VALUES ($1, $2, $3, $4, 1, $5, TRUE, 'pending')",
            approval_id, proposal_id, run_id, tenant_id, policy.risk_level,
        )
        await conn.execute("UPDATE proposals SET approval_id = $1 WHERE proposal_id = $2", approval_id, proposal_id)
        await conn.execute("UPDATE tool_calls SET proposal_id = $1 WHERE tool_call_id = $2", proposal_id, tool_call_id)

        await write_audit_event(conn, tenant_id=tenant_id, run_id=run_id,
            event_type="tool_call.received", producer="action-service",
            causation_id=tool_call_id, correlation_id=run_id,
            data={"tool_name": tool_name, "tool_call_id": tool_call_id, "via": "managed_run"})
        await write_audit_event(conn, tenant_id=tenant_id, run_id=run_id,
            event_type="proposal.created", producer="action-service",
            causation_id=tool_call_id, correlation_id=run_id,
            data={"proposal_id": proposal_id, "version": 1})
        await write_audit_event(conn, tenant_id=tenant_id, run_id=run_id,
            event_type="policy.evaluated", producer="action-service",
            causation_id=tool_call_id, correlation_id=run_id,
            data={"outcome": policy.outcome, "risk_level": policy.risk_level})
        await write_audit_event(conn, tenant_id=tenant_id, run_id=run_id,
            event_type="approval.required", producer="action-service",
            causation_id=tool_call_id, correlation_id=run_id,
            data={"approval_id": approval_id, "proposal_id": proposal_id})

    return {
        "status": "approval_required",
        "tool_call_id": tool_call_id,
        "proposal_id": proposal_id,
        "approval_id": approval_id,
        "risk_level": policy.risk_level,
    }


# ── Agentic turn ──────────────────────────────────────────────────────────────

async def _run_turn(
    conn: asyncpg.Connection,
    tenant_id: str,
    run_id: str,
    actor_id: str,
    system_prompt: str,
    messages: list[dict],
    api_key: str,
) -> dict:
    """
    Run one full agent turn. Processes ALL tool_use blocks in each Claude
    response as a batch — results for all tools are collected and returned
    in a single tool_result message.

    Returns one of:
      {"outcome": "completed",         "reply": str, "history": [...]}
      {"outcome": "awaiting_approval", "reply": str, "approval_id": str,
       "risk_level": str, "history": [...]}
      {"outcome": "error",             "error": str}
    """
    current_messages = list(messages)

    for _ in range(MAX_TOOL_ROUNDS):
        response = await _call_claude(system_prompt, current_messages, api_key)

        if response.stop_reason != "tool_use":
            break

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        tool_results: list[dict] = []
        pending_approval: dict | None = None

        for block in tool_blocks:
            contract = _contracts.get(block.name, {})

            if not contract.get("requires_approval", True):
                # Read-only: execute now
                result_text = await _execute_read_tool(block.name, block.input, tenant_id)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })
            else:
                # Write tool: create proposal + approval, give Claude a placeholder
                result = await _create_proposal_records(
                    conn, tenant_id, run_id, actor_id, block.name, block.input,
                )
                if result["status"] != "approval_required":
                    return {"outcome": "error", "error": result.get("error", "Tool dispatch failed.")}

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": (
                        f"Correction proposal submitted for human review. "
                        f"Approval ID: {result['approval_id']}. "
                        f"Risk level: {result['risk_level']}. "
                        "The run will pause until a human approves or rejects this proposal."
                    ),
                })
                # Track first pending approval (Phase 3 supports one at a time)
                if pending_approval is None:
                    pending_approval = {
                        "approval_id": result["approval_id"],
                        "risk_level": result["risk_level"],
                    }

        # Append assistant message + all tool results together
        current_messages = current_messages + [
            {"role": "assistant", "content": _serialize_blocks(response.content)},
            {"role": "user",      "content": tool_results},
        ]

        if pending_approval:
            # Let Claude acknowledge the approval requirement, then pause
            ack_response = await _call_claude(system_prompt, current_messages, api_key)
            ack_text = _extract_text(ack_response.content)
            # Save history including Claude's acknowledgment
            final_history = current_messages + [{"role": "assistant", "content": ack_text}]
            return {
                "outcome": "awaiting_approval",
                "reply": ack_text,
                "approval_id": pending_approval["approval_id"],
                "risk_level": pending_approval["risk_level"],
                "history": final_history,
            }

        # All reads — loop back

    final_text = _extract_text(response.content)
    final_history = current_messages + [{"role": "assistant", "content": final_text}]
    return {"outcome": "completed", "reply": final_text, "history": final_history}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/runs", response_model=RunResponse)
async def create_run(
    body: CreateRunRequest,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> RunResponse:
    tenant_id = auth["tenant_id"]
    run_id = str(uuid4())
    system_prompt = (
        body.system_prompt
        or AGENT_SYSTEM_PROMPTS.get(body.agent_id)
        or DEFAULT_SYSTEM_PROMPT
    )

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO runs
                (run_id, tenant_id, agent_id, agent_version, status,
                 state_version, metadata)
            VALUES ($1, $2, $3, $4, 'created', 0, $5)
            """,
            run_id, tenant_id, body.agent_id, body.agent_version,
            json.dumps({"conversation_history": [], "system_prompt": system_prompt}),
        )
        row = await conn.fetchrow("SELECT * FROM runs WHERE run_id = $1", run_id)

    return RunResponse(**dict(row))


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> RunResponse:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM runs WHERE run_id = $1 AND tenant_id = $2",
            run_id, auth["tenant_id"],
        )
    if not row:
        raise HTTPException(404, detail={"error": {"code": "not_found", "message": "Run not found.", "request_id": str(uuid4())}})
    return RunResponse(**dict(row))


@router.post("/runs/{run_id}/messages", response_model=RunMessageResponse)
async def send_message(
    run_id: str,
    body: RunMessageRequest,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> RunMessageResponse:
    tenant_id = auth["tenant_id"]
    actor_id = auth.get("user_id", "system")
    api_key = settings.anthropic_api_key

    if not api_key or api_key == "your-anthropic-api-key-here":
        raise HTTPException(503, detail={"error": {"code": "model_not_configured", "message": "ANTHROPIC_API_KEY is not set in .env.", "request_id": str(uuid4())}})

    async with pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT * FROM runs WHERE run_id = $1 AND tenant_id = $2",
            run_id, tenant_id,
        )
        if not run:
            raise HTTPException(404, detail={"error": {"code": "not_found", "message": "Run not found.", "request_id": str(uuid4())}})
        if run["status"] in ("completed", "failed", "cancelled"):
            raise HTTPException(409, detail={"error": {"code": "run_terminal", "message": f"Run is '{run['status']}'.", "request_id": str(uuid4())}})

        meta: dict = json.loads(run["metadata"]) if isinstance(run["metadata"], str) else run["metadata"]
        history: list = meta.get("conversation_history", [])
        system_prompt: str = meta.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        pending_tool: dict | None = meta.get("pending_tool")

        # ── RESUME PATH ───────────────────────────────────────────────────────
        if run["status"] == "awaiting_approval":
            if not pending_tool:
                raise HTTPException(409, detail={"error": {"code": "no_pending_tool", "message": "Run is awaiting_approval but has no pending tool state.", "request_id": str(uuid4())}})

            approval_id = pending_tool["approval_id"]
            approval = await conn.fetchrow(
                "SELECT status, resulting_command_id FROM approvals WHERE approval_id = $1",
                approval_id,
            )
            if not approval:
                raise HTTPException(500, detail={"error": {"code": "internal", "message": "Approval record missing.", "request_id": str(uuid4())}})

            if approval["status"] == "rejected":
                # Add a system note and let Claude close gracefully
                resume_messages = history + [{
                    "role": "user",
                    "content": "The human reviewer has rejected the correction proposal. Please inform the user and summarise what was found.",
                }]
                ack = await _call_claude(system_prompt, resume_messages, api_key)
                final_text = _extract_text(ack.content)
                new_meta = {**meta, "conversation_history": resume_messages + [{"role": "assistant", "content": final_text}], "pending_tool": None}
                await conn.execute("UPDATE runs SET status = 'completed', metadata = $1, completed_at = NOW(), updated_at = NOW() WHERE run_id = $2", json.dumps(new_meta), run_id)
                await write_audit_event(conn, tenant_id=tenant_id, run_id=run_id, event_type="run.completed", producer="agent-api", causation_id=run_id, correlation_id=run_id, data={"reason": "approval_rejected"})
                return RunMessageResponse(run_id=run_id, status="completed", message={"role": "assistant", "content": final_text})

            command_id = approval["resulting_command_id"]
            if not command_id:
                return RunMessageResponse(run_id=run_id, status="awaiting_approval", pending_approval={"approval_id": approval_id})

            command = await conn.fetchrow("SELECT status, downstream_reference FROM commands WHERE command_id = $1", command_id)
            if not command or command["status"] != "succeeded":
                return RunMessageResponse(
                    run_id=run_id, status="awaiting_approval",
                    pending_approval={"approval_id": approval_id, "command_id": command_id, "command_status": command["status"] if command else "pending"},
                )

            # Command succeeded — continue conversation
            resume_messages = history + [{
                "role": "user",
                "content": (
                    f"The correction proposal was approved by the human reviewer and successfully processed. "
                    f"Provider reference: {command['downstream_reference']}. "
                    "Please provide a final summary for the user."
                ),
            }]
            ack = await _call_claude(system_prompt, resume_messages, api_key)
            final_text = _extract_text(ack.content)
            new_meta = {**meta, "conversation_history": resume_messages + [{"role": "assistant", "content": final_text}], "pending_tool": None}
            await conn.execute("UPDATE runs SET status = 'completed', metadata = $1, completed_at = NOW(), updated_at = NOW() WHERE run_id = $2", json.dumps(new_meta), run_id)
            await write_audit_event(conn, tenant_id=tenant_id, run_id=run_id, event_type="run.completed", producer="agent-api", causation_id=run_id, correlation_id=run_id, data={"command_id": command_id, "downstream_reference": command["downstream_reference"]})
            return RunMessageResponse(run_id=run_id, status="completed", message={"role": "assistant", "content": final_text})

        # ── NEW MESSAGE PATH ──────────────────────────────────────────────────
        if not body.content:
            raise HTTPException(400, detail={"error": {"code": "empty_message", "message": "content is required.", "request_id": str(uuid4())}})

        new_messages = history + [{"role": "user", "content": body.content}]
        await conn.execute("UPDATE runs SET status = 'running', updated_at = NOW() WHERE run_id = $1", run_id)

        result = await _run_turn(conn, tenant_id, run_id, actor_id, system_prompt, new_messages, api_key)

        if result["outcome"] == "completed":
            new_meta = {**meta, "conversation_history": result["history"], "pending_tool": None}
            await conn.execute("UPDATE runs SET status = 'completed', metadata = $1, completed_at = NOW(), updated_at = NOW() WHERE run_id = $2", json.dumps(new_meta), run_id)
            await write_audit_event(conn, tenant_id=tenant_id, run_id=run_id, event_type="run.completed", producer="agent-api", causation_id=run_id, correlation_id=run_id, data={"reason": "end_turn"})
            return RunMessageResponse(run_id=run_id, status="completed", message={"role": "assistant", "content": result["reply"]})

        elif result["outcome"] == "awaiting_approval":
            new_meta = {
                **meta,
                "conversation_history": result["history"],
                "pending_tool": {"approval_id": result["approval_id"]},
            }
            await conn.execute(
                "UPDATE runs SET status = 'awaiting_approval', pending_approval_id = $1, metadata = $2, updated_at = NOW() WHERE run_id = $3",
                result["approval_id"], json.dumps(new_meta), run_id,
            )
            return RunMessageResponse(
                run_id=run_id, status="awaiting_approval",
                message={"role": "assistant", "content": result["reply"]} if result.get("reply") else None,
                pending_approval={"approval_id": result["approval_id"], "risk_level": result.get("risk_level", "high")},
            )

        else:
            await conn.execute("UPDATE runs SET status = 'failed', updated_at = NOW() WHERE run_id = $1", run_id)
            raise HTTPException(500, detail={"error": {"code": "turn_error", "message": result.get("error", "Unknown error."), "request_id": str(uuid4())}})


@router.get("/runs/{run_id}/events", response_model=list[RunEventResponse])
async def get_run_events(
    run_id: str,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[RunEventResponse]:
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM runs WHERE run_id = $1 AND tenant_id = $2", run_id, auth["tenant_id"])
        if not exists:
            raise HTTPException(404, detail={"error": {"code": "not_found", "message": "Run not found.", "request_id": str(uuid4())}})
        rows = await conn.fetch("SELECT * FROM audit_events WHERE run_id = $1 ORDER BY occurred_at", run_id)

    result = []
    for r in rows:
        data = r["data"]
        if isinstance(data, str):
            data = json.loads(data)
        result.append(RunEventResponse(
            event_id=r["event_id"], run_id=r["run_id"], type=r["type"],
            occurred_at=r["occurred_at"], producer=r["producer"],
            causation_id=r["causation_id"], data=data,
        ))
    return result


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    async with pool.acquire() as conn:
        run = await conn.fetchrow("SELECT status FROM runs WHERE run_id = $1 AND tenant_id = $2", run_id, auth["tenant_id"])
        if not run:
            raise HTTPException(404, detail={"error": {"code": "not_found", "message": "Run not found.", "request_id": str(uuid4())}})
        if run["status"] in ("completed", "failed", "cancelled"):
            raise HTTPException(409, detail={"error": {"code": "run_terminal", "message": f"Run is already '{run['status']}'.", "request_id": str(uuid4())}})
        await conn.execute("UPDATE runs SET status = 'cancelled', updated_at = NOW() WHERE run_id = $1", run_id)
    return {"run_id": run_id, "status": "cancelled"}
