"""
Handoffs — Phase 4.

POST /v1/runs/{run_id}/handoffs   Initiate a handoff from the external API
GET  /v1/handoffs/{handoff_id}    Get handoff state

The handoff transfer can also be triggered from inside a managed run via the
trigger_agent_handoff tool — that path goes through runs.py/_execute_handoff_records.
Both paths write the same DB records and audit events.
"""

import json
from uuid import uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from ..audit import write_audit_event
from ..auth import require_auth
from ..db import get_pool
from ..models import CreateHandoffRequest, HandoffResponse

router = APIRouter(tags=["handoffs"])


def _build_context_message(from_agent: str, reason: str, context_package: dict) -> str:
    parts = [
        f"Case received from {from_agent}.",
        f"Reason for handoff: {reason}",
    ]
    facts = context_package.get("structured_facts")
    if facts:
        parts.append(f"\nStructured facts:\n{json.dumps(facts, indent=2)}")
    uncertainty = context_package.get("uncertainty_statement")
    if uncertainty:
        parts.append(f"\nUncertainty: {uncertainty}")
    parts.append("\nPlease review the above and provide your analysis.")
    return "\n".join(parts)


@router.post("/runs/{run_id}/handoffs", response_model=HandoffResponse, status_code=201)
async def create_handoff(
    run_id: str,
    body: CreateHandoffRequest,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> HandoffResponse:
    tenant_id = auth["tenant_id"]

    async with pool.acquire() as conn:
        source_run = await conn.fetchrow(
            "SELECT * FROM runs WHERE run_id = $1 AND tenant_id = $2",
            run_id, tenant_id,
        )
        if not source_run:
            raise HTTPException(404, detail={"error": {"code": "not_found", "message": "Run not found.", "request_id": str(uuid4())}})
        if source_run["status"] in ("completed", "failed", "cancelled"):
            raise HTTPException(409, detail={"error": {"code": "run_terminal", "message": f"Source run is '{source_run['status']}'.", "request_id": str(uuid4())}})

        handoff_id = str(uuid4())
        target_run_id = str(uuid4())
        context_package = body.context_package or {}

        from ..routers.runs import AGENT_SYSTEM_PROMPTS, DEFAULT_SYSTEM_PROMPT
        target_system_prompt = AGENT_SYSTEM_PROMPTS.get(body.to_agent, DEFAULT_SYSTEM_PROMPT)
        context_message = _build_context_message(source_run["agent_id"], body.reason, context_package)

        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO runs (run_id, tenant_id, agent_id, status, state_version, metadata)
                VALUES ($1, $2, $3, 'created', 0, $4)
                """,
                target_run_id, tenant_id, body.to_agent,
                json.dumps({
                    "conversation_history": [{"role": "user", "content": context_message}],
                    "system_prompt": target_system_prompt,
                    "handoff_id": handoff_id,
                    "source_run_id": run_id,
                }),
            )
            await conn.execute(
                """
                INSERT INTO handoffs
                    (handoff_id, run_id, target_run_id, tenant_id, from_agent, to_agent,
                     handoff_type, reason, context_package, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'active')
                """,
                handoff_id, run_id, target_run_id, tenant_id,
                source_run["agent_id"], body.to_agent,
                body.handoff_type, body.reason, json.dumps(context_package),
            )
            await conn.execute(
                "UPDATE runs SET status = 'completed', completed_at = NOW(), updated_at = NOW() WHERE run_id = $1",
                run_id,
            )
            await write_audit_event(
                conn, tenant_id=tenant_id, run_id=run_id,
                event_type="run.handoff_initiated", producer="agent-api",
                causation_id=handoff_id, correlation_id=run_id,
                data={"handoff_id": handoff_id, "to_agent": body.to_agent, "target_run_id": target_run_id},
            )
            await write_audit_event(
                conn, tenant_id=tenant_id, run_id=run_id,
                event_type="run.completed", producer="agent-api",
                causation_id=handoff_id, correlation_id=run_id,
                data={"reason": "handoff", "handoff_id": handoff_id},
            )

        row = await conn.fetchrow("SELECT * FROM handoffs WHERE handoff_id = $1", handoff_id)

    return HandoffResponse(
        handoff_id=row["handoff_id"],
        run_id=row["run_id"],
        target_run_id=row["target_run_id"],
        from_agent=row["from_agent"],
        to_agent=row["to_agent"],
        handoff_type=row["handoff_type"],
        reason=row["reason"],
        status=row["status"],
        created_at=row["created_at"],
    )


@router.get("/handoffs/{handoff_id}", response_model=HandoffResponse)
async def get_handoff(
    handoff_id: str,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> HandoffResponse:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM handoffs WHERE handoff_id = $1 AND tenant_id = $2",
            handoff_id, auth["tenant_id"],
        )
    if not row:
        raise HTTPException(404, detail={"error": {"code": "not_found", "message": "Handoff not found.", "request_id": str(uuid4())}})

    return HandoffResponse(
        handoff_id=row["handoff_id"],
        run_id=row["run_id"],
        target_run_id=row["target_run_id"],
        from_agent=row["from_agent"],
        to_agent=row["to_agent"],
        handoff_type=row["handoff_type"],
        reason=row["reason"],
        status=row["status"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )
