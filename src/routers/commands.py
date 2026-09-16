from uuid import uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_auth
from ..db import get_pool
from ..models import CommandResponse

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
