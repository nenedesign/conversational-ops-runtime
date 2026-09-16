import json
from uuid import uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from ..audit import write_audit_event
from ..auth import require_auth
from ..db import get_pool
from ..models import CommandAttemptResponse, CommandResponse
from ..provider import ReconcileRequest, provider as _provider

router = APIRouter(tags=["commands"])


@router.get("/commands", response_model=list[CommandResponse])
async def list_commands(
    run_id: str | None = None,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[CommandResponse]:
    async with pool.acquire() as conn:
        if run_id:
            rows = await conn.fetch(
                "SELECT * FROM commands WHERE tenant_id = $1 AND run_id = $2 ORDER BY created_at DESC",
                auth["tenant_id"], run_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM commands WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT 100",
                auth["tenant_id"],
            )
    return [CommandResponse(**dict(r)) for r in rows]


@router.get("/commands/{command_id}", response_model=CommandResponse)
async def get_command(
    command_id: str,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> CommandResponse:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM commands WHERE command_id = $1 AND tenant_id = $2",
            command_id, auth["tenant_id"],
        )
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "not_found", "message": "Command not found.", "request_id": str(uuid4())}},
        )
    return CommandResponse(**dict(row))


@router.get("/commands/{command_id}/attempts", response_model=list[CommandAttemptResponse])
async def get_command_attempts(
    command_id: str,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[CommandAttemptResponse]:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM commands WHERE command_id = $1 AND tenant_id = $2",
            command_id, auth["tenant_id"],
        )
        if not exists:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "not_found", "message": "Command not found.", "request_id": str(uuid4())}},
            )
        rows = await conn.fetch(
            "SELECT * FROM command_attempts WHERE command_id = $1 ORDER BY attempt_number",
            command_id,
        )
    return [CommandAttemptResponse(**dict(r)) for r in rows]


@router.post("/commands/{command_id}/reconcile")
async def reconcile_command(
    command_id: str,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    tenant_id = auth["tenant_id"]

    async with pool.acquire() as conn:
        command = await conn.fetchrow(
            "SELECT * FROM commands WHERE command_id = $1 AND tenant_id = $2",
            command_id, tenant_id,
        )
        if not command:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "not_found", "message": "Command not found.", "request_id": str(uuid4())}},
            )
        if command["status"] != "unknown":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "not_reconcilable",
                        "message": (
                            f"Command is '{command['status']}', not 'unknown'. "
                            "Reconciliation is only valid for commands in the unknown state."
                        ),
                        "request_id": str(uuid4()),
                    }
                },
            )

        task = await conn.fetchrow(
            """
            SELECT * FROM reconciliation_tasks
            WHERE command_id = $1 AND status IN ('pending', 'in_progress')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            command_id,
        )
        if not task:
            raise HTTPException(
                status_code=500,
                detail={"error": {"code": "internal", "message": "No reconciliation task found.", "request_id": str(uuid4())}},
            )

        pv = await conn.fetchrow(
            "SELECT content FROM proposal_versions WHERE proposal_id = $1 AND version = $2",
            command["proposal_id"], command["proposal_version"],
        )
        content: dict = (
            json.loads(pv["content"]) if isinstance(pv["content"], str) else pv["content"]
        )

        await conn.execute(
            "UPDATE reconciliation_tasks SET status = 'in_progress', attempts = attempts + 1 WHERE task_id = $1",
            task["task_id"],
        )

    # Provider call outside DB connection
    reconcile_req = ReconcileRequest(
        idempotency_key=command["idempotency_key"],
        provider_reference=command["downstream_reference"],
        resource_id=content.get("employee_id", ""),
        attempt_count=task["attempts"] + 1,
        last_error=task["last_error"],
    )
    result = await _provider.reconcile_correction(reconcile_req)

    if result.status == "found_committed":
        new_status = "succeeded"
        task_resolution = "succeeded"
    elif result.status == "found_not_present":
        new_status = "failed"
        task_resolution = "failed"
    else:  # still_unknown — manual intervention required
        new_status = "unknown"
        task_resolution = None

    async with pool.acquire() as conn:
        async with conn.transaction():
            correlation = command["run_id"] or command_id

            if task_resolution:
                await conn.execute(
                    """
                    UPDATE commands
                    SET status = $1,
                        downstream_reference = COALESCE($2, downstream_reference),
                        completed_at = NOW()
                    WHERE command_id = $3
                    """,
                    new_status, result.provider_reference, command_id,
                )
                await conn.execute(
                    """
                    UPDATE reconciliation_tasks
                    SET status = 'resolved',
                        resolution = $1,
                        provider_reference = $2,
                        resolved_at = NOW()
                    WHERE task_id = $3
                    """,
                    task_resolution, result.provider_reference, task["task_id"],
                )
            else:
                await conn.execute(
                    """
                    UPDATE reconciliation_tasks
                    SET status = 'pending',
                        last_error = 'still_unknown',
                        next_attempt_at = NOW() + INTERVAL '60 seconds'
                    WHERE task_id = $1
                    """,
                    task["task_id"],
                )

            await write_audit_event(
                conn,
                tenant_id=tenant_id,
                run_id=command["run_id"],
                event_type="command.reconciled",
                producer="command-worker",
                causation_id=command_id,
                correlation_id=correlation,
                data={
                    "command_id": command_id,
                    "reconciliation_status": result.status,
                    "new_command_status": new_status,
                    "provider_reference": result.provider_reference,
                },
            )

    return {
        "command_id": command_id,
        "reconciliation_status": result.status,
        "command_status": new_status,
        "provider_reference": result.provider_reference,
    }
