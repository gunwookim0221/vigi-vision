"""Phase 7E successor Slice 2: bounded per-target replay and frame acquisition."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol

from vigi_vision.recording import RecordingUnavailableError
from vigi_vision.recording_models import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.recording_search_successor import (
    CoarseTargetAssignment,
    MultiSegmentCoarsePlan,
    SegmentCoverage,
    TargetAvailability,
)
from vigi_vision.reference_frame_decoder import (
    ReferenceFrameDecoder,
    ReferenceFrameDecodeRequest,
    ReferenceFrameDecodeTimeoutError,
)
from vigi_vision.reference_frame_models import (
    FrameSelectionPolicy,
    ReferenceFrameDecodeError,
    ReferenceFrameNoCandidateError,
    TimingPrecisionStatus,
)
from vigi_vision.replay import (
    ReplayAuthenticationError,
    ReplayClip,
    ReplayError,
    ReplayExtractionError,
    ReplayTimeoutError,
    ReplayUnavailableError,
)

DEFAULT_TARGET_PADDING_SECONDS = 5
MAXIMUM_TARGET_WINDOW_SECONDS = 10
MAXIMUM_FRAME_BYTES = 16 * 1024 * 1024
_SHA256_HEX_LENGTH = 64


class SuccessorTargetStatus(str, Enum):
    """Closed per-target outcome vocabulary consumed by Slice 3."""

    FRAME_AVAILABLE = "FRAME_AVAILABLE"
    UNAVAILABLE_GAP = "UNAVAILABLE_GAP"
    RECORDING_UNAVAILABLE = "RECORDING_UNAVAILABLE"
    REPLAY_TIMEOUT = "REPLAY_TIMEOUT"
    REPLAY_FAILED = "REPLAY_FAILED"
    DECODE_TIMEOUT = "DECODE_TIMEOUT"
    DECODE_UNAVAILABLE = "DECODE_UNAVAILABLE"


class SuccessorAcquisitionError(RuntimeError):
    """Base class for bounded successor acquisition failures."""


class SuccessorAcquisitionContractError(SuccessorAcquisitionError):
    """Raised when a plan or boundary returns facts outside its contract."""


class SuccessorAcquisitionCleanupError(SuccessorAcquisitionError):
    """Raised when invocation-owned temporary media cannot be removed."""


@dataclass(frozen=True, slots=True)
class SuccessorTargetAcquisitionPolicy:
    """Short-window and frame-size limits for one successor target."""

    target_padding_seconds: int = DEFAULT_TARGET_PADDING_SECONDS
    maximum_window_seconds: int = MAXIMUM_TARGET_WINDOW_SECONDS
    maximum_frame_bytes: int = MAXIMUM_FRAME_BYTES
    frame_selection_policy: FrameSelectionPolicy = FrameSelectionPolicy.NEAREST_DECODED_FRAME

    def __post_init__(self) -> None:
        """Validate bounded target acquisition limits."""
        if (
            type(self.target_padding_seconds) is not int
            or self.target_padding_seconds <= 0
            or type(self.maximum_window_seconds) is not int
            or self.maximum_window_seconds <= 0
            or self.maximum_window_seconds > self.target_padding_seconds * 2
            or type(self.maximum_frame_bytes) is not int
            or self.maximum_frame_bytes <= 0
            or type(self.frame_selection_policy) is not FrameSelectionPolicy
        ):
            raise SuccessorAcquisitionContractError


@dataclass(frozen=True, slots=True)
class SuccessorTargetAcquisitionResult:
    """Bounded target facts with an in-memory frame for classifier input."""

    plan_id: str
    target_id: str
    acquisition_id: str
    sequence: int
    requested_time_utc: datetime
    assigned_segment_id: str | None
    replay_window: RecordingWindow | None
    status: SuccessorTargetStatus
    frame_utc: datetime | None = None
    frame_pts_seconds: float | None = None
    frame_offset_seconds: float | None = None
    frame_bytes: bytes | None = field(default=None, repr=False)
    frame_sha256: str | None = None
    frame_size_bytes: int | None = None
    frame_width: int | None = None
    frame_height: int | None = None
    frame_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate target identity, status, and frame evidence consistency."""
        if (
            not self.plan_id
            or not self.target_id
            or not self.acquisition_id
            or type(self.sequence) is not int
            or self.sequence <= 0
            or not _is_whole_utc(self.requested_time_utc)
            or type(self.status) is not SuccessorTargetStatus
        ):
            raise SuccessorAcquisitionContractError
        if self.replay_window is not None and self.replay_window.channel_id <= 0:
            raise SuccessorAcquisitionContractError
        if self.status is SuccessorTargetStatus.UNAVAILABLE_GAP:
            if self.assigned_segment_id is not None or self.replay_window is not None:
                raise SuccessorAcquisitionContractError
        elif self.assigned_segment_id is None or self.replay_window is None:
            raise SuccessorAcquisitionContractError
        if self.status is SuccessorTargetStatus.FRAME_AVAILABLE:
            if (
                self.frame_utc is None
                or not _is_utc(self.frame_utc)
                or self.frame_pts_seconds is None
                or not math.isfinite(self.frame_pts_seconds)
                or self.frame_pts_seconds < 0
                or self.frame_offset_seconds is None
                or not math.isfinite(self.frame_offset_seconds)
                or not self.frame_bytes
                or self.frame_sha256 is None
                or len(self.frame_sha256) != _SHA256_HEX_LENGTH
                or self.frame_size_bytes != len(self.frame_bytes)
                or self.frame_width is None
                or self.frame_width <= 0
                or self.frame_height is None
                or self.frame_height <= 0
            ):
                raise SuccessorAcquisitionContractError
        elif any(
            value is not None
            for value in (
                self.frame_utc,
                self.frame_pts_seconds,
                self.frame_offset_seconds,
                self.frame_bytes,
                self.frame_sha256,
                self.frame_size_bytes,
                self.frame_width,
                self.frame_height,
            )
        ):
            raise SuccessorAcquisitionContractError


class SuccessorRecordingReplayBoundary(Protocol):
    """Existing public recording boundary used to build one target replay."""

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        """Build a replay request wholly contained by one assigned segment."""
        ...


class SuccessorReplayExtractionBoundary(Protocol):
    """Existing bounded replay extraction boundary."""

    def extract(self, request: ReplayRequest) -> ReplayClip:
        """Extract one invocation-owned temporary MP4."""
        ...


def successor_target_id(plan: MultiSegmentCoarsePlan, target: CoarseTargetAssignment) -> str:
    """Return a deterministic identity for one plan-owned coarse target."""
    _validate_target_membership(plan, target)
    return _hashed_identity(
        "successor-target-v1-",
        {
            "plan_id": plan.plan_id,
            "sequence": target.sequence,
            "requested_time_utc": _timestamp(target.requested_time_utc),
            "availability": target.availability.value,
            "segment_id": target.segment_id,
        },
    )


def successor_midpoint_target_id(
    plan: MultiSegmentCoarsePlan, target: CoarseTargetAssignment
) -> str:
    """Return a deterministic identity for a bounded narrowing target."""
    _validate_midpoint_target(plan, target)
    return _hashed_identity(
        "successor-midpoint-target-v1-",
        {
            "plan_id": plan.plan_id,
            "requested_time_utc": _timestamp(target.requested_time_utc),
            "segment_id": target.segment_id,
        },
    )


def build_successor_target_window(
    plan: MultiSegmentCoarsePlan,
    target: CoarseTargetAssignment,
    policy: SuccessorTargetAcquisitionPolicy | None = None,
) -> RecordingWindow | None:
    """Build a short window inside the one segment assigned to ``target``."""
    selected_policy = policy or SuccessorTargetAcquisitionPolicy()
    return _build_successor_target_window(plan, target, selected_policy, validate_membership=True)


def _build_successor_target_window(
    plan: MultiSegmentCoarsePlan,
    target: CoarseTargetAssignment,
    selected_policy: SuccessorTargetAcquisitionPolicy,
    *,
    validate_membership: bool,
) -> RecordingWindow | None:
    if validate_membership:
        _validate_target_membership(plan, target)
    if target.availability is TargetAvailability.UNAVAILABLE:
        return None
    coverage = _assigned_coverage(plan, target)
    start = max(
        coverage.start_utc,
        plan.anchor_time_utc,
        target.requested_time_utc - timedelta(seconds=selected_policy.target_padding_seconds),
    )
    end = min(
        coverage.end_utc,
        plan.search_end_utc,
        target.requested_time_utc + timedelta(seconds=selected_policy.target_padding_seconds),
    )
    if end <= start or (end - start).total_seconds() > selected_policy.maximum_window_seconds:
        raise SuccessorAcquisitionContractError
    return RecordingWindow(plan.channel_id, start, end)


@dataclass(slots=True)
class SuccessorTargetAcquisitionService:
    """Acquire each target once through replay and local nearest-PTS decoding."""

    recording_planner: SuccessorRecordingReplayBoundary
    replay_extractor: SuccessorReplayExtractionBoundary
    decoder: ReferenceFrameDecoder
    policy: SuccessorTargetAcquisitionPolicy = field(
        default_factory=SuccessorTargetAcquisitionPolicy
    )
    temporary_directory: Path | None = field(default=None, repr=False)
    _cache: dict[str, SuccessorTargetAcquisitionResult] = field(
        default_factory=dict, init=False, repr=False
    )

    def acquire_plan(
        self, plan: MultiSegmentCoarsePlan
    ) -> tuple[SuccessorTargetAcquisitionResult, ...]:
        """Acquire all targets independently, preserving plan order."""
        return tuple(self.acquire(plan, target) for target in plan.targets)

    def acquire(
        self, plan: MultiSegmentCoarsePlan, target: CoarseTargetAssignment
    ) -> SuccessorTargetAcquisitionResult:
        """Acquire one target or preserve its safe target-level unavailable state."""
        return self._acquire_with_identity(plan, target, successor_target_id(plan, target))

    def acquire_midpoint(
        self, plan: MultiSegmentCoarsePlan, target: CoarseTargetAssignment
    ) -> SuccessorTargetAcquisitionResult:
        """Acquire one narrowing midpoint through the same short-window path."""
        target_id = successor_midpoint_target_id(plan, target)
        return self._acquire_with_identity(plan, target, target_id, validate_membership=False)

    def _acquire_with_identity(  # noqa: C901
        self,
        plan: MultiSegmentCoarsePlan,
        target: CoarseTargetAssignment,
        target_id: str,
        *,
        validate_membership: bool = True,
    ) -> SuccessorTargetAcquisitionResult:
        window = _build_successor_target_window(
            plan,
            target,
            self.policy,
            validate_membership=validate_membership,
        )
        acquisition_id = _acquisition_id(plan, target, window, self.policy, target_id)
        cached = self._cache.get(acquisition_id)
        if cached is not None:
            return cached
        if window is None:
            result = SuccessorTargetAcquisitionResult(
                plan.plan_id,
                target_id,
                acquisition_id,
                target.sequence,
                target.requested_time_utc,
                None,
                None,
                SuccessorTargetStatus.UNAVAILABLE_GAP,
            )
            self._cache[acquisition_id] = result
            return result
        coverage = _assigned_coverage(plan, target)
        replay_request: ReplayRequest
        try:
            replay_request = self.recording_planner.plan_for_segment(
                _recording_segment(coverage), window
            )
        except RecordingUnavailableError:
            result = self._unavailable_result(
                plan,
                target,
                target_id,
                acquisition_id,
                window,
                SuccessorTargetStatus.RECORDING_UNAVAILABLE,
            )
            self._cache[acquisition_id] = result
            return result
        if replay_request.window != window:
            raise SuccessorAcquisitionContractError
        clip: ReplayClip | None = None
        try:
            try:
                clip = self.replay_extractor.extract(replay_request)
            except ReplayUnavailableError:
                result = self._unavailable_result(
                    plan,
                    target,
                    target_id,
                    acquisition_id,
                    window,
                    SuccessorTargetStatus.RECORDING_UNAVAILABLE,
                )
            except ReplayTimeoutError:
                result = self._unavailable_result(
                    plan,
                    target,
                    target_id,
                    acquisition_id,
                    window,
                    SuccessorTargetStatus.REPLAY_TIMEOUT,
                )
            except (ReplayAuthenticationError, ReplayExtractionError, ReplayError):
                result = self._unavailable_result(
                    plan,
                    target,
                    target_id,
                    acquisition_id,
                    window,
                    SuccessorTargetStatus.REPLAY_FAILED,
                )
            else:
                if (
                    clip.channel_id != plan.channel_id
                    or clip.requested_start_utc != window.start_utc
                    or clip.requested_end_utc != window.end_utc
                ):
                    raise SuccessorAcquisitionContractError
                result = self._decode_target(plan, target, target_id, acquisition_id, window, clip)
            self._cache[acquisition_id] = result
            return result
        finally:
            if clip is not None:
                try:
                    clip.remove()
                except OSError as exc:
                    raise SuccessorAcquisitionCleanupError from exc

    def _decode_target(  # noqa: C901, PLR0911, PLR0913
        self,
        plan: MultiSegmentCoarsePlan,
        target: CoarseTargetAssignment,
        target_id: str,
        acquisition_id: str,
        window: RecordingWindow,
        clip: ReplayClip,
    ) -> SuccessorTargetAcquisitionResult:
        if self.temporary_directory is not None:
            try:
                self.temporary_directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SuccessorAcquisitionCleanupError from exc
        frame_root = Path(
            tempfile.mkdtemp(prefix="vigi-vision-successor-", dir=self.temporary_directory)
        )
        frame_path = frame_root / "frame.jpg"
        try:
            try:
                evidence = self.decoder.decode(
                    ReferenceFrameDecodeRequest(
                        clip.temporary_mp4_path,
                        (target.requested_time_utc - window.start_utc).total_seconds(),
                        self.policy.frame_selection_policy,
                        frame_path,
                    )
                )
            except ReferenceFrameDecodeTimeoutError:
                return self._unavailable_result(
                    plan,
                    target,
                    target_id,
                    acquisition_id,
                    window,
                    SuccessorTargetStatus.DECODE_TIMEOUT,
                )
            except (ReferenceFrameDecodeError, ReferenceFrameNoCandidateError):
                return self._unavailable_result(
                    plan,
                    target,
                    target_id,
                    acquisition_id,
                    window,
                    SuccessorTargetStatus.DECODE_UNAVAILABLE,
                )
            frame_path = evidence.jpeg_path
            if not _is_contained_file(frame_path, frame_root):
                raise SuccessorAcquisitionContractError
            if (
                evidence.timing_precision_status is not TimingPrecisionStatus.MEASURED_CLIP_RELATIVE
                or evidence.local_pts_seconds is None
                or not math.isfinite(evidence.local_pts_seconds)
                or evidence.local_pts_seconds < 0
            ):
                return self._unavailable_result(
                    plan,
                    target,
                    target_id,
                    acquisition_id,
                    window,
                    SuccessorTargetStatus.DECODE_UNAVAILABLE,
                )
            try:
                frame_bytes = frame_path.read_bytes()
            except OSError:
                return self._unavailable_result(
                    plan,
                    target,
                    target_id,
                    acquisition_id,
                    window,
                    SuccessorTargetStatus.DECODE_UNAVAILABLE,
                )
            if not frame_bytes or len(frame_bytes) > self.policy.maximum_frame_bytes:
                return self._unavailable_result(
                    plan,
                    target,
                    target_id,
                    acquisition_id,
                    window,
                    SuccessorTargetStatus.DECODE_UNAVAILABLE,
                )
            frame_utc = window.start_utc + timedelta(seconds=evidence.local_pts_seconds)
            if frame_utc < window.start_utc or frame_utc > window.end_utc:
                return self._unavailable_result(
                    plan,
                    target,
                    target_id,
                    acquisition_id,
                    window,
                    SuccessorTargetStatus.DECODE_UNAVAILABLE,
                )
            return SuccessorTargetAcquisitionResult(
                plan.plan_id,
                target_id,
                acquisition_id,
                target.sequence,
                target.requested_time_utc,
                target.segment_id,
                window,
                SuccessorTargetStatus.FRAME_AVAILABLE,
                frame_utc,
                evidence.local_pts_seconds,
                (frame_utc - target.requested_time_utc).total_seconds(),
                frame_bytes,
                hashlib.sha256(frame_bytes).hexdigest(),
                len(frame_bytes),
                evidence.width,
                evidence.height,
                evidence.warnings,
            )
        finally:
            try:
                shutil.rmtree(frame_root)
            except OSError as exc:
                raise SuccessorAcquisitionCleanupError from exc

    @staticmethod
    def _unavailable_result(  # noqa: PLR0913
        plan: MultiSegmentCoarsePlan,
        target: CoarseTargetAssignment,
        target_id: str,
        acquisition_id: str,
        window: RecordingWindow,
        status: SuccessorTargetStatus,
    ) -> SuccessorTargetAcquisitionResult:
        return SuccessorTargetAcquisitionResult(
            plan.plan_id,
            target_id,
            acquisition_id,
            target.sequence,
            target.requested_time_utc,
            target.segment_id,
            window,
            status,
        )


def _validate_target_membership(
    plan: MultiSegmentCoarsePlan, target: CoarseTargetAssignment
) -> None:
    if target not in plan.targets:
        raise SuccessorAcquisitionContractError


def _validate_midpoint_target(plan: MultiSegmentCoarsePlan, target: CoarseTargetAssignment) -> None:
    if (
        target.availability is not TargetAvailability.AVAILABLE
        or not _is_whole_utc(target.requested_time_utc)
        or target.requested_time_utc < plan.anchor_time_utc
        or target.requested_time_utc > plan.search_end_utc
        or target.segment_id is None
        or sum(item.segment_id == target.segment_id for item in plan.segments) != 1
    ):
        raise SuccessorAcquisitionContractError
    coverage = next(item for item in plan.segments if item.segment_id == target.segment_id)
    inside_coverage = coverage.start_utc <= target.requested_time_utc < coverage.end_utc
    at_search_end = (
        target.requested_time_utc == plan.search_end_utc
        and target.requested_time_utc == coverage.end_utc
    )
    if not inside_coverage and not at_search_end:
        raise SuccessorAcquisitionContractError


def _assigned_coverage(
    plan: MultiSegmentCoarsePlan, target: CoarseTargetAssignment
) -> SegmentCoverage:
    if target.segment_id is None or target.availability is not TargetAvailability.AVAILABLE:
        raise SuccessorAcquisitionContractError
    matches = tuple(item for item in plan.segments if item.segment_id == target.segment_id)
    if len(matches) != 1:
        raise SuccessorAcquisitionContractError
    return matches[0]


def _recording_segment(coverage: SegmentCoverage) -> RecordingSegment:
    return RecordingSegment(
        coverage.channel_id,
        coverage.start_utc.date(),
        int(coverage.start_utc.timestamp()),
        int(coverage.end_utc.timestamp()),
        coverage.start_utc,
        coverage.end_utc,
    )


def _acquisition_id(
    plan: MultiSegmentCoarsePlan,
    target: CoarseTargetAssignment,
    window: RecordingWindow | None,
    policy: SuccessorTargetAcquisitionPolicy,
    target_id: str | None = None,
) -> str:
    return _hashed_identity(
        "successor-acquisition-v1-",
        {
            "plan_id": plan.plan_id,
            "target_id": target_id or successor_target_id(plan, target),
            "segment_id": target.segment_id,
            "window_start_utc": None if window is None else _timestamp(window.start_utc),
            "window_end_utc": None if window is None else _timestamp(window.end_utc),
            "frame_selection_policy": policy.frame_selection_policy.value,
            "target_padding_seconds": policy.target_padding_seconds,
            "maximum_window_seconds": policy.maximum_window_seconds,
        },
    )


def _hashed_identity(prefix: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _is_contained_file(path: Path, root: Path) -> bool:
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        return resolved_path.is_file() and resolved_path.is_relative_to(resolved_root)
    except OSError:
        return False


def _is_whole_utc(value: datetime) -> bool:
    return _is_utc(value) and value.microsecond == 0


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


__all__ = (
    "DEFAULT_TARGET_PADDING_SECONDS",
    "MAXIMUM_FRAME_BYTES",
    "MAXIMUM_TARGET_WINDOW_SECONDS",
    "SuccessorAcquisitionCleanupError",
    "SuccessorAcquisitionContractError",
    "SuccessorAcquisitionError",
    "SuccessorRecordingReplayBoundary",
    "SuccessorReplayExtractionBoundary",
    "SuccessorTargetAcquisitionPolicy",
    "SuccessorTargetAcquisitionResult",
    "SuccessorTargetAcquisitionService",
    "SuccessorTargetStatus",
    "build_successor_target_window",
    "successor_midpoint_target_id",
    "successor_target_id",
)
