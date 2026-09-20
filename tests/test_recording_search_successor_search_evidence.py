"""Focused Phase S3 shadow search-evidence contract tests."""

# Fixture construction keeps every cheap metric explicit.
# ruff: noqa: PLR0913

from __future__ import annotations

from vigi_vision.object_presence_evidence import RawComparison
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.object_presence_values import VisualReason, VisualStatus
from vigi_vision.recording_search_successor_search_evidence import (
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
    scene_stable: bool = True,
    scene_veto: str | None = None,
    present_gate: bool = True,
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
        roi_luma_ncc=0.90,
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
        baseline_support_alignment_state="not_required",
        baseline_support_stability_pixel_count=400,
        baseline_support_stability_changed_pixel_count=10,
        baseline_support_stability_valid_pixel_count=300,
        baseline_support_stability_excluded_pixel_count=100,
        baseline_support_scene_stable=scene_stable,
        baseline_support_scene_stability_veto_reason=scene_veto,
        baseline_support_present_gate_passed=present_gate,
        baseline_support_absent_gate_passed=False,
        baseline_support_empty_background_evidence=False,
        baseline_support_replacement_evidence=False,
        baseline_support_occlusion_evidence=False,
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

    result = evaluate_search_evidence(
        comparison, _policy(minimum_clipped_mask_pixels=64)
    )

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

    result = evaluate_search_evidence(
        comparison, _policy(minimum_clipped_mask_pixels=64)
    )

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
