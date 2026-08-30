"""Pure interpretation of authoritative coarse recording evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_c1_models import CoarseSampleStatus
from vigi_vision.recording_search_c1_planner import SupportDirection
from vigi_vision.recording_search_c2_models import (
    CoarseCandidateBracket,
    CoarseEvidenceSnapshot,
    CoarseInterpretationResult,
    CoarseInterpretationStatus,
    CoarseTargetEvidence,
)
from vigi_vision.recording_search_c2_support import (
    absence_support,
    build_bracket,
    has_later_present,
    validate_execution,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from vigi_vision.recording_search_c1_models import CoarseSampleResult


@dataclass(frozen=True, slots=True)
class _TargetStep:
    last_present: CoarseTargetEvidence | None
    unusable: bool
    bracket: CoarseCandidateBracket | None = None
    safe_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _TargetContext:
    snapshot: CoarseEvidenceSnapshot
    ordered: tuple[CoarseTargetEvidence, ...]
    by_target: Mapping[tuple[datetime, datetime | None], CoarseTargetEvidence]


def _interpret_coarse_evidence(snapshot: CoarseEvidenceSnapshot) -> CoarseInterpretationResult:
    if not snapshot.execution.complete:
        return _execution_incomplete(snapshot)
    if not _plan_matches(snapshot):
        return _safe_result(CoarseInterpretationStatus.CORRUPT, "coarse_plan_mismatch")
    by_target = {
        (target.requested_time_utc, target.origin_coarse_target_utc): target
        for target in snapshot.targets
    }
    try:
        validate_execution(snapshot, by_target)
    except ValueError:
        return _safe_result(CoarseInterpretationStatus.CORRUPT, "authoritative_evidence_invalid")
    return _interpret_targets(snapshot, by_target)


def _execution_incomplete(snapshot: CoarseEvidenceSnapshot) -> CoarseInterpretationResult:
    interrupted = any(
        sample.status is CoarseSampleStatus.INTERRUPTED for sample in snapshot.execution.samples
    )
    if interrupted:
        return _safe_result(
            CoarseInterpretationStatus.INTERRUPTED,
            "coarse_execution_interrupted",
        )
    return _safe_result(CoarseInterpretationStatus.INCOMPLETE, "coarse_execution_incomplete")


def _plan_matches(snapshot: CoarseEvidenceSnapshot) -> bool:
    requested = tuple(sample.requested_time_utc for sample in snapshot.execution.samples)
    return requested == snapshot.plan.target_times


def _interpret_targets(
    snapshot: CoarseEvidenceSnapshot,
    by_target: Mapping[tuple[datetime, datetime | None], CoarseTargetEvidence],
) -> CoarseInterpretationResult:
    ordered = tuple(
        sorted(
            (target for target in snapshot.targets if target.origin_coarse_target_utc is None),
            key=lambda target: target.requested_time_utc,
        )
    )
    context = _TargetContext(snapshot, ordered, by_target)
    last_present = _baseline_evidence(snapshot)
    unusable_count = 0
    unresolved = False
    for sample in snapshot.execution.samples:
        target = by_target[(sample.requested_time_utc, None)]
        step = _process_target(context, sample, target, last_present)
        if step.safe_reason is not None:
            status = (
                CoarseInterpretationStatus.INTERRUPTED
                if step.safe_reason == "coarse_execution_interrupted"
                else CoarseInterpretationStatus.CORRUPT
            )
            if step.safe_reason in {
                "missing_present_lower_bound",
                "nonmonotonic_visual_evidence",
            }:
                status = CoarseInterpretationStatus.INCONCLUSIVE
            return _safe_result(status, step.safe_reason)
        if step.bracket is not None:
            return CoarseInterpretationResult(
                status=CoarseInterpretationStatus.BRACKET_READY,
                bracket=step.bracket,
            )
        last_present = step.last_present
        if step.unusable:
            unusable_count += 1
            unresolved = True
        else:
            unusable_count = 0
        if unusable_count >= snapshot.maximum_consecutive_indeterminate_targets:
            operational_reason = _operational_reason(
                snapshot.execution.samples[
                    max(0, len(snapshot.execution.samples) - unusable_count) :
                ]
            )
            return _safe_result(
                CoarseInterpretationStatus.INCONCLUSIVE,
                operational_reason or "maximum_consecutive_unusable_targets",
            )
    if unresolved:
        return _safe_result(CoarseInterpretationStatus.INCONCLUSIVE, "insufficient_visual_evidence")
    return _safe_result(CoarseInterpretationStatus.NO_CANDIDATE, "no_supported_transition")


def _process_target(
    context: _TargetContext,
    sample: CoarseSampleResult,
    target: CoarseTargetEvidence,
    last_present: CoarseTargetEvidence | None,
) -> _TargetStep:
    match sample.status:
        case CoarseSampleStatus.SUCCESS:
            return _process_success(context, target, last_present)
        case CoarseSampleStatus.RECORDING_UNAVAILABLE:
            return _TargetStep(None, unusable=True)
        case (
            CoarseSampleStatus.ACQUISITION_FAILED
            | CoarseSampleStatus.TIMEOUT
            | CoarseSampleStatus.CLASSIFICATION_FAILED
            | CoarseSampleStatus.UNEXPECTED_ERROR
        ):
            return _TargetStep(None, unusable=True)
        case CoarseSampleStatus.INTERRUPTED:
            return _TargetStep(
                last_present,
                unusable=True,
                safe_reason="coarse_execution_interrupted",
            )


def _process_success(
    context: _TargetContext,
    target: CoarseTargetEvidence,
    last_present: CoarseTargetEvidence | None,
) -> _TargetStep:
    backward_logical_end = (
        context.snapshot.plan.support_direction is SupportDirection.BACKWARD_FROM_END
        and target.requested_time_utc == context.snapshot.plan.search_end_utc
        and target.classification is ClassificationOutcome.ABSENT
    )
    if target.is_alias and not backward_logical_end:
        return _TargetStep(last_present, unusable=False)
    if target.classification is None:
        return _TargetStep(last_present, unusable=True)
    match target.classification:
        case ClassificationOutcome.PRESENT:
            return _TargetStep(target, unusable=False)
        case ClassificationOutcome.ABSENT:
            return _process_absent(context, target, last_present)
        case ClassificationOutcome.INDETERMINATE:
            return _TargetStep(None, unusable=True)


def _process_absent(
    context: _TargetContext,
    target: CoarseTargetEvidence,
    last_present: CoarseTargetEvidence | None,
) -> _TargetStep:
    support = absence_support(context.snapshot, target, context.by_target)
    if support is None:
        return _TargetStep(last_present, unusable=True)
    if last_present is None:
        return _TargetStep(
            last_present,
            unusable=True,
            safe_reason="missing_present_lower_bound",
        )
    if has_later_present(context.ordered, target):
        return _TargetStep(
            last_present,
            unusable=True,
            safe_reason="nonmonotonic_visual_evidence",
        )
    return _TargetStep(
        last_present,
        unusable=False,
        bracket=build_bracket(context.snapshot, last_present, support),
    )


def _baseline_evidence(snapshot: CoarseEvidenceSnapshot) -> CoarseTargetEvidence | None:
    if snapshot.initial_present_evidence is not None:
        return snapshot.initial_present_evidence
    if snapshot.baseline_requested_time_utc is None:
        return None
    return CoarseTargetEvidence(
        requested_time_utc=snapshot.baseline_requested_time_utc,
        status=CoarseSampleStatus.SUCCESS,
        classification=ClassificationOutcome.PRESENT,
        observation_id=snapshot.baseline_observation_id,
        is_baseline=True,
    )


def _safe_result(status: CoarseInterpretationStatus, reason: str) -> CoarseInterpretationResult:
    return CoarseInterpretationResult(status=status, safe_reason=reason)


def _operational_reason(  # noqa: PLR0911 - explicit status precedence
    samples: tuple[CoarseSampleResult, ...],
) -> str | None:
    """Return a closed operational cause when an unusable run is not visual.

    A consecutive run may contain both visual INDETERMINATE samples and an
    acquisition/classification failure.  The latter has precedence: a visual
    terminal must never be inferred from evidence that was not actually
    acquired or classified.
    """
    for sample in samples:
        match sample.status:
            case CoarseSampleStatus.RECORDING_UNAVAILABLE:
                return "recording_unavailable"
            case CoarseSampleStatus.TIMEOUT:
                return (
                    "classifier_timeout"
                    if sample.safe_reason == "classifier_timeout"
                    else "acquisition_timeout"
                )
            case CoarseSampleStatus.ACQUISITION_FAILED:
                return "acquisition_failed"
            case CoarseSampleStatus.CLASSIFICATION_FAILED:
                return "classification_failed"
            case CoarseSampleStatus.INTERRUPTED:
                return "coarse_execution_interrupted"
            case CoarseSampleStatus.UNEXPECTED_ERROR:
                return "unexpected_error"
            case CoarseSampleStatus.SUCCESS:
                continue
    return None


interpret_coarse_evidence = _interpret_coarse_evidence
