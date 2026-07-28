from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vigi_vision.recording import RecordingSegment, RecordingWindow
from vigi_vision.reference_frame_artifacts import (
    ReferenceFrameArtifactStore,
    ReferenceFrameManifest,
)
from vigi_vision.reference_frame_models import (
    DecodedFrameEvidence,
    ReferenceFrameRequest,
    ReferenceFrameResourceCorruptError,
    ReferenceFrameResourceIncompatibleError,
    ReferenceFrameResourceNotFoundError,
    TimingPrecisionStatus,
    parse_reference_frame_request,
    segment_identity,
)
from vigi_vision.reference_frame_resources import ReferenceFrameResourceStore

_JPEG_BYTES = b"\xff\xd8\xff\xe0reference-frame\xff\xd9"


def test_resource_lookup_returns_compatible_result_and_fixed_image(tmp_path: Path) -> None:
    # Given
    resources, request, segment, resource_id = _completed_resource(tmp_path)

    # When
    result = resources.resolve_for_request(request, segment)
    image = resources.resolve_image(resource_id)

    # Then
    assert result is not None
    assert result.resource_id == resource_id
    assert result.jpeg_relative_path == Path(resource_id) / "frame.jpg"
    assert image.jpeg_path.read_bytes() == _JPEG_BYTES


def test_completed_resource_lookup_rejects_corrupt_manifest(tmp_path: Path) -> None:
    # Given
    resources, request, segment, resource_id = _completed_resource(tmp_path)
    manifest_path = tmp_path / "artifacts" / resource_id / "manifest.json"
    _ = manifest_path.write_text("{}", encoding="utf-8")

    # When / Then
    with pytest.raises(ReferenceFrameResourceCorruptError):
        _ = resources.resolve_for_request(request, segment)


def test_completed_resource_lookup_rejects_missing_jpeg(tmp_path: Path) -> None:
    # Given
    resources, request, segment, resource_id = _completed_resource(tmp_path)
    jpeg_path = tmp_path / "artifacts" / resource_id / "frame.jpg"
    jpeg_path.unlink()

    # When / Then
    with pytest.raises(ReferenceFrameResourceCorruptError):
        _ = resources.resolve_for_request(request, segment)


def test_completed_resource_lookup_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    # Given
    resources, request, segment, resource_id = _completed_resource(tmp_path)
    manifest_path = tmp_path / "artifacts" / resource_id / "manifest.json"
    manifest = manifest_path.read_text(encoding="utf-8")
    _ = manifest_path.write_text(
        manifest.replace(resource_id, "other-resource", 1), encoding="utf-8"
    )

    # When / Then
    with pytest.raises(ReferenceFrameResourceCorruptError):
        _ = resources.resolve_for_request(request, segment)


def test_completed_resource_lookup_rejects_incompatible_manifest(tmp_path: Path) -> None:
    # Given
    resources, request, segment, resource_id = _completed_resource(tmp_path)
    manifest_path = tmp_path / "artifacts" / resource_id / "manifest.json"
    manifest = manifest_path.read_text(encoding="utf-8")
    _ = manifest_path.write_text(
        manifest.replace(
            f'"selected_segment_id": "{segment_identity(segment)}"',
            '"selected_segment_id": "segment-incompatible"',
            1,
        ),
        encoding="utf-8",
    )

    # When / Then
    with pytest.raises(ReferenceFrameResourceIncompatibleError):
        _ = resources.resolve_for_request(request, segment)


@pytest.mark.parametrize("resource_id", ["../frame.jpg", "C:\\private.jpg", "invalid%2fpath"])
def test_image_lookup_rejects_non_resource_identifier(tmp_path: Path, resource_id: str) -> None:
    # Given
    resources = ReferenceFrameResourceStore(tmp_path / "artifacts")

    # When / Then
    with pytest.raises(ReferenceFrameResourceNotFoundError):
        _ = resources.resolve_image(resource_id)


def _completed_resource(
    tmp_path: Path,
) -> tuple[ReferenceFrameResourceStore, ReferenceFrameRequest, RecordingSegment, str]:
    request = parse_reference_frame_request(
        channel_id=1,
        requested_time_text="2026-07-20T03:34:18Z",
        now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    segment = RecordingSegment(
        channel_id=1,
        recording_day=request.requested_time_utc.date(),
        start_epoch_seconds=int((request.requested_time_utc - timedelta(minutes=1)).timestamp()),
        end_epoch_seconds=int((request.requested_time_utc + timedelta(minutes=1)).timestamp()),
        start_utc=request.requested_time_utc - timedelta(minutes=1),
        end_utc=request.requested_time_utc + timedelta(minutes=1),
    )
    artifact_root = tmp_path / "artifacts"
    session = ReferenceFrameArtifactStore(artifact_root).begin(request, segment)
    _ = session.jpeg_path.write_bytes(_JPEG_BYTES)
    evidence = DecodedFrameEvidence(
        jpeg_path=session.jpeg_path,
        local_pts_seconds=2.0,
        width=1280,
        height=720,
        timing_precision_status=TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
        warnings=(),
    )
    manifest = ReferenceFrameManifest(
        request=request,
        segment=segment,
        extraction_window=RecordingWindow(
            1,
            request.requested_time_utc - timedelta(seconds=2),
            request.requested_time_utc + timedelta(seconds=4),
        ),
        resource_id=session.resource_id,
        evidence=evidence,
        estimated_source_time_utc=None,
        offset_from_requested_seconds=None,
    )
    _ = session.finalize(manifest)
    return ReferenceFrameResourceStore(artifact_root), request, segment, session.resource_id
