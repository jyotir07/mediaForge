import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://mediaforge:mediaforge@postgres:5432/mediaforge_test"
)
# A separate Redis DB so a running compose worker never consumes test messages.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://redis:6379/15")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(database_url=TEST_DATABASE_URL, redis_url=TEST_REDIS_URL, storage_root=tmp_path / "media")


@pytest.fixture
def client_factory() -> Callable:
    @asynccontextmanager
    async def make(settings: Settings) -> AsyncIterator[AsyncClient]:
        app = create_app(settings)
        # httpx's ASGITransport does not run lifespan events, so drive them explicitly.
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                yield client

    return make
