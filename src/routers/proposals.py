import json
from uuid import uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_auth
from ..db import get_pool
from ..models import ProposalResponse, ProposalVersionResponse

router = APIRouter(tags=["proposals"])


@router.get("/proposals/{proposal_id}", response_model=ProposalResponse)
async def get_proposal(
    proposal_id: str,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> ProposalResponse:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM proposals WHERE proposal_id = $1 AND tenant_id = $2",
            proposal_id, auth["tenant_id"],
        )
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "not_found", "message": "Proposal not found.", "request_id": str(uuid4())}},
        )
    return ProposalResponse(**dict(row))


@router.get("/proposals/{proposal_id}/versions", response_model=list[ProposalVersionResponse])
async def list_proposal_versions(
    proposal_id: str,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[ProposalVersionResponse]:
    async with pool.acquire() as conn:
        proposal = await conn.fetchrow(
            "SELECT proposal_id FROM proposals WHERE proposal_id = $1 AND tenant_id = $2",
            proposal_id, auth["tenant_id"],
        )
        if not proposal:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "not_found", "message": "Proposal not found.", "request_id": str(uuid4())}},
            )
        rows = await conn.fetch(
            "SELECT * FROM proposal_versions WHERE proposal_id = $1 ORDER BY version ASC",
            proposal_id,
        )
    return [
        ProposalVersionResponse(**{**dict(r), "content": json.loads(r["content"]) if isinstance(r["content"], str) else r["content"]})
        for r in rows
    ]


@router.get("/proposals/{proposal_id}/versions/{version}", response_model=ProposalVersionResponse)
async def get_proposal_version(
    proposal_id: str,
    version: int,
    auth: dict = Depends(require_auth),
    pool: asyncpg.Pool = Depends(get_pool),
) -> ProposalVersionResponse:
    async with pool.acquire() as conn:
        proposal = await conn.fetchrow(
            "SELECT proposal_id FROM proposals WHERE proposal_id = $1 AND tenant_id = $2",
            proposal_id, auth["tenant_id"],
        )
        if not proposal:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "not_found", "message": "Proposal not found.", "request_id": str(uuid4())}},
            )
        row = await conn.fetchrow(
            "SELECT * FROM proposal_versions WHERE proposal_id = $1 AND version = $2",
            proposal_id, version,
        )
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "not_found", "message": "Proposal version not found.", "request_id": str(uuid4())}},
        )
    data = dict(row)
    if isinstance(data["content"], str):
        data["content"] = json.loads(data["content"])
    return ProposalVersionResponse(**data)
