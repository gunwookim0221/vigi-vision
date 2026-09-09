"""Phase 7E successor Slice 1: bounded multi-segment planning.

This module is deliberately planning-only.  It discovers and normalizes
recording coverage, creates coarse target assignments, and performs no replay,
classification, persistence, or media work.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from vigi_vision.recording_models import RecordingSegment, RecordingWindow
from vigi_vision.reference_frame_models import parse_reference_frame_request, segment_identity

if TYPE_CHECKING:
    from collections.abc import Iterable

SUCCESSOR_PLAN_POLICY_VERSION = "phase7e-multisegment-plan-v1"
DEFAULT_HORIZON_SECONDS = 1_800
MAXIMUM_HORIZON_SECONDS = 7_200
DEFAULT_COARSE_INTERVAL_SECONDS = 600
MINIMUM_COARSE_INTERVAL_SECONDS = 300
MAXIMUM_COARSE_INTERVAL_SECONDS = 600


class SuccessorPlanError(ValueError):
    """Raised when a successor plan cannot be represented safely."""


class TargetAvailability(str, Enum):
    """Closed availability state for one planned coarse target."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class SuccessorPlanningPolicy:
    """Bounded, non-persistent policy for the multi-segment successor."""

    policy_version: str = SUCCESSOR_PLAN_POLICY_VERSION
    default_horizon_seconds: int = DEFAULT_HORIZON_SECONDS
    maximum_horizon_seconds: int = MAXIMUM_HORIZON_SECONDS
    coarse_interval_seconds: int = DEFAULT_COARSE_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        """Validate the bounded successor policy."""
        if (
            not self.policy_version
            or type(self.default_horizon_seconds) is not int
            or self.default_horizon_seconds <= 0
            or type(self.maximum_horizon_seconds) is not int
            or self.maximum_horizon_seconds < self.default_horizon_seconds
            or type(self.coarse_interval_seconds) is not int
            or not MINIMUM_COARSE_INTERVAL_SECONDS
            <= self.coarse_interval_seconds
            <= MAXIMUM_COARSE_INTERVAL_SECONDS
        ):
            raise SuccessorPlanError


@dataclass(frozen=True, slots=True)
class SuccessorPlanRequest:
    """Confirmed anchor and requested successor horizon."""

    channel_id: int
    anchor_time_utc: datetime
    search_end_utc: datetime
    source_timezone: str

    def __post_init__(self) -> None:
        """Validate a whole-second, strictly later successor horizon."""
        if (
            type(self.channel_id) is not int
            or self.channel_id <= 0
            or not self.source_timezone
            or not _is_whole_utc(self.anchor_time_utc)
            or not _is_whole_utc(self.search_end_utc)
            or self.search_end_utc <= self.anchor_time_utc
        ):
            raise SuccessorPlanError

    @classmethod
    def from_text(  # noqa: PLR0913
        cls,
        *,
        channel_id: int,
        anchor_time_utc: datetime,
        search_end_time_text: str | None,
        source_timezone: str,
        now_utc: datetime,
        policy: SuccessorPlanningPolicy | None = None,
    ) -> SuccessorPlanRequest:
        """Normalize an optional local end time while retaining existing validation."""
        selected_policy = policy or SuccessorPlanningPolicy()
        if not _is_whole_utc(now_utc) or not _is_whole_utc(anchor_time_utc):
            raise SuccessorPlanError
        if anchor_time_utc > now_utc:
            raise SuccessorPlanError
        if search_end_time_text is None:
            if source_timezone == "Asia/Seoul":
                local_zone = timezone(timedelta(hours=9), "KST")
            else:
                try:
                    local_zone = ZoneInfo(source_timezone)
                except (ZoneInfoNotFoundError, ValueError):
                    raise SuccessorPlanError from None
            default_end = anchor_time_utc + timedelta(
                seconds=selected_policy.default_horizon_seconds
            )
            search_end_time_text = (
                default_end.astimezone(local_zone)
                .replace(tzinfo=None)
                .isoformat(timespec="seconds")
            )
        try:
            parsed = parse_reference_frame_request(
                channel_id=channel_id,
                requested_time_text=search_end_time_text,
                source_timezone=source_timezone,
                now_utc=now_utc,
            )
        except Exception as exc:
            raise SuccessorPlanError from exc
        if parsed.source_timezone != source_timezone:
            raise SuccessorPlanError
        horizon_seconds = int((parsed.requested_time_utc - anchor_time_utc).total_seconds())
        if horizon_seconds > selected_policy.maximum_horizon_seconds:
            raise SuccessorPlanError
        return cls(channel_id, anchor_time_utc, parsed.requested_time_utc, source_timezone)


@dataclass(frozen=True, slots=True)
class SegmentCoverage:
    """One usable source segment clipped to the requested half-open horizon."""

    segment_id: str
    channel_id: int
    start_utc: datetime
    end_utc: datetime

    def __post_init__(self) -> None:
        """Validate one clipped whole-second coverage interval."""
        if (
            not self.segment_id
            or self.channel_id <= 0
            or not _is_whole_utc(self.start_utc)
            or not _is_whole_utc(self.end_utc)
            or self.end_utc <= self.start_utc
        ):
            raise SuccessorPlanError


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """An uncovered half-open interval in the requested horizon."""

    start_utc: datetime
    end_utc: datetime

    def __post_init__(self) -> None:
        """Validate one positive whole-second uncovered interval."""
        if (
            not _is_whole_utc(self.start_utc)
            or not _is_whole_utc(self.end_utc)
            or self.end_utc <= self.start_utc
        ):
            raise SuccessorPlanError


@dataclass(frozen=True, slots=True)
class CoarseTargetAssignment:
    """One deterministic coarse target and its coverage assignment."""

    sequence: int
    requested_time_utc: datetime
    availability: TargetAvailability
    segment_id: str | None
    gap: CoverageGap | None

    def __post_init__(self) -> None:
        """Validate the mutually exclusive available/unavailable fields."""
        if (
            type(self.sequence) is not int
            or self.sequence <= 0
            or not _is_whole_utc(self.requested_time_utc)
        ):
            raise SuccessorPlanError
        if self.availability is TargetAvailability.AVAILABLE:
            if self.segment_id is None or self.gap is not None:
                raise SuccessorPlanError
        elif self.segment_id is not None or self.gap is None:
            raise SuccessorPlanError


@dataclass(frozen=True, slots=True)
class MultiSegmentCoarsePlan:
    """Immutable planning result; no media or artifact side effects occur."""

    policy_version: str
    channel_id: int
    anchor_time_utc: datetime
    search_end_utc: datetime
    horizon_seconds: int
    coarse_interval_seconds: int
    segments: tuple[SegmentCoverage, ...]
    gaps: tuple[CoverageGap, ...]
    targets: tuple[CoarseTargetAssignment, ...]
    plan_id: str

    def __post_init__(self) -> None:
        """Validate target ordering and the immutable plan identity shape."""
        if (
            not self.policy_version
            or self.channel_id <= 0
            or not _is_whole_utc(self.anchor_time_utc)
            or not _is_whole_utc(self.search_end_utc)
            or self.search_end_utc <= self.anchor_time_utc
            or self.horizon_seconds
            != int((self.search_end_utc - self.anchor_time_utc).total_seconds())
            or self.coarse_interval_seconds <= 0
            or not self.targets
            or not self.plan_id.startswith("successor-plan-v1-")
            or tuple(item.sequence for item in self.targets)
            != tuple(range(1, len(self.targets) + 1))
            or any(
                left.requested_time_utc >= right.requested_time_utc
                for left, right in zip(self.targets, self.targets[1:], strict=False)
            )
        ):
            raise SuccessorPlanError


class RecordingSegmentDiscovery(Protocol):
    """Public recording metadata boundary used by successor planning."""

    def find_segments_for_window(self, window: RecordingWindow) -> tuple[RecordingSegment, ...]:
        """Return every SDK segment intersecting the requested half-open window."""
        ...


@dataclass(frozen=True, slots=True)
class SuccessorPlanService:
    """Compose existing public recording discovery with pure successor planning."""

    discovery: RecordingSegmentDiscovery
    policy: SuccessorPlanningPolicy = field(default_factory=SuccessorPlanningPolicy)

    def plan(self, request: SuccessorPlanRequest) -> MultiSegmentCoarsePlan:
        """Discover metadata and build a plan without replay or persistence."""
        window = RecordingWindow(
            request.channel_id,
            request.anchor_time_utc,
            request.search_end_utc,
        )
        segments = self.discovery.find_segments_for_window(window)
        return build_successor_plan(request, segments, policy=self.policy)


def build_successor_plan(
    request: SuccessorPlanRequest,
    segments: Iterable[RecordingSegment],
    *,
    policy: SuccessorPlanningPolicy | None = None,
) -> MultiSegmentCoarsePlan:
    """Normalize coverage and assign bounded coarse targets deterministically."""
    selected_policy = policy or SuccessorPlanningPolicy()
    horizon_seconds = int((request.search_end_utc - request.anchor_time_utc).total_seconds())
    if horizon_seconds > selected_policy.maximum_horizon_seconds:
        raise SuccessorPlanError
    normalized = _normalize_segments(request, segments)
    gaps = _coverage_gaps(request, normalized)
    target_times = _target_times(
        request.anchor_time_utc,
        request.search_end_utc,
        selected_policy.coarse_interval_seconds,
    )
    maximum_targets = (
        horizon_seconds + selected_policy.coarse_interval_seconds - 1
    ) // selected_policy.coarse_interval_seconds
    if len(target_times) > maximum_targets:
        raise SuccessorPlanError
    targets = tuple(
        _assign_target(index, value, normalized, gaps, request.search_end_utc)
        for index, value in enumerate(target_times, start=1)
    )
    plan_payload = {
        "policy_version": selected_policy.policy_version,
        "channel_id": request.channel_id,
        "anchor_time_utc": _timestamp(request.anchor_time_utc),
        "search_end_utc": _timestamp(request.search_end_utc),
        "horizon_seconds": horizon_seconds,
        "coarse_interval_seconds": selected_policy.coarse_interval_seconds,
        "segments": [
            {
                "segment_id": item.segment_id,
                "channel_id": item.channel_id,
                "start_utc": _timestamp(item.start_utc),
                "end_utc": _timestamp(item.end_utc),
            }
            for item in normalized
        ],
        "gaps": [
            {"start_utc": _timestamp(item.start_utc), "end_utc": _timestamp(item.end_utc)}
            for item in gaps
        ],
        "targets": [
            {
                "sequence": item.sequence,
                "requested_time_utc": _timestamp(item.requested_time_utc),
                "availability": item.availability.value,
                "segment_id": item.segment_id,
                "gap": (
                    None
                    if item.gap is None
                    else {
                        "start_utc": _timestamp(item.gap.start_utc),
                        "end_utc": _timestamp(item.gap.end_utc),
                    }
                ),
            }
            for item in targets
        ],
    }
    identity = json.dumps(
        plan_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return MultiSegmentCoarsePlan(
        selected_policy.policy_version,
        request.channel_id,
        request.anchor_time_utc,
        request.search_end_utc,
        horizon_seconds,
        selected_policy.coarse_interval_seconds,
        normalized,
        gaps,
        targets,
        f"successor-plan-v1-{hashlib.sha256(identity).hexdigest()}",
    )


def _normalize_segments(
    request: SuccessorPlanRequest, segments: Iterable[RecordingSegment]
) -> tuple[SegmentCoverage, ...]:
    values: dict[tuple[str, datetime, datetime], SegmentCoverage] = {}
    for segment in segments:
        if segment.channel_id != request.channel_id:
            continue
        if not _is_whole_utc(segment.start_utc) or not _is_whole_utc(segment.end_utc):
            raise SuccessorPlanError
        start = max(request.anchor_time_utc, segment.start_utc)
        end = min(request.search_end_utc, segment.end_utc)
        if start >= end:
            continue
        coverage = SegmentCoverage(segment_identity(segment), segment.channel_id, start, end)
        values[(coverage.segment_id, coverage.start_utc, coverage.end_utc)] = coverage
    return tuple(
        sorted(values.values(), key=lambda item: (item.start_utc, item.end_utc, item.segment_id))
    )


def _coverage_gaps(
    request: SuccessorPlanRequest, segments: tuple[SegmentCoverage, ...]
) -> tuple[CoverageGap, ...]:
    cursor = request.anchor_time_utc
    gaps: list[CoverageGap] = []
    for segment in segments:
        if segment.start_utc > cursor:
            gaps.append(CoverageGap(cursor, segment.start_utc))
        cursor = max(cursor, segment.end_utc)
    if cursor < request.search_end_utc:
        gaps.append(CoverageGap(cursor, request.search_end_utc))
    return tuple(gaps)


def _assign_target(
    sequence: int,
    requested_time_utc: datetime,
    segments: tuple[SegmentCoverage, ...],
    gaps: tuple[CoverageGap, ...],
    request_end_utc: datetime,
) -> CoarseTargetAssignment:
    candidates = tuple(
        item for item in segments if item.start_utc <= requested_time_utc < item.end_utc
    )
    if not candidates and requested_time_utc == request_end_utc:
        candidates = tuple(item for item in segments if item.end_utc == requested_time_utc)
    if candidates:
        selected = min(candidates, key=lambda item: (item.start_utc, item.end_utc, item.segment_id))
        return CoarseTargetAssignment(
            sequence, requested_time_utc, TargetAvailability.AVAILABLE, selected.segment_id, None
        )
    gap = next(
        (item for item in gaps if item.start_utc <= requested_time_utc <= item.end_utc),
        None,
    )
    if gap is None:
        raise SuccessorPlanError
    return CoarseTargetAssignment(
        sequence, requested_time_utc, TargetAvailability.UNAVAILABLE, None, gap
    )


def _target_times(start: datetime, end: datetime, interval_seconds: int) -> tuple[datetime, ...]:
    values: list[datetime] = []
    cursor = start + timedelta(seconds=interval_seconds)
    while cursor < end:
        values.append(cursor)
        cursor += timedelta(seconds=interval_seconds)
    values.append(end)
    return tuple(values)


def _is_whole_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0) and value.microsecond == 0


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = (
    "DEFAULT_COARSE_INTERVAL_SECONDS",
    "DEFAULT_HORIZON_SECONDS",
    "MAXIMUM_HORIZON_SECONDS",
    "CoarseTargetAssignment",
    "CoverageGap",
    "MultiSegmentCoarsePlan",
    "RecordingSegmentDiscovery",
    "SegmentCoverage",
    "SuccessorPlanError",
    "SuccessorPlanRequest",
    "SuccessorPlanService",
    "SuccessorPlanningPolicy",
    "TargetAvailability",
    "build_successor_plan",
)
