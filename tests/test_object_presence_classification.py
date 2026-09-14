from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace

import pytest
from pydantic import ValidationError

from vigi_vision.investigation_confirmation_models import ConfirmationRoi, RoiProvenance
from vigi_vision.object_presence_comparator import (
    ClassifierInput,
    ObjectPresenceClassifier,
    binarize_mask_logits,
)
from vigi_vision.object_presence_evidence import ClassificationResult, RawComparison
from vigi_vision.object_presence_metrics import mean_centered_ncc, ratio
from vigi_vision.object_presence_models import (
    BinaryMask,
    ClassificationFailureReason,
    ClassificationOperationalError,
    ClassificationOutcome,
    DecodedRgbImage,
    VisualReason,
    VisualStatus,
)
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy


def _roi() -> ConfirmationRoi:
    return ConfirmationRoi(
        x=1,
        y=1,
        width=20,
        height=20,
        coordinate_space="source_pixels",
        provenance=RoiProvenance.MANUAL,
    )


def _image(offset: int = 0) -> DecodedRgbImage:
    return DecodedRgbImage.from_rows(
        tuple(
            tuple(
                ((x * 13 + y * 7 + offset) % 256, (x * 5 + y * 11) % 256, (x + y) % 256)
                for x in range(22)
            )
            for y in range(22)
        )
    )


def _different_image() -> DecodedRgbImage:
    return DecodedRgbImage.from_rows(
        tuple(
            tuple(((255, 255, 255) if (x + y) % 2 else (0, 0, 0)) for x in range(22))
            for y in range(22)
        )
    )


def _mask(predicate: Callable[[int, int], bool]) -> BinaryMask:
    return BinaryMask.from_rows(tuple(tuple(predicate(x, y) for x in range(22)) for y in range(22)))


def _block_mask(x_start: int = 3, y_start: int = 3) -> BinaryMask:
    return _mask(lambda x, y: x_start <= x < x_start + 8 and y_start <= y < y_start + 8)


def _disjoint_probe_mask() -> BinaryMask:
    return _mask(
        lambda x, y: (
            (x == 10 and y == 10) or (12 <= x < 20 and 2 <= y < 10 and not (x == 12 and y == 2))
        )
    )


def _classifier(
    minimum_mask_overlap_for_comparison: float = 0.1,
    minimum_comparison_area: int = 64,
    minimum_roi_pixels: int = 64,
    minimum_clipped_mask_pixels: int = 64,
    maximum_roi_mask_coverage_ratio: float = 0.95,
) -> ObjectPresenceClassifier:
    policy = ObjectPresenceDecisionPolicy(
        minimum_mask_overlap_for_comparison=minimum_mask_overlap_for_comparison,
        minimum_comparison_area=minimum_comparison_area,
        minimum_roi_pixels=minimum_roi_pixels,
        minimum_clipped_mask_pixels=minimum_clipped_mask_pixels,
        maximum_roi_mask_coverage_ratio=maximum_roi_mask_coverage_ratio,
    )
    return ObjectPresenceClassifier(policy)


def _support_scene(*, background: int = 180, shoe: bool = True, offset: int = 0) -> DecodedRgbImage:
    """Build a fixed-ROI shoe/carpet fixture without repository media."""
    rows = []
    for y in range(22):
        row = []
        for x in range(22):
            roi_x, roi_y = x - 1, y - 1
            if shoe and 6 <= roi_x < 14 and 6 <= roi_y < 14:
                value = 25 + ((roi_x * 17 + roi_y * 11) % 40) + offset
            else:
                value = background + ((roi_x * 3 + roi_y * 2) % 8) + offset
            value = max(0, min(255, value))
            row.append((value, value, value))
        rows.append(tuple(row))
    return DecodedRgbImage.from_rows(tuple(rows))


def _support_mask() -> BinaryMask:
    return _block_mask(x_start=7, y_start=7)


def _support_classifier(**overrides: object) -> ObjectPresenceClassifier:
    values: dict[str, object] = {
        "classifier_policy_version": "test-baseline-support-v1",
        "classifier_preprocessing_version": "test-baseline-support-v1",
        "baseline_support_mode": True,
        "minimum_mask_overlap_for_comparison": 0.1,
        "minimum_roi_pixels": 1,
        "minimum_clipped_mask_pixels": 1,
    }
    values.update(overrides)
    return ObjectPresenceClassifier(ObjectPresenceDecisionPolicy(**values))


def _support_input(
    probe_image: DecodedRgbImage, probe_mask: BinaryMask | None = None
) -> ClassifierInput:
    baseline_mask = _support_mask()
    return ClassifierInput(
        baseline_image=_support_scene(),
        probe_image=probe_image,
        baseline_mask=baseline_mask,
        probe_mask=probe_mask or baseline_mask,
        roi=_roi(),
    )


def _shift_image(image: DecodedRgbImage, dx: int, dy: int) -> DecodedRgbImage:
    """Translate a frame and fill the exposed border with the carpet."""
    rows = []
    for y in range(image.height):
        row = []
        for x in range(image.width):
            source_x, source_y = x - dx, y - dy
            if 0 <= source_x < image.width and 0 <= source_y < image.height:
                row.append(image.pixels[source_y][source_x])
            else:
                row.append((180, 180, 180))
        rows.append(tuple(row))
    return DecodedRgbImage.from_rows(tuple(rows))


def _rotate_image(image: DecodedRgbImage, degrees: int) -> DecodedRgbImage:
    """Rotate the synthetic ROI around its center and fill with carpet."""
    angle = math.radians(degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    center = (image.width - 1) / 2.0
    rows = []
    for y in range(image.height):
        row = []
        for x in range(image.width):
            relative_x, relative_y = x - center, y - center
            source_x = round(center + cosine * relative_x - sine * relative_y)
            source_y = round(center + sine * relative_x + cosine * relative_y)
            if 0 <= source_x < image.width and 0 <= source_y < image.height:
                row.append(image.pixels[source_y][source_x])
            else:
                row.append((180, 180, 180))
        rows.append(tuple(row))
    return DecodedRgbImage.from_rows(tuple(rows))


def _comparable(
    mask_iou: float = 0.2,
    roi_luma_ncc: float = 0.3,
    intersection: int = 20,
    union: int = 100,
) -> RawComparison:
    return RawComparison(
        baseline_mask_pixel_count=60,
        probe_mask_pixel_count=60,
        roi_pixel_count=100,
        mask_intersection_pixel_count=intersection,
        mask_union_pixel_count=union,
        baseline_mask_coverage=0.6,
        probe_mask_coverage=0.6,
        mask_iou=mask_iou,
        effective_comparison_area=intersection,
        roi_luma_ncc=roi_luma_ncc,
        visual_status=VisualStatus.COMPARABLE,
        unusable_reason=None,
    )


def _run_b_ambiguous_absent_comparison() -> RawComparison:
    """Redacted metric projection from the preserved real disappearance run."""
    return RawComparison(
        baseline_mask_pixel_count=9101,
        probe_mask_pixel_count=13770,
        roi_pixel_count=13770,
        mask_intersection_pixel_count=9101,
        mask_union_pixel_count=13770,
        baseline_mask_coverage=0.66093,
        probe_mask_coverage=1.0,
        mask_iou=0.66093,
        effective_comparison_area=None,
        roi_luma_ncc=0.118991,
        visual_status=VisualStatus.COMPARABLE,
        unusable_reason=None,
        comparison_mode="baseline_support_v3",
        baseline_support_pixel_count=9101,
        baseline_support_luma_similarity=0.78519,
        baseline_support_luma_ncc=-0.010106,
        baseline_support_edge_similarity=0.903789,
        baseline_support_change_ratio=0.641138,
        baseline_support_foreground_retention=0.140951,
        baseline_support_background_change_ratio=0.188086,
        baseline_support_alignment_dx=-15,
        baseline_support_alignment_dy=8,
        baseline_support_alignment_rotation_degrees=-5,
        baseline_support_alignment_overlap=0.862982,
        baseline_support_alignment_score=0.441883,
        baseline_support_alignment_margin=0.001846,
        baseline_support_stability_pixel_count=13770,
        baseline_support_stability_changed_pixel_count=281,
        baseline_support_stability_valid_pixel_count=1494,
        baseline_support_stability_excluded_pixel_count=12276,
        baseline_support_alignment_candidates_generated=385,
        baseline_support_alignment_candidates_evaluated=385,
        baseline_support_alignment_valid_candidates=385,
        baseline_support_alignment_state="ambiguous",
        baseline_support_scene_stable=True,
        baseline_support_scene_stability_veto_reason=None,
    )


def _input(
    baseline_mask: BinaryMask | None = None,
    probe_mask: BinaryMask | None = None,
    baseline_image: DecodedRgbImage | None = None,
    probe_image: DecodedRgbImage | None = None,
) -> ClassifierInput:
    return ClassifierInput(
        baseline_image=baseline_image or _image(),
        probe_image=probe_image or _image(3),
        baseline_mask=baseline_mask or _block_mask(),
        probe_mask=probe_mask or _block_mask(),
        roi=_roi(),
    )


def test_logits_use_inclusive_zero_threshold() -> None:
    assert binarize_mask_logits(((-0.1, 0.0, 0.1),), 0.0).rows == ((False, True, True),)


def test_matching_regions_are_present() -> None:
    result = _classifier().classify(_input(probe_image=_image()))
    assert result.outcome is ClassificationOutcome.PRESENT
    assert result.comparison.mask_iou == 1.0
    assert result.comparison.roi_luma_ncc == 1.0


def test_baseline_support_reclassifies_removed_shoe_as_absent() -> None:
    result = _support_classifier().classify(_support_input(_support_scene(shoe=False)))
    assert result.outcome is ClassificationOutcome.ABSENT
    assert result.comparison.comparison_mode == "baseline_support_v2"
    assert result.comparison.baseline_support_background_change_ratio == 0.0
    assert result.comparison.baseline_support_change_ratio is not None
    assert result.comparison.baseline_support_change_ratio >= 0.7
    assert result.comparison.baseline_support_foreground_retention is not None
    assert result.comparison.baseline_support_foreground_retention <= 0.3


def test_baseline_support_v2_accepts_floor_exposure_metrics_from_preserved_run() -> None:
    baseline = _support_classifier().compare(_support_input(_support_scene(shoe=False)))
    exposed_floor = baseline.model_copy(
        update={
            "baseline_support_luma_similarity": 0.844027,
            "baseline_support_luma_ncc": 0.119746,
            "baseline_support_change_ratio": 0.522446,
            "baseline_support_foreground_retention": 0.0,
            "baseline_support_background_change_ratio": 0.0,
            "comparison_mode": "baseline_support_v2",
        }
    )
    assert (
        _support_classifier().policy.decide(exposed_floor).outcome is ClassificationOutcome.ABSENT
    )
    assert (
        _support_classifier()
        .policy.decide(exposed_floor.model_copy(update={"comparison_mode": "baseline_support_v1"}))
        .outcome
        is ClassificationOutcome.INDETERMINATE
    )
    exposed_floor_v3 = exposed_floor.model_copy(
        update={
            "comparison_mode": "baseline_support_v3",
            "baseline_support_alignment_dx": 0,
            "baseline_support_alignment_dy": 0,
            "baseline_support_alignment_rotation_degrees": 0,
            "baseline_support_alignment_overlap": 1.0,
            "baseline_support_alignment_score": 0.2,
            "baseline_support_alignment_margin": 0.0,
        }
    )
    assert (
        _support_classifier(baseline_support_alignment_mode=True)
        .policy.decide(exposed_floor_v3)
        .outcome
        is ClassificationOutcome.ABSENT
    )


def test_baseline_support_keeps_shoe_present_through_global_exposure_shift() -> None:
    result = _support_classifier().classify(
        _support_input(_support_scene(background=200, offset=20))
    )
    assert result.outcome is ClassificationOutcome.PRESENT
    assert result.comparison.baseline_support_luma_ncc == 1.0


def test_baseline_support_keeps_shoe_present_through_small_compression_noise() -> None:
    baseline = _support_scene()
    rows = [list(row) for row in baseline.pixels]
    for y, row in enumerate(rows):
        for x, pixel in enumerate(row):
            noise = 3 if (x + 2 * y) % 5 == 0 else -2 if (x + y) % 7 == 0 else 0
            rows[y][x] = tuple(max(0, min(255, channel + noise)) for channel in pixel)
    result = _support_classifier().classify(
        _support_input(DecodedRgbImage.from_rows(tuple(tuple(row) for row in rows)))
    )
    assert result.outcome is ClassificationOutcome.PRESENT
    assert result.comparison.baseline_support_background_change_ratio is not None
    assert result.comparison.baseline_support_background_change_ratio <= 0.1


def test_baseline_support_fails_closed_for_partial_occlusion() -> None:
    probe = _support_scene()
    rows = [list(row) for row in probe.pixels]
    for y in range(7, 11):
        for x in range(7, 15):
            rows[y][x] = (180, 180, 180)
    occluded = DecodedRgbImage.from_rows(tuple(tuple(row) for row in rows))
    result = _support_classifier().classify(_support_input(occluded))
    assert result.outcome is ClassificationOutcome.INDETERMINATE
    assert result.reason_code is VisualReason.INSUFFICIENT_VISUAL_EVIDENCE


def test_baseline_support_does_not_accept_similar_dark_replacement() -> None:
    probe = _support_scene()
    rows = [list(row) for row in probe.pixels]
    for y in range(7, 15):
        for x in range(7, 15):
            value = 70 + ((x + y) % 3)
            rows[y][x] = (value, value, value)
    replacement = DecodedRgbImage.from_rows(tuple(tuple(row) for row in rows))
    result = _support_classifier().classify(_support_input(replacement))
    assert result.outcome is ClassificationOutcome.INDETERMINATE
    assert result.reason_code is VisualReason.INSUFFICIENT_VISUAL_EVIDENCE


def test_baseline_support_ignores_expanded_probe_mask_for_identity() -> None:
    expanded = _mask(lambda x, y: 1 <= x < 21 and 1 <= y < 21)
    result = _support_classifier().classify(_support_input(_support_scene(), probe_mask=expanded))
    assert result.outcome is ClassificationOutcome.PRESENT
    assert result.comparison.mask_iou is not None
    assert result.comparison.mask_iou < 0.3


def test_baseline_support_requires_valid_baseline_support() -> None:
    result = _support_classifier(minimum_clipped_mask_pixels=65).compare(
        _support_input(_support_scene())
    )
    assert result.visual_status is VisualStatus.UNUSABLE
    assert result.unusable_reason is VisualReason.INVALID_MASK


def test_baseline_support_keeps_small_registration_error_indeterminate() -> None:
    probe = _shift_image(_support_scene(), 1, 0)
    result = _support_classifier().classify(_support_input(probe))
    assert result.outcome is ClassificationOutcome.INDETERMINATE


def test_baseline_support_v3_accepts_bounded_object_translation() -> None:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )
    result = classifier.classify(_support_input(_shift_image(_support_scene(), 2, 0)))
    assert result.outcome is ClassificationOutcome.PRESENT
    assert result.comparison.comparison_mode == "baseline_support_v3"
    assert result.comparison.baseline_support_alignment_dx == 2
    assert result.comparison.baseline_support_alignment_margin is not None
    assert result.comparison.baseline_support_alignment_margin >= 0.02


def test_baseline_support_v3_accepts_one_pixel_object_translation() -> None:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )
    result = classifier.classify(_support_input(_shift_image(_support_scene(), 1, 0)))
    assert result.outcome is ClassificationOutcome.PRESENT
    assert result.comparison.baseline_support_alignment_dx in {0, 1, 2}


def test_alignment_diagnostics_sink_failure_does_not_change_result() -> None:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )

    def broken_sink(_name: str, _value: int) -> None:
        raise RuntimeError

    result = classifier.classify(_support_input(_support_scene()), diagnostics_sink=broken_sink)
    assert result.outcome is ClassificationOutcome.PRESENT


def test_baseline_support_v3_reclassifies_removed_shoe_as_absent() -> None:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )
    result = classifier.classify(_support_input(_support_scene(shoe=False)))
    assert result.outcome is ClassificationOutcome.ABSENT
    assert result.comparison.comparison_mode == "baseline_support_v3"
    assert result.comparison.baseline_support_alignment_margin is not None
    assert result.comparison.baseline_support_alignment_margin < 0.02
    assert result.comparison.baseline_support_decision_path == "absent"
    assert result.comparison.baseline_support_decision_reason == "absent_empty_background"
    assert result.comparison.baseline_support_absent_gate_passed is True
    assert result.comparison.baseline_support_empty_background_evidence is True


def test_preserved_run_b_absence_does_not_require_alignment_success() -> None:
    policy = ObjectPresenceDecisionPolicy(
        classifier_policy_version="test-preserved-run-b-v3",
        classifier_preprocessing_version="test-preserved-run-b-v3",
        baseline_support_mode=True,
        baseline_support_alignment_mode=True,
        minimum_mask_overlap_for_comparison=0.1,
        minimum_roi_pixels=1,
        minimum_clipped_mask_pixels=1,
    )
    result = policy.decide(_run_b_ambiguous_absent_comparison())
    assert result.outcome is ClassificationOutcome.ABSENT
    assert result.comparison.baseline_support_alignment_state == "ambiguous"
    assert result.comparison.baseline_support_present_gate_passed is False
    assert result.comparison.baseline_support_absent_gate_passed is True
    assert result.comparison.baseline_support_empty_background_evidence is True
    assert result.comparison.baseline_support_decision_path == "absent"
    assert result.comparison.baseline_support_decision_reason == "absent_empty_background"


def test_baseline_support_v3_accepts_bounded_object_rotation() -> None:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )
    result = classifier.classify(_support_input(_rotate_image(_support_scene(), 5)))
    assert result.outcome is ClassificationOutcome.PRESENT
    assert result.comparison.baseline_support_alignment_rotation_degrees in {-10, -5, 0, 5, 10}


def test_baseline_support_v3_preserves_presence_through_exposure_and_noise() -> None:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )
    exposure = classifier.classify(_support_input(_support_scene(background=200, offset=20)))
    assert exposure.outcome is ClassificationOutcome.PRESENT

    baseline = _support_scene()
    rows = [list(row) for row in baseline.pixels]
    for y, row in enumerate(rows):
        for x, pixel in enumerate(row):
            noise = 3 if (x + 2 * y) % 5 == 0 else -2 if (x + y) % 7 == 0 else 0
            row[x] = tuple(max(0, min(255, channel + noise)) for channel in pixel)
    noisy = DecodedRgbImage.from_rows(tuple(tuple(row) for row in rows))
    assert classifier.classify(_support_input(noisy)).outcome is ClassificationOutcome.PRESENT


def test_baseline_support_v3_keeps_large_camera_motion_indeterminate() -> None:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )
    result = classifier.classify(_support_input(_shift_image(_support_scene(), 5, 5)))
    assert result.outcome is ClassificationOutcome.INDETERMINATE
    assert result.comparison.baseline_support_decision_reason == "unstable_scene"


def test_baseline_support_v3_keeps_occlusion_and_replacement_indeterminate() -> None:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )
    probe = _support_scene()
    rows = [list(row) for row in probe.pixels]
    for y in range(7, 11):
        for x in range(7, 15):
            rows[y][x] = (180, 180, 180)
    occluded = DecodedRgbImage.from_rows(tuple(tuple(row) for row in rows))
    occluded_result = classifier.classify(_support_input(occluded))
    assert occluded_result.outcome is ClassificationOutcome.INDETERMINATE

    rows = [list(row) for row in probe.pixels]
    for y in range(7, 15):
        for x in range(7, 15):
            value = 70 + ((x + y) % 3)
            rows[y][x] = (value, value, value)
    replacement = DecodedRgbImage.from_rows(tuple(tuple(row) for row in rows))
    replacement_result = classifier.classify(_support_input(replacement))
    assert replacement_result.outcome is ClassificationOutcome.INDETERMINATE
    assert occluded_result.comparison.baseline_support_decision_reason == "roi_occluded"
    assert occluded_result.comparison.baseline_support_occlusion_evidence is True
    assert replacement_result.comparison.baseline_support_decision_reason == "replacement_candidate"
    assert replacement_result.comparison.baseline_support_replacement_evidence is True


def test_baseline_support_v3_does_not_let_probe_mask_block_present() -> None:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )
    expanded = _mask(lambda x, y: 1 <= x < 21 and 1 <= y < 21)
    result = classifier.classify(_support_input(_support_scene(), probe_mask=expanded))
    assert result.outcome is ClassificationOutcome.PRESENT


def test_baseline_support_rejects_large_camera_motion_as_unstable_background() -> None:
    probe = _shift_image(_support_scene(), 5, 5)
    result = _support_classifier().classify(_support_input(probe))
    assert result.outcome is ClassificationOutcome.INDETERMINATE
    assert result.comparison.baseline_support_background_change_ratio is not None
    assert result.comparison.baseline_support_background_change_ratio > 0.10


def test_persisted_v1_support_rows_keep_original_gate_semantics() -> None:
    classifier = _support_classifier()
    comparison = classifier.compare(_support_input(_support_scene()))
    v1 = comparison.model_copy(update={"comparison_mode": "baseline_support_v1"})
    result = classifier.policy.decide(v1)
    assert result.outcome is ClassificationOutcome.PRESENT


def test_disjoint_masks_are_absent_when_luma_is_different() -> None:
    probe = _disjoint_probe_mask()
    result = _classifier(
        minimum_mask_overlap_for_comparison=0.0, minimum_comparison_area=1
    ).classify(_input(probe_mask=probe, probe_image=_different_image()))
    assert result.outcome is ClassificationOutcome.ABSENT
    assert result.comparison.mask_iou is not None
    assert result.comparison.mask_iou <= 0.1
    assert result.comparison.roi_luma_ncc is not None
    assert result.comparison.roi_luma_ncc <= 0.2


def test_comparable_policy_gap_is_indeterminate() -> None:
    comparison = _comparable(roi_luma_ncc=0.3)
    result = ObjectPresenceDecisionPolicy(
        minimum_mask_overlap_for_comparison=0.1,
        minimum_comparison_area=20,
        minimum_clipped_mask_pixels=60,
    ).decide(comparison)
    assert result.outcome is ClassificationOutcome.INDETERMINATE
    assert result.reason_code is VisualReason.INSUFFICIENT_VISUAL_EVIDENCE


def test_present_boundaries_are_inclusive() -> None:
    comparison = RawComparison(
        baseline_mask_pixel_count=75,
        probe_mask_pixel_count=75,
        roi_pixel_count=100,
        mask_intersection_pixel_count=50,
        mask_union_pixel_count=100,
        baseline_mask_coverage=0.75,
        probe_mask_coverage=0.75,
        mask_iou=0.5,
        effective_comparison_area=50,
        roi_luma_ncc=0.6,
        visual_status=VisualStatus.COMPARABLE,
        unusable_reason=None,
    )
    result = ObjectPresenceDecisionPolicy(
        minimum_mask_overlap_for_comparison=0.0,
        minimum_comparison_area=1,
        minimum_clipped_mask_pixels=1,
    ).decide(comparison)
    assert result.outcome is ClassificationOutcome.PRESENT


def test_absent_boundaries_are_inclusive() -> None:
    comparison = RawComparison(
        baseline_mask_pixel_count=11,
        probe_mask_pixel_count=11,
        roi_pixel_count=100,
        mask_intersection_pixel_count=2,
        mask_union_pixel_count=20,
        baseline_mask_coverage=0.11,
        probe_mask_coverage=0.11,
        mask_iou=0.1,
        effective_comparison_area=2,
        roi_luma_ncc=0.2,
        visual_status=VisualStatus.COMPARABLE,
        unusable_reason=None,
    )
    result = ObjectPresenceDecisionPolicy(
        minimum_mask_overlap_for_comparison=0.0,
        minimum_comparison_area=1,
        minimum_clipped_mask_pixels=1,
    ).decide(comparison)
    assert result.outcome is ClassificationOutcome.ABSENT


def test_overlap_gate_is_evaluated_before_area_gate() -> None:
    result = _classifier().compare(
        _input(
            probe_mask=_disjoint_probe_mask(),
        )
    )
    assert result.visual_status is VisualStatus.UNUSABLE
    assert result.unusable_reason is VisualReason.INSUFFICIENT_MASK_OVERLAP
    assert result.effective_comparison_area is None
    assert result.roi_luma_ncc is None


def test_effective_area_gate_is_evaluated_after_overlap_gate() -> None:
    classifier = _classifier(minimum_mask_overlap_for_comparison=0.0, minimum_comparison_area=65)
    result = classifier.compare(_input())
    assert result.visual_status is VisualStatus.UNUSABLE
    assert result.unusable_reason is VisualReason.INSUFFICIENT_COMPARISON_AREA
    assert result.effective_comparison_area == 64


def test_overlap_threshold_equality_reaches_comparison() -> None:
    result = _classifier(minimum_mask_overlap_for_comparison=1.0).compare(_input())
    assert result.visual_status is VisualStatus.COMPARABLE


def test_area_threshold_equality_reaches_ncc() -> None:
    result = _classifier(
        minimum_mask_overlap_for_comparison=0.0, minimum_comparison_area=64
    ).compare(_input())
    assert result.visual_status is VisualStatus.COMPARABLE
    assert result.roi_luma_ncc is not None


def test_background_dominant_mask_is_not_comparable() -> None:
    mask = _mask(lambda x, y: not (x == 1 and y == 1))
    result = _classifier().compare(_input(baseline_mask=mask, probe_mask=mask))
    assert result.unusable_reason is VisualReason.BACKGROUND_DOMINANT
    assert result.mask_iou is None


def test_zero_luma_variance_is_indeterminate() -> None:
    image = DecodedRgbImage.from_rows(
        tuple(tuple((100, 100, 100) for _ in range(22)) for _ in range(22))
    )
    result = _classifier().compare(_input(baseline_image=image, probe_image=image))
    assert result.unusable_reason is VisualReason.ZERO_LUMA_VARIANCE
    assert result.roi_luma_ncc is None


def test_invalid_mask_is_minimal_closed_matrix_row() -> None:
    empty = _mask(lambda _x, _y: False)
    result = _classifier().compare(_input(baseline_mask=empty))
    assert result.unusable_reason is VisualReason.INVALID_MASK
    assert result.roi_pixel_count == 400
    assert result.baseline_mask_pixel_count is None
    assert result.mask_iou is None
    assert result.roi_luma_ncc is None


def test_luma_ncc_zero_denominator_is_not_absent() -> None:
    assert mean_centered_ncc((1.0, 1.0), (1.0, 2.0)) is None


def test_ratio_zero_denominator_is_operational() -> None:
    with pytest.raises(ClassificationOperationalError) as raised:
        _ = ratio(1, 0)
    assert raised.value.reason is ClassificationFailureReason.INVALID_NUMERIC_INPUT


@pytest.mark.parametrize(
    "roi",
    [
        ConfirmationRoi.model_construct(
            x=-1,
            y=1,
            width=20,
            height=20,
            coordinate_space="source_pixels",
            provenance=RoiProvenance.MANUAL,
        ),
        ConfirmationRoi(
            x=4,
            y=4,
            width=20,
            height=20,
            coordinate_space="source_pixels",
            provenance=RoiProvenance.MANUAL,
        ),
    ],
)
def test_invalid_geometry_fails_operationally(roi: ConfirmationRoi) -> None:
    with pytest.raises(ClassificationOperationalError) as raised:
        _ = ObjectPresenceClassifier(
            ObjectPresenceDecisionPolicy(minimum_mask_overlap_for_comparison=0.1)
        ).compare(replace(_input(), roi=roi))
    assert raised.value.reason is ClassificationFailureReason.INVALID_GEOMETRY


def test_source_dimensions_mismatch_fails_operationally() -> None:
    with pytest.raises(ClassificationOperationalError) as raised:
        _ = _classifier().compare(
            _input(
                probe_image=DecodedRgbImage.from_rows(
                    tuple(tuple((0, 0, 0) for _ in range(21)) for _ in range(22))
                )
            )
        )
    assert raised.value.reason is ClassificationFailureReason.INVALID_INPUT_SHAPE


def test_mask_dimensions_mismatch_fails_operationally() -> None:
    mask = BinaryMask.from_rows(tuple(tuple(True for _ in range(21)) for _ in range(22)))
    with pytest.raises(ClassificationOperationalError) as raised:
        _ = _classifier().compare(_input(baseline_mask=mask))
    assert raised.value.reason is ClassificationFailureReason.INVALID_MASK_STRUCTURE


def test_non_source_roi_space_fails_operationally() -> None:
    roi = _roi().model_copy(update={"coordinate_space": "normalized"})
    with pytest.raises(ClassificationOperationalError) as raised:
        _ = _classifier().compare(replace(_input(), roi=roi))
    assert raised.value.reason is ClassificationFailureReason.INVALID_GEOMETRY


def test_inputs_are_not_mutated() -> None:
    values = _input()
    baseline_before = values.baseline_image.pixels
    mask_before = values.baseline_mask.rows
    _ = _classifier().compare(values)
    assert values.baseline_image.pixels == baseline_before
    assert values.baseline_mask.rows == mask_before


def test_policy_identity_is_deterministic_and_field_sensitive() -> None:
    policy = ObjectPresenceDecisionPolicy(minimum_mask_overlap_for_comparison=0.1)
    same = policy.model_copy(deep=True)
    changed = policy.model_copy(update={"minimum_comparison_area": 65})
    changed_overlap = policy.model_copy(update={"minimum_mask_overlap_for_comparison": 0.2})
    changed_preprocessing = policy.model_copy(
        update={"classifier_preprocessing_version": "other-v1"}
    )
    assert policy.identity == same.identity
    assert policy.identity != changed.identity
    assert policy.identity != changed_overlap.identity
    assert policy.identity != changed_preprocessing.identity


def test_policy_rejects_non_finite_overlap() -> None:
    with pytest.raises(ValidationError):
        _ = ObjectPresenceDecisionPolicy(minimum_mask_overlap_for_comparison=float("nan"))


def test_policy_rejects_infinite_coverage() -> None:
    with pytest.raises(ValidationError):
        _ = ObjectPresenceDecisionPolicy(
            minimum_mask_overlap_for_comparison=0.1,
            maximum_roi_mask_coverage_ratio=float("inf"),
        )


def test_strict_matrix_rejects_unknown_and_forbidden_fields() -> None:
    payload = {
        "baseline_mask_pixel_count": None,
        "probe_mask_pixel_count": None,
        "roi_pixel_count": 100,
        "mask_intersection_pixel_count": None,
        "mask_union_pixel_count": None,
        "baseline_mask_coverage": None,
        "probe_mask_coverage": None,
        "mask_iou": None,
        "effective_comparison_area": None,
        "roi_luma_ncc": None,
        "visual_status": "unusable",
        "unusable_reason": "invalid_mask",
        "unexpected": True,
    }
    with pytest.raises(ValidationError):
        _ = RawComparison.model_validate(payload)


def test_strict_matrix_rejects_inconsistent_measurements() -> None:
    with pytest.raises(ValidationError):
        _ = RawComparison.model_validate(
            {**_comparable(roi_luma_ncc=0.3).model_dump(), "mask_iou": 0.3}
        )


def test_strict_matrix_rejects_invalid_ncc_domain() -> None:
    with pytest.raises(ValidationError):
        _ = RawComparison.model_validate(
            {**_comparable(roi_luma_ncc=0.3).model_dump(), "roi_luma_ncc": 1.1}
        )


def test_strict_models_reject_unknown_outcome_and_reason() -> None:
    with pytest.raises(ValidationError):
        _ = ClassificationResult.model_validate(
            {
                "outcome": "MAYBE",
                "reason_code": None,
                "comparison": _comparable(roi_luma_ncc=0.3).model_dump(),
            }
        )
    with pytest.raises(ValidationError):
        _ = RawComparison.model_validate(
            {
                **_comparable(roi_luma_ncc=0.3).model_dump(),
                "unusable_reason": "mystery",
            }
        )


def test_identical_inputs_and_policy_are_deterministic() -> None:
    classifier = _classifier()
    first = classifier.classify(_input(probe_image=_image()))
    second = classifier.classify(_input(probe_image=_image()))
    assert first == second


def test_binarize_malformed_shape_is_operational() -> None:
    with pytest.raises(ClassificationOperationalError) as raised:
        _ = binarize_mask_logits(((0.0,), (0.0, 0.0)))
    assert raised.value.reason is ClassificationFailureReason.INVALID_CLASSIFIER_OUTPUT


def test_unsupported_rgb_channel_layout_is_rejected() -> None:
    invalid = object.__new__(DecodedRgbImage)
    object.__setattr__(invalid, "pixels", (((0, 0, 0, 0),),))
    with pytest.raises(ValueError, match=r"^$"):
        invalid.__post_init__()


def test_operational_failure_cannot_be_a_visual_result() -> None:
    with pytest.raises(ClassificationOperationalError):
        _ = binarize_mask_logits(((float("nan"),),))
    assert ClassificationResult is not None
