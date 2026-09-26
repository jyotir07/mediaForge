import uuid

import pytest
from pydantic import ValidationError

from app.decision import ClipValue
from app.jobs.errors import ErrorCode, JobError
from app.schemas.analysis import ProposedClip
from app.schemas.edl import EDL, TrimOp
from app.services.edl import Kept, build_edl, validate_edl

MID = uuid.UUID("00000000-0000-0000-0000-00000000000a")


def kept(start, end, value=ClipValue.HIGH_VALUE, relevance=0.5) -> Kept:
    return Kept(ProposedClip(start=start, end=end, reason="r", source_segments=[]), value, relevance)


def spans(edl: EDL):
    return [(op.start, op.end) for op in edl.operations]


def test_fills_target_and_trims_last_clip_in_chronological_order():
    clips = [kept(s, s + 10, relevance=r) for s, r in [(80, 0.5), (0, 0.9), (40, 0.8), (20, 0.7), (60, 0.6)]]
    edl = build_edl(MID, 100.0, clips, target=45.0)
    assert spans(edl) == [(0, 10), (20, 30), (40, 50), (60, 70), (80, 85)]
    assert edl.duration == pytest.approx(45.0)


def test_high_value_clips_are_chosen_before_medium():
    clips = [kept(0, 10, ClipValue.MEDIUM_VALUE, 0.99), kept(50, 60, ClipValue.HIGH_VALUE, 0.1)]
    assert spans(build_edl(MID, 100.0, clips, target=10.0)) == [(50, 60)]


def test_overlapping_clips_merge():
    edl = build_edl(MID, 100.0, [kept(5, 15), kept(10, 20)], target=45.0)
    assert spans(edl) == [(5, 20)]


def test_low_value_clips_are_never_used():
    with pytest.raises(JobError) as exc:
        build_edl(MID, 100.0, [kept(0, 10, ClipValue.LOW_VALUE)], target=10.0)
    assert exc.value.code is ErrorCode.NO_CLIPS_SELECTED


def test_no_candidates_is_no_clips_selected():
    with pytest.raises(JobError) as exc:
        build_edl(MID, 100.0, [], target=10.0)
    assert exc.value.code is ErrorCode.NO_CLIPS_SELECTED


def test_leftover_shorter_than_min_len_is_not_added():
    edl = build_edl(MID, 100.0, [kept(0, 10), kept(20, 30)], target=10.4, min_len=1.0)
    assert spans(edl) == [(0, 10)]


def test_edl_model_rejects_bad_operations():
    with pytest.raises(ValidationError):
        TrimOp(start=5, end=5)
    with pytest.raises(ValidationError):
        TrimOp(start=-1, end=5)
    with pytest.raises(ValidationError):
        EDL(source_media_id=MID, operations=[])
    with pytest.raises(ValidationError):  # overlapping / unsorted
        EDL(source_media_id=MID, operations=[TrimOp(start=10, end=20), TrimOp(start=15, end=25)])


def test_validate_edl_rejects_range_past_media_end():
    edl = EDL(source_media_id=MID, operations=[TrimOp(start=10, end=90)])
    with pytest.raises(JobError) as exc:
        validate_edl(edl, media_duration=60.0)
    assert exc.value.code is ErrorCode.INVALID_RANGE
    validate_edl(EDL(source_media_id=MID, operations=[TrimOp(start=10, end=60)]), media_duration=60.0)
