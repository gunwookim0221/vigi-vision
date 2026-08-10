"""Phase 7A-2 bounded acquisition orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from vigi_vision.recording_search_a2_models import (
    AcquisitionOperationRecord,
    CanonicalProbeFrameRecord,
    ProbeFrameRequestRecord,
    ProbeRequestStatus,
    RecordingSearchManifestV2,
)
from vigi_vision.recording_search_a2_repository import read_schema2_children
from vigi_vision.recording_search_a2_support import (
    A2HandleBoundary,
    A2ServiceBoundary,
    AcquisitionBatch,
    acquire_new_targets,
    fractional_now,
    whole_text,
)
from vigi_vision.recording_search_models import (
    RecordingSearchArtifactError,
    RecordingSearchBaselineError,
    RecordingSearchManifestCorruptError,
    RecordingSearchState,
)


@dataclass(frozen=True, slots=True)
class ValidatedAcquisitionOutput:
    """Strict successful request/frame pair for the future classifier boundary."""

    request: ProbeFrameRequestRecord
    frame: CanonicalProbeFrameRecord


def validate_successful_request(
    service: A2ServiceBoundary,
    investigation_id: str,
    search_run_id: str,
    probe_request_id: str,
) -> ValidatedAcquisitionOutput:
    """Validate one indexed successful request without performing classification."""
    manifest = service.repository.load(investigation_id, search_run_id)
    if not isinstance(manifest, RecordingSearchManifestV2):
        raise RecordingSearchArtifactError
    run_path = service.repository.run_path(investigation_id, search_run_id)
    operations, frames, requests = read_schema2_children(
        service.repository.root,
        run_path,
        manifest,
    )
    request = requests.get(probe_request_id)
    if (
        request is None
        or request.status is not ProbeRequestStatus.SUCCEEDED
        or request.canonical_frame_id is None
        or request.operation_id not in operations
    ):
        raise RecordingSearchArtifactError
    frame = frames.get(request.canonical_frame_id)
    if frame is None or frame.operation_id not in operations:
        raise RecordingSearchArtifactError
    if (
        request.investigation_id != manifest.investigation_id
        or request.search_run_id != manifest.search_run_id
        or request.channel_id != manifest.confirmation.channel_id
        or frame.investigation_id != manifest.investigation_id
        or frame.search_run_id != manifest.search_run_id
        or frame.channel_id != request.channel_id
    ):
        raise RecordingSearchArtifactError
    return ValidatedAcquisitionOutput(request, frame)


def acquire_targets(
    service: A2ServiceBoundary,
    handle: A2HandleBoundary,
    requested_times: tuple[datetime, ...],
) -> tuple[ProbeFrameRequestRecord, ...]:
    """Acquire one ordered immutable request record for every target."""
    with service.a2_mutation(handle):
        manifest = _ensure_schema2(service, handle)
        run_path = service.repository.run_path(handle.investigation_id, handle.search_run_id)
        operations, frames, existing_requests = read_schema2_children(
            service.repository.root,
            run_path,
            manifest,
        )
        targets = _validate_targets(requested_times, manifest)
        selected, pending = _select_existing(existing_requests, manifest, targets)
        if not pending:
            return tuple(selected[_request_key(manifest, target)] for target in targets)
        operation = _new_operation(service, manifest, operations)
        admitted = service.repository.admit_operation(manifest, operation)
        try:
            produced, new_frames = acquire_new_targets(
                AcquisitionBatch(
                    service,
                    admitted,
                    operation,
                    frames,
                    existing_requests,
                ),
                tuple(pending),
            )
            _ = service.repository.publish_a2_bundle(
                admitted,
                tuple(produced),
                tuple(new_frames),
            )
        except RecordingSearchArtifactError:
            _fail_active_run(handle)
            raise
        persisted = service.repository.load(handle.investigation_id, handle.search_run_id)
        if not isinstance(persisted, RecordingSearchManifestV2):
            raise RecordingSearchManifestCorruptError
        _, _, persisted_requests = read_schema2_children(
            service.repository.root,
            run_path,
            persisted,
        )
        for target in pending:
            key = _request_key(admitted, target)
            record = _latest_request(persisted_requests, key)
            if record is None:
                _fail_active_run(handle)
                raise RecordingSearchArtifactError
            selected[key] = record
        return tuple(selected[_request_key(admitted, target)] for target in targets)


def _ensure_schema2(
    service: A2ServiceBoundary, handle: A2HandleBoundary
) -> RecordingSearchManifestV2:
    current = service.repository.load(handle.investigation_id, handle.search_run_id)
    if isinstance(current, RecordingSearchManifestV2):
        if current.state is not RecordingSearchState.RUNNING:
            raise RecordingSearchBaselineError
        return current
    if current.state is not RecordingSearchState.RUNNING:
        raise RecordingSearchBaselineError
    promoted = RecordingSearchManifestV2(
        schema_version=2,
        investigation_id=current.investigation_id,
        search_run_id=current.search_run_id,
        state=current.state,
        created_at_utc=current.created_at_utc,
        started_at_utc=current.started_at_utc,
        completed_at_utc=current.completed_at_utc,
        confirmation=current.confirmation,
        policy=current.policy,
        acquisition_operation_ids=(),
        probe_request_ids=(),
        canonical_frame_ids=(),
        failure_reason=None,
    )
    return service.repository.promote_schema2(promoted)


def _validate_targets(
    requested_times: tuple[datetime, ...], manifest: RecordingSearchManifestV2
) -> tuple[datetime, ...]:
    targets: list[datetime] = []
    seen: set[str] = set()
    for value in requested_times:
        if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
            raise RecordingSearchBaselineError
        normalized = value.astimezone(timezone.utc)
        text = whole_text(normalized)
        if (
            normalized < manifest.policy.search_start_utc
            or normalized > manifest.policy.search_end_utc
        ):
            raise RecordingSearchBaselineError
        if text not in seen:
            seen.add(text)
            targets.append(normalized)
    if not targets:
        raise RecordingSearchBaselineError
    return tuple(targets)


def _select_existing(
    existing: dict[str, ProbeFrameRequestRecord],
    manifest: RecordingSearchManifestV2,
    targets: tuple[datetime, ...],
) -> tuple[dict[tuple[str, int, str], ProbeFrameRequestRecord], list[datetime]]:
    selected: dict[tuple[str, int, str], ProbeFrameRequestRecord] = {}
    pending: list[datetime] = []
    for target in targets:
        key = _request_key(manifest, target)
        match = _latest_request(existing, key)
        if match is None or match.status is ProbeRequestStatus.FAILED:
            pending.append(target)
        else:
            selected[key] = match
    return selected, pending


def _new_operation(
    service: A2ServiceBoundary,
    manifest: RecordingSearchManifestV2,
    operations: dict[str, AcquisitionOperationRecord],
) -> AcquisitionOperationRecord:
    for _ in range(16):
        operation_id = service.operation_id_factory()
        if operation_id not in operations:
            return AcquisitionOperationRecord(
                record_type="acquisition_operation",
                operation_id=operation_id,
                investigation_id=manifest.investigation_id,
                search_run_id=manifest.search_run_id,
                operation_kind="recording_probe_acquisition_v1",
                state="ADMITTED",
                admitted_at_utc=fractional_now(service),
            )
    raise RecordingSearchArtifactError


def _request_key(manifest: RecordingSearchManifestV2, target: datetime) -> tuple[str, int, str]:
    return manifest.search_run_id, manifest.confirmation.channel_id, whole_text(target)


def _latest_request(
    requests: dict[str, ProbeFrameRequestRecord], key: tuple[str, int, str]
) -> ProbeFrameRequestRecord | None:
    matches = [
        request
        for request in requests.values()
        if (
            request.search_run_id,
            request.channel_id,
            whole_text(request.requested_time_utc),
        )
        == key
    ]
    return matches[-1] if matches else None


def _fail_active_run(handle: A2HandleBoundary) -> None:
    try:
        _ = handle.mark_terminal(RecordingSearchState.FAILED, "unexpected_error")
    except RecordingSearchBaselineError:
        return
