"""Phase S5 candidate-local verification for disappearance candidates.

The verifier is deliberately process-local.  It consumes the actual-frame
observations already produced by the approved S3/S4 path, reuses their
classifier outputs, and returns an internal evidence report.  It never maps an
unresolved candidate to ``ABSENT``/``PRESENT`` and never changes the public
successor terminal contract.
"""

# ruff: noqa: C901, D102, D105, PLR0913, PLR0915
# pyright: reportAny=false, reportArgumentType=false, reportUnnecessaryIsInstance=false, reportUnknownArgumentType=false, reportUnknownMemberType=false

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from time import perf_counter
from typing import TYPE_CHECKING

from vigi_vision.recording_search_successor_candidate_search import (
    SuccessorCandidateInterval,
    SuccessorSearchSample,
)
from vigi_vision.recording_search_successor_classification import (
    SuccessorObservation,
    SuccessorObservationState,
)
from vigi_vision.recording_search_successor_search_evidence import SearchEvidenceBand

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


class CandidateVerificationContractError(ValueError):
    """Raised when an internal S5 input or result is malformed."""


class CandidateVerificationCompletion(str, Enum):
    """Overall S5 lifecycle completion, independent from public terminal state."""

    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class CandidateVerificationStatus(str, Enum):
    """Candidate-local disposition; none of these are public result states."""

    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    UNRESOLVED = "UNRESOLVED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class VerificationSample:
    """Normalized observation/sample facts used by candidate-local verification."""

    observation_id: str
    frame_utc: datetime | None
    requested_time_utc: datetime | None
    state: str | None
    evidence_band: str | None
    comparison: dict[str, object] | None
    classifier_stage: str | None
    classifier_elapsed_ms: int | None
    available: bool
    gap: bool
    plan_id: str | None = None
    target_id: str | None = None
    acquisition_id: str | None = None
    frame_pts_seconds: float | None = None
    authority_identity: str | None = None
    reference_frame_resource_id: str | None = None
    roi_identity: str | None = None
    classifier_policy_identity: str | None = None
    ordinal: int | None = None
    frame_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.observation_id:
            raise CandidateVerificationContractError
        for value in (self.frame_utc, self.requested_time_utc):
            if value is not None and not _is_utc(value):
                raise CandidateVerificationContractError
        if self.classifier_elapsed_ms is not None and (
            type(self.classifier_elapsed_ms) is not int or self.classifier_elapsed_ms < 0
        ):
            raise CandidateVerificationContractError
        if self.frame_pts_seconds is not None and not math.isfinite(self.frame_pts_seconds):
            raise CandidateVerificationContractError
        if self.ordinal is not None and (type(self.ordinal) is not int or self.ordinal <= 0):
            raise CandidateVerificationContractError
        if type(self.available) is not bool or type(self.gap) is not bool:
            raise CandidateVerificationContractError
        if self.gap and self.available:
            raise CandidateVerificationContractError

    @classmethod
    def from_observation(cls, observation: SuccessorObservation) -> VerificationSample:
        state = observation.state.value
        evidence = getattr(observation, "_search_evidence", None)
        band = getattr(getattr(evidence, "band", None), "value", None)
        return cls(
            observation_id=observation.observation_id,
            frame_utc=observation.frame_utc,
            requested_time_utc=observation.requested_time_utc,
            state=state,
            evidence_band=band,
            comparison=observation.comparison,
            classifier_stage=observation.classifier_stage,
            classifier_elapsed_ms=observation.classifier_elapsed_ms,
            available=observation.frame_utc is not None and observation.state.is_visual,
            gap=observation.state is SuccessorObservationState.UNAVAILABLE_GAP,
            plan_id=observation.plan_id,
            target_id=observation.target_id,
            acquisition_id=observation.acquisition_id,
            frame_pts_seconds=observation.frame_pts_seconds,
            authority_identity=observation.authority_identity,
            reference_frame_resource_id=observation.reference_frame_resource_id,
            roi_identity=observation.roi_identity,
            classifier_policy_identity=observation.classifier_policy_identity,
            ordinal=observation.ordinal,
            frame_sha256=observation.frame_sha256,
        )

    @classmethod
    def from_search_sample(cls, sample: SuccessorSearchSample) -> VerificationSample:
        band = getattr(getattr(sample.evidence, "band", None), "value", None)
        return cls(
            observation_id=sample.observation_id,
            frame_utc=sample.frame_utc,
            requested_time_utc=sample.requested_time_utc,
            state=sample.classifier_state,
            evidence_band=band,
            comparison=None,
            classifier_stage=None,
            classifier_elapsed_ms=None,
            available=sample.available,
            gap=sample.gap,
        )


@dataclass(frozen=True, slots=True)
class CandidateVerificationMetrics:
    """Measured process-local work and preserved classifier evidence counts."""

    verification_duration_ms: float
    candidate_count: int
    candidate_evaluations: int
    actual_frame_count: int
    requested_sample_count: int
    reused_v3_invocations: int
    additional_v3_invocations: int
    segmentation_calls: int
    alignment_invocations: int
    alignment_comparisons: int
    replacement_evidence_count: int
    occlusion_evidence_count: int
    recovery_candidate_count: int
    gap_candidate_count: int
    overflowed: bool
    overflow_count: int

    def __post_init__(self) -> None:
        numeric = (
            self.candidate_count,
            self.candidate_evaluations,
            self.actual_frame_count,
            self.requested_sample_count,
            self.reused_v3_invocations,
            self.additional_v3_invocations,
            self.segmentation_calls,
            self.alignment_invocations,
            self.alignment_comparisons,
            self.replacement_evidence_count,
            self.occlusion_evidence_count,
            self.recovery_candidate_count,
            self.gap_candidate_count,
            self.overflow_count,
        )
        if any(type(item) is not int or item < 0 for item in numeric):
            raise CandidateVerificationContractError
        if not math.isfinite(self.verification_duration_ms) or self.verification_duration_ms < 0:
            raise CandidateVerificationContractError
        if type(self.overflowed) is not bool:
            raise CandidateVerificationContractError


@dataclass(frozen=True, slots=True)
class CandidateVerificationResult:
    """Internal result for one candidate; never serialized as a terminal state."""

    candidate: SuccessorCandidateInterval
    status: CandidateVerificationStatus
    reason_code: str
    sample_ids: tuple[str, ...]
    actual_frame_times_utc: tuple[datetime, ...]
    observed_states: tuple[str, ...]
    evidence_bands: tuple[str, ...]
    reused_v3_invocations: int
    alignment_invocations: int
    alignment_comparisons: int
    replacement_evidence: bool
    occlusion_evidence: bool

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, SuccessorCandidateInterval):
            raise CandidateVerificationContractError
        if not isinstance(self.status, CandidateVerificationStatus) or not self.reason_code:
            raise CandidateVerificationContractError
        if any(not isinstance(item, str) or not item for item in self.sample_ids):
            raise CandidateVerificationContractError
        if any(not _is_utc(item) for item in self.actual_frame_times_utc):
            raise CandidateVerificationContractError
        if self.reused_v3_invocations < 0 or self.alignment_invocations < 0:
            raise CandidateVerificationContractError
        if self.alignment_comparisons < 0:
            raise CandidateVerificationContractError


@dataclass(frozen=True, slots=True)
class CandidateVerificationReport:
    """Bounded internal S5 report for all retained candidates."""

    completion: CandidateVerificationCompletion
    results: tuple[CandidateVerificationResult, ...]
    metrics: CandidateVerificationMetrics

    def __post_init__(self) -> None:
        if not isinstance(self.completion, CandidateVerificationCompletion):
            raise CandidateVerificationContractError
        if not isinstance(self.results, tuple) or not isinstance(
            self.metrics, CandidateVerificationMetrics
        ):
            raise CandidateVerificationContractError

    @property
    def verified_count(self) -> int:
        return sum(item.status is CandidateVerificationStatus.VERIFIED for item in self.results)

    @property
    def unresolved_count(self) -> int:
        return sum(item.status is CandidateVerificationStatus.UNRESOLVED for item in self.results)


def verify_disappearance_candidates(
    candidates: Iterable[SuccessorCandidateInterval],
    samples: Iterable[SuccessorObservation | SuccessorSearchSample],
    *,
    overflowed: bool = False,
    overflow_count: int = 0,
    nonmonotonic_candidate_ids: Iterable[str] = (),
    coverage_incomplete_candidate_ids: Iterable[str] = (),
    should_cancel: Callable[[], bool] | None = None,
) -> CandidateVerificationReport:
    """Verify each retained S4 candidate using existing precise observations.

    No classifier/acquisition is invoked here.  S4 midpoint observations and
    coarse observations are merged by observation id or a complete, matching
    frame/provenance identity, preserving actual frame timestamps. Samples
    with incomplete identity remain separate. This makes redundant v3 work
    measurable (and currently zero) while retaining all existing classifier
    semantics.
    """
    started = perf_counter()
    candidate_items = tuple(candidates)
    if type(overflowed) is not bool or type(overflow_count) is not int or overflow_count < 0:
        raise CandidateVerificationContractError
    normalized: dict[str, VerificationSample] = {}
    nonmonotonic_ids = set(nonmonotonic_candidate_ids)
    coverage_incomplete_ids = set(coverage_incomplete_candidate_ids)
    for item in samples:
        if isinstance(item, SuccessorObservation):
            sample = VerificationSample.from_observation(item)
        elif isinstance(item, SuccessorSearchSample):
            sample = VerificationSample.from_search_sample(item)
        else:
            raise CandidateVerificationContractError
        previous = normalized.get(sample.observation_id)
        if previous is None or _sample_preferred(sample, previous):
            normalized[sample.observation_id] = sample
    actual_frame_ids: set[tuple[object, ...]] = set()
    requested_count = 0
    results: list[CandidateVerificationResult] = []
    reused_v3 = 0
    alignment_invocations = 0
    alignment_comparisons = 0
    replacement_count = 0
    occlusion_count = 0
    recovery_count = 0
    gap_count = 0
    completion = CandidateVerificationCompletion.COMPLETED
    claimed_ids = {
        observation_id
        for candidate in candidate_items
        for observation_id in (
            candidate.anchor_observation_id,
            candidate.drop_observation_id,
            candidate.recovery_observation_id,
            *candidate.supporting_observation_ids,
        )
        if observation_id is not None
    }
    for candidate in candidate_items:
        if should_cancel is not None and should_cancel():
            completion = CandidateVerificationCompletion.CANCELLED
            break
        selected = _candidate_samples(candidate, normalized.values(), claimed_ids)
        actual_frame_ids.update(
            _sample_metric_identity(item) for item in selected if item.frame_utc is not None
        )
        requested_count += sum(item.requested_time_utc is not None for item in selected)
        result = _verify_one(
            candidate,
            selected,
            nonmonotonic=candidate.candidate_id in nonmonotonic_ids,
            coverage_incomplete=candidate.candidate_id in coverage_incomplete_ids,
        )
        results.append(result)
        reused_v3 += result.reused_v3_invocations
        alignment_invocations += result.alignment_invocations
        alignment_comparisons += result.alignment_comparisons
        replacement_count += result.replacement_evidence
        occlusion_count += result.occlusion_evidence
        recovery_count += candidate.recovery_observation_id is not None
        gap_count += candidate.coverage_incomplete or any(item.gap for item in selected)
        if should_cancel is not None and should_cancel():
            completion = CandidateVerificationCompletion.CANCELLED
            break
    if completion is CandidateVerificationCompletion.CANCELLED:
        # A cancelled candidate is intentionally not emitted as a partial
        # candidate result; the caller routes the existing INTERRUPTED path.
        results = []
    duration_ms = max(0.0, (perf_counter() - started) * 1000.0)
    metrics = CandidateVerificationMetrics(
        duration_ms,
        len(candidate_items),
        len(results),
        len(actual_frame_ids),
        requested_count,
        reused_v3,
        0,
        _segmentation_calls(tuple(normalized.values())),
        alignment_invocations,
        alignment_comparisons,
        replacement_count,
        occlusion_count,
        recovery_count,
        gap_count,
        overflowed,
        overflow_count,
    )
    return CandidateVerificationReport(completion, tuple(results), metrics)


def _verify_one(
    candidate: SuccessorCandidateInterval,
    selected: tuple[VerificationSample, ...],
    *,
    nonmonotonic: bool = False,
    coverage_incomplete: bool = False,
) -> CandidateVerificationResult:
    by_id = {item.observation_id: item for item in selected}
    anchor = by_id.get(candidate.anchor_observation_id)
    drop = by_id.get(candidate.drop_observation_id)
    recovery = (
        by_id.get(candidate.recovery_observation_id)
        if candidate.recovery_observation_id is not None
        else None
    )
    states = tuple(item.state for item in selected if item.state is not None)
    bands = tuple(item.evidence_band for item in selected if item.evidence_band is not None)
    operational = any(
        item.state is not None
        and item.state
        not in {
            SuccessorObservationState.PRESENT.value,
            SuccessorObservationState.ABSENT.value,
            SuccessorObservationState.INDETERMINATE.value,
        }
        for item in selected
    )
    chronology_valid = _valid_actual_frame_binding(candidate, anchor, drop)
    gap = candidate.coverage_incomplete or coverage_incomplete or any(item.gap for item in selected)
    useful = any(
        item.evidence_band
        in {
            SearchEvidenceBand.STRONG_REFERENCE.value,
            SearchEvidenceBand.MATERIAL_DROP.value,
        }
        for item in selected
    )
    clear_direction = (
        chronology_valid
        and anchor is not None
        and drop is not None
        and anchor.state == SuccessorObservationState.PRESENT.value
        and drop.state == SuccessorObservationState.ABSENT.value
    )
    if gap:
        status = CandidateVerificationStatus.UNRESOLVED
        reason = "coverage_gap"
    elif operational:
        status = CandidateVerificationStatus.UNRESOLVED
        reason = "operational_failure"
    elif not chronology_valid:
        status = CandidateVerificationStatus.UNRESOLVED
        reason = "invalid_actual_frame_chronology"
    elif nonmonotonic:
        status = (
            CandidateVerificationStatus.PARTIAL
            if useful or states
            else CandidateVerificationStatus.UNRESOLVED
        )
        reason = "nonmonotonic_evidence"
    elif candidate.provisional:
        status = (
            CandidateVerificationStatus.PARTIAL
            if useful or states
            else CandidateVerificationStatus.UNRESOLVED
        )
        reason = "provisional_candidate_requires_later_evidence"
    elif clear_direction and recovery is None:
        status = CandidateVerificationStatus.VERIFIED
        reason = "qualified_present_to_absent"
    elif clear_direction:
        # A strong recovery does not identify whether the disappearance was a
        # true event, an occlusion, or a replacement; preserve uncertainty.
        status = CandidateVerificationStatus.PARTIAL
        reason = "recovery_ambiguous"
    elif useful or states:
        status = CandidateVerificationStatus.PARTIAL
        reason = "candidate_not_decisive"
    else:
        status = CandidateVerificationStatus.UNRESOLVED
        reason = "no_candidate_evidence"
    reused = sum(item.classifier_stage in {"completed", "timeout", "failed"} for item in selected)
    alignment_calls = 0
    alignment_comparison_count = 0
    replacement = False
    occlusion = False
    for item in selected:
        comparison = item.comparison or {}
        if "baseline_support_alignment_candidates_generated" in comparison:
            alignment_calls += 1
        value = comparison.get("baseline_support_alignment_candidates_evaluated")
        if isinstance(value, int) and value >= 0:
            alignment_comparison_count += value
        replacement = replacement or bool(comparison.get("baseline_support_replacement_evidence"))
        occlusion = occlusion or bool(comparison.get("baseline_support_occlusion_evidence"))
    return CandidateVerificationResult(
        candidate,
        status,
        reason,
        tuple(item.observation_id for item in selected),
        tuple(item.frame_utc for item in selected if item.frame_utc is not None),
        states,
        bands,
        reused,
        alignment_calls,
        alignment_comparison_count,
        replacement,
        occlusion,
    )


def _candidate_samples(
    candidate: SuccessorCandidateInterval,
    samples: Iterable[VerificationSample],
    claimed_ids: set[str],
) -> tuple[VerificationSample, ...]:
    ids = {
        candidate.anchor_observation_id,
        candidate.drop_observation_id,
        *candidate.supporting_observation_ids,
    }
    if candidate.recovery_observation_id is not None:
        ids.add(candidate.recovery_observation_id)
    selected = [
        item
        for item in samples
        if item.observation_id in ids
        or (
            item.frame_utc is not None
            and candidate.interval_start_utc <= item.frame_utc <= candidate.interval_end_utc
            and item.observation_id not in claimed_ids - ids
        )
        or (
            item.gap
            and item.requested_time_utc is not None
            and candidate.interval_start_utc
            <= item.requested_time_utc
            <= candidate.interval_end_utc
            and item.observation_id not in claimed_ids - ids
        )
    ]
    ordered = sorted(selected, key=_sample_key)
    unique_frames: list[VerificationSample] = []
    for item in ordered:
        equivalent_index = next(
            (
                index
                for index, previous in enumerate(unique_frames)
                if _samples_semantically_equivalent(item, previous)
            ),
            None,
        )
        if equivalent_index is None:
            unique_frames.append(item)
            continue
        previous = unique_frames[equivalent_index]
        if (
            (item.observation_id in ids and previous.observation_id not in ids)
            or _sample_preferred(item, previous)
        ):
            unique_frames[equivalent_index] = item
    return tuple(sorted(unique_frames, key=_sample_key))


def _sample_preferred(candidate: VerificationSample, previous: VerificationSample) -> bool:
    """Prefer full observations over S4's evidence-only midpoint projection."""
    candidate_identity = _sample_identity_key(candidate)
    previous_identity = _sample_identity_key(previous)
    if candidate_identity is not None and previous_identity is None:
        return True
    if candidate_identity is None and previous_identity is not None:
        return False
    candidate_operational = _is_operational_state(candidate.state)
    previous_operational = _is_operational_state(previous.state)
    if candidate_operational != previous_operational:
        return candidate_operational
    if candidate.comparison is not None and previous.comparison is None:
        return True
    return candidate.state is not None and previous.state is None


def _samples_semantically_equivalent(
    candidate: VerificationSample, previous: VerificationSample
) -> bool:
    """Return whether two samples have enough identity to safely collapse them."""
    if candidate.observation_id == previous.observation_id:
        return True
    candidate_identity = _sample_identity_key(candidate)
    previous_identity = _sample_identity_key(previous)
    return candidate_identity is not None and candidate_identity == previous_identity


def _sample_identity_key(sample: VerificationSample) -> tuple[object, ...] | None:
    """Build a strict identity key; incomplete provenance intentionally opts out."""
    required = (
        sample.frame_utc,
        sample.plan_id,
        sample.target_id,
        sample.acquisition_id,
        sample.authority_identity,
        sample.reference_frame_resource_id,
        sample.roi_identity,
        sample.classifier_policy_identity,
        sample.ordinal,
        sample.frame_sha256,
        sample.state,
        sample.classifier_stage,
    )
    if any(value is None for value in required):
        return None
    return (
        *required,
        sample.frame_pts_seconds,
        sample.requested_time_utc,
        sample.evidence_band,
        sample.available,
        sample.gap,
    )


def _sample_metric_identity(sample: VerificationSample) -> tuple[object, ...]:
    """Count proven duplicate frames once while retaining uncertain samples."""
    identity = _sample_identity_key(sample)
    if identity is not None:
        return ("frame", *identity)
    return ("observation", sample.observation_id)


def _valid_actual_frame_binding(
    candidate: SuccessorCandidateInterval,
    anchor: VerificationSample | None,
    drop: VerificationSample | None,
) -> bool:
    """Require actual endpoint chronology and membership in the S4 interval."""
    if anchor is None or drop is None:
        return False
    if anchor.frame_utc is None or drop.frame_utc is None:
        return False
    if not (
        candidate.interval_start_utc <= anchor.frame_utc <= candidate.interval_end_utc
        and candidate.interval_start_utc <= drop.frame_utc <= candidate.interval_end_utc
    ):
        return False
    return anchor.frame_utc < drop.frame_utc


def _is_operational_state(state: str | None) -> bool:
    return state is not None and state not in {
        SuccessorObservationState.PRESENT.value,
        SuccessorObservationState.ABSENT.value,
        SuccessorObservationState.INDETERMINATE.value,
    }


def _sample_key(sample: VerificationSample) -> tuple[datetime, str]:
    timestamp = sample.frame_utc or sample.requested_time_utc
    if timestamp is None:
        timestamp = datetime.max.replace(tzinfo=timezone.utc)
    return timestamp, sample.observation_id


def _segmentation_calls(samples: tuple[VerificationSample, ...]) -> int:
    """Count observed segmentation calls when preserved diagnostics carry it.

    Runtime observations do not persist predictor diagnostics; those samples
    therefore contribute zero rather than an invented estimate.  The preserved
    replay tool supplies the exact count separately.
    """
    return sum(
        int(item.comparison.get("segmentation_calls", 0))
        for item in samples
        if item.comparison is not None
        and isinstance(item.comparison.get("segmentation_calls"), int)
        and int(item.comparison.get("segmentation_calls", 0)) >= 0
    )


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timezone.utc.utcoffset(value)


__all__ = (
    "CandidateVerificationCompletion",
    "CandidateVerificationContractError",
    "CandidateVerificationMetrics",
    "CandidateVerificationReport",
    "CandidateVerificationResult",
    "CandidateVerificationStatus",
    "VerificationSample",
    "verify_disappearance_candidates",
)
