"""Deterministic Phase 7C-1 coarse target planning."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vigi_vision.recording_search_models import RecordingSearchBaseline, RecordingSearchPolicy


class SupportDirection(str, Enum):
    """Closed absence-support direction used by C1/C2 composition."""

    FORWARD = "FORWARD"
    BACKWARD_FROM_END = "BACKWARD_FROM_END"


@dataclass(frozen=True, slots=True)
class CoarseSamplingIdentity:
    """Identity binding shared by one active coarse-sampling execution."""

    investigation_id: str
    search_run_id: str
    phase6_confirmation_id: str
    baseline_identity: str

    def __post_init__(self) -> None:
        """Require every identity component to be a non-empty string."""
        if any(
            type(value) is not str or not value
            for value in (
                self.investigation_id,
                self.search_run_id,
                self.phase6_confirmation_id,
                self.baseline_identity,
            )
        ):
            raise ValueError


@dataclass(frozen=True, slots=True)
class CoarseSamplingPlan:
    """Ordered whole-second target times for one immutable policy snapshot."""

    search_start_utc: datetime
    search_end_utc: datetime
    interval_seconds: int
    target_times: tuple[datetime, ...]
    plan_id: str
    absence_confirmation_frames: int = 3
    absence_cadence_seconds: int = 1
    maximum_consecutive_indeterminate_targets: int = 3
    support_direction: SupportDirection = SupportDirection.FORWARD

    def __post_init__(self) -> None:
        """Reject plans that could drift, duplicate, or leave the requested window."""
        _validate_plan(self)


def build_coarse_sampling_plan(
    policy: RecordingSearchPolicy,
    *,
    support_direction: SupportDirection = SupportDirection.FORWARD,
) -> CoarseSamplingPlan:
    """Build the bounded chronological grid prescribed by the policy snapshot."""
    start = policy.search_start_utc
    end = policy.search_end_utc
    _require_whole_utc(start)
    _require_whole_utc(end)
    if start >= end:
        raise ValueError
    span_seconds = int((end - start).total_seconds())
    if span_seconds > policy.maximum_requested_span_seconds:
        raise ValueError
    interval = policy.coarse_interval_seconds
    if interval <= 0:
        raise ValueError

    targets = _target_times(start, end, interval)
    max_targets = (span_seconds + interval - 1) // interval
    if len(targets) > max_targets:
        raise ValueError
    payload = json.dumps(
        policy.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    plan_id = f"coarse-plan-{hashlib.sha256(payload).hexdigest()}"
    return CoarseSamplingPlan(
        start,
        end,
        interval,
        targets,
        plan_id,
        policy.absence_confirmation_frames,
        policy.absence_cadence_seconds,
        policy.maximum_consecutive_indeterminate_targets,
        support_direction,
    )


def baseline_identity_for(baseline: RecordingSearchBaseline) -> str:
    """Return the canonical identity of the Phase 6 baseline facts."""
    payload = json.dumps(
        {
            "identity_schema": "phase7c-baseline-v1",
            "baseline": baseline.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"baseline-{hashlib.sha256(payload).hexdigest()}"


def confirmation_run_id_for(
    plan: CoarseSamplingPlan,
    target: datetime,
    identity: CoarseSamplingIdentity,
) -> str:
    """Return the canonical identity for one policy-derived support batch."""
    if target not in plan.target_times:
        raise ValueError
    payload_value = {
        "identity_schema": "phase7c-confirmation-v2",
        "investigation_id": identity.investigation_id,
        "search_run_id": identity.search_run_id,
        "phase6_confirmation_id": identity.phase6_confirmation_id,
        "baseline_identity": identity.baseline_identity,
        "plan_id": plan.plan_id,
        "origin_target_utc": target.isoformat(),
        "support_count": plan.absence_confirmation_frames,
        "support_cadence_seconds": plan.absence_cadence_seconds,
    }
    if plan.support_direction is SupportDirection.BACKWARD_FROM_END:
        payload_value["support_direction"] = plan.support_direction.value
    payload = json.dumps(
        payload_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"coarse-confirmation-{hashlib.sha256(payload).hexdigest()}"


def support_target_times(
    plan: CoarseSamplingPlan,
    target: datetime,
) -> tuple[datetime, ...]:
    """Return the closed direction-aware support targets for one plan target."""
    if target not in plan.target_times:
        raise ValueError
    cadence = plan.absence_cadence_seconds
    count = plan.absence_confirmation_frames
    if plan.support_direction is SupportDirection.BACKWARD_FROM_END:
        if target != plan.search_end_utc:
            return ()
        values = tuple(
            target - index * cadence * timedelta(seconds=1) for index in range(count, 0, -1)
        )
        if any(value < plan.search_start_utc or value >= plan.search_end_utc for value in values):
            return ()
        return values
    values = tuple(target + index * cadence * timedelta(seconds=1) for index in range(count))
    if any(value < plan.search_start_utc or value > plan.search_end_utc for value in values):
        return ()
    return values


def _target_times(start: datetime, end: datetime, interval: int) -> tuple[datetime, ...]:
    targets: list[datetime] = []
    cursor = start + timedelta(seconds=interval)
    while cursor < end:
        targets.append(cursor)
        cursor += timedelta(seconds=interval)
    targets.append(end)
    return tuple(targets)


def _require_whole_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise ValueError
    if value.astimezone(timezone.utc) != value:
        raise ValueError


def _validate_plan(plan: CoarseSamplingPlan) -> None:
    if type(plan.interval_seconds) is not int or plan.interval_seconds <= 0:
        raise ValueError
    if type(plan.support_direction) is not SupportDirection:
        raise ValueError
    if (
        type(plan.absence_confirmation_frames) is not int
        or plan.absence_confirmation_frames <= 0
        or type(plan.absence_cadence_seconds) is not int
        or plan.absence_cadence_seconds <= 0
        or type(plan.maximum_consecutive_indeterminate_targets) is not int
        or plan.maximum_consecutive_indeterminate_targets <= 0
    ):
        raise ValueError
    _require_whole_utc(plan.search_start_utc)
    _require_whole_utc(plan.search_end_utc)
    if plan.search_start_utc >= plan.search_end_utc:
        raise ValueError
    span_seconds = int((plan.search_end_utc - plan.search_start_utc).total_seconds())
    if plan.absence_confirmation_frames > span_seconds + 1:
        raise ValueError
    _validate_target_times(plan)
    if type(plan.plan_id) is not str or not plan.plan_id.startswith("coarse-plan-"):
        raise ValueError


def _validate_target_times(plan: CoarseSamplingPlan) -> None:
    if not plan.target_times:
        raise ValueError
    for value in plan.target_times:
        _require_whole_utc(value)
    if any(left >= right for left, right in pairwise(plan.target_times)):
        raise ValueError
    if plan.target_times[0] <= plan.search_start_utc:
        raise ValueError
    if plan.target_times[-1] != plan.search_end_utc:
        raise ValueError
    if any(
        value < plan.search_start_utc or value > plan.search_end_utc for value in plan.target_times
    ):
        raise ValueError
    expected = _target_times(plan.search_start_utc, plan.search_end_utc, plan.interval_seconds)
    if plan.target_times != expected:
        raise ValueError
