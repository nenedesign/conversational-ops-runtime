import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import settings
from .db import close_pool, get_pool, init_pool
from .routers import approvals, commands, handoffs, proposals, runs, tool_calls
from .tool_contracts import load_contracts
from .worker import dispatch_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_contracts()
    await init_pool()
    worker_task = asyncio.create_task(dispatch_loop(get_pool()))
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await close_pool()


app = FastAPI(
    title="Conversational Operations Runtime",
    version="0.1.0",
    description=(
        "Governed action runtime for AI agents. "
        "POST /v1/tool-calls is the primary entry point. "
        "Managed runs (/v1/runs) are optional."
    ),
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    from uuid import uuid4
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "An unexpected error occurred.", "request_id": str(uuid4())}},
    )


# Versioned prefix for all routes
PREFIX = "/v1"

app.include_router(runs.router, prefix=PREFIX)
app.include_router(handoffs.router, prefix=PREFIX)
app.include_router(tool_calls.router, prefix=PREFIX)
app.include_router(proposals.router, prefix=PREFIX)
app.include_router(approvals.router, prefix=PREFIX)
app.include_router(commands.router, prefix=PREFIX)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": settings.api_version}
