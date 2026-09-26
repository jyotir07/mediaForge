from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from app.api import artifacts, exports, health, jobs, media
from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.logging import configure_logging
from app.storage.local import Storage

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        app.state.settings = settings
        app.state.engine = create_engine(settings)
        app.state.session_factory = create_session_factory(app.state.engine)
        app.state.redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=5)
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        app.state.storage = Storage(settings.storage_root)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    app = FastAPI(title="MediaForge", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(media.router)
    app.include_router(jobs.router)
    app.include_router(exports.router)
    app.include_router(artifacts.router)
    # Mounted last so API routes always take precedence over the UI.
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app
