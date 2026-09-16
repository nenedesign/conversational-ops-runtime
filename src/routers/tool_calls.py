import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException

from ..audit import write_audit_event
from ..auth import require_auth
from ..db import get_pool
from ..models import (
    ApprovalRef,
    CommandRef,
    ProposalRef,
    ToolCallSubmissionRequest,
    ToolCallSubmissionResponse,
)
from ..policy import evaluate_policy
from ..tool_contracts import validate_arguments

from ..provider import PrepareCorrectionRequest, provider as _provider

router = APIRouter(tags=["tool-calls"])


def _request_hash(method: str, path: str, tenant_id: str, body: dict) -> str:
    payload = json.dumps({"method": method, "path": path, "tenant": tenant_id, "body": body}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


async def _check_idempotency(
    conn: asyncpg.Connection,
    idempotency_key: str,
    tenant_id: str,
    key_hash: str,
) -> dict | None:
    row = await conn.fetchrow(
        "SELECT key_hash, response_status, response_body FROM idempotency_keys WHERE idempotency_key = $1 AND tenant_id = $2",
        idempotency_key,
        tenant_id,
    )
    if row is None:
        return None
    if row["key_hash"] != key_hash:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "idempotency_conflict",
                    "message": "Idempotency-Key was already used with different parameters.",
                    "request_id": str(uuid4()),
                }
            },
        )
    body = row["response_body"]
    if isinstance(body, str):
        body = json.loads(body)
    return {"status": row["response_status"], "body": body}


async def _store_idempotency(
    conn: asyncpg.Connection,
    idempotency_key: str,
    tenant_id: str,
    key_hash: str,
    request_id: str,
    response_status: int,
    response_body: dict,
) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    await conn.execute(
        """
        INSERT INTO idempotency_keys
            (key_hash, idempotency_key, tenant_id, method, path, request_id, response_status, response_body, expires_at)
        VALUES ($1, $2, $3, 'POST', '/v1/tool-calls', $4, $5, $6, $7)
        ON CONFLICT (key_hash) DO NOTHING
        """,
        key_hash,
        idempotency_key,
        tenant_id,
        request_id,
        response_status,
        json.dumps(response_body),
        expires_at,
    )


@router.post("/tool-calls", response_model=ToolCallSubmissionResponse)
async def submit_tool_call(
    request: ToolCallSubmissionRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> ToolCallSubmissionResponse:
    tenant_id = auth["tenant_id"]
    actor_id = (request.context.actor_id if request.context else None) or "system"
    run_id = request.context.run_id if request.context else None
    request_id = str(uuid4())
    key_hash = _request_hash("POST", "/v1/tool-calls", tenant_id, request.model_dump())

    async with pool.acquire() as conn:
        # Idempotency check
        existing = await _check_idempotency(conn, idempotency_key, tenant_id, key_hash)
        if existing:
            return ToolCallSubmissionResponse(**existing["body"])

        # Validate arguments against tool contract
        validation_error = validate_arguments(request.action.tool_name, request.action.arguments)
        if validation_error:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "code": "validation_error",
                        "message": validation_error,
                        "request_id": request_id,
                        "details": {"field": "action.arguments"},
                    }
                },
            )

        # Policy gate
        policy = evaluate_policy(request.action.tool_name)

        if policy.outcome == "rejected":
            response = ToolCallSubmissionResponse(
                tool_call_id=str(uuid4()),
                status="rejected",
                rejection_reason=f"Policy rejected tool '{request.action.tool_name}'.",
                next_action="error",
            )
            return response

        # Prepare with simulated provider
        prep = PrepareCorrectionRequest(
            employee_id=request.action.arguments.get("employee_id", ""),
            correction_type=request.action.arguments.get("correction_type", ""),
            evidence_ids=request.action.arguments.get("evidence_ids", []),
            requested_by=actor_id,
            tenant_id=tenant_id,
        )
        proposal_content = await _provider.prepare_correction(prep)

        # All DB writes in one transaction
        async with conn.transaction():
            tool_call_id = str(uuid4())
            proposal_id = str(uuid4())
            version_id = str(uuid4())

            await conn.execute(
                """
                INSERT INTO tool_calls
                    (tool_call_id, run_id, tenant_id, tool_name, arguments, status)
                VALUES ($1, $2, $3, $4, $5, 'proposal_created')
                """,
                tool_call_id, run_id, tenant_id,
                request.action.tool_name,
                json.dumps(request.action.arguments),
            )

            await conn.execute(
                """
                INSERT INTO proposals
                    (proposal_id, run_id, tenant_id, tool_call_id, status, current_version)
                VALUES ($1, $2, $3, $4, 'awaiting_approval', 1)
                """,
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

            approval_id: str | None = None
            if policy.requires_approval:
                approval_id = str(uuid4())
                await conn.execute(
                    """
                    INSERT INTO approvals
                        (approval_id, proposal_id, run_id, tenant_id,
                         current_proposal_version, risk_level, requires_approval, status)
                    VALUES ($1, $2, $3, $4, 1, $5, TRUE, 'pending')
                    """,
                    approval_id, proposal_id, run_id, tenant_id, policy.risk_level,
                )
                await conn.execute(
                    "UPDATE proposals SET approval_id = $1 WHERE proposal_id = $2",
                    approval_id, proposal_id,
                )
                await conn.execute(
                    "UPDATE tool_calls SET proposal_id = $1 WHERE tool_call_id = $2",
                    proposal_id, tool_call_id,
                )

            correlation = run_id or tool_call_id
            await write_audit_event(
                conn,
                tenant_id=tenant_id, run_id=run_id,
                event_type="tool_call.received", producer="action-service",
                causation_id=tool_call_id, correlation_id=correlation,
                data={"tool_name": request.action.tool_name, "tool_call_id": tool_call_id},
            )
            await write_audit_event(
                conn,
                tenant_id=tenant_id, run_id=run_id,
                event_type="proposal.created", producer="action-service",
                causation_id=tool_call_id, correlation_id=correlation,
                data={"proposal_id": proposal_id, "version": 1},
            )
            await write_audit_event(
                conn,
                tenant_id=tenant_id, run_id=run_id,
                event_type="policy.evaluated", producer="action-service",
                causation_id=tool_call_id, correlation_id=correlation,
                data={
                    "outcome": policy.outcome,
                    "risk_level": policy.risk_level,
                    "requires_approval": policy.requires_approval,
                },
            )
            if approval_id:
                await write_audit_event(
                    conn,
                    tenant_id=tenant_id, run_id=run_id,
                    event_type="approval.required", producer="action-service",
                    causation_id=tool_call_id, correlation_id=correlation,
                    data={"approval_id": approval_id, "proposal_id": proposal_id},
                )

            # Build response before storing idempotency key
            response_body: dict = {
                "tool_call_id": tool_call_id,
                "status": "approval_required" if approval_id else "proposal_created",
                "proposal": {
                    "proposal_id": proposal_id,
                    "current_version": 1,
                    "status": "awaiting_approval",
                },
                "approval": {
                    "approval_id": approval_id,
                    "status": "pending",
                    "risk_level": policy.risk_level,
                    "expires_at": None,
                } if approval_id else None,
                "command": None,
                "rejection_reason": None,
                "next_action": "await_approval" if approval_id else "complete",
            }
            await _store_idempotency(
                conn, idempotency_key, tenant_id, key_hash, request_id, 200, response_body
            )

    return ToolCallSubmissionResponse(**response_body)


@router.get("/tool-calls/{tool_call_id}")
async def get_tool_call(
    tool_call_id: str,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tool_calls WHERE tool_call_id = $1 AND tenant_id = $2",
            tool_call_id, auth["tenant_id"],
        )
    if not row:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": "Tool call not found.", "request_id": str(uuid4())}})
    return dict(row)
