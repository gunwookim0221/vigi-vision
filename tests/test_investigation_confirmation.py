import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vigi_vision.investigation_confirmation_models import (
    ConfirmationCandidateMismatchError,
    ConfirmationConflictError,
    ConfirmationImageDimensionMismatchError,
    ConfirmationInProgressError,
    ConfirmationInvalidRoiError,
    ConfirmationOutcome,
    ConfirmationRequest,
    ConfirmationRoi,
    ConfirmedInputInvalidError,
    LegacyInvestigationError,
    RoiProvenance,
    artifact_relative_path,
)
from vigi_vision.investigation_confirmation_repository import (
    InvestigationConfirmationRepository,
)
from vigi_vision.investigation_confirmation_service import (
    InvestigationConfirmationService,
)
from vigi_vision.recording import RecordingSegment, RecordingWindow
from vigi_vision.reference_frame_artifacts import (
    ReferenceFrameArtifactStore,
    ReferenceFrameManifest,
)
from vigi_vision.reference_frame_models import (
    DecodedFrameEvidence,
    ReferenceFrameResourceCorruptError,
    TimingPrecisionStatus,
    parse_reference_frame_request,
)
from vigi_vision.reference_frame_resources import ReferenceFrameResourceStore

_JPEG_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjI4LjEwMgD/2wBDAAgEBAQEBAUFBQUFBQYGBgYGBgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABLAAEBAAAAAAAAAAAAAAAAAAAACAEBAAAAAAAAAAAAAAAAAAAAABABAAAAAAAAAAAAAAAAAAAAABEBAAAAAAAAAAAAAAAAAAAAAP/AABEIAtAFAAMBIgACEQADEQD/2gAMAwEAAhEDEQA/AJ/AB//Z"
)
_NOW = datetime(2026, 8, 2, 4, 5, 6, tzinfo=timezone.utc)


def test_confirmation_publishes_and_identical_retry_reuses_original_timestamp(
    tmp_path: Path,
) -> None:
    # Given
    context = _context(tmp_path)
    request = _request(context.resource_id)

    # When
    first = context.service.confirm(request)
    before = first.artifact_directory.joinpath("manifest.json").read_bytes()
    second = context.service.confirm(request)

    # Then
    assert first.outcome is ConfirmationOutcome.CREATED
    assert second.outcome is ConfirmationOutcome.REUSED
    assert second.manifest.confirmed_at_utc == first.manifest.confirmed_at_utc
    assert second.artifact_directory.joinpath("manifest.json").read_bytes() == before
    assert second.manifest.confirmation.reference_frame.resource_id == context.resource_id
    assert "frame.jpg" not in before.decode("utf-8")


def test_different_roi_conflicts_without_overwriting_existing_package(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    _ = context.service.confirm(_request(context.resource_id))
    conflicting = _request(context.resource_id, roi=(8, 120))
    manifest_path = context.investigation_root / context.investigation_id / "manifest.json"
    original = manifest_path.read_bytes()

    # When / Then
    with pytest.raises(ConfirmationConflictError):
        _ = context.service.confirm(conflicting)
    assert manifest_path.read_bytes() == original


def test_candidate_offset_is_a_stale_state_guard(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    stale = _request(context.resource_id, candidate_offset=-9)

    # When / Then
    with pytest.raises(ConfirmationCandidateMismatchError):
        _ = context.service.confirm(stale)
    assert not context.investigation_root.exists()


def test_service_rejects_roi_outside_trusted_dimensions(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    request = _request(context.resource_id, roi=(1278, 4))

    # When / Then
    with pytest.raises(ConfirmationInvalidRoiError):
        _ = context.service.confirm(request)


def test_confirmation_request_rejects_roi_outside_source_dimensions(
    tmp_path: Path,
) -> None:
    # Given
    context = _context(tmp_path)
    request = _request(context.resource_id, roi=(1278, 4))

    # When / Then
    with pytest.raises(ConfirmationInvalidRoiError):
        _ = context.service.confirm(request)


def test_validated_request_rejects_dimension_override(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    request = _request(context.resource_id, source_width=640)

    # When / Then
    with pytest.raises(ConfirmationImageDimensionMismatchError):
        _ = context.service.confirm(request)


def test_loader_returns_source_pixel_roi_and_trusted_jpeg(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    result = context.service.confirm(_request(context.resource_id))

    # When
    loaded = context.service.load_confirmed(result.manifest.investigation_id)

    # Then
    assert loaded.investigation_id == result.manifest.investigation_id
    assert loaded.roi.x == 10
    assert loaded.roi.width == 120
    assert loaded.source_width == 1280
    assert loaded.source_height == 720
    assert loaded.jpeg_path.read_bytes() == _JPEG_BYTES


def test_loader_rejects_legacy_manifest(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    legacy_id = context.investigation_root / "object-disappearance-ch1-20260720T033428Z"
    legacy_id.mkdir(parents=True)
    _ = legacy_id.joinpath("manifest.json").write_text('{"schema_version": 1}', encoding="utf-8")

    # When / Then
    with pytest.raises(LegacyInvestigationError):
        _ = context.service.load_confirmed(legacy_id.name)


def test_loader_revalidates_the_referenced_resource(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    result = context.service.confirm(_request(context.resource_id))
    context.resource_root.joinpath(context.resource_id, "frame.jpg").unlink()

    # When / Then
    with pytest.raises(ConfirmedInputInvalidError):
        _ = context.service.load_confirmed(result.manifest.investigation_id)


def test_missing_resource_fails_before_confirmation_publication(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    context.resource_root.joinpath(context.resource_id, "frame.jpg").unlink()

    # When / Then
    with pytest.raises(ReferenceFrameResourceCorruptError):
        _ = context.service.confirm(_request(context.resource_id))
    assert not context.investigation_root.exists()


def test_live_claim_is_not_recovered(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    claim = context.investigation_root / f".{context.investigation_id}.claim"
    _ = claim.parent.mkdir(parents=True, exist_ok=True)
    _ = claim.write_text(
        json.dumps(
            {
                "operation_id": "1234567890abcdef1234567890abcdef",
                "created_at_utc": "2026-08-02T04:00:00Z",
                "heartbeat_at_utc": "2026-08-02T04:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    # When / Then
    with pytest.raises(ConfirmationInProgressError):
        _ = context.service.confirm(_request(context.resource_id))
    assert claim.exists()


def test_stale_claim_is_recovered_without_deleting_unverifiable_claim(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    claim = context.investigation_root / f".{context.investigation_id}.claim"
    _ = claim.parent.mkdir(parents=True, exist_ok=True)
    _ = claim.write_text("not-json", encoding="utf-8")

    # When / Then
    with pytest.raises(ConfirmationInProgressError):
        _ = context.service.confirm(_request(context.resource_id))
    assert claim.read_text(encoding="utf-8") == "not-json"


def test_stale_claim_with_valid_metadata_is_replaced(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    claim = context.investigation_root / f".{context.investigation_id}.claim"
    _ = claim.parent.mkdir(parents=True, exist_ok=True)
    _ = claim.write_text(
        json.dumps(
            {
                "operation_id": "1234567890abcdef1234567890abcdef",
                "created_at_utc": "2026-08-01T03:00:00Z",
                "heartbeat_at_utc": "2026-08-01T03:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    # When
    result = context.service.confirm(_request(context.resource_id))

    # Then
    assert result.outcome is ConfirmationOutcome.CREATED
    assert not claim.exists()


def test_final_directory_wins_over_leftover_claim(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    first = context.service.confirm(_request(context.resource_id))
    claim = context.investigation_root / f".{context.investigation_id}.claim"
    _ = claim.write_text("not-json", encoding="utf-8")

    # When
    second = context.service.confirm(_request(context.resource_id))

    # Then
    assert second.outcome is ConfirmationOutcome.REUSED
    assert second.manifest.confirmed_at_utc == first.manifest.confirmed_at_utc
    assert claim.exists()


def test_canonical_artifact_path_is_credential_free(tmp_path: Path) -> None:
    # Given
    context = _context(tmp_path)
    result = context.service.confirm(_request(context.resource_id))

    # When
    manifest_text = result.artifact_directory.joinpath("manifest.json").read_text(encoding="utf-8")

    # Then
    assert result.manifest.artifact_directory_relative == artifact_relative_path(
        result.manifest.investigation_id
    )
    assert str(context.resource_root) not in manifest_text
    assert "frame.jpg" not in manifest_text


@dataclass(frozen=True, slots=True)
class Context:
    service: InvestigationConfirmationService
    resource_id: str
    resource_root: Path
    investigation_root: Path
    channel_id: int
    confirmation_anchor_time_utc: datetime

    @property
    def investigation_id(self) -> str:
        token = self.confirmation_anchor_time_utc.strftime("%Y%m%dT%H%M%SZ")
        return f"object-disappearance-v3-ch{self.channel_id}-{token}"


def _context(
    tmp_path: Path,
    *,
    channel_id: int = 1,
    requested_time_text: str = "2026-07-20T12:34:18",
    confirmation_time_text: str = "2026-07-20T12:34:28",
) -> Context:
    resource_root = tmp_path / "reference-frames"
    request = parse_reference_frame_request(
        channel_id=channel_id,
        requested_time_text=requested_time_text,
        source_timezone="Asia/Seoul",
        now_utc=_NOW,
    )
    confirmation_anchor = parse_reference_frame_request(
        channel_id=channel_id,
        requested_time_text=confirmation_time_text,
        source_timezone="Asia/Seoul",
        now_utc=_NOW,
    )
    segment = RecordingSegment(
        channel_id=channel_id,
        recording_day=request.requested_time_utc.date(),
        start_epoch_seconds=int((request.requested_time_utc - timedelta(minutes=1)).timestamp()),
        end_epoch_seconds=int((request.requested_time_utc + timedelta(minutes=1)).timestamp()),
        start_utc=request.requested_time_utc - timedelta(minutes=1),
        end_utc=request.requested_time_utc + timedelta(minutes=1),
    )
    session = ReferenceFrameArtifactStore(resource_root).begin(request, segment)
    _ = session.jpeg_path.write_bytes(_JPEG_BYTES)
    _ = session.finalize(
        ReferenceFrameManifest(
            request=request,
            segment=segment,
            extraction_window=RecordingWindow(
                channel_id,
                request.requested_time_utc - timedelta(seconds=2),
                request.requested_time_utc + timedelta(seconds=4),
            ),
            resource_id=session.resource_id,
            evidence=DecodedFrameEvidence(
                jpeg_path=session.jpeg_path,
                local_pts_seconds=2.0,
                width=1280,
                height=720,
                timing_precision_status=TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
                warnings=(),
            ),
            estimated_source_time_utc=None,
            offset_from_requested_seconds=None,
        )
    )
    investigation_root = tmp_path / "investigations"
    resources = ReferenceFrameResourceStore(resource_root)

    def clock() -> datetime:
        return _NOW

    repository = InvestigationConfirmationRepository(investigation_root, resources, clock)
    service = InvestigationConfirmationService(resources, repository, clock)
    return Context(
        service,
        session.resource_id,
        resource_root,
        investigation_root,
        channel_id,
        confirmation_anchor.requested_time_utc,
    )


def _request(
    resource_id: str,
    *,
    candidate_offset: int = -10,
    source_width: int = 1280,
    roi: tuple[int, int] = (10, 120),
) -> ConfirmationRequest:
    return ConfirmationRequest(
        reference_frame_resource_id=resource_id,
        reference_time="2026-07-20T12:34:28",
        source_timezone="Asia/Seoul",
        candidate_offset_seconds=candidate_offset,
        source_width=source_width,
        source_height=720,
        roi=ConfirmationRoi(
            x=roi[0],
            y=20,
            width=roi[1],
            height=80,
            coordinate_space="source_pixels",
            provenance=RoiProvenance.ASSISTED_THEN_ADJUSTED,
        ),
    )


def build_context(
    tmp_path: Path,
    *,
    channel_id: int = 1,
    requested_time_text: str = "2026-07-20T12:34:18",
    confirmation_time_text: str = "2026-07-20T12:34:28",
) -> Context:
    return _context(
        tmp_path,
        channel_id=channel_id,
        requested_time_text=requested_time_text,
        confirmation_time_text=confirmation_time_text,
    )


def build_request(resource_id: str, *, roi: tuple[int, int] = (10, 120)) -> ConfirmationRequest:
    return _request(resource_id, roi=roi)
