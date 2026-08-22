from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast

import pytest

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_c2_models import (
    CoarseInterpretationResult,
    CoarseInterpretationStatus,
)
from vigi_vision.recording_search_d1_models import (
    NarrowingResult,
    NarrowingStatus,
)
from vigi_vision.recording_search_d2_identity import evidence_snapshot_digest
from vigi_vision.recording_search_d2_models import (
    D2EvidenceReference,
    D2EvidenceRole,
    D2EvidenceSnapshot,
    D2SourceRevision,
    D2SupportGroup,
    OperationalStopReason,
)
from vigi_vision.recording_search_d2_results import (
    C2NoCandidate,
    C2OperationalStop,
    C2VisualInconclusive,
    D1NonTerminalStop,
    D1OperationalStop,
    D1VisualTerminal,
)
from vigi_vision.recording_search_d2_validator import adapt_c2_result, adapt_d1_result

UTC = timezone.utc
START = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)


def _reference(
    role: D2EvidenceRole,
    index: int,
    *,
    support_group_id: str | None = None,
    support_index: int | None = None,
    classification: ClassificationOutcome = ClassificationOutcome.PRESENT,
) -> D2EvidenceReference:
    requested = START + timedelta(seconds=index)
    return D2EvidenceReference(
        role=role,
        target_id=None if role is D2EvidenceRole.BASELINE else f"target-{index}",
        requested_time_utc=requested,
        acquisition_operation_id=None if role is D2EvidenceRole.BASELINE else f"acq-{index}",
        probe_request_id=None if role is D2EvidenceRole.BASELINE else f"probe-{index}",
        classification_operation_id=None if role is D2EvidenceRole.BASELINE else f"class-{index}",
        observation_id=f"observation-{index}",
        canonical_frame_id=None if role is D2EvidenceRole.BASELINE else f"frame-{index}",
        alias_id=None,
        decode_session_id=None if role is D2EvidenceRole.BASELINE else "decode-1",
        decoded_frame_utc=None if role is D2EvidenceRole.BASELINE else requested,
        decoded_pts=None if role is D2EvidenceRole.BASELINE else index,
        decoded_ordinal=None if role is D2EvidenceRole.BASELINE else index,
        support_group_id=support_group_id,
        support_index=support_index,
        is_phase6_baseline=role is D2EvidenceRole.BASELINE,
        classification=classification,
    )


def _snapshot() -> D2EvidenceSnapshot:
    group_id = "support-1"
    references = (
        _reference(D2EvidenceRole.BASELINE, 0),
        _reference(D2EvidenceRole.COARSE_TARGET, 1),
        _reference(D2EvidenceRole.ABSENCE_SUPPORT, 2, support_group_id=group_id, support_index=0),
        _reference(D2EvidenceRole.ABSENCE_SUPPORT, 3, support_group_id=group_id, support_index=1),
        _reference(D2EvidenceRole.ABSENCE_SUPPORT, 4, support_group_id=group_id, support_index=2),
    )
    return D2EvidenceSnapshot(
        investigation_id="investigation-1",
        search_run_id="search-run-1",
        phase6_confirmation_id="confirmation-1",
        baseline_observation_id="observation-0",
        plan_id="plan-1",
        policy_identity="policy-1",
        source_revision=D2SourceRevision(
            manifest_digest="a" * 64,
            c2_bracket_id="c2-bracket-1",
            d1_source_bracket_id="d1-bracket-1",
        ),
        references=references,
        support_groups=(
            D2SupportGroup(
                support_group_id=group_id,
                origin_target_id="target-2",
                support_count=3,
                cadence_seconds=1,
                decode_session_id="decode-1",
                member_target_ids=("target-2", "target-3", "target-4"),
                member_observation_ids=("observation-2", "observation-3", "observation-4"),
                member_canonical_frame_ids=("frame-2", "frame-3", "frame-4"),
            ),
        ),
    )


def test_evidence_snapshot_digest_is_deterministic() -> None:
    snapshot = _snapshot()

    assert evidence_snapshot_digest(snapshot) == evidence_snapshot_digest(snapshot)
    assert len(evidence_snapshot_digest(snapshot)) == 64


def test_evidence_snapshot_rejects_reordered_support_references() -> None:
    snapshot = _snapshot()

    with pytest.raises(ValueError, match=r"^$"):
        _ = replace(
            snapshot,
            references=(
                snapshot.references[0],
                snapshot.references[1],
                snapshot.references[3],
                snapshot.references[2],
                snapshot.references[4],
            ),
        )


def test_c2_unknown_reason_fails_closed() -> None:
    result = CoarseInterpretationResult(
        status=CoarseInterpretationStatus.INCONCLUSIVE,
        safe_reason="future_reason",
    )

    adapted = adapt_c2_result(result)

    assert isinstance(adapted, C2OperationalStop)
    assert adapted.reason is OperationalStopReason.ADAPTER_UNKNOWN_RESULT


def test_c2_visual_result_requires_strict_evidence_snapshot() -> None:
    result = CoarseInterpretationResult(
        status=CoarseInterpretationStatus.INCONCLUSIVE,
        safe_reason="insufficient_visual_evidence",
    )

    adapted = adapt_c2_result(result)

    assert isinstance(adapted, C2OperationalStop)
    assert adapted.reason is OperationalStopReason.INCOMPLETE_EVIDENCE


def test_c2_no_candidate_requires_and_returns_complete_present_grid() -> None:
    result = CoarseInterpretationResult(
        status=CoarseInterpretationStatus.NO_CANDIDATE,
        safe_reason="no_supported_transition",
    )

    adapted = adapt_c2_result(result, _snapshot())

    assert isinstance(adapted, C2NoCandidate)
    assert adapted.complete_present_grid[0].role is D2EvidenceRole.COARSE_TARGET
    assert len(adapted.evidence_snapshot_digest) == 64


def test_c2_visual_inconclusive_requires_reopened_visual_refs() -> None:
    snapshot = _snapshot()
    snapshot = replace(
        snapshot,
        references=(
            snapshot.references[0],
            replace(
                snapshot.references[1],
                classification=ClassificationOutcome.INDETERMINATE,
            ),
            *snapshot.references[2:],
        ),
    )
    result = CoarseInterpretationResult(
        status=CoarseInterpretationStatus.INCONCLUSIVE,
        safe_reason="insufficient_visual_evidence",
    )

    adapted = adapt_c2_result(result, snapshot)

    assert isinstance(adapted, C2VisualInconclusive)
    assert adapted.reason.value == "insufficient_visual_evidence"


def test_c2_operational_reason_precedence_is_closed() -> None:
    result = CoarseInterpretationResult(
        status=CoarseInterpretationStatus.INCONCLUSIVE,
        safe_reason="recording_unavailable",
    )

    adapted = adapt_c2_result(result, _snapshot())

    assert isinstance(adapted, C2OperationalStop)
    assert adapted.reason is OperationalStopReason.RECORDING_COVERAGE_GAP


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("acquisition_timeout", OperationalStopReason.TIMEOUT),
        ("classifier_timeout", OperationalStopReason.CLASSIFICATION_TIMEOUT),
        ("acquisition_failed", OperationalStopReason.ACQUISITION_FAILED),
        ("decode_failed", OperationalStopReason.DECODE_FAILED),
        ("classification_failed", OperationalStopReason.CLASSIFICATION_FAILED),
        ("unexpected_error", OperationalStopReason.UNEXPECTED_ERROR),
        ("coarse_execution_interrupted", OperationalStopReason.INTERRUPTED),
    ],
)
def test_c2_operational_causes_never_become_visual(
    reason: str, expected: OperationalStopReason
) -> None:
    result = CoarseInterpretationResult(
        status=CoarseInterpretationStatus.INCONCLUSIVE,
        safe_reason=reason,
    )

    adapted = adapt_c2_result(result, _snapshot())

    assert isinstance(adapted, C2OperationalStop)
    assert adapted.reason is expected


def test_c2_unknown_status_fails_closed() -> None:
    result = CoarseInterpretationResult(
        status=cast("CoarseInterpretationStatus", cast("object", "future-status")),
        safe_reason="future_reason",
    )

    adapted = adapt_c2_result(result)

    assert isinstance(adapted, C2OperationalStop)
    assert adapted.reason is OperationalStopReason.ADAPTER_UNKNOWN_RESULT


def test_c2_contradictory_bracket_shape_fails_closed() -> None:
    result = cast(
        "CoarseInterpretationResult",
        cast(
            "object",
            SimpleNamespace(
                status=CoarseInterpretationStatus.BRACKET_READY,
                bracket=object(),
                safe_reason=None,
            ),
        ),
    )

    adapted = adapt_c2_result(result)

    assert isinstance(adapted, C2OperationalStop)
    assert adapted.reason is OperationalStopReason.ADAPTER_UNKNOWN_RESULT


def test_d1_nonterminal_stop_requires_history_but_preserves_reason() -> None:
    result = NarrowingResult(
        status=NarrowingStatus.INDETERMINATE,
        safe_reason="no_distinct_midpoint",
    )

    adapted = adapt_d1_result(result, history=())

    assert isinstance(adapted, D1NonTerminalStop)
    assert adapted.reason.value == "no_distinct_midpoint"


def test_d1_visual_terminal_requires_history_and_strict_refs() -> None:
    snapshot = _snapshot()
    snapshot = replace(
        snapshot,
        references=(
            snapshot.references[0],
            replace(
                snapshot.references[1],
                classification=ClassificationOutcome.INDETERMINATE,
            ),
            *snapshot.references[2:],
        ),
    )
    result = NarrowingResult(
        status=NarrowingStatus.INDETERMINATE,
        safe_reason="visual_indeterminate",
    )

    adapted = adapt_d1_result(result, snapshot, history=())

    assert isinstance(adapted, D1VisualTerminal)
    assert adapted.reason.value == "insufficient_visual_evidence"


def test_d1_timeout_is_operational_and_has_no_digest() -> None:
    result = NarrowingResult(
        status=NarrowingStatus.INDETERMINATE,
        safe_reason="acquisition_timeout",
    )

    adapted = adapt_d1_result(result, _snapshot())

    assert isinstance(adapted, D1OperationalStop)
    assert adapted.reason is OperationalStopReason.TIMEOUT


def test_d1_classifier_timeout_is_distinct_from_acquisition_timeout() -> None:
    result = NarrowingResult(
        status=NarrowingStatus.INDETERMINATE,
        safe_reason="classifier_timeout",
    )

    adapted = adapt_d1_result(result, _snapshot())

    assert isinstance(adapted, D1OperationalStop)
    assert adapted.reason is OperationalStopReason.CLASSIFICATION_TIMEOUT


def test_d1_interruption_maps_to_operational_stop() -> None:
    result = NarrowingResult(
        status=NarrowingStatus.INTERRUPTED,
        safe_reason="interrupted",
    )

    adapted = adapt_d1_result(result)

    assert isinstance(adapted, D1OperationalStop)
    assert adapted.reason is OperationalStopReason.INTERRUPTED


def test_d1_contradictory_bracket_shape_fails_closed() -> None:
    result = cast(
        "NarrowingResult",
        cast(
            "object",
            SimpleNamespace(
                status=NarrowingStatus.READY,
                narrowed_bracket=object(),
                safe_reason=None,
            ),
        ),
    )

    adapted = adapt_d1_result(result)

    assert isinstance(adapted, D1OperationalStop)
    assert adapted.reason is OperationalStopReason.ADAPTER_UNKNOWN_RESULT
