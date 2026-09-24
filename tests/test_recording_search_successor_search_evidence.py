"""Focused Phase S3 shadow search-evidence contract tests."""

# Fixture construction keeps every cheap metric explicit.
# ruff: noqa: PLR0913

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from vigi_vision.object_presence_evidence import RawComparison
from vigi_vision.object_presence_policy import (
    ObjectPresenceDecisionPolicy,
    _registration_stability_veto,  # pyright: ignore[reportPrivateUsage]
)
from vigi_vision.object_presence_values import VisualReason, VisualStatus
from vigi_vision.recording_search_successor_candidate_search import (
    SuccessorSearchSample,
    form_disappearance_candidates,
)
from vigi_vision.recording_search_successor_search_evidence import (
    SearchEvidence,
    SearchEvidenceBand,
    evaluate_search_evidence,
)


def _policy(
    *, minimum_clipped_mask_pixels: int = 1, minimum_roi_pixels: int = 1
) -> ObjectPresenceDecisionPolicy:
    return ObjectPresenceDecisionPolicy(
        classifier_policy_version="test-s3",
        classifier_preprocessing_version="test-s3",
        baseline_support_mode=True,
        baseline_support_alignment_mode=True,
        minimum_mask_overlap_for_comparison=0.1,
        minimum_roi_pixels=minimum_roi_pixels,
        minimum_clipped_mask_pixels=minimum_clipped_mask_pixels,
    )


def _comparison(
    *,
    similarity: float = 0.90,
    ncc: float = 0.90,
    edge: float = 0.90,
    change: float = 0.05,
    foreground: float = 0.90,
    background_change: float = 0.02,
    roi_ncc: float | None = 0.90,
    scene_stable: bool = True,
    scene_veto: str | None = None,
    alignment_state: str = "aligned",
    alignment_dx: int | None = 0,
    alignment_dy: int | None = 0,
    alignment_rotation: int | None = 0,
    alignment_overlap: float | None = 1.0,
    alignment_score: float | None = 0.90,
    alignment_margin: float | None = 0.10,
    present_gate: bool = True,
    replacement_evidence: bool = False,
    occlusion_evidence: bool = False,
    decision_path: str = "present",
    decision_reason: str = "present_identity_retained",
) -> RawComparison:
    return RawComparison(
        baseline_mask_pixel_count=100,
        probe_mask_pixel_count=100,
        roi_pixel_count=400,
        mask_intersection_pixel_count=80,
        mask_union_pixel_count=120,
        baseline_mask_coverage=0.25,
        probe_mask_coverage=0.25,
        mask_iou=0.666667,
        effective_comparison_area=80,
        roi_luma_ncc=roi_ncc,
        visual_status=VisualStatus.COMPARABLE,
        unusable_reason=None,
        comparison_mode="baseline_support_v3",
        baseline_support_pixel_count=100,
        baseline_support_luma_similarity=similarity,
        baseline_support_luma_ncc=ncc,
        baseline_support_edge_similarity=edge,
        baseline_support_change_ratio=change,
        baseline_support_foreground_retention=foreground,
        baseline_support_background_change_ratio=background_change,
        baseline_support_alignment_dx=alignment_dx,
        baseline_support_alignment_dy=alignment_dy,
        baseline_support_alignment_rotation_degrees=alignment_rotation,
        baseline_support_alignment_overlap=alignment_overlap,
        baseline_support_alignment_score=alignment_score,
        baseline_support_alignment_margin=alignment_margin,
        baseline_support_alignment_state=alignment_state,
        baseline_support_stability_pixel_count=400,
        baseline_support_stability_changed_pixel_count=10,
        baseline_support_stability_valid_pixel_count=300,
        baseline_support_stability_excluded_pixel_count=100,
        baseline_support_scene_stable=scene_stable,
        baseline_support_scene_stability_veto_reason=scene_veto,
        baseline_support_present_gate_passed=present_gate,
        baseline_support_absent_gate_passed=False,
        baseline_support_empty_background_evidence=False,
        baseline_support_replacement_evidence=replacement_evidence,
        baseline_support_occlusion_evidence=occlusion_evidence,
        baseline_support_decision_path=decision_path,
        baseline_support_decision_reason=decision_reason,
    )


def _comparison_with_support(
    support_pixel_count: int,
    *,
    roi_pixel_count: int = 400,
    similarity: float = 0.90,
    ncc: float = 0.90,
    edge: float = 0.90,
    change: float = 0.05,
    foreground: float = 0.90,
    present_gate: bool = True,
    decision_path: str = "present",
    decision_reason: str = "present_identity_retained",
) -> RawComparison:
    """Build a strictly validated baseline-support row at a chosen size."""
    payload = _comparison(
        similarity=similarity,
        ncc=ncc,
        edge=edge,
        change=change,
        foreground=foreground,
        present_gate=present_gate,
        decision_path=decision_path,
        decision_reason=decision_reason,
    ).model_dump()
    coverage = support_pixel_count / roi_pixel_count
    payload.update(
        {
            "baseline_mask_pixel_count": support_pixel_count,
            "probe_mask_pixel_count": support_pixel_count,
            "mask_intersection_pixel_count": support_pixel_count,
            "mask_union_pixel_count": support_pixel_count,
            "baseline_mask_coverage": coverage,
            "probe_mask_coverage": coverage,
            "mask_iou": 1.0,
            "effective_comparison_area": support_pixel_count,
            "roi_pixel_count": roi_pixel_count,
            "baseline_support_pixel_count": support_pixel_count,
            "baseline_support_stability_pixel_count": roi_pixel_count,
            "baseline_support_stability_changed_pixel_count": 0,
            "baseline_support_stability_valid_pixel_count": roi_pixel_count,
            "baseline_support_stability_excluded_pixel_count": 0,
        }
    )
    return RawComparison.model_validate(payload)


def _rack_absent_comparison() -> RawComparison:
    return _comparison(
        similarity=0.771023,
        ncc=0.006280,
        edge=0.935570,
        change=0.783061,
        foreground=0.556322,
        background_change=0.268713,
        roi_ncc=0.685426,
        scene_stable=True,
        alignment_state="ambiguous",
        alignment_dx=-16,
        alignment_dy=14,
        alignment_rotation=10,
        alignment_overlap=0.969944,
        alignment_score=0.587073,
        alignment_margin=0.001438,
        present_gate=False,
        occlusion_evidence=True,
        decision_path="indeterminate",
        decision_reason="unstable_scene",
    )


def _registration_veto_ordinary_drop_comparison() -> RawComparison:
    return _comparison(
        similarity=0.40,
        ncc=0.05,
        edge=0.30,
        change=0.80,
        foreground=0.10,
        background_change=0.05,
        roi_ncc=0.18,
        scene_stable=True,
        alignment_state="ambiguous",
        alignment_dx=8,
        alignment_dy=6,
        alignment_rotation=5,
        alignment_overlap=0.98,
        alignment_score=0.62,
        alignment_margin=0.001,
        present_gate=False,
        decision_path="indeterminate",
        decision_reason="unstable_scene",
    )


def test_strong_reference_uses_reference_relative_cheap_evidence() -> None:
    result = evaluate_search_evidence(_comparison(), _policy(), fast_present_hit=True)

    assert result is not None
    assert result.band is SearchEvidenceBand.STRONG_REFERENCE
    assert result.reference_basis == "confirmed_reference"
    assert result.fast_present_hit is True


def test_material_drop_requires_object_reference_loss_and_stable_scene() -> None:
    comparison = _comparison(
        similarity=0.40,
        ncc=0.10,
        edge=0.30,
        change=0.80,
        foreground=0.10,
        present_gate=False,
        decision_path="indeterminate",
        decision_reason="insufficient_visual_evidence",
    )

    result = evaluate_search_evidence(comparison, _policy())

    assert result is not None
    assert result.band is SearchEvidenceBand.MATERIAL_DROP
    assert result.object_degradation is True
    assert result.scene_discontinuity is False
    assert result.registration_stability_veto is False
    assert result.registration_veto_overridden is False


def test_spatially_localized_support_drop_recovers_rack_absence() -> None:
    """A stable context may outvote aggregate background noise, not scene motion."""
    comparison = _rack_absent_comparison()

    result = evaluate_search_evidence(
        comparison,
        _policy().model_copy(
            update={
                "baseline_support_absent_foreground_maximum": 0.43,
                "baseline_support_background_change_maximum": 0.12,
            }
        ),
    )

    assert result is not None
    assert result.band is SearchEvidenceBand.MATERIAL_DROP
    assert result.scene_discontinuity is False
    assert result.object_degradation is True
    assert result.localized_support_drop is True
    assert result.localized_support_change_advantage == 0.514348
    assert result.localized_roi_support_ncc_advantage == 0.679146
    assert result.registration_stability_veto is True
    assert result.registration_veto_overridden is True


def test_registration_camera_motion_veto_remains_non_directional() -> None:
    comparison = _comparison(
        similarity=0.65,
        ncc=0.05,
        edge=0.80,
        change=0.80,
        foreground=0.55,
        background_change=0.25,
        roi_ncc=0.18,
        alignment_state="ambiguous",
        alignment_dx=8,
        alignment_dy=6,
        alignment_rotation=5,
        alignment_overlap=0.98,
        alignment_score=0.62,
        alignment_margin=0.001,
        present_gate=False,
        occlusion_evidence=True,
        decision_path="indeterminate",
        decision_reason="unstable_scene",
    )

    assert _registration_stability_veto(comparison) is True
    result = evaluate_search_evidence(comparison, _policy())

    assert result is not None
    assert result.band is SearchEvidenceBand.USABLE_AMBIGUOUS
    assert result.scene_discontinuity is True
    assert result.localized_support_drop is False
    assert result.localized_support_change_advantage == 0.55
    assert result.localized_roi_support_ncc_advantage == 0.13
    assert result.registration_stability_veto is True
    assert result.registration_veto_overridden is False
    start = datetime(2026, 9, 22, 3, 18, 55, tzinfo=timezone.utc)
    strong = evaluate_search_evidence(_comparison(), _policy())
    assert strong is not None
    formed = form_disappearance_candidates(
        (
            SuccessorSearchSample("present", start, strong, "PRESENT"),
            SuccessorSearchSample(
                "motion-1", start + timedelta(seconds=15), result, "INDETERMINATE"
            ),
            SuccessorSearchSample(
                "motion-2", start + timedelta(seconds=20), result, "INDETERMINATE"
            ),
        )
    )
    assert formed.candidates == ()


def test_registration_veto_blocks_low_background_ordinary_material_drop() -> None:
    comparison = _registration_veto_ordinary_drop_comparison()

    assert _registration_stability_veto(comparison) is True
    result = evaluate_search_evidence(comparison, _policy())

    assert result is not None
    assert result.band is SearchEvidenceBand.USABLE_AMBIGUOUS
    assert result.reason_code == "scene_instability_suppressed_direction"
    assert result.scene_discontinuity is True
    assert result.object_degradation is True
    assert result.localized_support_drop is False
    assert result.registration_stability_veto is True
    assert result.registration_veto_overridden is False


def test_material_drop_shape_rejects_unoverridden_registration_veto() -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - closed evidence shape uses bare ValueError
        _ = SearchEvidence(
            SearchEvidenceBand.MATERIAL_DROP,
            "object_reference_support_drop",
            scene_stable=True,
            object_degradation=True,
            registration_stability_veto=True,
        )


def test_registration_veto_ordinary_drop_cannot_form_s4_candidate_or_public_transition() -> None:
    policy = _policy()
    strong = evaluate_search_evidence(_comparison(), policy)
    suppressed = evaluate_search_evidence(
        _registration_veto_ordinary_drop_comparison(),
        policy,
    )
    assert strong is not None
    assert suppressed is not None
    start = datetime(2026, 9, 22, 3, 18, 55, tzinfo=timezone.utc)
    samples = (
        SuccessorSearchSample("present", start, strong, "PRESENT"),
        SuccessorSearchSample(
            "motion-1", start + timedelta(seconds=15), suppressed, "INDETERMINATE"
        ),
        SuccessorSearchSample(
            "motion-2", start + timedelta(seconds=20), suppressed, "INDETERMINATE"
        ),
    )

    formed = form_disappearance_candidates(samples)

    assert formed.candidates == ()
    assert all(sample.classifier_state != "ABSENT" for sample in samples[1:])


def test_registration_override_requires_complete_ncc_and_alignment_evidence() -> None:
    base = _rack_absent_comparison()
    cases = (
        base.model_copy(update={"roi_luma_ncc": None}),
        base.model_copy(update={"roi_luma_ncc": float("nan")}),
        base.model_copy(update={"baseline_support_alignment_margin": None}),
        base.model_copy(update={"baseline_support_alignment_score": float("nan")}),
    )

    for comparison in cases:
        result = evaluate_search_evidence(comparison, _policy())
        assert result is not None
        assert result.band is SearchEvidenceBand.USABLE_AMBIGUOUS
        assert result.localized_support_drop is False
        assert result.registration_veto_overridden is False


def test_registration_override_ncc_boundary_is_inclusive() -> None:
    policy = _policy()
    at_boundary = _rack_absent_comparison().model_copy(
        update={
            "roi_luma_ncc": policy.present_luma_ncc_minimum,
            "baseline_support_luma_ncc": policy.baseline_support_absent_ncc_maximum,
        }
    )
    below_boundary = at_boundary.model_copy(
        update={"roi_luma_ncc": policy.present_luma_ncc_minimum - 0.000001}
    )

    accepted = evaluate_search_evidence(at_boundary, policy)
    rejected = evaluate_search_evidence(below_boundary, policy)

    assert accepted is not None
    assert accepted.band is SearchEvidenceBand.MATERIAL_DROP
    assert accepted.registration_veto_overridden is True
    assert accepted.localized_roi_support_ncc_advantage == 0.4
    assert rejected is not None
    assert rejected.band is SearchEvidenceBand.USABLE_AMBIGUOUS
    assert rejected.registration_veto_overridden is False
    assert rejected.localized_roi_support_ncc_advantage == 0.399999


def test_controlled_rack_occlusion_remains_non_directional() -> None:
    comparison = _comparison(
        similarity=0.758071,
        ncc=0.068171,
        edge=0.903155,
        change=0.699227,
        foreground=0.596733,
        background_change=0.577731,
        scene_stable=False,
        scene_veto="global_scene_change",
        present_gate=False,
        occlusion_evidence=True,
        decision_path="indeterminate",
        decision_reason="unstable_scene",
    )

    result = evaluate_search_evidence(comparison, _policy())

    assert result is not None
    assert result.band is SearchEvidenceBand.USABLE_AMBIGUOUS
    assert result.localized_support_drop is False
    assert result.localized_support_change_advantage == 0.121496


def test_controlled_rack_replacement_remains_non_directional() -> None:
    comparison = _comparison(
        similarity=0.522460,
        ncc=0.135319,
        edge=0.882568,
        change=0.854659,
        foreground=0.808227,
        background_change=0.969883,
        scene_stable=False,
        scene_veto="global_scene_change",
        present_gate=False,
        replacement_evidence=True,
        decision_path="indeterminate",
        decision_reason="unstable_scene",
    )

    result = evaluate_search_evidence(comparison, _policy())

    assert result is not None
    assert result.band is SearchEvidenceBand.USABLE_AMBIGUOUS
    assert result.localized_support_drop is False
    assert result.localized_support_change_advantage == -0.115224


def test_localized_change_advantage_boundary_is_inclusive() -> None:
    at_boundary = _comparison(
        similarity=0.65,
        ncc=0.20,
        edge=0.80,
        change=0.71,
        foreground=0.69,
        background_change=0.31,
        present_gate=False,
        decision_path="indeterminate",
        decision_reason="insufficient_visual_evidence",
    )
    below_boundary = at_boundary.model_copy(
        update={"baseline_support_background_change_ratio": 0.310001}
    )

    accepted = evaluate_search_evidence(at_boundary, _policy())
    rejected = evaluate_search_evidence(below_boundary, _policy())

    assert accepted is not None
    assert accepted.band is SearchEvidenceBand.MATERIAL_DROP
    assert accepted.localized_support_drop is True
    assert accepted.localized_support_change_advantage == 0.4
    assert rejected is not None
    assert rejected.band is SearchEvidenceBand.USABLE_AMBIGUOUS
    assert rejected.localized_support_drop is False
    assert rejected.localized_support_change_advantage == 0.399999


def test_localized_drop_never_overrides_spatial_veto_or_replacement() -> None:
    base = _comparison(
        similarity=0.65,
        ncc=0.10,
        edge=0.80,
        change=0.80,
        foreground=0.60,
        background_change=0.30,
        present_gate=False,
        decision_path="indeterminate",
        decision_reason="unstable_scene",
    )
    scene_veto = base.model_copy(
        update={
            "baseline_support_scene_stable": False,
            "baseline_support_scene_stability_veto_reason": "global_scene_change",
        }
    )
    replacement = base.model_copy(update={"baseline_support_replacement_evidence": True})
    unknown_replacement = base.model_copy(update={"baseline_support_replacement_evidence": None})

    for comparison in (scene_veto, replacement, unknown_replacement):
        result = evaluate_search_evidence(comparison, _policy())
        assert result is not None
        assert result.band is SearchEvidenceBand.USABLE_AMBIGUOUS
        assert result.localized_support_drop is False


def test_non_finite_reference_metric_fails_closed_as_insufficient() -> None:
    malformed = _comparison().model_copy(update={"baseline_support_luma_ncc": float("nan")})

    result = evaluate_search_evidence(malformed, _policy())

    assert result is not None
    assert result.band is SearchEvidenceBand.INSUFFICIENT
    assert result.reason_code == "missing_reference_support_metrics"


def test_localized_rack_drop_propagates_without_changing_classifier_state() -> None:
    selected_policy = _policy().model_copy(
        update={
            "baseline_support_absent_foreground_maximum": 0.43,
            "baseline_support_background_change_maximum": 0.12,
        }
    )
    strong = evaluate_search_evidence(_comparison(), selected_policy)
    occluded = evaluate_search_evidence(
        _comparison(
            ncc=0.068171,
            change=0.699227,
            foreground=0.596733,
            background_change=0.577731,
            scene_stable=False,
            scene_veto="global_scene_change",
            present_gate=False,
            decision_path="indeterminate",
            decision_reason="unstable_scene",
        ),
        selected_policy,
    )
    absent = evaluate_search_evidence(
        _rack_absent_comparison(),
        selected_policy,
    )
    assert strong is not None
    assert occluded is not None
    assert absent is not None
    start = datetime(2026, 9, 22, 3, 18, 55, tzinfo=timezone.utc)
    result = form_disappearance_candidates(
        (
            SuccessorSearchSample("present", start, strong, "PRESENT"),
            SuccessorSearchSample(
                "occluded", start + timedelta(seconds=5), occluded, "INDETERMINATE"
            ),
            SuccessorSearchSample(
                "absent-1", start + timedelta(seconds=15), absent, "INDETERMINATE"
            ),
            SuccessorSearchSample(
                "absent-2", start + timedelta(seconds=20), absent, "INDETERMINATE"
            ),
        )
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].qualified is True
    assert result.candidates[0].interval_start_utc == start
    assert result.candidates[0].interval_end_utc == start + timedelta(seconds=15)


def test_sparse_reference_support_is_insufficient_before_material_drop() -> None:
    comparison = _comparison_with_support(
        1,
        similarity=0.40,
        ncc=0.10,
        edge=0.30,
        change=0.80,
        foreground=0.10,
        present_gate=False,
        decision_path="indeterminate",
        decision_reason="insufficient_visual_evidence",
    )

    result = evaluate_search_evidence(comparison, _policy(minimum_clipped_mask_pixels=64))

    assert result is not None
    assert result.band is SearchEvidenceBand.INSUFFICIENT
    assert result.band is not SearchEvidenceBand.MATERIAL_DROP


def test_reference_support_at_policy_minimum_keeps_normal_evidence_eligible() -> None:
    comparison = _comparison_with_support(
        64,
        similarity=0.40,
        ncc=0.10,
        edge=0.30,
        change=0.80,
        foreground=0.10,
        present_gate=False,
        decision_path="indeterminate",
        decision_reason="insufficient_visual_evidence",
    )

    result = evaluate_search_evidence(comparison, _policy(minimum_clipped_mask_pixels=64))

    assert result is not None
    assert result.band is SearchEvidenceBand.MATERIAL_DROP


def test_undersized_reference_roi_is_insufficient_before_material_drop() -> None:
    comparison = _comparison_with_support(
        1,
        roi_pixel_count=32,
        similarity=0.40,
        ncc=0.10,
        edge=0.30,
        change=0.80,
        foreground=0.10,
        present_gate=False,
        decision_path="indeterminate",
        decision_reason="insufficient_visual_evidence",
    )

    result = evaluate_search_evidence(
        comparison,
        _policy(minimum_clipped_mask_pixels=1, minimum_roi_pixels=64),
    )

    assert result is not None
    assert result.band is SearchEvidenceBand.INSUFFICIENT
    assert result.band is not SearchEvidenceBand.MATERIAL_DROP


def test_fast_present_miss_alone_does_not_create_material_drop() -> None:
    comparison = _comparison(
        similarity=0.80,
        ncc=0.80,
        edge=0.80,
        change=0.20,
        foreground=0.80,
        present_gate=False,
    )

    result = evaluate_search_evidence(comparison, _policy(), fast_present_hit=False)

    assert result is not None
    assert result.band is not SearchEvidenceBand.MATERIAL_DROP


def test_scene_only_global_change_is_suppressed_from_directional_drop() -> None:
    comparison = _comparison(
        scene_stable=False,
        scene_veto="global_scene_change",
        background_change=0.90,
    )

    result = evaluate_search_evidence(comparison, _policy())

    assert result is not None
    assert result.band is SearchEvidenceBand.USABLE_AMBIGUOUS
    assert result.scene_discontinuity is True
    assert result.scene_only_suppressed is True


def test_camera_instability_cannot_become_material_drop_even_with_support_loss() -> None:
    comparison = _comparison(
        similarity=0.40,
        ncc=0.10,
        edge=0.30,
        change=0.80,
        foreground=0.10,
        scene_stable=False,
        scene_veto="registration_failed",
        background_change=0.80,
        present_gate=False,
        decision_path="indeterminate",
        decision_reason="unstable_scene",
    )

    result = evaluate_search_evidence(comparison, _policy())

    assert result is not None
    assert result.band is SearchEvidenceBand.USABLE_AMBIGUOUS
    assert result.band is not SearchEvidenceBand.MATERIAL_DROP
    assert result.object_degradation is True


def test_unusable_visual_comparison_is_insufficient() -> None:
    comparison = RawComparison(
        baseline_mask_pixel_count=None,
        probe_mask_pixel_count=None,
        roi_pixel_count=400,
        mask_intersection_pixel_count=None,
        mask_union_pixel_count=None,
        baseline_mask_coverage=None,
        probe_mask_coverage=None,
        mask_iou=None,
        effective_comparison_area=None,
        roi_luma_ncc=None,
        visual_status=VisualStatus.UNUSABLE,
        unusable_reason=VisualReason.INVALID_MASK,
    )

    result = evaluate_search_evidence(comparison, _policy())

    assert result is not None
    assert result.band is SearchEvidenceBand.INSUFFICIENT


def test_missing_or_operational_prerequisite_has_no_search_evidence() -> None:
    assert evaluate_search_evidence(None, _policy()) is None


def test_conflicting_scene_and_object_signals_remain_ambiguous() -> None:
    comparison = _comparison(
        similarity=0.40,
        ncc=0.10,
        edge=0.30,
        change=0.80,
        foreground=0.10,
        scene_stable=False,
        scene_veto="global_scene_change",
        background_change=0.90,
        present_gate=False,
        decision_path="indeterminate",
        decision_reason="conflicting_visual_evidence",
    )

    result = evaluate_search_evidence(comparison, _policy())

    assert result is not None
    assert result.band is SearchEvidenceBand.USABLE_AMBIGUOUS


def test_evaluator_does_not_change_classifier_state_vocabulary() -> None:
    comparison = _comparison(present_gate=False)
    before = comparison.model_dump(mode="json")

    result = evaluate_search_evidence(comparison, _policy())

    assert result is not None
    assert comparison.model_dump(mode="json") == before
    assert {"PRESENT", "ABSENT", "INDETERMINATE"} == {
        "PRESENT",
        "ABSENT",
        "INDETERMINATE",
    }
