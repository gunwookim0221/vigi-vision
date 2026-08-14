"""Typed acquisition helpers for Phase 7A-2 publication."""

from __future__ import annotations

import tempfile
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol

from vigi_vision.investigation_confirmation_integrity import compute_jpeg_integrity
from vigi_vision.investigation_confirmation_models import ConfirmationArtifactError
from vigi_vision.recording import RecordingSegment, RecordingWindow
from vigi_vision.recording_models import RecordingUnavailableError
from vigi_vision.recording_search_a2_decoder import MissingProvenanceError
from vigi_vision.recording_search_a2_models import (
    AcquisitionOperationRecord,
    BatchDecodeRequest,
    CanonicalProbeFrameRecord,
    DecodedTargetResult,
    ProbeFrameRequestRecord,
    ProbeRequestStatus,
    RecordingSearchManifestV2,
    acquisition_id_for,
    canonical_frame_id_for,
    decoded_frame_utc_for,
)
from vigi_vision.recording_search_models import (
    RecordingSearchArtifactError,
)
from vigi_vision.reference_frame_decoder import ReferenceFrameDecodeTimeoutError
from vigi_vision.reference_frame_models import ReferenceFrameDecodeError, segment_identity
from vigi_vision.replay import ReplayError, ReplayTimeoutError

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from vigi_vision.recording_search_a2_decoder import RecordingProbeBatchDecoder
    from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
    from vigi_vision.recording_search_models import RecordingSearchManifest, RecordingSearchState
    from vigi_vision.reference_frame_service import (
        RecordingSegmentPlanningBoundary,
        ReplayExtractionBoundary,
    )


class A2RepositoryBoundary(Protocol):
    """Repository methods required by acquisition helpers."""

    @property
    def root(self) -> Path:
        """Return the confined repository root."""
        ...

    @property
    def now_utc(self) -> Callable[[], datetime]:
        """Return the repository clock."""
        ...

    def run_path(self, investigation_id: str, search_run_id: str) -> Path:
        """Return one confined run path."""
        ...

    def load(self, investigation_id: str, search_run_id: str) -> object:
        """Strictly reload a persisted manifest."""
        ...

    def load_for_probe_admission(self, investigation_id: str, search_run_id: str) -> object:
        """Load a manifest while deferring indexed JPEG payload validation."""
        ...

    def promote_schema2(self, manifest: RecordingSearchManifestV2) -> RecordingSearchManifestV2:
        """Promote an active schema-1 run."""
        ...

    def admit_operation(
        self,
        manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
        operation: AcquisitionOperationRecord,
    ) -> RecordingSearchManifestV2 | RecordingSearchManifestV3:
        """Admit one acquisition operation."""
        ...

    def publish_a2_bundle(
        self,
        manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
        request_records: tuple[ProbeFrameRequestRecord, ...],
        frame_records: tuple[tuple[CanonicalProbeFrameRecord, bytes], ...],
    ) -> RecordingSearchManifestV2 | RecordingSearchManifestV3:
        """Publish one immutable acquisition bundle."""
        ...


class A2HandleBoundary(Protocol):
    """Active handle identity used by the mutation boundary."""

    @property
    def investigation_id(self) -> str:
        """Return the immutable investigation binding."""
        ...

    @property
    def search_run_id(self) -> str:
        """Return the immutable search-run binding."""
        ...

    @property
    def closed(self) -> bool:
        """Return whether this handle is closed."""
        ...

    def mark_terminal(
        self, state: RecordingSearchState, failure_reason: str
    ) -> RecordingSearchManifest | RecordingSearchManifestV2:
        """Publish a safe terminal state."""
        ...


class A2ServiceBoundary(Protocol):
    """Service values required by acquisition helpers."""

    @property
    def repository(self) -> A2RepositoryBoundary:
        """Return the A2 repository."""
        ...

    @property
    def recording_planner(self) -> RecordingSegmentPlanningBoundary | None:
        """Return the recording planner boundary."""
        ...

    @property
    def replay_extractor(self) -> ReplayExtractionBoundary | None:
        """Return the replay extractor boundary."""
        ...

    @property
    def batch_decoder(self) -> RecordingProbeBatchDecoder | None:
        """Return the batch decoder boundary."""
        ...

    @property
    def now_utc(self) -> Callable[[], datetime]:
        """Return the service clock."""
        ...

    @property
    def probe_request_id_factory(self) -> Callable[[], str]:
        """Return the request-ID factory."""
        ...

    @property
    def operation_id_factory(self) -> Callable[[], str]:
        """Return the operation-ID factory."""
        ...

    def a2_mutation(self, handle: A2HandleBoundary) -> AbstractContextManager[None]:
        """Hold the active run's shared mutation mutex."""
        ...


@dataclass(frozen=True, slots=True)
class AcquisitionBatch:
    """Immutable context for one admitted acquisition batch."""

    service: A2ServiceBoundary
    manifest: RecordingSearchManifestV2
    operation: AcquisitionOperationRecord
    existing_frames: dict[str, CanonicalProbeFrameRecord]
    existing_requests: dict[str, ProbeFrameRequestRecord]


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Immutable context for creating one request record."""

    service: A2ServiceBoundary
    manifest: RecordingSearchManifestV2
    operation: AcquisitionOperationRecord
    used_ids: set[str]


def acquire_new_targets(
    batch: AcquisitionBatch,
    targets: tuple[datetime, ...],
) -> tuple[list[ProbeFrameRequestRecord], list[tuple[CanonicalProbeFrameRecord, bytes]]]:
    """Plan, decode, validate, and convert one admitted batch."""
    service = batch.service
    manifest = batch.manifest
    if service.recording_planner is None or service.replay_extractor is None:
        return _failed_requests(
            RequestContext(
                service,
                manifest,
                batch.operation,
                set(batch.existing_requests),
            ),
            targets,
            "acquisition_failed",
        ), []
    if service.batch_decoder is None:
        return _failed_requests(
            RequestContext(
                service,
                manifest,
                batch.operation,
                set(batch.existing_requests),
            ),
            targets,
            "missing_provenance",
        ), []
    groups, failures = _group_targets(service, manifest, targets)
    decoded: dict[str, DecodedTargetResult] = {}
    for segment, grouped in groups:
        decoded.update(_decode_group(service, manifest, segment, grouped, failures))
    return _records_from_results(
        batch,
        targets,
        decoded,
        failures,
    )


def _group_targets(
    service: A2ServiceBoundary,
    manifest: RecordingSearchManifestV2,
    targets: tuple[datetime, ...],
) -> tuple[list[tuple[RecordingSegment, tuple[datetime, ...]]], dict[str, str]]:
    groups: list[tuple[RecordingSegment, list[datetime]]] = []
    failures: dict[str, str] = {}
    planner = service.recording_planner
    if planner is None:
        return [], {whole_text(target): "acquisition_failed" for target in targets}
    for target in targets:
        try:
            segment = planner.find_covering_segment(manifest.confirmation.channel_id, target)
        except RecordingUnavailableError:
            failures[whole_text(target)] = "recording_unavailable"
            continue
        except (OSError, ValueError, ReplayError):
            failures[whole_text(target)] = "acquisition_failed"
            continue
        for known, grouped in groups:
            if known == segment:
                grouped.append(target)
                break
        else:
            groups.append((segment, [target]))
    return [(segment, tuple(grouped)) for segment, grouped in groups], failures


def _decode_group(
    service: A2ServiceBoundary,
    manifest: RecordingSearchManifestV2,
    segment: RecordingSegment,
    targets: tuple[datetime, ...],
    failures: dict[str, str],
) -> dict[str, DecodedTargetResult]:
    try:
        window, results = _decode_group_results(service, manifest, segment, targets)
        _validate_decoded_results(results, segment, window, targets)
        return {
            whole_text(result.requested_time_utc): replace(
                result,
                segment=segment,
                extraction_window=window,
            )
            for result in results
        }
    except RecordingSearchArtifactError:
        raise
    except RecordingUnavailableError:
        _mark_failures(failures, targets, "recording_unavailable")
    except MissingProvenanceError:
        _mark_failures(failures, targets, "missing_provenance")
    except (ReferenceFrameDecodeTimeoutError, ReplayTimeoutError):
        _mark_failures(failures, targets, "decode_failed")
    except (ReferenceFrameDecodeError, ReplayError, OSError, ValueError):
        _mark_failures(failures, targets, "decode_failed")
    except Exception:  # noqa: BLE001 - safe decoder boundary redacts unknown failures.
        _mark_failures(failures, targets, "unexpected_error")
    return {}


def _validate_decoded_results(
    results: tuple[DecodedTargetResult, ...],
    segment: RecordingSegment,
    window: RecordingWindow,
    targets: tuple[datetime, ...],
) -> None:
    try:
        if tuple(result.requested_time_utc for result in results) != targets:
            _raise_missing_provenance()
    except (AttributeError, TypeError, ValueError):
        _raise_missing_provenance()
    decoded_times = [_validated_decoded_time(result, segment, window) for result in results]
    if any(left > right for left, right in pairwise(decoded_times)):
        _raise_missing_provenance()


def _validated_decoded_time(
    result: DecodedTargetResult,
    segment: RecordingSegment,
    window: RecordingWindow,
) -> datetime:
    try:
        if (result.segment is not None and result.segment != segment) or (
            result.extraction_window is not None and result.extraction_window != window
        ):
            _raise_missing_provenance()
        if (
            result.source_pts < 0
            or result.decoded_pts < 0
            or result.decoded_ordinal < 0
            or result.source_time_base.numerator <= 0
            or result.source_time_base.denominator <= 0
            or result.replay_time_base.numerator <= 0
            or result.replay_time_base.denominator <= 0
        ):
            _raise_missing_provenance()
        return decoded_frame_utc_for(
            result.physical_replay_origin_utc,
            result.source_pts,
            result.source_time_base,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_missing_provenance()


def _decode_group_results(
    service: A2ServiceBoundary,
    manifest: RecordingSearchManifestV2,
    segment: RecordingSegment,
    targets: tuple[datetime, ...],
) -> tuple[RecordingWindow, tuple[DecodedTargetResult, ...]]:
    planner = service.recording_planner
    extractor = service.replay_extractor
    decoder = service.batch_decoder
    if planner is None or extractor is None or decoder is None:
        raise MissingProvenanceError
    start = min(targets)
    end = min(max(targets) + timedelta(seconds=6), segment.end_utc)
    if end <= start:
        _raise_recording_unavailable()
    window = RecordingWindow(manifest.confirmation.channel_id, start, end)
    replay_request = planner.plan_for_segment(segment, window)
    clip = extractor.extract(replay_request)
    try:
        batch = BatchDecodeRequest(
            manifest.confirmation.channel_id,
            segment,
            window,
            replay_request,
            clip,
        )
        return window, tuple(decoder.decode_targets(batch, targets))
    finally:
        try:
            clip.remove()
        except OSError as error:
            _raise_artifact_cleanup(error)


def _raise_recording_unavailable() -> None:
    raise RecordingUnavailableError


def _raise_artifact_cleanup(error: OSError) -> None:
    raise RecordingSearchArtifactError from error


def _raise_missing_provenance() -> NoReturn:
    raise MissingProvenanceError


def _raise_artifact_conflict() -> None:
    raise RecordingSearchArtifactError


def _records_from_results(
    batch: AcquisitionBatch,
    targets: tuple[datetime, ...],
    decoded: dict[str, DecodedTargetResult],
    failures: dict[str, str],
) -> tuple[list[ProbeFrameRequestRecord], list[tuple[CanonicalProbeFrameRecord, bytes]]]:
    service = batch.service
    manifest = batch.manifest
    operation = batch.operation
    existing_frames = batch.existing_frames
    existing_requests = batch.existing_requests
    requests: list[ProbeFrameRequestRecord] = []
    frames: list[tuple[CanonicalProbeFrameRecord, bytes]] = []
    known_frames = dict(existing_frames)
    aliases = dict(existing_requests)
    used_request_ids = set(existing_requests)
    request_context = RequestContext(service, manifest, operation, used_request_ids)
    for target in targets:
        target_key = whole_text(target)
        reason = failures.get(target_key)
        result = decoded.get(target_key)
        if reason is not None or result is None:
            requests.append(
                _failed_request(
                    request_context,
                    target,
                    reason or "missing_provenance",
                )
            )
            continue
        try:
            frame, payload = _frame_record(service, manifest, operation, result)
            known = known_frames.get(frame.canonical_frame_id)
            if known is not None:
                if not _same_physical_frame(known, frame):
                    _raise_artifact_conflict()
                frame = known
            else:
                known_frames[frame.canonical_frame_id] = frame
                frames.append((frame, payload))
            alias = next(
                (
                    request.probe_request_id
                    for request in aliases.values()
                    if request.status is ProbeRequestStatus.SUCCEEDED
                    and request.canonical_frame_id == frame.canonical_frame_id
                ),
                None,
            )
            request = _success_request(request_context, target, frame, alias)
            requests.append(request)
            aliases[request.probe_request_id] = request
        except RecordingSearchArtifactError:
            raise
        except (ConfirmationArtifactError, OSError, ValueError):
            requests.append(_failed_request(request_context, target, "invalid_artifact"))
    return requests, frames


def _same_physical_frame(
    existing: CanonicalProbeFrameRecord, candidate: CanonicalProbeFrameRecord
) -> bool:
    fields = (
        "investigation_id",
        "search_run_id",
        "channel_id",
        "source_segment_id",
        "segment_start_utc",
        "segment_end_utc",
        "physical_replay_origin_utc",
        "source_pts",
        "source_time_base",
        "decoded_frame_utc",
        "source_width",
        "source_height",
        "jpeg_sha256",
        "jpeg_size_bytes",
    )
    return all(getattr(existing, field) == getattr(candidate, field) for field in fields)


def _frame_record(
    service: A2ServiceBoundary,
    manifest: RecordingSearchManifestV2,
    operation: AcquisitionOperationRecord,
    result: DecodedTargetResult,
) -> tuple[CanonicalProbeFrameRecord, bytes]:
    """Validate one decoder result and create its canonical frame record."""
    decoded_utc = decoded_frame_utc_for(
        result.physical_replay_origin_utc,
        result.source_pts,
        result.source_time_base,
    )
    if result.segment is None or result.extraction_window is None:
        raise MissingProvenanceError
    segment_id = segment_identity(result.segment)
    if (
        result.segment.channel_id != manifest.confirmation.channel_id
        or result.source_width != manifest.confirmation.source_width
        or result.source_height != manifest.confirmation.source_height
        or decoded_utc < result.segment.start_utc
        or decoded_utc >= result.segment.end_utc
    ):
        raise ValueError
    frame_id = canonical_frame_id_for(
        manifest.investigation_id,
        manifest.search_run_id,
        manifest.confirmation.channel_id,
        segment_id,
        decoded_utc,
    )
    run_path = service.repository.run_path(manifest.investigation_id, manifest.search_run_id)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".phase7a2-{operation.operation_id}-", dir=run_path))
    temp_path = temp_dir / "candidate.jpg"
    try:
        _ = temp_path.write_bytes(result.jpeg_bytes)
        integrity = compute_jpeg_integrity(
            temp_path,
            result.source_width,
            result.source_height,
        )
    finally:
        with suppress(OSError):
            temp_path.unlink(missing_ok=True)
        with suppress(OSError):
            temp_dir.rmdir()
    extraction = result.extraction_window
    acquisition_id = acquisition_id_for(
        segment_id,
        extraction.start_utc,
        extraction.end_utc,
        manifest.policy.acquisition_policy_version,
    )
    acquired_at = fractional_now(service)
    if acquired_at < operation.admitted_at_utc:
        raise ValueError
    frame = CanonicalProbeFrameRecord(
        record_type="canonical_probe_frame",
        canonical_frame_id=frame_id,
        investigation_id=manifest.investigation_id,
        search_run_id=manifest.search_run_id,
        operation_id=operation.operation_id,
        channel_id=manifest.confirmation.channel_id,
        acquisition_id=acquisition_id,
        source_segment_id=segment_id,
        segment_start_utc=result.segment.start_utc,
        segment_end_utc=result.segment.end_utc,
        extraction_start_utc=extraction.start_utc,
        extraction_end_utc=extraction.end_utc,
        decode_session_id=result.decode_session_id,
        physical_replay_origin_utc=result.physical_replay_origin_utc,
        source_pts=result.source_pts,
        source_time_base=result.source_time_base,
        decoded_frame_utc=decoded_utc,
        decoded_pts=result.decoded_pts,
        replay_time_base=result.replay_time_base,
        decoded_ordinal=result.decoded_ordinal,
        source_width=result.source_width,
        source_height=result.source_height,
        jpeg_relative_path=f"evidence/frames/{frame_id}.jpg",
        jpeg_sha256=integrity.sha256,
        jpeg_size_bytes=integrity.size_bytes,
        acquired_at_utc=acquired_at,
    )
    return frame, result.jpeg_bytes


def _failed_requests(
    context: RequestContext,
    targets: tuple[datetime, ...],
    reason: str,
) -> list[ProbeFrameRequestRecord]:
    """Create safe failed request records for every target in a batch."""
    return [_failed_request(context, target, reason) for target in targets]


def _failed_request(
    context: RequestContext,
    target: datetime,
    reason: str,
) -> ProbeFrameRequestRecord:
    request_id = _new_request_id(context.service, context.used_ids)
    completed = _whole_now(context.service)
    return ProbeFrameRequestRecord(
        record_type="probe_frame_request",
        probe_request_id=request_id,
        investigation_id=context.manifest.investigation_id,
        search_run_id=context.manifest.search_run_id,
        operation_id=context.operation.operation_id,
        channel_id=context.manifest.confirmation.channel_id,
        requested_time_utc=target,
        status=ProbeRequestStatus.FAILED,
        canonical_frame_id=None,
        alias_of_probe_request_id=None,
        failure_reason=reason,
        created_at_utc=completed,
        completed_at_utc=completed,
    )


def _success_request(
    context: RequestContext,
    target: datetime,
    frame: CanonicalProbeFrameRecord,
    alias: str | None,
) -> ProbeFrameRequestRecord:
    request_id = _new_request_id(context.service, context.used_ids)
    created = _whole_now(context.service)
    return ProbeFrameRequestRecord(
        record_type="probe_frame_request",
        probe_request_id=request_id,
        investigation_id=context.manifest.investigation_id,
        search_run_id=context.manifest.search_run_id,
        operation_id=context.operation.operation_id,
        channel_id=context.manifest.confirmation.channel_id,
        requested_time_utc=target,
        status=ProbeRequestStatus.SUCCEEDED,
        canonical_frame_id=frame.canonical_frame_id,
        alias_of_probe_request_id=alias,
        failure_reason=None,
        created_at_utc=created,
        completed_at_utc=created,
    )


def _new_request_id(service: A2ServiceBoundary, used: set[str]) -> str:
    for _ in range(16):
        request_id = service.probe_request_id_factory()
        if request_id not in used:
            used.add(request_id)
            return request_id
    raise RecordingSearchArtifactError


def _whole_now(service: A2ServiceBoundary) -> datetime:
    value = service.now_utc()
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise RecordingSearchArtifactError
    return value.astimezone(timezone.utc).replace(microsecond=0)


def fractional_now(service: A2ServiceBoundary) -> datetime:
    """Return one canonical fractional UTC timestamp for A2 records."""
    value = service.now_utc()
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise RecordingSearchArtifactError
    if value.microsecond == 0:
        value = value.replace(microsecond=1)
    return value.astimezone(timezone.utc)


def whole_text(value: datetime) -> str:
    """Serialize one whole UTC second for request identity."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise ValueError
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mark_failures(failures: dict[str, str], targets: tuple[datetime, ...], reason: str) -> None:
    for target in targets:
        failures[whole_text(target)] = reason
