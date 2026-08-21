"""Pure deterministic midpoint planning for Phase 7D-1."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, TypeAlias

from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
from vigi_vision.recording_search_d1_identity import source_bracket_identity
from vigi_vision.recording_search_d1_models import (
    NarrowingState,
    NarrowingStopReason,
    NarrowingTarget,
)

if TYPE_CHECKING:
    from vigi_vision.recording_search_models import RecordingSearchPolicy


MidpointBounds: TypeAlias = CoarseCandidateBracket | NarrowingState

_MIN_INTERVAL_SECONDS = 2


def midpoint_target(
    bounds: MidpointBounds,
    policy: RecordingSearchPolicy,
    iteration: int,
) -> NarrowingTarget | None:
    """Plan one strict interior whole-second midpoint or stop safely."""
    _validate_bounds(bounds, policy)
    if type(iteration) is not int or iteration < 0:
        raise ValueError
    lower_bound_utc, upper_bound_utc = _bounds(bounds)
    width = _whole_seconds(upper_bound_utc - lower_bound_utc)
    stop_resolution = policy.binary_stop_resolution_seconds
    maximum = maximum_narrowing_iterations(width, stop_resolution)
    if width <= stop_resolution or (
        not isinstance(bounds, NarrowingState) and iteration >= maximum
    ):
        return None
    midpoint = lower_bound_utc + timedelta(seconds=width // 2)
    if not lower_bound_utc < midpoint < upper_bound_utc:
        return None
    source_id = (
        bounds.source_bracket_id
        if isinstance(bounds, NarrowingState)
        else source_bracket_identity(bounds)
    )
    policy_id = _policy_identity(policy)
    target_id = _target_identity(bounds, source_id, policy_id, midpoint, iteration)
    return NarrowingTarget(
        target_id=target_id,
        requested_time_utc=midpoint,
        lower_bound_utc=lower_bound_utc,
        upper_bound_utc=upper_bound_utc,
        iteration=iteration,
        source_bracket_id=source_id,
        policy_version=policy.policy_version,
    )


def support_target_id(target: NarrowingTarget, requested_time_utc: datetime) -> str:
    """Derive a deterministic identity for one midpoint support target."""
    _require_whole_utc(requested_time_utc)
    payload = f"{target.target_id}|{requested_time_utc.isoformat()}"
    return f"{target.target_id}-support-{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def narrowing_stop_reason(
    bounds: MidpointBounds,
    policy: RecordingSearchPolicy,
    iteration: int,
) -> NarrowingStopReason | None:
    """Return the deterministic finite stop reason, if this interval is done."""
    lower_bound_utc, upper_bound_utc = _bounds(bounds)
    width = _whole_seconds(upper_bound_utc - lower_bound_utc)
    if width <= policy.binary_stop_resolution_seconds:
        return NarrowingStopReason.TARGET_PRECISION_REACHED
    if iteration >= maximum_narrowing_iterations(width, policy.binary_stop_resolution_seconds):
        return NarrowingStopReason.MAXIMUM_ITERATIONS
    if width < _MIN_INTERVAL_SECONDS:
        return NarrowingStopReason.NO_DISTINCT_MIDPOINT
    return None


def maximum_narrowing_iterations(width_seconds: int, resolution_seconds: int) -> int:
    """Compute a finite exact upper bound without floating-point arithmetic."""
    if type(width_seconds) is not int or type(resolution_seconds) is not int:
        raise ValueError
    if width_seconds <= 0 or resolution_seconds <= 0:
        raise ValueError
    iterations = 0
    remaining = width_seconds
    while remaining > resolution_seconds:
        remaining = (remaining + 1) // 2
        iterations += 1
    return iterations


def _validate_bounds(bounds: MidpointBounds, policy: RecordingSearchPolicy) -> None:
    lower_bound_utc, upper_bound_utc = _bounds(bounds)
    _require_whole_utc(lower_bound_utc)
    _require_whole_utc(upper_bound_utc)
    _require_whole_utc(policy.search_start_utc)
    _require_whole_utc(policy.search_end_utc)
    if (
        lower_bound_utc >= upper_bound_utc
        or lower_bound_utc < policy.search_start_utc
        or upper_bound_utc > policy.search_end_utc
    ):
        raise ValueError


def _policy_identity(policy: RecordingSearchPolicy) -> str:
    serialized = json.dumps(
        policy.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _target_identity(
    bounds: MidpointBounds,
    source_id: str,
    policy_id: str,
    midpoint: datetime,
    iteration: int,
) -> str:
    lower_bound_utc, upper_bound_utc = _bounds(bounds)
    if isinstance(bounds, NarrowingState):
        phase6_confirmation_id = bounds.phase6_confirmation_id
        baseline_identity = bounds.baseline_identity
    else:
        phase6_confirmation_id = bounds.identity.phase6_confirmation_id
        baseline_identity = bounds.identity.baseline_identity
    payload = "|".join(
        (
            bounds.investigation_id,
            bounds.search_run_id,
            phase6_confirmation_id,
            baseline_identity,
            source_id,
            lower_bound_utc.isoformat(),
            upper_bound_utc.isoformat(),
            str(iteration),
            midpoint.isoformat(),
            policy_id,
        )
    )
    return f"narrowing-target-{hashlib.sha256(payload.encode()).hexdigest()}"


def _whole_seconds(value: timedelta) -> int:
    if value.microseconds != 0:
        raise ValueError
    return value.days * 86_400 + value.seconds


def _bounds(bounds: MidpointBounds) -> tuple[datetime, datetime]:
    if isinstance(bounds, NarrowingState):
        return bounds.lower_bound_utc, bounds.upper_bound_utc
    return bounds.last_present_requested_time_utc, bounds.first_absent_requested_time_utc


def _require_whole_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise ValueError
    if value.astimezone(timezone.utc) != value:
        raise ValueError
