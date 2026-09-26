import asyncio
import logging
from collections.abc import Awaitable

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from app.logging import log_event

router = APIRouter()

CHECK_TIMEOUT_S = 2.0


async def _check(name: str, probe: Awaitable[object]) -> str:
    try:
        await asyncio.wait_for(probe, CHECK_TIMEOUT_S)
        return "ok"
    except Exception as exc:
        log_event("health.check_failed", level=logging.WARNING, dependency=name, error=repr(exc))
        return "error"


async def _ping_postgres(request: Request) -> None:
    async with request.app.state.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@router.get("/healthz")
async def healthz(request: Request, response: Response) -> dict[str, str]:
    postgres = await _check("postgres", _ping_postgres(request))
    redis = await _check("redis", request.app.state.redis.ping())
    healthy = postgres == "ok" and redis == "ok"
    response.status_code = 200 if healthy else 503
    return {"status": "ok" if healthy else "degraded", "postgres": postgres, "redis": redis}
