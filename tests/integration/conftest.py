from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base
from app.storage.local import Storage
from app.workers.runner import Worker
from tests.conftest import TEST_DATABASE_URL


@pytest.fixture(scope="session")
def alembic_cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return cfg


@pytest.fixture(scope="session", autouse=True)
def migrated_db(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "head")


@pytest.fixture(autouse=True)
async def clean_db() -> AsyncIterator[None]:
    yield
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
            await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    finally:
        await engine.dispose()


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            yield s
            await s.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def redis(settings) -> AsyncIterator[Redis]:
    r = Redis.from_url(settings.redis_url)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.fixture
def storage(settings) -> Storage:
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    return Storage(settings.storage_root)


@pytest.fixture
def worker(settings, session_factory, redis, storage) -> Worker:
    return Worker(settings, session_factory, redis, storage, lease_s=2, heartbeat_s=0.2, dequeue_timeout_s=1)
