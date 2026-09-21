"""Phase S4 internal disappearance-candidate formation and narrowing.

This module deliberately sits below the public successor result and terminal
contracts.  It consumes the closed S3 :class:`SearchEvidence` bands carried
along with process-local observations and returns bounded, non-persistent
candidate intervals.  It never changes classifier state and never produces a
definitive disappearance result.
"""

# The state machine keeps the safety branches visible and intentionally uses
# small internal dataclasses rather than a public schema.
# ruff: noqa: C901, D102, D105, E501, PLR0912, PLR0915, PLR2004, RUF022
# pyright: reportAny=false, reportUnnecessaryIsInstance=false, reportUnknownArgumentType=false, reportUnknownMemberType=false

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING

from vigi_vision.recording_search_successor_search_evidence import (
    SearchEvidence,
    SearchEvidenceBand,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from vigi_vision.recording_search_successor_classification import SuccessorObservation


class CandidateSearchContractError(ValueError):
    """Raised when an internal S4 candidate contract is malformed."""


class EvidenceNarrowingCancelledError(RuntimeError):
    """Signal lifecycle cancellation from a midpoint sampler."""


@dataclass(frozen=True, slots=True)
class SuccessorSearchSample:
    """One chronological sample and its independent S3 search evidence.

    ``classifier_state`` is deliberately descriptive only.  Directional
    decisions use ``evidence`` exclusively, so an INDETERMINATE state without
    S3 evidence cannot create or move a candidate bound.
    """

    observation_id: str
    frame_utc: datetime | None
    evidence: SearchEvidence | None = None
    classifier_state: str | None = None
    requested_time_utc: datetime | None = None
    available: bool = True
    gap: bool = False

    def __post_init__(self) -> None:
        if not self.observation_id:
            raise CandidateSearchContractError
        if self.frame_utc is not None and not _is_utc(self.frame_utc):
            raise CandidateSearchContractError
        if self.requested_time_utc is not None and not _is_utc(self.requested_time_utc):
            raise CandidateSearchContractError
        if type(self.available) is not bool or type(self.gap) is not bool:
            raise CandidateSearchContractError
        if self.gap and self.available:
            raise CandidateSearchContractError
        if self.evidence is not None and not isinstance(self.evidence, SearchEvidence):
            raise CandidateSearchContractError
        if self.evidence is not None and self.frame_utc is None:
            raise CandidateSearchContractError

    @classmethod
    def from_observation(
        cls,
        observation: SuccessorObservation,
        evidence: SearchEvidence | None = None,
    ) -> SuccessorSearchSample:
        """Adapt a successor observation without re-running S3 evaluation."""
        state = getattr(observation.state, "value", None)
        if evidence is None:
            evidence = getattr(observation, "_search_evidence", None)
        gap = state == "UNAVAILABLE_GAP"
        available = observation.frame_utc is not None and state in {
            "PRESENT",
            "ABSENT",
            "INDETERMINATE",
        }
        return cls(
            observation.observation_id,
            observation.frame_utc,
            evidence,
            None if state is None else str(state),
            observation.requested_time_utc,
            available,
            gap,
        )


# A short alias makes the internal shape easy to use in focused tests without
# exposing any public/persisted schema.
CandidateSearchSample = SuccessorSearchSample


@dataclass(frozen=True, slots=True)
class SuccessorCandidateFormationPolicy:
    """Bounded S4 candidate policy.

    The first material drop opens a provisional candidate.  A later material
    drop while the same run is open supplies persistence and marks it
    qualified.  A strong recovery closes the run but never removes it.
    """

    maximum_candidates: int = 8

    def __post_init__(self) -> None:
        if type(self.maximum_candidates) is not int or not 0 < self.maximum_candidates <= 128:
            raise CandidateSearchContractError


@dataclass(frozen=True, slots=True)
class SuccessorCandidateInterval:
    """Internal conservative interval from the last strong anchor to first drop."""

    candidate_id: str
    anchor_observation_id: str
    drop_observation_id: str
    interval_start_utc: datetime
    interval_end_utc: datetime
    qualified: bool = False
    provisional: bool = True
    coverage_incomplete: bool = False
    recovery_observation_id: str | None = None
    supporting_observation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.candidate_id.startswith("successor-candidate-v1-")
            or not self.anchor_observation_id
            or not self.drop_observation_id
            or not _is_utc(self.interval_start_utc)
            or not _is_utc(self.interval_end_utc)
            or self.interval_end_utc <= self.interval_start_utc
            or type(self.qualified) is not bool
            or type(self.provisional) is not bool
            or self.provisional != (not self.qualified)
            or type(self.coverage_incomplete) is not bool
            or not isinstance(self.supporting_observation_ids, tuple)
            or any(not isinstance(item, str) or not item for item in self.supporting_observation_ids)
        ):
            raise CandidateSearchContractError

    @property
    def width_seconds(self) -> float:
        """Return the conservative interval width in seconds."""
        return (self.interval_end_utc - self.interval_start_utc).total_seconds()

    @property
    def is_provisional(self) -> bool:
        """Compatibility spelling for callers describing tail uncertainty."""
        return self.provisional

    @property
    def start_utc(self) -> datetime:
        """Return the conservative left bound."""
        return self.interval_start_utc

    @property
    def end_utc(self) -> datetime:
        """Return the conservative right bound."""
        return self.interval_end_utc


@dataclass(frozen=True, slots=True)
class SuccessorCandidateFormationResult:
    """Deterministic bounded candidate output and explicit overflow facts."""

    candidates: tuple[SuccessorCandidateInterval, ...]
    overflowed: bool = False
    overflow_count: int = 0
    overflow_candidate_ids: tuple[str, ...] = ()
    ignored_drop_count: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.candidates, tuple)
            or any(not isinstance(item, SuccessorCandidateInterval) for item in self.candidates)
            or type(self.overflowed) is not bool
            or type(self.overflow_count) is not int
            or self.overflow_count < 0
            or type(self.ignored_drop_count) is not int
            or self.ignored_drop_count < 0
            or not isinstance(self.overflow_candidate_ids, tuple)
            or any(not isinstance(item, str) or not item for item in self.overflow_candidate_ids)
            or self.overflowed != (self.overflow_count > 0)
            or self.overflow_count != len(self.overflow_candidate_ids)
        ):
            raise CandidateSearchContractError

    @property
    def qualified_candidates(self) -> tuple[SuccessorCandidateInterval, ...]:
        return tuple(item for item in self.candidates if item.qualified)

    @property
    def candidate_intervals(self) -> tuple[SuccessorCandidateInterval, ...]:
        """Return candidates under the design's interval-oriented spelling."""
        return self.candidates

    @property
    def overflow(self) -> bool:
        """Return whether the bounded internal candidate limit was exceeded."""
        return self.overflowed


def form_disappearance_candidates(
    samples: Iterable[SuccessorSearchSample],
    *,
    seed_reference_time_utc: datetime | None = None,
    seed_reference_observation_id: str = "confirmed_reference",
    policy: SuccessorCandidateFormationPolicy | None = None,
) -> SuccessorCandidateFormationResult:
    """Form conservative, chronologically ordered candidate intervals.

    Only ``STRONG_REFERENCE`` and ``MATERIAL_DROP`` bands move directional
    state.  Ambiguous, insufficient, scene-only, operational, and classifier
    state-only samples preserve uncertainty.  A missing/operational sample
    between anchor and drop marks the resulting candidate as coverage
    incomplete; no fabricated frame time is introduced.
    """
    selected_policy = policy or SuccessorCandidateFormationPolicy()
    if seed_reference_time_utc is not None and not _is_utc(seed_reference_time_utc):
        raise CandidateSearchContractError
    if not seed_reference_observation_id:
        raise CandidateSearchContractError
    ordered = _ordered_samples(samples)
    anchor_time = seed_reference_time_utc
    anchor_id = seed_reference_observation_id if anchor_time is not None else None
    gap_since_anchor = False
    open_candidate: _OpenCandidate | None = None
    formed: list[SuccessorCandidateInterval] = []
    ignored_drop_count = 0

    for sample in ordered:
        if (
            not sample.available
            or sample.gap
            or (
                sample.classifier_state is not None
                and sample.classifier_state not in {"PRESENT", "ABSENT", "INDETERMINATE"}
            )
        ):
            if anchor_time is not None:
                gap_since_anchor = True
            if open_candidate is not None:
                open_candidate.coverage_incomplete = True
            continue
        evidence = sample.evidence
        if evidence is None:
            # Classifier state alone, including INDETERMINATE, is inert.
            continue
        if evidence.band is SearchEvidenceBand.STRONG_REFERENCE:
            if open_candidate is not None:
                formed.append(open_candidate.freeze(sample.observation_id))
                open_candidate = None
            anchor_time = sample.frame_utc
            anchor_id = sample.observation_id
            gap_since_anchor = False
            continue
        if evidence.band is not SearchEvidenceBand.MATERIAL_DROP:
            # Scene-only S3 suppression is intentionally non-directional.  Do
            # not add a fallback branch here that treats it as a drop.
            continue
        if sample.frame_utc is None or anchor_time is None or anchor_id is None:
            ignored_drop_count += 1
            continue
        if sample.frame_utc <= anchor_time:
            ignored_drop_count += 1
            continue
        if open_candidate is None:
            open_candidate = _OpenCandidate(
                anchor_observation_id=anchor_id,
                drop_observation_id=sample.observation_id,
                interval_start_utc=anchor_time,
                interval_end_utc=sample.frame_utc,
                coverage_incomplete=gap_since_anchor,
                supporting_observation_ids=[anchor_id, sample.observation_id],
            )
        else:
            # A repeated material sample belongs to the same transition, not
            # a new candidate.  It supplies persistence/qualification only.
            open_candidate.qualified = True
            open_candidate.supporting_observation_ids.append(sample.observation_id)
            open_candidate.coverage_incomplete = (
                open_candidate.coverage_incomplete or gap_since_anchor
            )
        gap_since_anchor = False

    if open_candidate is not None:
        formed.append(open_candidate.freeze())

    # Runs are already chronological.  Keep the first bounded candidates and
    # explicitly return every discarded identity so no later event vanishes
    # silently when the internal limit is reached.
    merged = merge_candidate_intervals(tuple(formed))
    bounded = tuple(merged[: selected_policy.maximum_candidates])
    overflow_ids = tuple(item.candidate_id for item in merged[selected_policy.maximum_candidates :])
    return SuccessorCandidateFormationResult(
        bounded,
        bool(overflow_ids),
        len(overflow_ids),
        overflow_ids,
        ignored_drop_count,
    )


def merge_candidate_intervals(
    candidates: tuple[SuccessorCandidateInterval, ...],
) -> tuple[SuccessorCandidateInterval, ...]:
    """Merge overlapping internal runs deterministically while keeping disjoint runs."""
    if not candidates:
        return ()
    ordered = sorted(candidates, key=lambda item: (item.interval_start_utc, item.interval_end_utc, item.candidate_id))
    merged: list[SuccessorCandidateInterval] = []
    for current in ordered:
        if not merged or current.interval_start_utc > merged[-1].interval_end_utc:
            merged.append(current)
            continue
        previous = merged[-1]
        start = previous.interval_start_utc
        end = max(previous.interval_end_utc, current.interval_end_utc)
        qualified = previous.qualified or current.qualified
        supporting = tuple(dict.fromkeys((*previous.supporting_observation_ids, *current.supporting_observation_ids)))
        merged[-1] = SuccessorCandidateInterval(
            _candidate_id(previous.anchor_observation_id, previous.drop_observation_id, start, end),
            previous.anchor_observation_id,
            previous.drop_observation_id,
            start,
            end,
            qualified,
            not qualified,
            previous.coverage_incomplete or current.coverage_incomplete,
            previous.recovery_observation_id or current.recovery_observation_id,
            supporting,
        )
    return tuple(merged)


@dataclass(slots=True)
class _OpenCandidate:
    anchor_observation_id: str
    drop_observation_id: str
    interval_start_utc: datetime
    interval_end_utc: datetime
    qualified: bool = False
    coverage_incomplete: bool = False
    supporting_observation_ids: list[str] = field(default_factory=list)

    def freeze(self, recovery_observation_id: str | None = None) -> SuccessorCandidateInterval:
        candidate_id = _candidate_id(
            self.anchor_observation_id,
            self.drop_observation_id,
            self.interval_start_utc,
            self.interval_end_utc,
        )
        return SuccessorCandidateInterval(
            candidate_id,
            self.anchor_observation_id,
            self.drop_observation_id,
            self.interval_start_utc,
            self.interval_end_utc,
            self.qualified,
            not self.qualified,
            self.coverage_incomplete,
            recovery_observation_id,
            tuple(dict.fromkeys(self.supporting_observation_ids)),
        )


class EvidenceNarrowingCompletion(str, Enum):
    """Safe completion reasons for an unpublished S4 interval."""

    TARGET_WIDTH_REACHED = "TARGET_WIDTH_REACHED"
    ITERATION_LIMIT = "ITERATION_LIMIT"
    AMBIGUOUS = "AMBIGUOUS"
    INSUFFICIENT = "INSUFFICIENT"
    GAP = "GAP"
    NONMONOTONIC = "NONMONOTONIC"
    NO_PROGRESS = "NO_PROGRESS"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class EvidenceNarrowingPolicy:
    """Bounded midpoint policy, intentionally independent of terminal schema."""

    target_width_seconds: int = 30
    maximum_iterations: int = 6

    def __post_init__(self) -> None:
        if (
            type(self.target_width_seconds) is not int
            or not 0 < self.target_width_seconds <= 30
            or type(self.maximum_iterations) is not int
            or not 0 < self.maximum_iterations <= 6
        ):
            raise CandidateSearchContractError


@dataclass(frozen=True, slots=True)
class EvidenceNarrowingResult:
    """Internal bounded interval; never adapted to the PRESENT/ABSENT result type."""

    candidate_id: str
    interval_start_utc: datetime
    interval_end_utc: datetime
    interval_width_seconds: float
    iterations: int
    midpoint_samples: tuple[SuccessorSearchSample, ...]
    completion: EvidenceNarrowingCompletion
    reason_code: str
    coverage_incomplete: bool = False

    def __post_init__(self) -> None:
        if (
            not self.candidate_id.startswith("successor-candidate-v1-")
            or not _is_utc(self.interval_start_utc)
            or not _is_utc(self.interval_end_utc)
            or self.interval_end_utc <= self.interval_start_utc
            or not math.isfinite(self.interval_width_seconds)
            or self.interval_width_seconds
            != (self.interval_end_utc - self.interval_start_utc).total_seconds()
            or type(self.iterations) is not int
            or self.iterations < 0
            or not isinstance(self.completion, EvidenceNarrowingCompletion)
            or not self.reason_code
            or type(self.coverage_incomplete) is not bool
            or not isinstance(self.midpoint_samples, tuple)
        ):
            raise CandidateSearchContractError


def narrow_candidate_interval(
    candidate: SuccessorCandidateInterval,
    midpoint_sampler: Callable[[datetime], SuccessorSearchSample | None],
    *,
    policy: EvidenceNarrowingPolicy | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> EvidenceNarrowingResult:
    """Narrow one qualified candidate using only S3 directional evidence.

    Ambiguous/insufficient/gap/nonmonotonic midpoints stop safely at the
    current enclosing interval.  Actual decoded-frame timestamps, not the
    requested midpoint, determine every bound update.
    """
    selected_policy = policy or EvidenceNarrowingPolicy()
    left = candidate.interval_start_utc
    right = candidate.interval_end_utc
    coarse_left = left
    coarse_right = right
    samples: list[SuccessorSearchSample] = []
    seen_times: set[datetime] = {left, right}
    phase = "strong"
    completion = EvidenceNarrowingCompletion.TARGET_WIDTH_REACHED
    reason = "target_width_reached"
    coverage_incomplete = candidate.coverage_incomplete
    iterations = 0
    if coverage_incomplete:
        return EvidenceNarrowingResult(
            candidate.candidate_id,
            left,
            right,
            (right - left).total_seconds(),
            0,
            (),
            EvidenceNarrowingCompletion.GAP,
            "candidate_gap",
            coverage_incomplete=True,
        )
    while (right - left).total_seconds() > selected_policy.target_width_seconds:
        if should_cancel is not None and should_cancel():
            completion = EvidenceNarrowingCompletion.CANCELLED
            reason = "cancelled"
            break
        if iterations >= selected_policy.maximum_iterations:
            completion = EvidenceNarrowingCompletion.ITERATION_LIMIT
            reason = "iteration_limit"
            break
        requested = _midpoint(left, right)
        if requested in seen_times:
            completion = EvidenceNarrowingCompletion.NO_PROGRESS
            reason = "no_progress"
            break
        iterations += 1
        try:
            sample = midpoint_sampler(requested)
        except EvidenceNarrowingCancelledError:
            completion = EvidenceNarrowingCompletion.CANCELLED
            reason = "cancelled"
            break
        if sample is None or sample.gap or not sample.available or sample.frame_utc is None:
            coverage_incomplete = True
            left = coarse_left
            right = coarse_right
            completion = EvidenceNarrowingCompletion.GAP
            reason = "midpoint_gap"
            break
        actual = sample.frame_utc
        seen_times.add(actual)
        samples.append(sample)
        if actual <= left or actual >= right:
            completion = EvidenceNarrowingCompletion.NO_PROGRESS
            reason = "no_progress"
            break
        evidence = sample.evidence
        if evidence is None:
            completion = EvidenceNarrowingCompletion.AMBIGUOUS
            reason = "midpoint_ambiguous"
            break
        if evidence.band is SearchEvidenceBand.STRONG_REFERENCE:
            if phase == "drop":
                left = coarse_left
                right = coarse_right
                completion = EvidenceNarrowingCompletion.NONMONOTONIC
                reason = "nonmonotonic_strong_after_drop"
                break
            phase = "strong"
            left = actual
        elif evidence.band is SearchEvidenceBand.MATERIAL_DROP:
            phase = "drop"
            right = actual
        elif evidence.band is SearchEvidenceBand.INSUFFICIENT:
            completion = EvidenceNarrowingCompletion.INSUFFICIENT
            reason = "midpoint_insufficient"
            break
        else:
            completion = EvidenceNarrowingCompletion.AMBIGUOUS
            reason = "midpoint_ambiguous"
            break
    else:
        completion = EvidenceNarrowingCompletion.TARGET_WIDTH_REACHED
        reason = "target_width_reached"
    if completion is EvidenceNarrowingCompletion.TARGET_WIDTH_REACHED and (
        right - left
    ).total_seconds() > selected_policy.target_width_seconds:
        completion = EvidenceNarrowingCompletion.ITERATION_LIMIT
        reason = "iteration_limit"
    return EvidenceNarrowingResult(
        candidate.candidate_id,
        left,
        right,
        (right - left).total_seconds(),
        iterations,
        tuple(samples),
        completion,
        reason,
        coverage_incomplete,
    )


# Friendly aliases for callers/tests that describe the operation as S4.
form_candidates = form_disappearance_candidates
narrow_candidate = narrow_candidate_interval


def _ordered_samples(samples: Iterable[SuccessorSearchSample]) -> tuple[SuccessorSearchSample, ...]:
    unique: dict[str, SuccessorSearchSample] = {}
    for sample in samples:
        if not isinstance(sample, SuccessorSearchSample):
            raise CandidateSearchContractError
        current = unique.get(sample.observation_id)
        if current is None or _sample_key(sample) < _sample_key(current):
            unique[sample.observation_id] = sample
    return tuple(sorted(unique.values(), key=_sample_key))


def _sample_key(sample: SuccessorSearchSample) -> tuple[datetime, str]:
    timestamp = sample.frame_utc or sample.requested_time_utc
    if timestamp is None:
        timestamp = datetime.max.replace(tzinfo=timezone.utc)
    return timestamp, sample.observation_id


def _candidate_id(
    anchor_id: str, drop_id: str, start: datetime, end: datetime
) -> str:
    payload = {
        "anchor_observation_id": anchor_id,
        "drop_observation_id": drop_id,
        "interval_start_utc": start.isoformat(),
        "interval_end_utc": end.isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "successor-candidate-v1-" + hashlib.sha256(encoded).hexdigest()


def _midpoint(left: datetime, right: datetime) -> datetime:
    return left + timedelta(microseconds=(right - left).total_seconds() * 500_000)


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


__all__ = (
    "CandidateSearchSample",
    "CandidateSearchContractError",
    "EvidenceNarrowingCancelledError",
    "EvidenceNarrowingCompletion",
    "EvidenceNarrowingPolicy",
    "EvidenceNarrowingResult",
    "SuccessorCandidateFormationPolicy",
    "SuccessorCandidateFormationResult",
    "SuccessorCandidateInterval",
    "SuccessorSearchSample",
    "form_candidates",
    "form_disappearance_candidates",
    "merge_candidate_intervals",
    "narrow_candidate",
    "narrow_candidate_interval",
)
