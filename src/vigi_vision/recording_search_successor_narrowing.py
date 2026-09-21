"""Phase 7E successor Slice 4: bounded binary narrowing.

The narrowing boundary consumes only the provisional adjacent PRESENT/ABSENT
bracket produced by Slice 3.  Each midpoint is acquired through Slice 2 and
classified through Slice 3; no terminal Schema 5--7 publication is performed.
"""

# The result intentionally carries the complete immutable evidence references.
# pyright: reportPrivateUsage=false, reportUnnecessaryIsInstance=false
# ruff: noqa: D102, D105, PLR0913

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING

from vigi_vision.recording_search_successor import (
    CoarseTargetAssignment,
    MultiSegmentCoarsePlan,
    TargetAvailability,
)
from vigi_vision.recording_search_successor_classification import (
    SuccessorClassificationAuthority,
    SuccessorCoarseClassificationResult,
    SuccessorCoarseClassificationService,
    SuccessorObservation,
    SuccessorObservationState,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vigi_vision.recording_search_successor_acquisition import (
        SuccessorTargetAcquisitionService,
    )


_NARROWING_VERSION = "phase7e-successor-binary-narrowing-v1"
DEFAULT_NARROWING_TARGET_WIDTH_SECONDS = 30
MAXIMUM_NARROWING_ITERATIONS = 6
_SAFE_REASON_CODES = frozenset(
    {
        "target_width_reached",
        "iteration_limit",
        "midpoint_gap",
        "midpoint_acquisition_unavailable",
        "midpoint_indeterminate",
        "midpoint_classification_unavailable",
        "no_progress",
        "cancelled",
        "historical_baseline_boundary",
    }
)
_LOGGER = logging.getLogger("uvicorn.error.vigi_vision.phase7e")


class SuccessorNarrowingContractError(ValueError):
    """Raised when an immutable Slice 3 bracket cannot be narrowed safely."""


class SuccessorNarrowingCompletion(str, Enum):
    """Closed completion vocabulary for one bounded narrowing invocation."""

    NARROWED = "NARROWED"
    ITERATION_LIMIT = "ITERATION_LIMIT"
    INCOMPLETE_COVERAGE = "INCOMPLETE_COVERAGE"
    INDETERMINATE_OBSERVATION = "INDETERMINATE_OBSERVATION"
    ACQUISITION_UNAVAILABLE = "ACQUISITION_UNAVAILABLE"
    CLASSIFICATION_UNAVAILABLE = "CLASSIFICATION_UNAVAILABLE"
    NO_PROGRESS = "NO_PROGRESS"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class SuccessorNarrowingPolicy:
    """MVP binary-search bounds; these values are identity-bearing policy."""

    policy_version: str = _NARROWING_VERSION
    target_width_seconds: int = DEFAULT_NARROWING_TARGET_WIDTH_SECONDS
    maximum_iterations: int = MAXIMUM_NARROWING_ITERATIONS

    def __post_init__(self) -> None:
        if (
            not self.policy_version
            or type(self.target_width_seconds) is not int
            or not 0 < self.target_width_seconds <= DEFAULT_NARROWING_TARGET_WIDTH_SECONDS
            or type(self.maximum_iterations) is not int
            or not 0 < self.maximum_iterations <= MAXIMUM_NARROWING_ITERATIONS
        ):
            raise SuccessorNarrowingContractError

    @property
    def identity(self) -> str:
        return _digest_identity(
            "successor-narrowing-policy-v1-",
            {
                "policy_version": self.policy_version,
                "target_width_seconds": self.target_width_seconds,
                "maximum_iterations": self.maximum_iterations,
            },
        )


@dataclass(frozen=True, slots=True)
class SuccessorBinaryNarrowingResult:
    """Bounded evidence and the final actual-frame bracket."""

    plan_id: str
    source_bracket_id: str
    authority_identity: str
    roi_identity: str
    last_present: SuccessorObservation
    first_absent: SuccessorObservation
    interval_start_utc: datetime
    interval_end_utc: datetime
    interval_width_seconds: float
    iterations: int
    midpoint_observations: tuple[SuccessorObservation, ...]
    coarse_observations: tuple[SuccessorObservation, ...]
    completion: SuccessorNarrowingCompletion
    reason_code: str
    policy_identity: str
    narrowing_id: str

    def __post_init__(self) -> None:
        if (
            not self.plan_id
            or not self.source_bracket_id.startswith("successor-source-bracket-v1-")
            or not self.authority_identity
            or not self.roi_identity
            or self.last_present.state is not SuccessorObservationState.PRESENT
            or self.first_absent.state is not SuccessorObservationState.ABSENT
            or self.last_present.frame_utc is None
            or self.first_absent.frame_utc is None
            or self.interval_start_utc != self.last_present.frame_utc
            or self.interval_end_utc != self.first_absent.frame_utc
            or not _is_utc(self.interval_start_utc)
            or not _is_utc(self.interval_end_utc)
            or self.interval_end_utc <= self.interval_start_utc
            or not math.isfinite(self.interval_width_seconds)
            or self.interval_width_seconds
            != (self.interval_end_utc - self.interval_start_utc).total_seconds()
            or type(self.iterations) is not int
            or self.iterations < 0
            or not isinstance(self.completion, SuccessorNarrowingCompletion)
            or self.reason_code not in _SAFE_REASON_CODES
            or not self.policy_identity.startswith("successor-narrowing-policy-v1-")
            or not self.narrowing_id.startswith("successor-narrowing-v1-")
        ):
            raise SuccessorNarrowingContractError
        if any(item.plan_id != self.plan_id for item in self.midpoint_observations):
            raise SuccessorNarrowingContractError
        if any(
            item.authority_identity != self.authority_identity
            for item in self.midpoint_observations
        ):
            raise SuccessorNarrowingContractError
        if any(item.roi_identity != self.roi_identity for item in self.midpoint_observations):
            raise SuccessorNarrowingContractError

    @property
    def last_present_frame_utc(self) -> datetime:
        """Return the actual frame time of the final PRESENT observation."""
        return _frame_time(self.last_present)

    @property
    def first_absent_frame_utc(self) -> datetime:
        """Return the actual frame time of the first ABSENT observation."""
        return _frame_time(self.first_absent)


@dataclass(slots=True)
class SuccessorBinaryNarrowingService:
    """Serially narrow one validated Slice 3 bracket."""

    acquisition_service: SuccessorTargetAcquisitionService
    classification_service: SuccessorCoarseClassificationService
    policy: SuccessorNarrowingPolicy = field(default_factory=SuccessorNarrowingPolicy)
    should_cancel: Callable[[], bool] | None = field(default=None, repr=False)

    def narrow(  # noqa: C901, PLR0912, PLR0915
        self,
        plan: MultiSegmentCoarsePlan,
        coarse_result: SuccessorCoarseClassificationResult,
        authority: SuccessorClassificationAuthority,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> SuccessorBinaryNarrowingResult:
        """Narrow a PRESENT→ABSENT bracket using actual midpoint frame times."""
        self._validate_input(plan, coarse_result, authority)
        bracket = coarse_result.candidate_bracket
        if bracket is None:
            raise SuccessorNarrowingContractError
        by_id = {item.observation_id: item for item in coarse_result.observations}
        left = by_id[bracket.present_observation_id]
        right = by_id[bracket.absent_observation_id]
        source_bracket_id = _source_bracket_id(plan, left, right, authority)
        midpoint_observations: list[SuccessorObservation] = []
        seen_midpoints: set[datetime] = set()
        iterations = 0
        progress_generation = 0
        completion = SuccessorNarrowingCompletion.NARROWED
        reason_code = "target_width_reached"
        cancellation = should_cancel or self.should_cancel

        # The historical Phase 6 baseline may precede the successor plan's
        # acquisition horizon.  No midpoint before the anchor can be acquired
        # without fabricating source coverage, so retain that honest bracket
        # rather than converting it into a gap failure.
        if left.frame_utc is not None and left.frame_utc < plan.anchor_time_utc:
            completion = SuccessorNarrowingCompletion.NARROWED
            reason_code = "historical_baseline_boundary"

        while (
            completion is SuccessorNarrowingCompletion.NARROWED
            and reason_code == "target_width_reached"
        ) and (
            _width_seconds(left, right) > self.policy.target_width_seconds
            and iterations < self.policy.maximum_iterations
        ):
            if cancellation is not None and cancellation():
                completion = SuccessorNarrowingCompletion.CANCELLED
                reason_code = "cancelled"
                break
            left_frame = _frame_time(left)
            right_frame = _frame_time(right)
            midpoint = _midpoint(left_frame, right_frame)
            if midpoint in seen_midpoints or midpoint <= left_frame or midpoint >= right_frame:
                completion = SuccessorNarrowingCompletion.NO_PROGRESS
                reason_code = "no_progress"
                _safe_progress_log(
                    stage="no_progress",
                    left_frame_utc=left_frame,
                    right_frame_utc=right_frame,
                    requested_target_utc=midpoint,
                    selected_frame_time_utc=None,
                    observation_outcome=None,
                    observation_reason=None,
                    next_left_frame_utc=left_frame,
                    next_right_frame_utc=right_frame,
                    iteration_count=iterations,
                    progress_generation=progress_generation,
                    no_progress_reason=reason_code,
                )
                break
            seen_midpoints.add(midpoint)
            iterations += 1
            _safe_progress_log(
                stage="target_requested",
                left_frame_utc=left_frame,
                right_frame_utc=right_frame,
                requested_target_utc=midpoint,
                selected_frame_time_utc=None,
                observation_outcome=None,
                observation_reason=None,
                next_left_frame_utc=None,
                next_right_frame_utc=None,
                iteration_count=iterations,
                progress_generation=progress_generation,
                no_progress_reason=None,
            )
            segment_id = _segment_for_midpoint(plan, midpoint)
            if segment_id is None:
                completion = SuccessorNarrowingCompletion.INCOMPLETE_COVERAGE
                reason_code = "midpoint_gap"
                _safe_progress_log(
                    stage="terminal",
                    left_frame_utc=left_frame,
                    right_frame_utc=right_frame,
                    requested_target_utc=midpoint,
                    selected_frame_time_utc=None,
                    observation_outcome=None,
                    observation_reason=reason_code,
                    next_left_frame_utc=left_frame,
                    next_right_frame_utc=right_frame,
                    iteration_count=iterations,
                    progress_generation=progress_generation,
                    no_progress_reason=None,
                )
                break
            target = CoarseTargetAssignment(
                len(midpoint_observations) + 1,
                midpoint,
                TargetAvailability.AVAILABLE,
                segment_id,
                None,
            )
            acquisition = self.acquisition_service.acquire_midpoint(plan, target)
            if cancellation is None:
                observation = self.classification_service.classify_target(
                    plan, target, acquisition, authority
                )
            else:
                observation = self.classification_service.classify_target(
                    plan,
                    target,
                    acquisition,
                    authority,
                    cancellation=cancellation,
                )
            midpoint_observations.append(observation)
            progress_generation += 1
            outcome = _completion_for_observation(observation)
            if outcome is not None:
                completion, reason_code = outcome
                _safe_progress_log(
                    stage="terminal",
                    left_frame_utc=left_frame,
                    right_frame_utc=right_frame,
                    requested_target_utc=midpoint,
                    selected_frame_time_utc=observation.frame_utc,
                    observation_outcome=observation.state.value,
                    observation_reason=observation.reason_code,
                    next_left_frame_utc=left_frame,
                    next_right_frame_utc=right_frame,
                    iteration_count=iterations,
                    progress_generation=progress_generation,
                    no_progress_reason=None,
                )
                break
            if (
                observation.frame_utc is None
                or observation.frame_utc <= left_frame
                or observation.frame_utc >= right_frame
            ):
                completion = SuccessorNarrowingCompletion.NO_PROGRESS
                reason_code = "no_progress"
                _safe_progress_log(
                    stage="no_progress",
                    left_frame_utc=left_frame,
                    right_frame_utc=right_frame,
                    requested_target_utc=midpoint,
                    selected_frame_time_utc=observation.frame_utc,
                    observation_outcome=observation.state.value,
                    observation_reason=observation.reason_code,
                    next_left_frame_utc=left_frame,
                    next_right_frame_utc=right_frame,
                    iteration_count=iterations,
                    progress_generation=progress_generation,
                    no_progress_reason=reason_code,
                )
                break
            if observation.state is SuccessorObservationState.PRESENT:
                left = observation
            elif observation.state is SuccessorObservationState.ABSENT:
                right = observation
            else:
                completion = SuccessorNarrowingCompletion.INDETERMINATE_OBSERVATION
                reason_code = "midpoint_indeterminate"
                _safe_progress_log(
                    stage="terminal",
                    left_frame_utc=left_frame,
                    right_frame_utc=right_frame,
                    requested_target_utc=midpoint,
                    selected_frame_time_utc=observation.frame_utc,
                    observation_outcome=observation.state.value,
                    observation_reason=observation.reason_code,
                    next_left_frame_utc=left_frame,
                    next_right_frame_utc=right_frame,
                    iteration_count=iterations,
                    progress_generation=progress_generation,
                    no_progress_reason=None,
                )
                break
            if _width_seconds(left, right) <= self.policy.target_width_seconds:
                completion = SuccessorNarrowingCompletion.NARROWED
                reason_code = "target_width_reached"
                break
        else:
            if reason_code == "target_width_reached":
                completion = SuccessorNarrowingCompletion.NARROWED
                reason_code = "target_width_reached"

        if (
            completion is SuccessorNarrowingCompletion.NARROWED
            and reason_code == "target_width_reached"
            and _width_seconds(left, right) > self.policy.target_width_seconds
        ):
            completion = SuccessorNarrowingCompletion.ITERATION_LIMIT
            reason_code = "iteration_limit"
        if iterations > self.policy.maximum_iterations:
            raise SuccessorNarrowingContractError
        _safe_progress_log(
            stage="completed",
            left_frame_utc=left.frame_utc,
            right_frame_utc=right.frame_utc,
            requested_target_utc=None,
            selected_frame_time_utc=None,
            observation_outcome=completion.value,
            observation_reason=reason_code,
            next_left_frame_utc=left.frame_utc,
            next_right_frame_utc=right.frame_utc,
            iteration_count=iterations,
            progress_generation=progress_generation,
            no_progress_reason=(
                reason_code if completion is SuccessorNarrowingCompletion.NO_PROGRESS else None
            ),
        )
        return _result(
            plan,
            source_bracket_id,
            authority,
            left,
            right,
            iterations,
            tuple(midpoint_observations),
            coarse_result.observations,
            completion,
            reason_code,
            self.policy,
        )

    def _validate_input(
        self,
        plan: MultiSegmentCoarsePlan,
        coarse_result: SuccessorCoarseClassificationResult,
        authority: SuccessorClassificationAuthority,
    ) -> None:
        self.classification_service.validate_authority(plan, authority)
        if (
            coarse_result.plan_id != plan.plan_id
            or coarse_result.authority_identity != authority.authority_identity
        ):
            raise SuccessorNarrowingContractError
        bracket = coarse_result.candidate_bracket
        if bracket is None:
            raise SuccessorNarrowingContractError
        by_id = {item.observation_id: item for item in coarse_result.observations}
        try:
            left = by_id[bracket.present_observation_id]
            right = by_id[bracket.absent_observation_id]
        except KeyError as exc:
            raise SuccessorNarrowingContractError from exc
        if (
            left.state is not SuccessorObservationState.PRESENT
            or right.state is not SuccessorObservationState.ABSENT
            or left.frame_utc is None
            or right.frame_utc is None
            or not _is_utc(left.frame_utc)
            or not _is_utc(right.frame_utc)
            or left.frame_utc >= right.frame_utc
            or right.sequence != left.sequence + 1
            or bracket.present_frame_utc != left.frame_utc
            or bracket.absent_frame_utc != right.frame_utc
            or any(
                item.plan_id != plan.plan_id
                or item.authority_identity != authority.authority_identity
                or item.roi_identity != authority.roi_identity
                for item in (left, right)
            )
            or any(
                gap.start_utc < right.frame_utc and gap.end_utc > left.frame_utc
                for gap in plan.gaps
            )
        ):
            raise SuccessorNarrowingContractError


def _result(
    plan: MultiSegmentCoarsePlan,
    source_bracket_id: str,
    authority: SuccessorClassificationAuthority,
    left: SuccessorObservation,
    right: SuccessorObservation,
    iterations: int,
    midpoint_observations: tuple[SuccessorObservation, ...],
    coarse_observations: tuple[SuccessorObservation, ...],
    completion: SuccessorNarrowingCompletion,
    reason_code: str,
    policy: SuccessorNarrowingPolicy,
) -> SuccessorBinaryNarrowingResult:
    if left.frame_utc is None or right.frame_utc is None:
        raise SuccessorNarrowingContractError
    width = (right.frame_utc - left.frame_utc).total_seconds()
    narrowing_id = _digest_identity(
        "successor-narrowing-v1-",
        {
            "version": _NARROWING_VERSION,
            "plan_id": plan.plan_id,
            "source_bracket_id": source_bracket_id,
            "authority_identity": authority.authority_identity,
            "roi_identity": authority.roi_identity,
            "left_observation_id": left.observation_id,
            "right_observation_id": right.observation_id,
            "midpoint_observation_ids": [item.observation_id for item in midpoint_observations],
            "completion": completion.value,
            "reason_code": reason_code,
            "policy_identity": policy.identity,
        },
    )
    return SuccessorBinaryNarrowingResult(
        plan.plan_id,
        source_bracket_id,
        authority.authority_identity,
        authority.roi_identity,
        left,
        right,
        left.frame_utc,
        right.frame_utc,
        width,
        iterations,
        midpoint_observations,
        coarse_observations,
        completion,
        reason_code,
        policy.identity,
        narrowing_id,
    )


def _completion_for_observation(
    observation: SuccessorObservation,
) -> tuple[SuccessorNarrowingCompletion, str] | None:
    if observation.state is SuccessorObservationState.INDETERMINATE:
        return SuccessorNarrowingCompletion.INDETERMINATE_OBSERVATION, "midpoint_indeterminate"
    if observation.state in {
        SuccessorObservationState.RECORDING_UNAVAILABLE,
        SuccessorObservationState.REPLAY_TIMEOUT,
        SuccessorObservationState.REPLAY_FAILED,
        SuccessorObservationState.DECODE_TIMEOUT,
        SuccessorObservationState.DECODE_UNAVAILABLE,
        SuccessorObservationState.UNAVAILABLE_GAP,
    }:
        return (
            SuccessorNarrowingCompletion.ACQUISITION_UNAVAILABLE,
            "midpoint_acquisition_unavailable",
        )
    if observation.state in {
        SuccessorObservationState.CLASSIFIER_TIMEOUT,
        SuccessorObservationState.CLASSIFIER_FAILED,
    }:
        return (
            SuccessorNarrowingCompletion.CLASSIFICATION_UNAVAILABLE,
            "midpoint_classification_unavailable",
        )
    return None


def _segment_for_midpoint(plan: MultiSegmentCoarsePlan, midpoint: datetime) -> str | None:
    candidates = tuple(
        item
        for item in plan.segments
        if item.start_utc <= midpoint < item.end_utc
        or midpoint == plan.search_end_utc == item.end_utc
    )
    if not candidates:
        return None
    return min(
        candidates, key=lambda item: (item.start_utc, item.end_utc, item.segment_id)
    ).segment_id


def _midpoint(left: datetime | None, right: datetime | None) -> datetime:
    if left is None or right is None:
        raise SuccessorNarrowingContractError
    midpoint = left + timedelta(seconds=int((right - left).total_seconds()) // 2)
    return midpoint.replace(microsecond=0)


def _width_seconds(left: SuccessorObservation, right: SuccessorObservation) -> float:
    return (_frame_time(right) - _frame_time(left)).total_seconds()


def _frame_time(observation: SuccessorObservation) -> datetime:
    if observation.frame_utc is None:
        raise SuccessorNarrowingContractError
    return observation.frame_utc


def _source_bracket_id(
    plan: MultiSegmentCoarsePlan,
    left: SuccessorObservation,
    right: SuccessorObservation,
    authority: SuccessorClassificationAuthority,
) -> str:
    return _digest_identity(
        "successor-source-bracket-v1-",
        {
            "version": _NARROWING_VERSION,
            "plan_id": plan.plan_id,
            "left_observation_id": left.observation_id,
            "right_observation_id": right.observation_id,
            "left_frame_utc": _timestamp(left.frame_utc),
            "right_frame_utc": _timestamp(right.frame_utc),
            "authority_identity": authority.authority_identity,
            "roi_identity": authority.roi_identity,
        },
    )


def _digest_identity(prefix: str, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}{hashlib.sha256(encoded.encode()).hexdigest()}"


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timezone.utc.utcoffset(value)


def _timestamp(value: datetime | None) -> str | None:
    return (
        None if value is None else value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    )


def _safe_progress_log(
    *,
    stage: str,
    left_frame_utc: datetime | None,
    right_frame_utc: datetime | None,
    requested_target_utc: datetime | None,
    selected_frame_time_utc: datetime | None,
    observation_outcome: str | None,
    observation_reason: str | None,
    next_left_frame_utc: datetime | None,
    next_right_frame_utc: datetime | None,
    iteration_count: int,
    progress_generation: int,
    no_progress_reason: str | None,
) -> None:
    fields = {
        "phase": "narrowing",
        "stage": stage,
        "current_interval": {
            "start_utc": _timestamp(left_frame_utc),
            "end_utc": _timestamp(right_frame_utc),
        },
        "previous_interval": {
            "start_utc": _timestamp(left_frame_utc),
            "end_utc": _timestamp(right_frame_utc),
        },
        "requested_target_utc": _timestamp(requested_target_utc),
        "selected_frame_time_utc": _timestamp(selected_frame_time_utc),
        "observation_outcome": observation_outcome,
        "observation_reason": observation_reason,
        "next_interval": {
            "start_utc": _timestamp(next_left_frame_utc),
            "end_utc": _timestamp(next_right_frame_utc),
        },
        "iteration_count": iteration_count,
        "progress_generation": progress_generation,
        "no_progress_reason": no_progress_reason,
    }
    try:
        _LOGGER.info(
            "phase7e.narrowing_progress %s",
            json.dumps(fields, sort_keys=True, separators=(",", ":")),
        )
    except Exception:  # noqa: BLE001 - diagnostics never alter narrowing.
        return


__all__ = (
    "DEFAULT_NARROWING_TARGET_WIDTH_SECONDS",
    "MAXIMUM_NARROWING_ITERATIONS",
    "SuccessorBinaryNarrowingResult",
    "SuccessorBinaryNarrowingService",
    "SuccessorNarrowingCompletion",
    "SuccessorNarrowingContractError",
    "SuccessorNarrowingPolicy",
)
