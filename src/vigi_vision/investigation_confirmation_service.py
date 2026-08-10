"""Trusted confirmation validation and the strict Phase 7 handoff."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, final

from vigi_vision.investigation_confirmation_integrity import (
    JpegDecoder,
    JpegIntegrity,
    compute_jpeg_integrity,
)
from vigi_vision.investigation_confirmation_models import (
    CONFIRMATION_KIND,
    CONFIRMATION_SCENARIO,
    CONFIRMATION_SCHEMA_TWO,
    CONFIRMATION_SCHEMA_VERSION,
    ConfirmationArtifactError,
    ConfirmationCandidateMismatchError,
    ConfirmationConflictError,
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
    LegacyInvestigationError,
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
    jpeg_decoder: JpegDecoder | None = field(default=None, repr=False)

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
        integrity = compute_jpeg_integrity(
            resource.jpeg_path,
            resource.width,
            resource.height,
            self.jpeg_decoder,
        )
        manifest = self._manifest(resource, anchor, candidate_offset, request, integrity)
        return self.repository.publish(manifest)

    def reconfirm_for_recording_search(self, investigation_id: str) -> ConfirmationResult:
        """Reconfirm one immutable schema 2 package as a new schema 3 package."""
        legacy, resource = self._validated_manifest(investigation_id, allow_schema_two=True)
        if legacy.schema_version != CONFIRMATION_SCHEMA_TWO:
            raise ConfirmationConflictError
        integrity = compute_jpeg_integrity(
            resource.jpeg_path,
            resource.width,
            resource.height,
            self.jpeg_decoder,
        )
        manifest = _schema_three_manifest(legacy, resource, integrity, self.now_utc)
        return self.repository.publish(manifest)

    def load_confirmation_manifest(self, investigation_id: str) -> ConfirmationManifest:
        """Load one strictly revalidated manifest for the Phase 6 API."""
        manifest, _ = self._validated_manifest(investigation_id, allow_schema_two=True)
        return manifest

    def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
        """Load a published schema 3 package and resolve its trusted JPEG."""
        manifest, resource = self._validated_manifest(investigation_id)
        if manifest.schema_version != CONFIRMATION_SCHEMA_VERSION:
            raise LegacyInvestigationError
        reference = manifest.confirmation.reference_frame
        if reference.jpeg_sha256 is None or reference.jpeg_size_bytes is None:
            raise ConfirmedInputInvalidError
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
            jpeg_sha256=reference.jpeg_sha256,
            jpeg_size_bytes=reference.jpeg_size_bytes,
            jpeg_path=resource.jpeg_path,
        )

    def _validated_manifest(
        self,
        investigation_id: str,
        *,
        allow_schema_two: bool = False,
    ) -> tuple[ConfirmationManifest, ReferenceFrameResourceMetadata]:
        """Load and revalidate one manifest against its immutable frame resource."""
        manifest = self.repository.load(investigation_id)
        try:
            resource = self.repository.resolve_resource_for_manifest(manifest)
            _validate_loaded_manifest(manifest, resource)
            if manifest.schema_version == CONFIRMATION_SCHEMA_VERSION:
                expected = compute_jpeg_integrity(
                    resource.jpeg_path,
                    resource.width,
                    resource.height,
                    self.jpeg_decoder,
                )
                reference = manifest.confirmation.reference_frame
                if (
                    reference.jpeg_sha256 != expected.sha256
                    or reference.jpeg_size_bytes != expected.size_bytes
                ):
                    raise ConfirmedInputInvalidError
            elif not allow_schema_two:
                raise LegacyInvestigationError
        except (
            ConfirmationArtifactError,
            ConfirmationImageDimensionMismatchError,
            ConfirmationInvalidRoiError,
            ConfirmationCandidateMismatchError,
        ):
            raise ConfirmedInputInvalidError from None
        except ReferenceFrameError:
            raise ConfirmedInputInvalidError from None
        return manifest, resource

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
        integrity: JpegIntegrity,
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
            jpeg_sha256=integrity.sha256,
            jpeg_size_bytes=integrity.size_bytes,
        )
        timing = ConfirmationTiming(
            decoded_local_pts_seconds=resource.decoded_local_pts_seconds,
            estimated_source_time_utc=resource.estimated_source_time_utc,
            offset_from_requested_seconds=resource.offset_from_requested_seconds,
            timing_precision_status=resource.timing_precision_status,
            warnings=resource.warnings,
        )
        investigation_id = investigation_id_for(
            resource.request.channel_id,
            anchor.requested_time_utc,
            schema_version=3,
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
    expected_id = investigation_id_for(
        resource.request.channel_id,
        manifest.anchor_time_utc,
        schema_version=manifest.schema_version,
    )
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


def _schema_three_manifest(
    legacy: ConfirmationManifest,
    resource: ReferenceFrameResourceMetadata,
    integrity: JpegIntegrity,
    now_utc: Callable[[], datetime],
) -> ConfirmationManifest:
    reference = legacy.confirmation.reference_frame.model_copy(
        update={"jpeg_sha256": integrity.sha256, "jpeg_size_bytes": integrity.size_bytes}
    )
    confirmation = legacy.confirmation.model_copy(update={"reference_frame": reference})
    investigation_id = investigation_id_for(
        legacy.confirmation.channel_id,
        legacy.anchor_time_utc,
        schema_version=3,
    )
    if resource.request.channel_id != legacy.confirmation.channel_id:
        raise ConfirmationCandidateMismatchError
    return ConfirmationManifest(
        schema_version=3,
        investigation_id=investigation_id,
        investigation_kind=legacy.investigation_kind,
        scenario_id=legacy.scenario_id,
        status=legacy.status,
        anchor_time_utc=legacy.anchor_time_utc,
        source_timezone=legacy.source_timezone,
        confirmed_at_utc=_canonical_now(now_utc),
        artifact_directory_relative=artifact_relative_path(investigation_id),
        confirmation=confirmation,
    )


def _canonical_now(now_utc: Callable[[], datetime]) -> datetime:
    value = now_utc()
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ConfirmationRequestError
    return value.astimezone(timezone.utc).replace(microsecond=0)
