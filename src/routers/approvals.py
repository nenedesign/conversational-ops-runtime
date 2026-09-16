import json
from uuid import uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from ..audit import write_audit_event
from ..auth import require_auth
from ..db import get_pool
from ..models import ApprovalResponse, ApproveRequest, ApproveResponse, RejectRequest

from ..provider import PrepareCorrectionRequest, provider as _provider

router = APIRouter(tags=["approvals"])


@router.get("/approvals/{approval_id}", response_model=ApprovalResponse)
async def get_approval(
    approval_id: str,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> ApprovalResponse:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM approvals WHERE approval_id = $1 AND tenant_id = $2",
            approval_id, auth["tenant_id"],
        )
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "not_found", "message": "Approval not found.", "request_id": str(uuid4())}},
        )
    return ApprovalResponse(**dict(row))


@router.post("/approvals/{approval_id}/claim")
async def claim_approval(
    approval_id: str,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    actor_id = auth.get("user_id", "system")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM approvals WHERE approval_id = $1 AND tenant_id = $2",
            approval_id, auth["tenant_id"],
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "not_found", "message": "Approval not found.", "request_id": str(uuid4())}},
            )
        if row["status"] not in ("pending",):
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "approval_not_claimable", "message": f"Approval is '{row['status']}', not claimable.", "request_id": str(uuid4())}},
            )
        if row["claimed_by"] is not None:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "already_claimed", "message": "Approval is already claimed.", "request_id": str(uuid4())}},
            )
        await conn.execute(
            "UPDATE approvals SET status = 'claimed', claimed_by = $1, claimed_at = NOW() WHERE approval_id = $2",
            actor_id, approval_id,
        )
    return {"approval_id": approval_id, "status": "claimed", "claimed_by": actor_id}


@router.post("/approvals/{approval_id}/approve", response_model=ApproveResponse)
async def approve(
    approval_id: str,
    body: ApproveRequest,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> ApproveResponse:
    tenant_id = auth["tenant_id"]
    approver_id = auth.get("user_id", "system")

    async with pool.acquire() as conn:
        # Load approval
        approval = await conn.fetchrow(
            "SELECT * FROM approvals WHERE approval_id = $1 AND tenant_id = $2",
            approval_id, tenant_id,
        )
        if not approval:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "not_found", "message": "Approval not found.", "request_id": str(uuid4())}},
            )
        if approval["status"] not in ("pending", "claimed"):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "approval_not_actionable",
                        "message": f"Approval is '{approval['status']}', cannot approve.",
                        "request_id": str(uuid4()),
                    }
                },
            )
        if body.proposal_version != approval["current_proposal_version"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "proposal_version_mismatch",
                        "message": (
                            f"Submitted proposal_version {body.proposal_version} does not match "
                            f"current version {approval['current_proposal_version']}."
                        ),
                        "request_id": str(uuid4()),
                    }
                },
            )

        proposal_id = approval["proposal_id"]
        run_id = approval["run_id"]

        # Load current proposal version
        pv = await conn.fetchrow(
            "SELECT * FROM proposal_versions WHERE proposal_id = $1 AND version = $2",
            proposal_id, body.proposal_version,
        )
        if not pv:
            raise HTTPException(status_code=500, detail={"error": {"code": "internal", "message": "Proposal version not found.", "request_id": str(uuid4())}})

        content = json.loads(pv["content"]) if isinstance(pv["content"], str) else pv["content"]
        stored_resource_version = pv["resource_version"]
        employee_id = content.get("employee_id", "")

        # Authorization recheck: compare stored resource_version vs current
        current_record = await _provider.get_payroll_record(employee_id, tenant_id)

        correlation = run_id or approval_id
        await write_audit_event(
            conn,
            tenant_id=tenant_id, run_id=run_id,
            event_type="authorization.rechecked", producer="action-service",
            causation_id=approval_id, correlation_id=correlation,
            data={
                "proposal_id": proposal_id,
                "stored_resource_version": stored_resource_version,
                "current_resource_version": current_record.resource_version,
            },
        )

        if current_record.resource_version != stored_resource_version:
            # Resource changed during review — stale approval
            # Re-prepare with fresh data
            tool_call_row = await conn.fetchrow(
                "SELECT tool_name, arguments FROM tool_calls WHERE tool_call_id = (SELECT tool_call_id FROM proposals WHERE proposal_id = $1)",
                proposal_id,
            )
            args = json.loads(tool_call_row["arguments"]) if isinstance(tool_call_row["arguments"], str) else tool_call_row["arguments"]
            actor_id = args.get("requested_by", "system")

            prep = PrepareCorrectionRequest(
                employee_id=employee_id,
                correction_type=args.get("correction_type", ""),
                evidence_ids=args.get("evidence_ids", []),
                requested_by=actor_id,
                tenant_id=tenant_id,
            )
            new_proposal_content = await _provider.prepare_correction(prep)

            async with conn.transaction():
                new_version_num = body.proposal_version + 1
                new_version_id = str(uuid4())

                await conn.execute(
                    """
                    INSERT INTO proposal_versions
                        (version_id, proposal_id, run_id, tenant_id, version, content,
                         evidence_ids, risk_level, resource_version, policy_version,
                         uncertainty_statement, created_by, created_by_type, reason)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, 'system', 'stale_recheck')
                    """,
                    new_version_id, proposal_id, run_id, tenant_id,
                    new_version_num,
                    json.dumps(new_proposal_content.proposal_content),
                    new_proposal_content.evidence_citations,
                    new_proposal_content.risk_level,
                    new_proposal_content.resource_version,
                    new_proposal_content.policy_version,
                    new_proposal_content.uncertainty_statement,
                    approver_id,
                )
                await conn.execute(
                    "UPDATE proposals SET current_version = $1, updated_at = NOW() WHERE proposal_id = $2",
                    new_version_num, proposal_id,
                )
                await conn.execute(
                    """
                    UPDATE approvals
                    SET status = 'pending', current_proposal_version = $1,
                        claimed_by = NULL, claimed_at = NULL, updated_at = NOW()
                    WHERE approval_id = $2
                    """,
                    new_version_num, approval_id,
                )
                await write_audit_event(
                    conn,
                    tenant_id=tenant_id, run_id=run_id,
                    event_type="approval.stale", producer="action-service",
                    causation_id=approval_id, correlation_id=correlation,
                    data={
                        "approval_id": approval_id,
                        "proposal_id": proposal_id,
                        "new_version": new_version_num,
                        "stored_resource_version": stored_resource_version,
                        "current_resource_version": current_record.resource_version,
                    },
                )

            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "stale_approval",
                        "message": (
                            "The underlying resource changed while this approval was pending. "
                            f"A new proposal version ({new_version_num}) has been created for re-review."
                        ),
                        "request_id": str(uuid4()),
                        "details": {
                            "new_proposal_version": new_version_num,
                            "proposal_id": proposal_id,
                        },
                    }
                },
            )

        # Authorization passed — create command
        async with conn.transaction():
            command_id = str(uuid4())
            command_idempotency_key = str(uuid4())

            tool_call_row = await conn.fetchrow(
                "SELECT tool_call_id, tool_name FROM tool_calls WHERE tool_call_id = (SELECT tool_call_id FROM proposals WHERE proposal_id = $1)",
                proposal_id,
            )

            await conn.execute(
                """
                INSERT INTO commands
                    (command_id, run_id, tenant_id, tool_call_id, proposal_id,
                     proposal_version, approval_id, actor_id, tool_name,
                     idempotency_key, status, authorized_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'authorized', NOW())
                """,
                command_id, run_id, tenant_id,
                tool_call_row["tool_call_id"], proposal_id,
                body.proposal_version, approval_id, approver_id,
                tool_call_row["tool_name"], command_idempotency_key,
            )

            await conn.execute(
                """
                UPDATE approvals
                SET status = 'approved', approver_id = $1, approver_note = $2,
                    decided_at = NOW(), decided_proposal_version = $3,
                    resulting_command_id = $4, updated_at = NOW()
                WHERE approval_id = $5
                """,
                approver_id, body.approver_note, body.proposal_version,
                command_id, approval_id,
            )
            await conn.execute(
                "UPDATE proposals SET status = 'approved', approved_version = $1, updated_at = NOW() WHERE proposal_id = $2",
                body.proposal_version, proposal_id,
            )
            await conn.execute(
                "UPDATE tool_calls SET status = 'command_created', updated_at = NOW() WHERE tool_call_id = $1",
                tool_call_row["tool_call_id"],
            )

            # Outbox event written in the same transaction as the command record.
            # The command worker picks this up and dispatches via commit_correction.
            outbox_event_id = str(uuid4())
            await conn.execute(
                """
                INSERT INTO outbox (event_id, command_id, status, attempt_number)
                VALUES ($1, $2, 'pending', 0)
                """,
                outbox_event_id, command_id,
            )

            await write_audit_event(
                conn,
                tenant_id=tenant_id, run_id=run_id,
                event_type="authorization.passed", producer="action-service",
                causation_id=approval_id, correlation_id=correlation,
                data={
                    "approval_id": approval_id,
                    "proposal_id": proposal_id,
                    "proposal_version": body.proposal_version,
                    "approver_id": approver_id,
                },
            )
            await write_audit_event(
                conn,
                tenant_id=tenant_id, run_id=run_id,
                event_type="command.created", producer="action-service",
                causation_id=approval_id, correlation_id=correlation,
                data={"command_id": command_id, "tool_name": tool_call_row["tool_name"]},
            )

    return ApproveResponse(
        approval_id=approval_id,
        status="approved",
        command_id=command_id,
        message="Approval accepted. Command created and authorized for dispatch.",
    )


@router.post("/approvals/{approval_id}/reject")
async def reject_approval(
    approval_id: str,
    body: RejectRequest,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    tenant_id = auth["tenant_id"]
    approver_id = auth.get("user_id", "system")

    async with pool.acquire() as conn:
        approval = await conn.fetchrow(
            "SELECT * FROM approvals WHERE approval_id = $1 AND tenant_id = $2",
            approval_id, tenant_id,
        )
        if not approval:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "not_found", "message": "Approval not found.", "request_id": str(uuid4())}},
            )
        if approval["status"] not in ("pending", "claimed"):
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "approval_not_actionable", "message": f"Approval is '{approval['status']}', cannot reject.", "request_id": str(uuid4())}},
            )
        if body.proposal_version != approval["current_proposal_version"]:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "proposal_version_mismatch", "message": f"Submitted proposal_version {body.proposal_version} does not match current version {approval['current_proposal_version']}.", "request_id": str(uuid4())}},
            )

        run_id = approval["run_id"]
        proposal_id = approval["proposal_id"]
        correlation = run_id or approval_id

        async with conn.transaction():
            await conn.execute(
                """
                UPDATE approvals
                SET status = 'rejected', approver_id = $1, decided_at = NOW(),
                    decided_proposal_version = $2, updated_at = NOW()
                WHERE approval_id = $3
                """,
                approver_id, body.proposal_version, approval_id,
            )
            await conn.execute(
                "UPDATE proposals SET status = 'rejected', updated_at = NOW() WHERE proposal_id = $1",
                proposal_id,
            )
            tool_call_id = await conn.fetchval(
                "SELECT tool_call_id FROM proposals WHERE proposal_id = $1", proposal_id,
            )
            if tool_call_id:
                await conn.execute(
                    "UPDATE tool_calls SET status = 'rejected', updated_at = NOW() WHERE tool_call_id = $1",
                    tool_call_id,
                )
            await write_audit_event(
                conn,
                tenant_id=tenant_id, run_id=run_id,
                event_type="approval.rejected", producer="action-service",
                causation_id=approval_id, correlation_id=correlation,
                data={
                    "approval_id": approval_id,
                    "proposal_id": proposal_id,
                    "reason": body.reason,
                    "approver_id": approver_id,
                },
            )

    return {
        "approval_id": approval_id,
        "status": "rejected",
        "reason": body.reason,
    }
