import json
from uuid import uuid4

import asyncpg


async def write_audit_event(
    conn: asyncpg.Connection,
    *,
    tenant_id: str,
    event_type: str,
    producer: str,
    causation_id: str,
    correlation_id: str,
    run_id: str | None = None,
    data: dict | None = None,
) -> str:
    event_id = str(uuid4())
    await conn.execute(
        """
        INSERT INTO audit_events
            (event_id, tenant_id, run_id, type, causation_id, correlation_id, producer, data)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        event_id,
        tenant_id,
        run_id,
        event_type,
        causation_id,
        correlation_id,
        producer,
        json.dumps(data or {}),
    )
    return event_id
