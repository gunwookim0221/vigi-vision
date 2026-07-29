from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from vigi_vision.reference_frame_candidate_api_models import (
    ReferenceFrameCandidateSetBody,
    parse_reference_frame_candidate_set_request,
)
from vigi_vision.reference_frame_candidate_models import DEFAULT_CANDIDATE_OFFSETS
from vigi_vision.reference_frame_models import ReferenceFrameInputError


def test_candidate_set_uses_default_offsets_and_naive_kst() -> None:
    request = parse_reference_frame_candidate_set_request(
        body=ReferenceFrameCandidateSetBody(channel_id=1, reference_time="2026-07-20 12:34:18"),
        now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert request.offsets_seconds == DEFAULT_CANDIDATE_OFFSETS
    assert request.reference_time.requested_time_utc == datetime(
        2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc
    )
    assert request.reference_time.source_timezone == "Asia/Seoul"


@pytest.mark.parametrize("offsets_seconds", [(0,), (-300, -1, 0, 1, 300)])
def test_candidate_set_accepts_one_or_five_offsets(offsets_seconds: tuple[int, ...]) -> None:
    body = ReferenceFrameCandidateSetBody(
        channel_id=1,
        reference_time="2026-07-20T03:34:18Z",
        offsets_seconds=offsets_seconds,
    )

    assert body.offsets_seconds == offsets_seconds


@pytest.mark.parametrize(
    "offsets_seconds",
    [(), (-1, 0, 1, 2, 3, 4), (-1, 0, 0), (-301,), (301,)],
)
def test_candidate_set_rejects_invalid_offset_cardinality_or_range(
    offsets_seconds: tuple[int, ...],
) -> None:
    with pytest.raises(ValidationError):
        _ = ReferenceFrameCandidateSetBody(
            channel_id=1,
            reference_time="2026-07-20T03:34:18Z",
            offsets_seconds=offsets_seconds,
        )


@pytest.mark.parametrize("offset", [True, 1.0, "1", None])
def test_candidate_set_rejects_non_integer_offsets(offset: float | str | None) -> None:
    with pytest.raises(ValidationError):
        _ = ReferenceFrameCandidateSetBody.model_validate(
            {
                "channel_id": 1,
                "reference_time": "2026-07-20T03:34:18Z",
                "offsets_seconds": [offset],
            }
        )


def test_candidate_set_preserves_explicit_timezone_and_derives_candidate_times() -> None:
    request = parse_reference_frame_candidate_set_request(
        body=ReferenceFrameCandidateSetBody(
            channel_id=1,
            reference_time="2026-07-20 00:00:10",
            source_timezone="Asia/Seoul",
            offsets_seconds=(-60, 0, 60),
        ),
        now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    candidates = request.candidates()

    assert tuple(candidate.offset_seconds for candidate in candidates) == (-60, 0, 60)
    assert tuple(candidate.request.requested_time_utc for candidate in candidates) == (
        datetime(2026, 7, 19, 14, 59, 10, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 15, 0, 10, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 15, 1, 10, tzinfo=timezone.utc),
    )
    assert all(candidate.request.source_timezone == "Asia/Seoul" for candidate in candidates)


def test_candidate_set_accepts_offset_aware_reference_time() -> None:
    request = parse_reference_frame_candidate_set_request(
        body=ReferenceFrameCandidateSetBody(
            channel_id=1,
            reference_time="2026-07-20T12:34:18+09:00",
            offsets_seconds=(0,),
        ),
        now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert request.reference_time.requested_time_utc == datetime(
        2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc
    )
    assert request.reference_time.source_timezone == "UTC+09:00"


def test_candidate_set_rejects_timestamp_underflow_safely() -> None:
    body = ReferenceFrameCandidateSetBody(
        channel_id=1,
        reference_time="0001-01-01T00:00:00Z",
        offsets_seconds=(-1,),
    )

    with pytest.raises(ReferenceFrameInputError):
        _ = parse_reference_frame_candidate_set_request(
            body=body,
            now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
        )
