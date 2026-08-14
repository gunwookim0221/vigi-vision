from __future__ import annotations

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
