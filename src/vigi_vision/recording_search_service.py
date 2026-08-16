"""Phase 7A-1 recording-search validation and lifecycle service."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from threading import RLock
from typing import TYPE_CHECKING, NoReturn, Protocol, final

from vigi_vision.channel_selection import Channel, usable_channels
from vigi_vision.durable_io import is_safe_contained_path
from vigi_vision.investigation_confirmation_integrity import compute_jpeg_integrity
from vigi_vision.investigation_confirmation_models import (
    ConfirmationError,
    ConfirmedInvestigationInput,
    LegacyInvestigationError,
)
from vigi_vision.recording_search_a2_decoder import RecordingProbeBatchDecoder  # noqa: TC001
from vigi_vision.recording_search_a2_models import (
    ProbeFrameRequestRecord,
    RecordingSearchManifestV2,
)
from vigi_vision.recording_search_a2_service import acquire_targets
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b4_authority import ClassificationAttemptSlot
from vigi_vision.recording_search_b4_models import (
    ClassificationOperationalError,
    ClassificationOperationalReason,
    PublishedClassificationResult,
)
from vigi_vision.recording_search_c1_models import CoarseSampleStatus
from vigi_vision.recording_search_c1_planner import (
    CoarseSamplingIdentity,
    baseline_identity_for,
    build_coarse_sampling_plan,
)
from vigi_vision.recording_search_c1_service import CoarseSamplingExecutor
from vigi_vision.recording_search_c2_interpreter import interpret_coarse_evidence
from vigi_vision.recording_search_c2_models import (
    CoarseEvidenceSnapshot,
    CoarseInterpretationResult,
    CoarseInterpretationStatus,
    CoarseTargetEvidence,
)
from vigi_vision.recording_search_c2_service import capture_coarse_evidence_snapshot
from vigi_vision.recording_search_lock import LocalInvestigationLock
from vigi_vision.recording_search_models import (
    Phase8HandoffStatus,
    ReconfirmationRequiredError,
    RecordingSearchArtifactError,
    RecordingSearchBaseline,
    RecordingSearchBaselineError,
    RecordingSearchManifest,
    RecordingSearchManifestCorruptError,
    RecordingSearchNotFoundError,
    RecordingSearchOutcome,
    RecordingSearchRequest,
    RecordingSearchState,
    default_policy,
)
from vigi_vision.reference_frame_models import ReferenceFrameError, parse_reference_frame_request
from vigi_vision.reference_frame_service import (  # noqa: TC001
    RecordingSegmentPlanningBoundary,
    ReplayExtractionBoundary,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from vigi_vision.investigation_confirmation_integrity import JpegDecoder
    from vigi_vision.recording_search_a2_support import A2HandleBoundary
    from vigi_vision.recording_search_b3_models import ClassifyRecordingProbeRequest
    from vigi_vision.recording_search_b4_service import ObservationClassificationService
    from vigi_vision.recording_search_c1_models import CoarseSamplingResult
    from vigi_vision.recording_search_c1_planner import CoarseSamplingPlan
    from vigi_vision.recording_search_repository import RecordingSearchRepository


def _new_run_id() -> str:
    return f"search-run-{token_hex(4)}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


_C2_DIGEST_LENGTH = 64


def _incomplete_snapshot(
    handle: RecordingSearchRunHandle,
    execution: CoarseSamplingResult,
) -> CoarseEvidenceSnapshot:
    targets = tuple(
        CoarseTargetEvidence(
            requested_time_utc=sample.requested_time_utc,
            status=sample.status,
            probe_request_id=sample.probe_request_id,
        )
        for sample in execution.samples
    )
    return CoarseEvidenceSnapshot(
        investigation_id=handle.investigation_id,
        search_run_id=handle.search_run_id,
        identity=CoarseSamplingIdentity(
            handle.investigation_id,
            handle.search_run_id,
            handle.phase6_confirmation_id,
            handle.baseline_identity,
        ),
        plan=execution.plan,
        policy_version="unavailable",
        absence_confirmation_frames=execution.plan.absence_confirmation_frames,
        absence_cadence_seconds=execution.plan.absence_cadence_seconds,
        baseline_observation_id="unavailable",
        manifest_digest="0" * _C2_DIGEST_LENGTH,
        execution=execution,
        targets=targets,
        maximum_consecutive_indeterminate_targets=execution.plan.maximum_consecutive_indeterminate_targets,
    )


def _schema2_snapshot(
    manifest: RecordingSearchManifestV2,
    plan: CoarseSamplingPlan,
    execution: CoarseSamplingResult,
) -> CoarseEvidenceSnapshot:
    identity = CoarseSamplingIdentity(
        manifest.investigation_id,
        manifest.search_run_id,
        manifest.investigation_id,
        baseline_identity_for(manifest.confirmation),
    )
    if execution.identity != identity:
        raise RecordingSearchManifestCorruptError
    targets = tuple(
        CoarseTargetEvidence(
            requested_time_utc=sample.requested_time_utc,
            status=sample.status,
            probe_request_id=sample.probe_request_id,
        )
        for sample in execution.samples
    )
    return CoarseEvidenceSnapshot(
        investigation_id=manifest.investigation_id,
        search_run_id=manifest.search_run_id,
        identity=identity,
        plan=plan,
        policy_version=manifest.policy.policy_version,
        absence_confirmation_frames=manifest.policy.absence_confirmation_frames,
        absence_cadence_seconds=manifest.policy.absence_cadence_seconds,
        baseline_observation_id="unpublished",
        manifest_digest=hashlib.sha256(manifest.canonical_json().encode("utf-8")).hexdigest(),
        execution=execution,
        targets=targets,
        maximum_consecutive_indeterminate_targets=plan.maximum_consecutive_indeterminate_targets,
    )


def _interpret_incomplete_snapshot(
    service: RecordingSearchService,
    handle: RecordingSearchRunHandle,
    execution: CoarseSamplingResult,
) -> CoarseInterpretationResult:
    try:
        with service.a2_mutation(handle):
            snapshot = _incomplete_snapshot(handle, execution)
    except (RecordingSearchBaselineError, RecordingSearchNotFoundError):
        return CoarseInterpretationResult(
            status=CoarseInterpretationStatus.INTERRUPTED,
            safe_reason="inactive_run_handle",
        )
    except (RecordingSearchManifestCorruptError, ValueError):
        return CoarseInterpretationResult(
            status=CoarseInterpretationStatus.CORRUPT,
            safe_reason="authoritative_evidence_invalid",
        )
    return interpret_coarse_evidence(snapshot)


def _status_manifest(
    value: RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchManifestV3,
) -> RecordingSearchManifest | RecordingSearchManifestV2:
    match value:
        case RecordingSearchManifestV3():
            return value.as_status_manifest()
        case RecordingSearchManifest() | RecordingSearchManifestV2():
            return value


class ConfirmationLoader(Protocol):
    """Phase 6 schema 3 loader boundary."""

    def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
        """Load trusted confirmation facts."""
        ...


class ChannelInventory(Protocol):
    """Current NVR channel inventory boundary."""

    def channels(self) -> tuple[Channel, ...]:
        """Return channel metadata."""
        ...


class PolicyAvailability(Protocol):
    """Configured Phase 7 policy availability boundary."""

    def is_available(self) -> bool:
        """Return whether required policy inputs are available."""
        ...


@dataclass(frozen=True, slots=True)
class StaticPolicyAvailability:
    """Default availability for the documented policy snapshot."""

    def is_available(self) -> bool:
        """Return the static policy availability result."""
        return True


@dataclass(frozen=True, slots=True)
class RecordingSearchStartResult:
    """Result and authoritative manifest of a start attempt."""

    manifest: RecordingSearchManifest | RecordingSearchManifestV2
    outcome: RecordingSearchOutcome
    baseline_bytes: bytes = field(repr=False)
    run_handle: RecordingSearchRunHandle | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _ActiveRun:
    run_id: str
    os_lock: LocalInvestigationLock = field(repr=False)
    mutation_lock: RLock = field(repr=False)


@final
class RecordingSearchRunHandle:
    """Process-owned handle for the active investigation lock."""

    __slots__ = (
        "_baseline_bytes",
        "_baseline_identity",
        "_classification_attempts",
        "_closed",
        "_investigation_id",
        "_mutation_lock",
        "_phase6_confirmation_id",
        "_search_run_id",
        "_service",
    )

    def __init__(  # noqa: PLR0913 - immutable handle identity is explicit.
        self,
        service: RecordingSearchService,
        investigation_id: str,
        search_run_id: str,
        phase6_confirmation_id: str,
        baseline_identity: str,
        baseline_bytes: bytes,
        mutation_lock: RLock,
    ) -> None:
        """Retain the active lock and invocation-owned baseline bytes."""
        self._service = service
        self._investigation_id = investigation_id
        self._search_run_id = search_run_id
        self._phase6_confirmation_id = phase6_confirmation_id
        self._baseline_identity = baseline_identity
        self._baseline_bytes = bytes(baseline_bytes)
        self._classification_attempts = ClassificationAttemptSlot()
        self._mutation_lock = mutation_lock
        self._closed = False

    @property
    def investigation_id(self) -> str:
        """Return the immutable investigation binding owned by this handle."""
        return self._investigation_id

    @property
    def search_run_id(self) -> str:
        """Return the immutable search-run binding owned by this handle."""
        return self._search_run_id

    @property
    def baseline_bytes(self) -> bytes:
        """Return the immutable baseline bytes captured for this handle."""
        return self._baseline_bytes

    @property
    def phase6_confirmation_id(self) -> str:
        """Return the Phase 6 package identity bound to the run."""
        return self._phase6_confirmation_id

    @property
    def baseline_identity(self) -> str:
        """Return the canonical identity of the validated baseline."""
        return self._baseline_identity

    @property
    def classification_attempts(self) -> ClassificationAttemptSlot:
        """Return the invocation-local attempt marker for this active handle."""
        return self._classification_attempts

    def mark_terminal(
        self, state: RecordingSearchState, failure_reason: str
    ) -> RecordingSearchManifest | RecordingSearchManifestV2:
        """Persist a supported terminal state and release the lock."""
        if self._closed:
            raise RecordingSearchBaselineError
        self._closed = True
        return self._service.mark_terminal_for_handle(self, state, failure_reason)

    def release(self) -> None:
        """Release the process-owned lock without claiming completion."""
        if not self._closed:
            self._closed = True
            self._service.release_handle(self)

    @property
    def closed(self) -> bool:
        """Return whether this handle has released its active ownership."""
        return self._closed


@dataclass(slots=True)
class RecordingSearchService:
    """Compose Phase 6 loading with the Phase 7A-1 local run lifecycle."""

    confirmation_service: ConfirmationLoader = field(repr=False)
    repository: RecordingSearchRepository = field(repr=False)
    channel_inventory: ChannelInventory = field(repr=False)
    artifact_root: Path = field(repr=False)
    now_utc: Callable[[], datetime] = _utc_now
    jpeg_decoder: JpegDecoder | None = field(default=None, repr=False)
    policy_availability: PolicyAvailability = field(
        default_factory=StaticPolicyAvailability, repr=False
    )
    lock_timeout_seconds: float = field(default=0.5, repr=False)
    id_factory: Callable[[], str] = field(default=_new_run_id, repr=False)
    operation_id_factory: Callable[[], str] = field(
        default=lambda: f"acquisition-op-{token_hex(8)}", repr=False
    )
    probe_request_id_factory: Callable[[], str] = field(
        default=lambda: f"probe-request-{token_hex(8)}", repr=False
    )
    recording_planner: RecordingSegmentPlanningBoundary | None = field(default=None, repr=False)
    replay_extractor: ReplayExtractionBoundary | None = field(default=None, repr=False)
    batch_decoder: RecordingProbeBatchDecoder | None = field(default=None, repr=False)
    classification_service: ObservationClassificationService | None = field(
        default=None, repr=False
    )
    _active: dict[str, _ActiveRun] = field(default_factory=dict, init=False, repr=False)
    _guard: RLock = field(default_factory=RLock, init=False, repr=False)

    def acquire_targets(
        self, handle: RecordingSearchRunHandle, requested_times: tuple[datetime, ...]
    ) -> tuple[ProbeFrameRequestRecord, ...]:
        """Acquire ordered recording probes through the Phase 7A-2 boundary."""
        return acquire_targets(self, handle, requested_times)

    def build_coarse_plan(self, handle: RecordingSearchRunHandle) -> CoarseSamplingPlan:
        """Build the chronological Phase 7C-1 plan for the active run."""
        with self.a2_mutation(handle):
            manifest = self.repository.load(handle.investigation_id, handle.search_run_id)
            if isinstance(manifest, RecordingSearchManifestV3):
                policy = manifest.as_schema2().policy
                state = manifest.state
            else:
                policy = manifest.policy
                state = manifest.state
            if state not in {RecordingSearchState.RUNNING, "RUNNING"}:
                raise RecordingSearchBaselineError
        return build_coarse_sampling_plan(policy)

    def execute_coarse_sampling(self, handle: RecordingSearchRunHandle) -> CoarseSamplingResult:
        """Execute the Phase 7C-1 plan through A2 acquisition and B4 classification."""
        plan = self.build_coarse_plan(handle)
        return CoarseSamplingExecutor(self).execute(handle, plan)

    def interpret_coarse_sampling(
        self, handle: RecordingSearchRunHandle, execution: CoarseSamplingResult
    ) -> CoarseInterpretationResult:
        """Recompute a non-persistent interpretation from one active run."""
        if not execution.complete:
            return _interpret_incomplete_snapshot(self, handle, execution)
        try:
            with self.a2_mutation(handle):
                loaded = self._load_coarse_snapshot(handle, execution)
        except (RecordingSearchBaselineError, RecordingSearchNotFoundError):
            return CoarseInterpretationResult(
                status=CoarseInterpretationStatus.INTERRUPTED,
                safe_reason="inactive_run_handle",
            )
        except (
            RecordingSearchManifestCorruptError,
            RecordingSearchArtifactError,
            ValueError,
        ):
            return CoarseInterpretationResult(
                status=CoarseInterpretationStatus.CORRUPT,
                safe_reason="authoritative_evidence_invalid",
            )
        if isinstance(loaded, CoarseInterpretationResult):
            return loaded
        return interpret_coarse_evidence(loaded)

    def _load_coarse_snapshot(
        self, handle: RecordingSearchRunHandle, execution: CoarseSamplingResult
    ) -> CoarseEvidenceSnapshot | CoarseInterpretationResult:
        manifest = self.repository.load(handle.investigation_id, handle.search_run_id)
        if isinstance(manifest, RecordingSearchManifestV3):
            if manifest.state != "RUNNING":
                return CoarseInterpretationResult(
                    status=CoarseInterpretationStatus.CORRUPT,
                    safe_reason="lifecycle_state_invalid",
                )
            plan = build_coarse_sampling_plan(manifest.as_schema2().policy)
            return capture_coarse_evidence_snapshot(self.repository, manifest, plan, execution)
        if not isinstance(manifest, RecordingSearchManifestV2):
            return CoarseInterpretationResult(
                status=CoarseInterpretationStatus.CORRUPT,
                safe_reason="unsupported_manifest_state",
            )
        if any(sample.status is CoarseSampleStatus.SUCCESS for sample in execution.samples):
            return CoarseInterpretationResult(
                status=CoarseInterpretationStatus.CORRUPT,
                safe_reason="classification_evidence_missing",
            )
        plan = build_coarse_sampling_plan(manifest.policy)
        if plan != execution.plan:
            return CoarseInterpretationResult(
                status=CoarseInterpretationStatus.CORRUPT,
                safe_reason="coarse_plan_mismatch",
            )
        return _schema2_snapshot(manifest, plan, execution)

    def classify(
        self,
        handle: RecordingSearchRunHandle,
        request: ClassifyRecordingProbeRequest,
    ) -> PublishedClassificationResult:
        """Run the sole authoritative Phase 7B classification operation."""
        service = self.classification_service
        if service is None:
            raise ClassificationOperationalError(
                ClassificationOperationalReason.CLASSIFIER_UNAVAILABLE
            )
        return service.classify(handle, request)

    @contextmanager
    def a2_mutation(self, handle: A2HandleBoundary) -> Generator[None, None, None]:
        """Hold the active handle guard and shared A2 mutation mutex."""
        with self._guard:
            active = self._active.get(handle.investigation_id)
            if (
                handle.closed
                or active is None
                or active.run_id != handle.search_run_id
                or not active.os_lock.held
            ):
                raise RecordingSearchBaselineError
            with active.mutation_lock:
                yield

    def _active_for_handle(self, handle: RecordingSearchRunHandle) -> _ActiveRun:
        """Validate active ownership before an A2 mutation acquires its mutex."""
        with self._guard:
            active = self._active.get(handle.investigation_id)
            if (
                handle.closed
                or active is None
                or active.run_id != handle.search_run_id
                or not active.os_lock.held
            ):
                raise RecordingSearchBaselineError
            return active

    def start(self, request: RecordingSearchRequest) -> RecordingSearchStartResult:
        """Validate the baseline and create one active run."""
        baseline, end_utc = self._validate_baseline(request)
        with self._guard:
            active = self._active.get(request.investigation_id)
            if active is not None:
                manifest = _status_manifest(
                    self.repository.load(request.investigation_id, active.run_id)
                )
                return RecordingSearchStartResult(
                    manifest=manifest,
                    outcome=RecordingSearchOutcome.ALREADY_RUNNING,
                    baseline_bytes=bytes(baseline[1]),
                )
            lock = LocalInvestigationLock(self.repository.lock_path(request.investigation_id))
            try:
                if not lock.try_acquire(self.lock_timeout_seconds):
                    existing = self.repository.latest_nonterminal(request.investigation_id)
                    if existing is None:
                        _raise_baseline()
                    return RecordingSearchStartResult(
                        manifest=_status_manifest(existing),
                        outcome=RecordingSearchOutcome.ALREADY_RUNNING,
                        baseline_bytes=bytes(baseline[1]),
                    )
                previous = self.repository.latest_nonterminal(request.investigation_id)
                if previous is not None:
                    _ = self.repository.transition(
                        request.investigation_id,
                        previous.search_run_id,
                        RecordingSearchState.INTERRUPTED,
                        "process_lock_released",
                    )
                run_id = self._new_unique_run_id(request.investigation_id)
                created = _canonical_now(self.now_utc())
                manifest = RecordingSearchManifest(
                    schema_version=1,
                    investigation_id=request.investigation_id,
                    search_run_id=run_id,
                    state=RecordingSearchState.PENDING,
                    created_at_utc=created,
                    started_at_utc=None,
                    completed_at_utc=None,
                    confirmation=baseline[0],
                    policy=default_policy(baseline[0].reference_requested_time_utc, end_utc),
                    canonical_observation_ids=(),
                    target_alias_ids=(),
                    candidate_interval=None,
                    failure_reason=None,
                    phase8_handoff_status=Phase8HandoffStatus.NOT_APPLICABLE,
                    phase8_failure_reason=None,
                )
                _ = self.repository.create(manifest)
                running = _status_manifest(
                    self.repository.transition(
                        request.investigation_id, run_id, RecordingSearchState.RUNNING
                    )
                )
                mutation_lock = RLock()
                handle = RecordingSearchRunHandle(
                    self,
                    request.investigation_id,
                    run_id,
                    request.investigation_id,
                    baseline_identity_for(baseline[0]),
                    bytes(baseline[1]),
                    mutation_lock,
                )
                self._active[request.investigation_id] = _ActiveRun(run_id, lock, mutation_lock)
                return RecordingSearchStartResult(
                    manifest=running,
                    outcome=RecordingSearchOutcome.STARTED,
                    baseline_bytes=bytes(baseline[1]),
                    run_handle=handle,
                )
            except Exception:
                lock.release()
                raise

    def status(
        self, investigation_id: str, search_run_id: str
    ) -> RecordingSearchManifest | RecordingSearchManifestV2:
        """Return persisted status and reconcile an unowned active run."""
        with self._guard:
            active = self._active.get(investigation_id)
            if active is not None:
                if active.run_id != search_run_id:
                    return _status_manifest(self.repository.load(investigation_id, search_run_id))
                return _status_manifest(self.repository.load(investigation_id, search_run_id))
            lock = LocalInvestigationLock(self.repository.lock_path(investigation_id))
            try:
                if not lock.try_acquire(self.lock_timeout_seconds):
                    return _status_manifest(self.repository.load(investigation_id, search_run_id))
                persisted = self.repository.load(investigation_id, search_run_id)
                manifest = _status_manifest(persisted)
                if manifest.state in (RecordingSearchState.PENDING, RecordingSearchState.RUNNING):
                    return _status_manifest(
                        self.repository.transition(
                            investigation_id,
                            search_run_id,
                            RecordingSearchState.INTERRUPTED,
                            "process_lock_released",
                        )
                    )
                return manifest
            finally:
                lock.release()

    def close(self) -> None:
        """Release all locks owned by this process instance."""
        if self.classification_service is not None:
            self.classification_service.close()
        with self._guard:
            active = tuple(self._active.values())
            self._active.clear()
            for item in active:
                item.os_lock.release()

    def mark_terminal_for_handle(
        self,
        handle: RecordingSearchRunHandle,
        state: RecordingSearchState,
        failure_reason: str,
    ) -> RecordingSearchManifest | RecordingSearchManifestV2:
        """Persist a terminal state for a process-owned handle."""
        with self._guard:
            active = self._active.get(handle.investigation_id)
            if active is None or active.run_id != handle.search_run_id:
                raise RecordingSearchBaselineError
            with active.mutation_lock:
                try:
                    return _status_manifest(
                        self.repository.transition(
                            handle.investigation_id,
                            handle.search_run_id,
                            state,
                            failure_reason,
                        )
                    )
                finally:
                    _ = self._active.pop(handle.investigation_id, None)
                    active.os_lock.release()

    def release_handle(self, handle: RecordingSearchRunHandle) -> None:
        """Release a process-owned handle without publishing completion."""
        with self._guard:
            active = self._active.get(handle.investigation_id)
            if active is None or active.run_id != handle.search_run_id:
                return
            with active.mutation_lock:
                _ = self._active.pop(handle.investigation_id, None)
                active.os_lock.release()

    def _validate_baseline(
        self, request: RecordingSearchRequest
    ) -> tuple[tuple[RecordingSearchBaseline, bytes], datetime]:
        try:
            loaded = self._load_confirmed(request.investigation_id)
            self._validate_authoritative_facts(request, loaded)
            end_utc = self._validate_search_end(request, loaded)
            raw = self._read_verified_jpeg(loaded)
            return (self._baseline_from(loaded), raw), end_utc
        except (RecordingSearchBaselineError, ReconfirmationRequiredError):
            raise
        except Exception:  # noqa: BLE001 - safe baseline boundary redacts unknown failures.
            raise RecordingSearchBaselineError from None

    def _load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
        try:
            return self.confirmation_service.load_confirmed(investigation_id)
        except LegacyInvestigationError:
            raise ReconfirmationRequiredError from None
        except (ConfirmationError, ReferenceFrameError, OSError, ValueError):
            raise RecordingSearchBaselineError from None
        except Exception:  # noqa: BLE001 - safe boundary redacts unknown loader failures.
            raise RecordingSearchBaselineError from None

    def _validate_authoritative_facts(
        self, request: RecordingSearchRequest, loaded: ConfirmedInvestigationInput
    ) -> None:
        if loaded.investigation_id != request.investigation_id:
            raise RecordingSearchBaselineError
        if loaded.source_timezone != request.source_timezone:
            raise RecordingSearchBaselineError
        if not is_safe_contained_path(self.artifact_root, loaded.jpeg_path, require_target=True):
            raise RecordingSearchBaselineError
        if loaded.jpeg_path.is_symlink():
            raise RecordingSearchBaselineError
        if loaded.roi.coordinate_space != "source_pixels":
            raise RecordingSearchBaselineError
        if (
            loaded.roi.x + loaded.roi.width > loaded.source_width
            or loaded.roi.y + loaded.roi.height > loaded.source_height
        ):
            raise RecordingSearchBaselineError
        channels = usable_channels(tuple(self.channel_inventory.channels()))
        if not any(channel.channel_id == loaded.channel_id for channel in channels):
            raise RecordingSearchBaselineError
        if not self.policy_availability.is_available():
            raise RecordingSearchBaselineError

    def _validate_search_end(
        self, request: RecordingSearchRequest, loaded: ConfirmedInvestigationInput
    ) -> datetime:
        try:
            end_request = parse_reference_frame_request(
                channel_id=loaded.channel_id,
                requested_time_text=request.search_end_time_text,
                source_timezone=request.source_timezone,
                now_utc=_canonical_now(self.now_utc()),
            )
        except (ReferenceFrameError, ValueError):
            raise RecordingSearchBaselineError from None
        if end_request.source_timezone != loaded.source_timezone:
            raise RecordingSearchBaselineError
        if not loaded.requested_time_utc < end_request.requested_time_utc:
            raise RecordingSearchBaselineError
        if end_request.requested_time_utc > loaded.requested_time_utc + timedelta(hours=24):
            raise RecordingSearchBaselineError
        return end_request.requested_time_utc

    def _read_verified_jpeg(self, loaded: ConfirmedInvestigationInput) -> bytes:
        try:
            integrity = compute_jpeg_integrity(
                loaded.jpeg_path,
                loaded.source_width,
                loaded.source_height,
                self.jpeg_decoder,
            )
            raw = loaded.jpeg_path.read_bytes()
        except (ConfirmationError, OSError, ValueError):
            raise RecordingSearchBaselineError from None
        if (
            integrity.sha256 != loaded.jpeg_sha256
            or integrity.size_bytes != loaded.jpeg_size_bytes
            or hashlib.sha256(raw).hexdigest() != loaded.jpeg_sha256
            or len(raw) != loaded.jpeg_size_bytes
        ):
            raise RecordingSearchBaselineError
        return raw

    @staticmethod
    def _baseline_from(loaded: ConfirmedInvestigationInput) -> RecordingSearchBaseline:
        return RecordingSearchBaseline(
            channel_id=loaded.channel_id,
            reference_frame_resource_id=loaded.reference_frame_resource_id,
            anchor_time_utc=loaded.anchor_time_utc,
            reference_requested_time_utc=loaded.requested_time_utc,
            source_timezone=loaded.source_timezone,
            source_width=loaded.source_width,
            source_height=loaded.source_height,
            roi=loaded.roi,
            jpeg_sha256=loaded.jpeg_sha256,
            jpeg_size_bytes=loaded.jpeg_size_bytes,
            candidate_offset_seconds=loaded.candidate_offset_seconds,
            generation_policy_version=loaded.generation_policy_version,
            frame_selection_policy=loaded.frame_selection_policy,
            estimated_source_time_utc=loaded.estimated_source_time_utc,
            decoded_local_pts_seconds=loaded.decoded_local_pts_seconds,
            timing_precision_status=loaded.timing_precision_status,
            warnings=loaded.warnings,
        )

    def _new_unique_run_id(self, investigation_id: str) -> str:
        for _ in range(8):
            value = self.id_factory()
            if self.repository.run_path(investigation_id, value).exists():
                continue
            return value
        raise RecordingSearchBaselineError


def _canonical_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise RecordingSearchBaselineError
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _raise_baseline() -> NoReturn:
    raise RecordingSearchBaselineError
