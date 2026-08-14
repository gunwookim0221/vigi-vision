"""Pure Phase 7B classifier entry point and logits preparation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from vigi_vision.object_presence_evidence import RawComparison
from vigi_vision.object_presence_metrics import (
    ComparisonMeasurements,
    background_dominant,
    clipped_masks,
    contains_prompt,
    fail,
    invalid_mask,
    mask_count,
    mask_intersection,
    mean_centered_ncc,
    ratio,
    roi_luma,
    unusable,
)
from vigi_vision.object_presence_models import (
    BinaryMask,
    ClassificationFailureReason,
    ClassificationOperationalError,
    ClassificationResult,
    DecodedRgbImage,
    VisualReason,
    VisualStatus,
    quantize_metric,
)

if TYPE_CHECKING:
    from vigi_vision.investigation_confirmation_models import ConfirmationRoi
    from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy


@dataclass(frozen=True, slots=True)
class ClassifierInput:
    """Already decoded, source-aligned values accepted by the pure classifier."""

    baseline_image: DecodedRgbImage
    probe_image: DecodedRgbImage
    baseline_mask: BinaryMask
    probe_mask: BinaryMask
    roi: ConfirmationRoi


@dataclass(frozen=True, slots=True)
class ObjectPresenceClassifier:
    """Deterministic in-memory mask and aligned-ROI comparison."""

    policy: ObjectPresenceDecisionPolicy

    def compare(self, values: ClassifierInput) -> RawComparison:
        """Return one validated raw comparison without performing I/O."""
        _validate_input(values)
        roi_pixels = values.roi.width * values.roi.height
        baseline_mask, probe_mask = clipped_masks(
            values.baseline_mask.rows, values.probe_mask.rows, values.roi
        )
        measurements, terminal = _measure_masks(
            baseline_mask, probe_mask, values.roi, self.policy, roi_pixels
        )
        if terminal is not None:
            return terminal
        if measurements is None:
            fail(ClassificationFailureReason.INVALID_CLASSIFIER_OUTPUT)
        ncc = mean_centered_ncc(
            roi_luma(values.baseline_image, values.roi),
            roi_luma(values.probe_image, values.roi),
        )
        if ncc is None:
            return unusable(measurements, VisualReason.ZERO_LUMA_VARIANCE)
        return RawComparison(
            baseline_mask_pixel_count=measurements.baseline_mask_pixel_count,
            probe_mask_pixel_count=measurements.probe_mask_pixel_count,
            roi_pixel_count=measurements.roi_pixel_count,
            mask_intersection_pixel_count=measurements.mask_intersection_pixel_count,
            mask_union_pixel_count=measurements.mask_union_pixel_count,
            baseline_mask_coverage=measurements.baseline_mask_coverage,
            probe_mask_coverage=measurements.probe_mask_coverage,
            mask_iou=measurements.mask_iou,
            effective_comparison_area=measurements.effective_comparison_area,
            roi_luma_ncc=quantize_metric(ncc),
            visual_status=VisualStatus.COMPARABLE,
            unusable_reason=None,
        )

    def classify(self, values: ClassifierInput) -> ClassificationResult:
        """Return the conservative three-state result for one in-memory input."""
        return self.policy.decide(self.compare(values))


def binarize_mask_logits(logits: object, threshold: float = 0.0) -> BinaryMask:
    """Apply the inclusive threshold to finite source-sized logits."""
    if not math.isfinite(threshold):
        fail(ClassificationFailureReason.INVALID_NUMERIC_INPUT)
    try:
        if not isinstance(logits, Sequence):
            fail(ClassificationFailureReason.INVALID_CLASSIFIER_OUTPUT)
        rows = tuple(_binarize_row(row, threshold) for row in logits)
        return BinaryMask.from_rows(rows)
    except (TypeError, ValueError) as error:
        raise ClassificationOperationalError(
            ClassificationFailureReason.INVALID_CLASSIFIER_OUTPUT
        ) from error


def _binarize_row(row: object, threshold: float) -> tuple[bool, ...]:
    if not isinstance(row, Sequence):
        fail(ClassificationFailureReason.INVALID_CLASSIFIER_OUTPUT)
    return tuple(_logit_is_positive(value, threshold) for value in row)


def _logit_is_positive(value: object, threshold: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        fail(ClassificationFailureReason.INVALID_NUMERIC_INPUT)
    return value >= threshold


def _validate_input(values: ClassifierInput) -> None:
    if (
        values.baseline_image.width != values.probe_image.width
        or values.baseline_image.height != values.probe_image.height
    ):
        fail(ClassificationFailureReason.INVALID_INPUT_SHAPE)
    for mask in (values.baseline_mask, values.probe_mask):
        if mask.width != values.baseline_image.width or mask.height != values.baseline_image.height:
            fail(ClassificationFailureReason.INVALID_MASK_STRUCTURE)
    if (
        values.roi.coordinate_space != "source_pixels"
        or values.roi.x < 0
        or values.roi.y < 0
        or values.roi.x + values.roi.width > values.baseline_image.width
        or values.roi.y + values.roi.height > values.baseline_image.height
    ):
        fail(ClassificationFailureReason.INVALID_GEOMETRY)


def _measure_masks(
    baseline: tuple[tuple[bool, ...], ...],
    probe: tuple[tuple[bool, ...], ...],
    roi: ConfirmationRoi,
    policy: ObjectPresenceDecisionPolicy,
    roi_pixels: int,
) -> tuple[ComparisonMeasurements | None, RawComparison | None]:
    terminal: RawComparison | None = None
    measurements: ComparisonMeasurements | None = None
    baseline_count = mask_count(baseline)
    probe_count = mask_count(probe)
    if not contains_prompt(baseline, roi) or not contains_prompt(probe, roi):
        terminal = invalid_mask(roi_pixels)
    else:
        baseline_coverage = ratio(baseline_count, roi_pixels)
        probe_coverage = ratio(probe_count, roi_pixels)
        measurements = ComparisonMeasurements(
            roi_pixel_count=roi_pixels,
            baseline_mask_pixel_count=baseline_count,
            probe_mask_pixel_count=probe_count,
            baseline_mask_coverage=baseline_coverage,
            probe_mask_coverage=probe_coverage,
        )
        if (
            baseline_coverage >= policy.maximum_roi_mask_coverage_ratio
            or probe_coverage >= policy.maximum_roi_mask_coverage_ratio
        ):
            terminal = background_dominant(measurements)
        elif (
            baseline_count < policy.minimum_clipped_mask_pixels
            or probe_count < policy.minimum_clipped_mask_pixels
        ):
            terminal = invalid_mask(roi_pixels)
        else:
            intersection = mask_intersection(baseline, probe)
            union = baseline_count + probe_count - intersection
            if union <= 0:
                terminal = invalid_mask(roi_pixels)
            else:
                mask_iou = ratio(intersection, union)
                measurements = replace(
                    measurements,
                    mask_intersection_pixel_count=intersection,
                    mask_union_pixel_count=union,
                    mask_iou=mask_iou,
                )
                if mask_iou < policy.minimum_mask_overlap_for_comparison:
                    terminal = unusable(measurements, VisualReason.INSUFFICIENT_MASK_OVERLAP)
                else:
                    measurements = replace(measurements, effective_comparison_area=intersection)
                    if (
                        roi_pixels < policy.minimum_roi_pixels
                        or intersection < policy.minimum_comparison_area
                    ):
                        terminal = unusable(measurements, VisualReason.INSUFFICIENT_COMPARISON_AREA)
    return measurements, terminal
