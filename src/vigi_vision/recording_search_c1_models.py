"""Typed values returned by the Phase 7C-1 execution foundation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

from vigi_vision.recording_search_c1_planner import (
    CoarseSamplingIdentity,
    confirmation_run_id_for,
)

if TYPE_CHECKING:
    from vigi_vision.object_presence_values import ClassificationOutcome
    from vigi_vision.recording_search_c1_planner import CoarseSamplingPlan


class CoarseSampleStatus(str, Enum):
    """Closed operational status for one executed coarse target."""

    SUCCESS = "SUCCESS"
    RECORDING_UNAVAILABLE = "RECORDING_UNAVAILABLE"
    ACQUISITION_FAILED = "ACQUISITION_FAILED"
    TIMEOUT = "TIMEOUT"
    CLASSIFICATION_FAILED = "CLASSIFICATION_FAILED"
    INTERRUPTED = "INTERRUPTED"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


@dataclass(frozen=True, slots=True)
class CoarseSampleResult:
    """One target result without interpreting visual states as search outcomes."""

    requested_time_utc: datetime
    status: CoarseSampleStatus
    probe_request_id: str | None = None
    classification: ClassificationOutcome | None = None
    safe_reason: str | None = None

    def __post_init__(self) -> None:
        """Require success and failure fields to remain mutually exclusive."""
        if (
            self.requested_time_utc.tzinfo is None
            or self.requested_time_utc.utcoffset() != timedelta(0)
            or self.requested_time_utc.microsecond != 0
        ):
            raise ValueError
        if self.status is CoarseSampleStatus.SUCCESS:
            if self.probe_request_id is None or self.classification is None or self.safe_reason:
                raise ValueError
        elif self.classification is not None or not self.safe_reason:
            raise ValueError
        if self.probe_request_id is not None and not self.probe_request_id:
            raise ValueError


@dataclass(frozen=True, slots=True)
class CoarseSupportResult:
    """Bounded confirmation samples derived from one coarse target."""

    identity: CoarseSamplingIdentity
    origin_target_utc: datetime
    confirmation_run_id: str
    support_indices: tuple[int, ...]
    samples: tuple[CoarseSampleResult, ...]

    def __post_init__(self) -> None:
        """Require one deterministic, complete support sequence."""
        if (
            self.origin_target_utc.tzinfo is None
            or self.origin_target_utc.utcoffset() != timedelta(0)
            or self.origin_target_utc.microsecond != 0
            or not self.confirmation_run_id
            or not self.samples
            or self.support_indices != tuple(range(len(self.samples)))
        ):
            raise ValueError
        request_ids = tuple(sample.probe_request_id for sample in self.samples)
        if any(request_id is None for request_id in request_ids) or len(set(request_ids)) != len(
            request_ids
        ):
            raise ValueError


@dataclass(frozen=True, slots=True)
class CoarseSamplingResult:
    """Ordered execution evidence; it carries no bracket or terminal outcome."""

    identity: CoarseSamplingIdentity
    plan: CoarseSamplingPlan
    samples: tuple[CoarseSampleResult, ...]
    complete: bool
    support_results: tuple[CoarseSupportResult, ...] = ()

    def __post_init__(self) -> None:  # noqa: C901 - validates ordered evidence invariants.
        """Ensure results remain aligned with the immutable target plan."""
        if len(self.samples) > len(self.plan.target_times):
            raise ValueError
        if (
            tuple(sample.requested_time_utc for sample in self.samples)
            != self.plan.target_times[: len(self.samples)]
        ):
            raise ValueError
        if self.complete and len(self.samples) != len(self.plan.target_times):
            raise ValueError
        origins: set[datetime] = set()
        for support in self.support_results:
            if support.origin_target_utc in origins:
                raise ValueError
            origins.add(support.origin_target_utc)
            if support.origin_target_utc not in self.plan.target_times:
                raise ValueError
            if support.identity != self.identity:
                raise ValueError
            if len(support.samples) != self.plan.absence_confirmation_frames:
                raise ValueError
            if support.support_indices != tuple(range(self.plan.absence_confirmation_frames)):
                raise ValueError
            if support.confirmation_run_id != confirmation_run_id_for(
                self.plan, support.origin_target_utc, self.identity
            ):
                raise ValueError
            expected = tuple(
                support.origin_target_utc
                + index * self.plan.absence_cadence_seconds * timedelta(seconds=1)
                for index in range(self.plan.absence_confirmation_frames)
            )
            actual = tuple(sample.requested_time_utc for sample in support.samples)
            if actual != expected or any(value > self.plan.search_end_utc for value in actual):
                raise ValueError
            if any(sample.probe_request_id is None for sample in support.samples):
                raise ValueError
