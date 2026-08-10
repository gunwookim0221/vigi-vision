"""Phase 7A-1 recording-search validation and lifecycle service."""

from __future__ import annotations

import hashlib
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
from vigi_vision.recording_search_lock import LocalInvestigationLock
from vigi_vision.recording_search_models import (
    Phase8HandoffStatus,
    ReconfirmationRequiredError,
    RecordingSearchBaseline,
    RecordingSearchBaselineError,
    RecordingSearchManifest,
    RecordingSearchOutcome,
    RecordingSearchRequest,
    RecordingSearchState,
    default_policy,
)
from vigi_vision.reference_frame_models import ReferenceFrameError, parse_reference_frame_request

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vigi_vision.investigation_confirmation_integrity import JpegDecoder
    from vigi_vision.recording_search_repository import RecordingSearchRepository


def _new_run_id() -> str:
    return f"search-run-{token_hex(4)}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


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

    manifest: RecordingSearchManifest
    outcome: RecordingSearchOutcome
    baseline_bytes: bytes = field(repr=False)
    run_handle: RecordingSearchRunHandle | None = field(default=None, repr=False)


@final
class RecordingSearchRunHandle:
    """Process-owned handle for the active investigation lock."""

    __slots__ = ("_closed", "_service", "baseline_bytes", "investigation_id", "search_run_id")

    def __init__(
        self,
        service: RecordingSearchService,
        investigation_id: str,
        search_run_id: str,
        baseline_bytes: bytes,
    ) -> None:
        """Retain the active lock and invocation-owned baseline bytes."""
        self._service = service
        self.investigation_id = investigation_id
        self.search_run_id = search_run_id
        self.baseline_bytes = baseline_bytes
        self._closed = False

    def mark_terminal(
        self, state: RecordingSearchState, failure_reason: str
    ) -> RecordingSearchManifest:
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
    _active: dict[str, tuple[str, LocalInvestigationLock]] = field(
        default_factory=dict, init=False, repr=False
    )
    _guard: RLock = field(default_factory=RLock, init=False, repr=False)

    def start(self, request: RecordingSearchRequest) -> RecordingSearchStartResult:
        """Validate the baseline and create one active run."""
        baseline, end_utc = self._validate_baseline(request)
        with self._guard:
            active = self._active.get(request.investigation_id)
            if active is not None:
                manifest = self.repository.load(request.investigation_id, active[0])
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
                        manifest=existing,
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
                running = self.repository.transition(
                    request.investigation_id, run_id, RecordingSearchState.RUNNING
                )
                handle = RecordingSearchRunHandle(
                    self,
                    request.investigation_id,
                    run_id,
                    bytes(baseline[1]),
                )
                self._active[request.investigation_id] = (run_id, lock)
                return RecordingSearchStartResult(
                    manifest=running,
                    outcome=RecordingSearchOutcome.STARTED,
                    baseline_bytes=bytes(baseline[1]),
                    run_handle=handle,
                )
            except Exception:
                lock.release()
                raise

    def status(self, investigation_id: str, search_run_id: str) -> RecordingSearchManifest:
        """Return persisted status and reconcile an unowned active run."""
        with self._guard:
            active = self._active.get(investigation_id)
            if active is not None:
                if active[0] != search_run_id:
                    return self.repository.load(investigation_id, search_run_id)
                return self.repository.load(investigation_id, search_run_id)
            lock = LocalInvestigationLock(self.repository.lock_path(investigation_id))
            try:
                if not lock.try_acquire(self.lock_timeout_seconds):
                    return self.repository.load(investigation_id, search_run_id)
                manifest = self.repository.load(investigation_id, search_run_id)
                if manifest.state in (RecordingSearchState.PENDING, RecordingSearchState.RUNNING):
                    return self.repository.transition(
                        investigation_id,
                        search_run_id,
                        RecordingSearchState.INTERRUPTED,
                        "process_lock_released",
                    )
                return manifest
            finally:
                lock.release()

    def close(self) -> None:
        """Release all locks owned by this process instance."""
        with self._guard:
            active = tuple(self._active.values())
            self._active.clear()
            for _, lock in active:
                lock.release()

    def mark_terminal_for_handle(
        self,
        handle: RecordingSearchRunHandle,
        state: RecordingSearchState,
        failure_reason: str,
    ) -> RecordingSearchManifest:
        """Persist a terminal state for a process-owned handle."""
        with self._guard:
            active = self._active.get(handle.investigation_id)
            if active is None or active[0] != handle.search_run_id:
                raise RecordingSearchBaselineError
            try:
                return self.repository.transition(
                    handle.investigation_id,
                    handle.search_run_id,
                    state,
                    failure_reason,
                )
            finally:
                _ = self._active.pop(handle.investigation_id, None)
                active[1].release()

    def release_handle(self, handle: RecordingSearchRunHandle) -> None:
        """Release a process-owned handle without publishing completion."""
        with self._guard:
            active = self._active.get(handle.investigation_id)
            if active is None or active[0] != handle.search_run_id:
                return
            _ = self._active.pop(handle.investigation_id, None)
            active[1].release()

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
