"""Trusted confirmation validation and the strict Phase 7 handoff."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, final

from vigi_vision.investigation_confirmation_models import (
    CONFIRMATION_KIND,
    CONFIRMATION_SCENARIO,
    CONFIRMATION_SCHEMA_VERSION,
    ConfirmationArtifactError,
    ConfirmationCandidateMismatchError,
    ConfirmationImageDimensionMismatchError,
    ConfirmationInvalidRoiError,
    ConfirmationManifest,
    ConfirmationRecord,
    ConfirmationReferenceFrame,
    ConfirmationRequest,
    ConfirmationRequestError,
    ConfirmationResult,
    ConfirmationTiming,
    ConfirmedInputInvalidError,
    ConfirmedInvestigationInput,
    artifact_relative_path,
    investigation_id_for,
)
from vigi_vision.investigation_confirmation_repository import InvestigationConfirmationRepository
from vigi_vision.reference_frame_models import ReferenceFrameError, parse_reference_frame_request

if TYPE_CHECKING:
    from collections.abc import Callable

    from vigi_vision.investigation_confirmation_repository import (
        InvestigationConfirmationRepository,
    )
    from vigi_vision.reference_frame_models import ReferenceFrameRequest
    from vigi_vision.reference_frame_resources import (
        ReferenceFrameResourceMetadata,
        ReferenceFrameResourceStore,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@final
@dataclass(frozen=True, slots=True)
class InvestigationConfirmationService:
    """Validate browser confirmation state against trusted persisted resources."""

    resource_store: ReferenceFrameResourceStore = field(repr=False)
    repository: InvestigationConfirmationRepository = field(repr=False)
    now_utc: Callable[[], datetime] = _utc_now

    def confirm(self, request: ConfirmationRequest) -> ConfirmationResult:
        """Create or reuse one immutable confirmation package."""
        resource = self.resource_store.resolve_resource(request.reference_frame_resource_id)
        anchor = self._anchor_request(resource, request)
        candidate_offset = _candidate_offset(resource.request, anchor)
        if request.candidate_offset_seconds != candidate_offset:
            raise ConfirmationCandidateMismatchError
        if request.source_width != resource.width or request.source_height != resource.height:
            raise ConfirmationImageDimensionMismatchError
        _validate_roi(request, resource.width, resource.height)
        manifest = self._manifest(resource, anchor, candidate_offset, request)
        return self.repository.publish(manifest)

    def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
        """Load a published schema 2 package and resolve its trusted JPEG."""
        manifest = self.repository.load(investigation_id)
        try:
            resource = self.repository.resolve_resource_for_manifest(manifest)
            _validate_loaded_manifest(manifest, resource)
        except (
            ConfirmationArtifactError,
            ConfirmationImageDimensionMismatchError,
            ConfirmationInvalidRoiError,
            ConfirmationCandidateMismatchError,
        ):
            raise ConfirmedInputInvalidError from None
        except ReferenceFrameError:
            raise ConfirmedInputInvalidError from None
        return ConfirmedInvestigationInput(
            investigation_id=manifest.investigation_id,
            channel_id=manifest.confirmation.channel_id,
            anchor_time_utc=manifest.anchor_time_utc,
            source_timezone=manifest.source_timezone,
            candidate_offset_seconds=manifest.confirmation.candidate_offset_seconds,
            reference_frame_resource_id=manifest.confirmation.reference_frame.resource_id,
            requested_time_text=manifest.confirmation.reference_frame.requested_time,
            requested_time_utc=manifest.confirmation.reference_frame.requested_time_utc,
            generation_policy_version=(
                manifest.confirmation.reference_frame.generation_policy_version
            ),
            frame_selection_policy=manifest.confirmation.reference_frame.frame_selection_policy.value,
            estimated_source_time_utc=manifest.confirmation.timing.estimated_source_time_utc,
            decoded_local_pts_seconds=manifest.confirmation.timing.decoded_local_pts_seconds,
            timing_precision_status=manifest.confirmation.timing.timing_precision_status.value,
            warnings=manifest.confirmation.timing.warnings,
            source_width=manifest.confirmation.reference_frame.width,
            source_height=manifest.confirmation.reference_frame.height,
            roi=manifest.confirmation.roi,
            jpeg_path=resource.jpeg_path,
        )

    def _anchor_request(
        self, resource: ReferenceFrameResourceMetadata, request: ConfirmationRequest
    ) -> ReferenceFrameRequest:
        anchor = parse_reference_frame_request(
            channel_id=resource.request.channel_id,
            requested_time_text=request.reference_time,
            source_timezone=request.source_timezone,
            now_utc=self.now_utc(),
        )
        if anchor.source_timezone != resource.request.source_timezone:
            raise ConfirmationCandidateMismatchError
        return anchor

    def _manifest(
        self,
        resource: ReferenceFrameResourceMetadata,
        anchor: ReferenceFrameRequest,
        candidate_offset: int,
        request: ConfirmationRequest,
    ) -> ConfirmationManifest:
        reference = ConfirmationReferenceFrame(
            resource_id=resource.resource_id,
            schema_version=resource.manifest_schema_version,
            generation_policy_version=resource.request.generation_policy_version,
            requested_time=resource.request.requested_time_text,
            requested_time_utc=resource.request.requested_time_utc,
            source_timezone=resource.request.source_timezone,
            frame_selection_policy=resource.request.frame_selection_policy,
            width=resource.width,
            height=resource.height,
        )
        timing = ConfirmationTiming(
            decoded_local_pts_seconds=resource.decoded_local_pts_seconds,
            estimated_source_time_utc=resource.estimated_source_time_utc,
            offset_from_requested_seconds=resource.offset_from_requested_seconds,
            timing_precision_status=resource.timing_precision_status,
            warnings=resource.warnings,
        )
        investigation_id = investigation_id_for(
            resource.request.channel_id, anchor.requested_time_utc
        )
        return ConfirmationManifest(
            schema_version=CONFIRMATION_SCHEMA_VERSION,
            investigation_id=investigation_id,
            investigation_kind=CONFIRMATION_KIND,
            scenario_id=CONFIRMATION_SCENARIO,
            status="confirmed",
            artifact_directory_relative=artifact_relative_path(investigation_id),
            anchor_time_utc=anchor.requested_time_utc,
            source_timezone=anchor.source_timezone,
            confirmed_at_utc=_canonical_now(self.now_utc),
            confirmation=ConfirmationRecord(
                channel_id=resource.request.channel_id,
                candidate_offset_seconds=candidate_offset,
                reference_frame=reference,
                timing=timing,
                roi=request.roi,
            ),
        )


def _candidate_offset(resource: ReferenceFrameRequest, anchor: ReferenceFrameRequest) -> int:
    difference = resource.requested_time_utc - anchor.requested_time_utc
    seconds = difference.total_seconds()
    if seconds != int(seconds):
        raise ConfirmationCandidateMismatchError
    return int(seconds)


def _validate_roi(request: ConfirmationRequest, width: int, height: int) -> None:
    roi = request.roi
    if roi.x + roi.width > width or roi.y + roi.height > height:
        raise ConfirmationInvalidRoiError


def _validate_loaded_manifest(
    manifest: ConfirmationManifest, resource: ReferenceFrameResourceMetadata
) -> None:
    expected_id = investigation_id_for(resource.request.channel_id, manifest.anchor_time_utc)
    if (
        manifest.investigation_id != expected_id
        or manifest.source_timezone != resource.request.source_timezone
        or manifest.confirmation.channel_id != resource.request.channel_id
        or manifest.confirmation.reference_frame.width != resource.width
        or manifest.confirmation.reference_frame.height != resource.height
    ):
        raise ConfirmationCandidateMismatchError
    anchor = resource.request.with_offset(-manifest.confirmation.candidate_offset_seconds)
    if anchor.requested_time_utc != manifest.anchor_time_utc:
        raise ConfirmationCandidateMismatchError
    derived = _candidate_offset(resource.request, anchor)
    if manifest.confirmation.candidate_offset_seconds != derived:
        raise ConfirmationCandidateMismatchError
    roi = manifest.confirmation.roi
    if roi.x + roi.width > resource.width or roi.y + roi.height > resource.height:
        raise ConfirmationInvalidRoiError


def _canonical_now(now_utc: Callable[[], datetime]) -> datetime:
    value = now_utc()
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ConfirmationRequestError
    return value.astimezone(timezone.utc).replace(microsecond=0)
