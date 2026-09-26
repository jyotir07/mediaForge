"""Idempotent building blocks shared by media job handlers."""

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.jobs.errors import JobError
from app.jobs.states import Stage
from app.media.ffmpeg import run_ffmpeg
from app.models import Artifact
from app.storage.local import media_key
from app.workers.context import JobContext


def ffmpeg_timeout(duration_seconds: float) -> float:
    return max(120.0, duration_seconds * 10)


async def upsert_artifact(
    ctx: JobContext, type_: str, key: str, mime_type: str, metadata: dict[str, Any] | None = None
) -> uuid.UUID:
    """Register an artifact; storage_key is unique, so a retried step updates instead of duplicating."""
    size = ctx.storage.path(key).stat().st_size
    stmt = (
        pg_insert(Artifact)
        .values(
            id=uuid.uuid4(),
            media_id=ctx.media.id,
            job_id=ctx.job.id,
            type=type_,
            storage_key=key,
            mime_type=mime_type,
            size_bytes=size,
            metadata_=metadata,
        )
        .on_conflict_do_update(
            index_elements=[Artifact.storage_key],
            set_={"size_bytes": size, "job_id": ctx.job.id, "metadata": metadata},
        )
        .returning(Artifact.id)
    )
    async with ctx.session_factory() as s:
        artifact_id = (await s.execute(stmt)).scalar_one()
        await s.commit()
    return artifact_id


@asynccontextmanager
async def logged(ctx: JobContext, step: str) -> AsyncIterator[Path]:
    """Yield a log path for an ffmpeg step. On failure the full log is kept as a LOG artifact (the job row
    only stores a bounded summary); on success it is discarded."""
    log_key = media_key(ctx.media.id, "logs", f"{ctx.job.id}-a{ctx.job.attempt}-{step}.log")
    path = ctx.storage.path(log_key)
    try:
        yield path
    except JobError:
        if path.exists():
            await upsert_artifact(ctx, "LOG", log_key, "text/plain")
        raise
    ctx.storage.delete(log_key)


async def produce(
    ctx: JobContext,
    key: str,
    build_args: Callable[[Path], list[str]],
    *,
    step: str,
    stage: Stage,
    message: str,
    total_seconds: float | None,
    progress_range: tuple[float, float] = (0.0, 1.0),
) -> bool:
    """Create `key` with ffmpeg unless it already exists. Output goes to a temp file that is atomically
    renamed, so an existing key is always a complete artifact. Returns True if ffmpeg ran."""
    if ctx.storage.exists(key):
        return False

    lo, hi = progress_range

    async def on_progress(fraction: float) -> None:
        await ctx.progress(stage, lo + (hi - lo) * fraction, message)

    await ctx.progress(stage, lo, message, force=True)
    tmp = ctx.storage.tmp_path(key)
    try:
        async with logged(ctx, step) as log_path:
            await run_ffmpeg(
                build_args(tmp),
                total_seconds=total_seconds,
                on_progress=on_progress,
                log_path=log_path,
                timeout_s=ffmpeg_timeout(total_seconds or 0),
            )
        ctx.storage.commit(tmp, key)
    finally:
        tmp.unlink(missing_ok=True)
    return True
