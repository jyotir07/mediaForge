from pydantic import BaseModel, Field


class SceneDescription(BaseModel):
    segment_index: int
    summary: str
    relevance: float = Field(ge=0, le=1, description="How strong a highlight this segment is, 0-1.")


class SceneAnalysis(BaseModel):
    overall_summary: str
    scenes: list[SceneDescription]


class ProposedClip(BaseModel):
    start: float
    end: float
    reason: str
    source_segments: list[int]


class EditProposal(BaseModel):
    target_duration_seconds: float = Field(gt=0, le=600)
    clips: list[ProposedClip]
