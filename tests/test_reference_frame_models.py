import traceback
from datetime import datetime, timedelta, timezone

import pytest

from vigi_vision.recording import RecordingSegment
from vigi_vision.reference_frame_models import (
    ReferenceFrameInputError,
    ReferenceFrameRequest,
    UnsupportedReferenceFrameSourceError,
    build_reference_replay_window,
    parse_reference_frame_request,
)


def test_parse_reference_frame_request_normalizes_supported_naive_kst_time() -> None:
    # Given
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)

    # When
    request = parse_reference_frame_request(
        channel_id=1,
        requested_time_text="2026-07-20 12:34:18",
        source_timezone="Asia/Seoul",
        now_utc=now,
    )

    # Then
    assert request.requested_time_utc == datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc)
    assert request.source_timezone == "Asia/Seoul"


def test_parse_reference_frame_request_accepts_aware_time_without_redundant_timezone() -> None:
    # Given / When
    request = parse_reference_frame_request(
        channel_id=1,
        requested_time_text="2026-07-20T03:34:18+00:00",
        now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    # Then
    assert request.requested_time_utc == datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc)
    assert request.source_timezone == "UTC"


def test_parse_reference_frame_request_accepts_rfc3339_z_on_supported_python_versions() -> None:
    # Given / When
    request = parse_reference_frame_request(
        channel_id=1,
        requested_time_text="2026-07-20T03:34:18Z",
        now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    # Then
    assert request.requested_time_utc == datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc)
    assert request.source_timezone == "UTC"


def test_parse_reference_frame_request_rejects_aware_time_timezone_disagreement() -> None:
    # Given / When / Then
    with pytest.raises(ReferenceFrameInputError):
        _ = parse_reference_frame_request(
            channel_id=1,
            requested_time_text="2026-07-20T12:34:18+00:00",
            source_timezone="Asia/Seoul",
            now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
        )


def test_reference_frame_input_error_traceback_redacts_untrusted_time_text() -> None:
    # Given
    untrusted_time = "opaque-sensitive-marker"

    # When
    with pytest.raises(ReferenceFrameInputError) as exception_info:
        _ = parse_reference_frame_request(
            channel_id=1,
            requested_time_text=untrusted_time,
            now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
        )

    # Then
    rendered = "".join(
        traceback.format_exception(
            type(exception_info.value),
            exception_info.value,
            exception_info.value.__traceback__,
        )
    )
    assert untrusted_time not in rendered


def test_reference_frame_input_error_redacts_traversal_like_timezone() -> None:
    # Given
    untrusted_timezone = "../opaque-timezone-marker"

    # When
    with pytest.raises(ReferenceFrameInputError) as exception_info:
        _ = parse_reference_frame_request(
            channel_id=1,
            requested_time_text="2026-07-20 12:34:18",
            source_timezone=untrusted_timezone,
            now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
        )

    # Then
    rendered = "".join(
        traceback.format_exception(
            type(exception_info.value),
            exception_info.value,
            exception_info.value.__traceback__,
        )
    )
    assert untrusted_timezone not in rendered


@pytest.mark.parametrize("channel_id", [0, -1])
def test_parse_reference_frame_request_rejects_invalid_channel(channel_id: int) -> None:
    # Given / When / Then
    with pytest.raises(ReferenceFrameInputError):
        _ = parse_reference_frame_request(
            channel_id=channel_id,
            requested_time_text="2026-07-20 12:34:18",
            source_timezone="Asia/Seoul",
            now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    ("requested_time", "source_timezone"),
    [
        ("not-a-time", "Asia/Seoul"),
        ("2026-07-20 12:34:18", "Invalid/Timezone"),
        ("2026-07-20 12:34:18.500", "Asia/Seoul"),
    ],
)
def test_parse_reference_frame_request_rejects_malformed_time_or_timezone(
    requested_time: str, source_timezone: str
) -> None:
    # Given / When / Then
    with pytest.raises(ReferenceFrameInputError):
        _ = parse_reference_frame_request(
            channel_id=1,
            requested_time_text=requested_time,
            source_timezone=source_timezone,
            now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    "requested_time",
    [
        "2026-11-01 01:30:00",
        "2026-03-08 02:30:00",
    ],
)
def test_parse_reference_frame_request_rejects_ambiguous_or_nonexistent_dst_time(
    requested_time: str,
) -> None:
    # Given / When / Then
    with pytest.raises(ReferenceFrameInputError):
        _ = parse_reference_frame_request(
            channel_id=1,
            requested_time_text=requested_time,
            source_timezone="America/New_York",
            now_utc=datetime(2026, 12, 1, tzinfo=timezone.utc),
        )


def test_parse_reference_frame_request_rejects_future_and_non_nvr_source() -> None:
    # Given / When / Then
    with pytest.raises(ReferenceFrameInputError):
        _ = parse_reference_frame_request(
            channel_id=1,
            requested_time_text="2026-07-22 12:34:18",
            source_timezone="Asia/Seoul",
            now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
        )
    with pytest.raises(UnsupportedReferenceFrameSourceError):
        _ = parse_reference_frame_request(
            channel_id=1,
            requested_time_text="2026-07-20 12:34:18",
            source_kind="ipc",
            now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
        )


def test_replay_window_clips_to_selected_segment_boundary() -> None:
    # Given
    request = parse_reference_frame_request(
        channel_id=1,
        requested_time_text="2026-07-20 12:34:18",
        now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    segment = RecordingSegment(
        1,
        request.requested_time_utc.date(),
        int(request.requested_time_utc.timestamp()),
        int((request.requested_time_utc + timedelta(seconds=3)).timestamp()),
        request.requested_time_utc,
        request.requested_time_utc + timedelta(seconds=3),
    )

    # When
    window = build_reference_replay_window(request, segment)

    # Then
    assert window.start_utc == segment.start_utc
    assert window.end_utc == segment.end_utc


def test_replay_window_clips_nominal_offsets_instead_of_sliding() -> None:
    # Given
    request = parse_reference_frame_request(
        channel_id=1,
        requested_time_text="2026-07-20 12:34:18",
        now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    segment = RecordingSegment(
        1,
        request.requested_time_utc.date(),
        int(request.requested_time_utc.timestamp()),
        int((request.requested_time_utc + timedelta(seconds=10)).timestamp()),
        request.requested_time_utc,
        request.requested_time_utc + timedelta(seconds=10),
    )

    # When
    window = build_reference_replay_window(request, segment)

    # Then
    assert window.start_utc == request.requested_time_utc
    assert window.end_utc == request.requested_time_utc + timedelta(seconds=4)


def test_reference_frame_request_rejects_invalid_generation_policy_version() -> None:
    # Given / When / Then
    with pytest.raises(ReferenceFrameInputError):
        _ = ReferenceFrameRequest(
            channel_id=1,
            requested_time_text="2026-07-20T03:34:18Z",
            source_timezone="UTC",
            requested_time_utc=datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc),
            generation_policy_version=0,
        )
