"""
Command worker — Phase 2.

Polls the outbox for pending commands and dispatches them to the provider.
Handles all four CommitResult outcomes:
  committed  → command.succeeded
  rejected   → command.failed
  duplicate  → command.succeeded (idempotent)
  unknown    → command.unknown + reconciliation_task created

The provider call happens outside any database connection to avoid holding
a connection during network I/O. Two transactions bracket the call:
  TX1 — claim outbox event, create attempt, mark command dispatched
  TX2 — record outcome, update command + attempt + outbox, write audit events

If the worker crashes between TX1 and TX2, the lease expires and the event
is reclaimed. The provider's idempotency key ensures the second commit call
returns "duplicate", which is treated as succeeded.
"""

import asyncio
import json
import logging
from uuid import uuid4

import asyncpg

from .audit import write_audit_event
from .provider import CommitCorrectionRequest, ReconcileRequest, provider as _provider

logger = logging.getLogger(__name__)

WORKER_ID = "worker-1"
LEASE_SECONDS = 30
POLL_INTERVAL = 2  # seconds between polls when the queue is empty


async def dispatch_loop(pool: asyncpg.Pool) -> None:
    """Long-running background task. Polls the outbox and dispatches commands."""
    while True:
        try:
            dispatched = await _try_dispatch(pool)
            if not dispatched:
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("dispatch_loop unhandled error")
            await asyncio.sleep(POLL_INTERVAL)


async def _try_dispatch(pool: asyncpg.Pool) -> bool:
    """
    Claim and dispatch one pending outbox event.

    Returns True if an event was found and processed, False if the queue
    was empty (caller should back off before next poll).
    """
    # ── TX1: claim event, create attempt, mark command dispatched ────────────
    async with pool.acquire() as conn:
        async with conn.transaction():
            outbox_row = await conn.fetchrow(
                """
                UPDATE outbox
                SET status = 'claimed',
                    claimed_at = NOW(),
                    lease_expires_at = NOW() + ($1 * INTERVAL '1 second'),
                    worker_id = $2,
                    attempt_number = attempt_number + 1,
                    updated_at = NOW()
                WHERE event_id = (
                    SELECT event_id FROM outbox
                    WHERE status = 'pending'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
                    ORDER BY created_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                LEASE_SECONDS, WORKER_ID,
            )

            if outbox_row is None:
                return False

            command_id = outbox_row["command_id"]
            attempt_number = outbox_row["attempt_number"]

            command = await conn.fetchrow(
                "SELECT * FROM commands WHERE command_id = $1",
                command_id,
            )

            if command is None or command["status"] not in ("authorized",):
                # Already in a terminal state — skip silently.
                await conn.execute(
                    "UPDATE outbox SET status = 'completed', updated_at = NOW() WHERE event_id = $1",
                    outbox_row["event_id"],
                )
                return True

            pv = await conn.fetchrow(
                "SELECT content FROM proposal_versions WHERE proposal_id = $1 AND version = $2",
                command["proposal_id"], command["proposal_version"],
            )
            content: dict = (
                json.loads(pv["content"])
                if isinstance(pv["content"], str)
                else pv["content"]
            )

            attempt_id = str(uuid4())
            await conn.execute(
                """
                INSERT INTO command_attempts
                    (attempt_id, command_id, attempt_number, worker_id,
                     status, request_payload)
                VALUES ($1, $2, $3, $4, 'in_progress', $5)
                """,
                attempt_id, command_id, attempt_number, WORKER_ID,
                json.dumps({"employee_id": content.get("employee_id", "")}),
            )

            await conn.execute(
                """
                UPDATE commands
                SET status = 'dispatched', dispatched_at = NOW()
                WHERE command_id = $1
                """,
                command_id,
            )
            await conn.execute(
                "UPDATE outbox SET status = 'dispatched', updated_at = NOW() WHERE event_id = $1",
                outbox_row["event_id"],
            )

            correlation = command["run_id"] or command_id
            await write_audit_event(
                conn,
                tenant_id=command["tenant_id"],
                run_id=command["run_id"],
                event_type="command.dispatched",
                producer="command-worker",
                causation_id=command_id,
                correlation_id=correlation,
                data={
                    "command_id": command_id,
                    "attempt": attempt_number,
                    "worker_id": WORKER_ID,
                },
            )

    # ── Provider call — outside any connection ────────────────────────────────
    commit_req = CommitCorrectionRequest(
        employee_id=content.get("employee_id", ""),
        correction_type=content.get("correction_type", ""),
        approved_content=content,
        approver_id=command["actor_id"],
        approved_proposal_version=command["proposal_version"],
        tenant_id=command["tenant_id"],
    )

    try:
        result = await _provider.commit_correction(commit_req, command["idempotency_key"])
    except Exception as exc:
        # Unexpected exception — treat as unknown so reconciliation can resolve it
        logger.exception("commit_correction raised unexpectedly: %s", exc)

        class _UnknownResult:
            status = "unknown"
            provider_reference = None
            rejection_reason = str(exc)
            is_duplicate = False

        result = _UnknownResult()  # type: ignore[assignment]

    # ── TX2: record outcome ───────────────────────────────────────────────────
    async with pool.acquire() as conn:
        async with conn.transaction():
            correlation = command["run_id"] or command_id

            if result.status in ("committed", "duplicate"):
                await conn.execute(
                    """
                    UPDATE commands
                    SET status = 'succeeded',
                        downstream_reference = $1,
                        completed_at = NOW()
                    WHERE command_id = $2
                    """,
                    result.provider_reference,
                    command_id,
                )
                await conn.execute(
                    """
                    UPDATE command_attempts
                    SET status = 'succeeded',
                        provider_reference = $1,
                        completed_at = NOW()
                    WHERE attempt_id = $2
                    """,
                    result.provider_reference,
                    attempt_id,
                )
                await conn.execute(
                    "UPDATE outbox SET status = 'completed', updated_at = NOW() WHERE event_id = $1",
                    outbox_row["event_id"],
                )
                await write_audit_event(
                    conn,
                    tenant_id=command["tenant_id"],
                    run_id=command["run_id"],
                    event_type="command.succeeded",
                    producer="command-worker",
                    causation_id=command_id,
                    correlation_id=correlation,
                    data={
                        "command_id": command_id,
                        "provider_reference": result.provider_reference,
                        "is_duplicate": result.is_duplicate,
                    },
                )

            elif result.status == "rejected":
                await conn.execute(
                    """
                    UPDATE commands
                    SET status = 'failed', completed_at = NOW()
                    WHERE command_id = $1
                    """,
                    command_id,
                )
                await conn.execute(
                    """
                    UPDATE command_attempts
                    SET status = 'failed',
                        error = $1,
                        completed_at = NOW()
                    WHERE attempt_id = $2
                    """,
                    result.rejection_reason,
                    attempt_id,
                )
                await conn.execute(
                    "UPDATE outbox SET status = 'completed', updated_at = NOW() WHERE event_id = $1",
                    outbox_row["event_id"],
                )
                await write_audit_event(
                    conn,
                    tenant_id=command["tenant_id"],
                    run_id=command["run_id"],
                    event_type="command.failed",
                    producer="command-worker",
                    causation_id=command_id,
                    correlation_id=correlation,
                    data={
                        "command_id": command_id,
                        "rejection_reason": result.rejection_reason,
                    },
                )

            elif result.status == "unknown":
                task_id = str(uuid4())
                await conn.execute(
                    "UPDATE commands SET status = 'unknown' WHERE command_id = $1",
                    command_id,
                )
                await conn.execute(
                    """
                    UPDATE command_attempts
                    SET status = 'unknown', completed_at = NOW()
                    WHERE attempt_id = $1
                    """,
                    attempt_id,
                )
                await conn.execute(
                    """
                    UPDATE outbox
                    SET status = 'reconcile_required', updated_at = NOW()
                    WHERE event_id = $1
                    """,
                    outbox_row["event_id"],
                )
                await conn.execute(
                    """
                    INSERT INTO reconciliation_tasks
                        (task_id, command_id, tenant_id, status,
                         idempotency_key, attempts)
                    VALUES ($1, $2, $3, 'pending', $4, 1)
                    """,
                    task_id,
                    command_id,
                    command["tenant_id"],
                    command["idempotency_key"],
                )
                await write_audit_event(
                    conn,
                    tenant_id=command["tenant_id"],
                    run_id=command["run_id"],
                    event_type="command.unknown",
                    producer="command-worker",
                    causation_id=command_id,
                    correlation_id=correlation,
                    data={
                        "command_id": command_id,
                        "attempt": attempt_number,
                        "reconciliation_task_id": task_id,
                    },
                )

    return True
