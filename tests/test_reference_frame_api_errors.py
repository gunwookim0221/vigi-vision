import pytest

from vigi_vision.ffmpeg import FfmpegUnavailableError
from vigi_vision.recording import RecordingUnavailableError
from vigi_vision.reference_frame_api_errors import domain_error
from vigi_vision.reference_frame_models import (
    ReferenceFrameArtifactConflictError,
    ReferenceFrameInputError,
    ReferenceFrameResourceCorruptError,
    ReferenceFrameResourceIncompatibleError,
    ReferenceFrameResourceNotFoundError,
)
from vigi_vision.replay import ReplayTimeoutError


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (ReferenceFrameInputError("invalid_time"), 422, "invalid_request"),
        (RecordingUnavailableError(), 404, "recording_unavailable"),
        (ReferenceFrameResourceNotFoundError(), 404, "resource_not_found"),
        (ReferenceFrameArtifactConflictError(), 409, "artifact_conflict"),
        (ReferenceFrameResourceIncompatibleError(), 409, "incompatible_resource"),
        (ReferenceFrameResourceCorruptError(), 500, "resource_corrupt"),
        (ReplayTimeoutError(), 504, "replay_timeout"),
        (FfmpegUnavailableError(), 503, "media_tool_unavailable"),
    ],
)
def test_domain_error_returns_fixed_safe_mapping(
    error: Exception, status_code: int, code: str
) -> None:
    # Given / When
    mapped = domain_error(error)

    # Then
    assert mapped.status_code == status_code
    assert mapped.code == code
    assert "rtsp://" not in mapped.message
    assert "password" not in mapped.message


def test_domain_error_redacts_unexpected_exception_text() -> None:
    # Given
    sensitive_marker = "rtsp://user:password@nvr.example/private"

    # When
    mapped = domain_error(RuntimeError(sensitive_marker))

    # Then
    assert mapped.status_code == 500
    assert mapped.code == "internal_error"
    assert sensitive_marker not in mapped.message
