"""Synchronous orchestration for one durable recorded reference frame."""

from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, final

from vigi_vision.channel_selection import Channel
from vigi_vision.recording import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.reference_frame_artifacts import (
    ReferenceFrameArtifactStore,
    ReferenceFrameManifest,
)
from vigi_vision.reference_frame_decoder import (
    ReferenceFrameDecoder,
    ReferenceFrameDecodeRequest,
)
from vigi_vision.reference_frame_models import (
    MANIFEST_SCHEMA_VERSION,
    DecodedFrameEvidence,
    ReferenceFrameArtifactConflictError,
    ReferenceFrameChannelNotFoundError,
    ReferenceFrameCleanupError,
    ReferenceFrameOutcome,
    ReferenceFrameRequest,
    ReferenceFrameResolution,
    ReferenceFrameResourceCorruptError,
    ReferenceFrameResult,
    ReferenceFrameSegmentMismatchError,
    build_reference_replay_window,
)
from vigi_vision.reference_frame_resources import ReferenceFrameResourceStore
from vigi_vision.replay import ReplayClip


class RecordingSegmentPlanningBoundary(Protocol):
    """Resolve one selected segment and plan a replay contained in that segment."""

    def find_covering_segment(self, channel_id: int, instant_utc: datetime) -> RecordingSegment:
        """Return the selected recording segment for a requested UTC instant."""
        ...

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        """Return a credential-free replay plan contained in the selected segment."""
        ...


class ReplayExtractionBoundary(Protocol):
    """Extract one caller-owned bounded temporary replay clip."""

    def extract(self, request: ReplayRequest) -> ReplayClip:
        """Return a removable local MP4 for the supplied credential-free replay plan."""
        ...


class ChannelInventoryBoundary(Protocol):
    """Optional inventory seam used to distinguish a missing channel from no recording."""

    def channels(self) -> tuple[Channel, ...]:
        """Return credential-free NVR channel inventory."""
        ...


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameService:
    """Create one durable JPEG without HTTP, OpenAI, ROI, or analysis dependencies."""

    planner: RecordingSegmentPlanningBoundary = field(repr=False)
    replay_extractor: ReplayExtractionBoundary = field(repr=False)
    decoder: ReferenceFrameDecoder = field(repr=False)
    artifacts: ReferenceFrameArtifactStore = field(repr=False)
    channel_inventory: ChannelInventoryBoundary | None = field(default=None, repr=False)
    completed_resources: ReferenceFrameResourceStore | None = field(default=None, repr=False)

    def execute(self, request: ReferenceFrameRequest) -> ReferenceFrameResult:
        """Run selected-segment replay extraction, decoding, and durable artifact publication."""
        return self.execute_or_resolve(request).result

    def execute_or_resolve(self, request: ReferenceFrameRequest) -> ReferenceFrameResolution:
        """Create a new frame or return a verified compatible completed resource."""
        inventory_warnings = self._inventory_warnings(request)
        segment = self.planner.find_covering_segment(request.channel_id, request.requested_time_utc)
        extraction_window = build_reference_replay_window(request, segment)
        replay_request = self.planner.plan_for_segment(segment, extraction_window)
        _validate_replay_plan(segment, extraction_window, replay_request)
        existing = self._completed_resource(request, segment)
        if existing is not None:
            return ReferenceFrameResolution(existing, ReferenceFrameOutcome.REUSED)
        try:
            session = self.artifacts.begin(request, segment)
        except ReferenceFrameArtifactConflictError:
            existing = self._completed_resource(request, segment)
            if existing is not None:
                return ReferenceFrameResolution(existing, ReferenceFrameOutcome.REUSED)
            raise
        clip: ReplayClip | None = None
        completed = False
        try:
            clip = self.replay_extractor.extract(replay_request)
            target_offset_seconds = (
                request.requested_time_utc - extraction_window.start_utc
            ).total_seconds()
            evidence = self.decoder.decode(
                ReferenceFrameDecodeRequest(
                    clip.temporary_mp4_path,
                    target_offset_seconds,
                    request.frame_selection_policy,
                    session.jpeg_path,
                )
            )
            warnings = inventory_warnings + evidence.warnings
            combined_evidence = DecodedFrameEvidence(
                evidence.jpeg_path,
                evidence.local_pts_seconds,
                evidence.width,
                evidence.height,
                evidence.timing_precision_status,
                warnings,
            )
            manifest = ReferenceFrameManifest(
                request,
                segment,
                extraction_window,
                session.resource_id,
                combined_evidence,
                None,
                None,
            )
            _remove_replay_clip(clip)
            clip = None
            _, _ = session.finalize(manifest)
            completed = True
            result = ReferenceFrameResult(
                resource_id=session.resource_id,
                manifest_schema_version=MANIFEST_SCHEMA_VERSION,
                generation_policy_version=request.generation_policy_version,
                channel_id=request.channel_id,
                requested_time_text=request.requested_time_text,
                source_timezone=request.source_timezone,
                requested_time_utc=request.requested_time_utc,
                selected_segment=segment,
                extraction_window=extraction_window,
                frame_selection_policy=request.frame_selection_policy,
                jpeg_relative_path=Path(session.resource_id) / "frame.jpg",
                manifest_relative_path=Path(session.resource_id) / "manifest.json",
                width=combined_evidence.width,
                height=combined_evidence.height,
                decoded_local_pts_seconds=combined_evidence.local_pts_seconds,
                estimated_source_time_utc=None,
                offset_from_requested_seconds=None,
                timing_precision_status=combined_evidence.timing_precision_status,
                warnings=combined_evidence.warnings,
            )
            return ReferenceFrameResolution(result, ReferenceFrameOutcome.CREATED)
        finally:
            if clip is not None:
                with suppress(OSError):
                    clip.remove()
            if not completed:
                session.discard()

    def _inventory_warnings(self, request: ReferenceFrameRequest) -> tuple[str, ...]:
        if self.channel_inventory is None:
            return ()
        channel = next(
            (
                item
                for item in self.channel_inventory.channels()
                if item.channel_id == request.channel_id
            ),
            None,
        )
        if channel is None:
            raise ReferenceFrameChannelNotFoundError
        if channel.online:
            return ()
        return ("The channel is currently offline; historical recordings may still be available.",)

    def _completed_resource(
        self, request: ReferenceFrameRequest, segment: RecordingSegment
    ) -> ReferenceFrameResult | None:
        if self.completed_resources is None:
            return None
        try:
            return self.completed_resources.resolve_for_request(request, segment)
        except ReferenceFrameResourceCorruptError:
            raise ReferenceFrameArtifactConflictError from None


def _validate_replay_plan(
    segment: RecordingSegment, window: RecordingWindow, replay_request: ReplayRequest
) -> None:
    if (
        replay_request.window != window
        or segment.channel_id != window.channel_id
        or window.start_utc < segment.start_utc
        or window.end_utc > segment.end_utc
    ):
        raise ReferenceFrameSegmentMismatchError


def _remove_replay_clip(clip: ReplayClip) -> None:
    try:
        clip.remove()
    except OSError:
        raise ReferenceFrameCleanupError from None
