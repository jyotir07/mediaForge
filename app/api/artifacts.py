import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Artifact, EditPlan
from app.storage.local import Storage

router = APIRouter()


class EditPlanOut(BaseModel):
    id: uuid.UUID
    media_id: uuid.UUID
    request: str
    target_duration_seconds: float | None
    decisions: list[dict[str, Any]]
    accepted_operations: list[dict[str, Any]]


@router.get("/artifacts/{artifact_id}/file")
async def get_artifact_file(
    artifact_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> FileResponse:
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(404, "artifact not found")
    storage: Storage = request.app.state.storage
    return FileResponse(storage.path(artifact.storage_key), media_type=artifact.mime_type)


@router.get("/edit-plans/{plan_id}")
async def get_edit_plan(plan_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> EditPlanOut:
    plan = await session.get(EditPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "edit plan not found")
    return EditPlanOut(
        id=plan.id,
        media_id=plan.media_id,
        request=plan.request,
        target_duration_seconds=plan.candidate_data.get("target_duration_seconds"),
        decisions=plan.decisions,
        accepted_operations=plan.accepted_operations,
    )
