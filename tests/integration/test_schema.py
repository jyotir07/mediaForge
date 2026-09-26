import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError

from app.models import Job, Media


async def _media(session) -> Media:
    media = Media(original_filename="a.mp4", storage_key="media/x/source.mp4", size_bytes=10)
    session.add(media)
    await session.flush()
    return media


async def test_two_live_jobs_cannot_share_an_idempotency_key(session):
    media = await _media(session)
    session.add(Job(media_id=media.id, type="probe", status="QUEUED", idempotency_key="k1"))
    await session.flush()

    session.add(Job(media_id=media.id, type="probe", status="QUEUED", idempotency_key="k1"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_failed_job_does_not_block_a_new_job_with_same_key(session):
    media = await _media(session)
    session.add(Job(media_id=media.id, type="probe", status="FAILED", idempotency_key="k1"))
    await session.flush()

    session.add(Job(media_id=media.id, type="probe", status="QUEUED", idempotency_key="k1"))
    await session.flush()


def test_migrations_downgrade_and_upgrade_roundtrip(alembic_cfg):
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")


def test_models_match_latest_migration(alembic_cfg):
    command.check(alembic_cfg)
