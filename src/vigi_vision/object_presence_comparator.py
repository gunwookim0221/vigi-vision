"""Pure Phase 7B classifier entry point and logits preparation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

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


_SUPPORT_CHANGE_THRESHOLD: Final[float] = 32.0


@dataclass(frozen=True, slots=True)
class ObjectPresenceClassifier:
    """Deterministic in-memory mask and aligned-ROI comparison."""

    policy: ObjectPresenceDecisionPolicy

    def compare(self, values: ClassifierInput) -> RawComparison:
        """Return one validated raw comparison without performing I/O."""
        _validate_input(values)
        if self.policy.baseline_support_mode:
            return _compare_with_baseline_support(values, self.policy)
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


def _compare_with_baseline_support(
    values: ClassifierInput, policy: ObjectPresenceDecisionPolicy
) -> RawComparison:
    """Compare probe pixels against the immutable baseline mask support.

    The independently predicted probe mask remains diagnostic evidence only;
    it never defines the target identity or gates the support comparison.
    """
    roi_pixels = values.roi.width * values.roi.height
    baseline_mask, probe_mask = clipped_masks(
        values.baseline_mask.rows, values.probe_mask.rows, values.roi
    )
    baseline_count = mask_count(baseline_mask)
    if not contains_prompt(baseline_mask, values.roi):
        return invalid_mask(roi_pixels)
    baseline_coverage = ratio(baseline_count, roi_pixels)
    if baseline_coverage >= policy.maximum_roi_mask_coverage_ratio:
        return background_dominant(
            ComparisonMeasurements(
                roi_pixels,
                baseline_count,
                None,
                None,
                None,
                baseline_coverage,
                None,
                None,
                None,
                None,
            )
        )
    if baseline_count < policy.minimum_clipped_mask_pixels:
        return invalid_mask(roi_pixels)

    probe_count = mask_count(probe_mask)
    probe_coverage = ratio(probe_count, roi_pixels) if probe_count else None
    intersection = mask_intersection(baseline_mask, probe_mask)
    union = baseline_count + probe_count - intersection
    mask_iou = ratio(intersection, union) if union else None
    baseline_luma = roi_luma(values.baseline_image, values.roi)
    probe_luma = roi_luma(values.probe_image, values.roi)
    support_indices = tuple(
        index
        for index, present in enumerate(value for row in baseline_mask for value in row)
        if present
    )
    background_indices = tuple(
        index
        for index, present in enumerate(value for row in baseline_mask for value in row)
        if not present
    )
    support_baseline = tuple(baseline_luma[index] for index in support_indices)
    support_probe = tuple(probe_luma[index] for index in support_indices)
    background_baseline = tuple(baseline_luma[index] for index in background_indices)
    background_probe = tuple(probe_luma[index] for index in background_indices)
    support_luma_ncc_raw = mean_centered_ncc(support_baseline, support_probe)
    support_luma_ncc = (
        None if support_luma_ncc_raw is None else quantize_metric(support_luma_ncc_raw)
    )
    (
        support_luma_similarity,
        change_ratio,
        foreground_retention,
        background_change_ratio,
        normalized_probe,
    ) = _support_luma_metrics(
        support_baseline,
        support_probe,
        background_baseline,
        background_probe,
    )
    edge_similarity = _support_edge_similarity(
        baseline_luma,
        baseline_mask,
        values.roi.width,
        normalized_probe,
        support_indices,
    )
    roi_ncc_raw = mean_centered_ncc(baseline_luma, probe_luma)
    roi_ncc = None if roi_ncc_raw is None else quantize_metric(roi_ncc_raw)
    if (
        support_luma_similarity is None
        or support_luma_ncc is None
        or change_ratio is None
        or foreground_retention is None
        or background_change_ratio is None
    ):
        return unusable(
            ComparisonMeasurements(
                roi_pixels,
                baseline_count,
                probe_count or None,
                intersection if probe_count else None,
                union if probe_count else None,
                baseline_coverage,
                probe_coverage,
                mask_iou,
                None,
                roi_ncc,
            ),
            VisualReason.ZERO_LUMA_VARIANCE,
        )
    return RawComparison(
        baseline_mask_pixel_count=baseline_count,
        probe_mask_pixel_count=probe_count or None,
        roi_pixel_count=roi_pixels,
        mask_intersection_pixel_count=intersection if probe_count else None,
        mask_union_pixel_count=union if probe_count else None,
        baseline_mask_coverage=baseline_coverage,
        probe_mask_coverage=probe_coverage,
        mask_iou=mask_iou,
        effective_comparison_area=None,
        roi_luma_ncc=roi_ncc,
        visual_status=VisualStatus.COMPARABLE,
        unusable_reason=None,
        comparison_mode="baseline_support_v1",
        baseline_support_pixel_count=baseline_count,
        baseline_support_luma_similarity=support_luma_similarity,
        baseline_support_luma_ncc=support_luma_ncc,
        baseline_support_edge_similarity=edge_similarity,
        baseline_support_change_ratio=change_ratio,
        baseline_support_foreground_retention=foreground_retention,
        baseline_support_background_change_ratio=background_change_ratio,
    )


def _support_luma_metrics(
    baseline: tuple[float, ...],
    probe: tuple[float, ...],
    baseline_background: tuple[float, ...],
    probe_background: tuple[float, ...],
) -> tuple[float | None, float | None, float | None, float | None, tuple[float, ...]]:
    if (
        not baseline
        or len(baseline) != len(probe)
        or not baseline_background
        or not probe_background
    ):
        return None, None, None, None, ()
    baseline_background_mean = sum(baseline_background) / len(baseline_background)
    probe_background_mean = sum(probe_background) / len(probe_background)
    baseline_variance = sum(
        (value - baseline_background_mean) ** 2 for value in baseline_background
    )
    probe_variance = sum((value - probe_background_mean) ** 2 for value in probe_background)
    if not math.isfinite(baseline_variance) or not math.isfinite(probe_variance):
        return None, None, None, None, ()
    if probe_variance <= 0.0 or baseline_variance <= 0.0:
        scale = 1.0
    else:
        scale = math.sqrt(baseline_variance / probe_variance)
        scale = min(2.0, max(0.5, scale))
    normalized = tuple(
        min(
            255.0,
            max(0.0, (value - probe_background_mean) * scale + baseline_background_mean),
        )
        for value in probe
    )
    normalized_background = tuple(
        min(
            255.0,
            max(0.0, (value - probe_background_mean) * scale + baseline_background_mean),
        )
        for value in probe_background
    )
    mean_difference = sum(
        abs(left - right) for left, right in zip(baseline, normalized, strict=True)
    ) / len(baseline)
    similarity = quantize_metric(max(0.0, 1.0 - mean_difference / 255.0))
    changed = sum(
        abs(left - right) > _SUPPORT_CHANGE_THRESHOLD
        for left, right in zip(baseline, normalized, strict=True)
    )
    background_changed = sum(
        abs(left - right) > _SUPPORT_CHANGE_THRESHOLD
        for left, right in zip(baseline_background, normalized_background, strict=True)
    )
    baseline_contrast = sum(abs(value - baseline_background_mean) for value in baseline) / len(
        baseline
    )
    probe_contrast = sum(abs(value - baseline_background_mean) for value in normalized) / len(
        normalized
    )
    if baseline_contrast <= 0.0 or not math.isfinite(probe_contrast):
        foreground_retention = None
    else:
        foreground_retention = quantize_metric(
            min(1.0, max(0.0, probe_contrast / baseline_contrast))
        )
    return (
        similarity,
        quantize_metric(changed / len(baseline)),
        foreground_retention,
        quantize_metric(background_changed / len(baseline_background)),
        normalized,
    )


def _support_edge_similarity(
    baseline_luma: tuple[float, ...],
    support: tuple[tuple[bool, ...], ...],
    width: int,
    normalized_probe: tuple[float, ...],
    support_indices: tuple[int, ...],
) -> float | None:
    if not support_indices:
        return None
    support_set = set(support_indices)
    normalized_by_index = dict(zip(support_indices, normalized_probe, strict=True))
    gradients: list[tuple[float, float]] = []
    height = len(support)
    for y in range(height):
        for x in range(width):
            index = y * width + x
            if index not in support_set:
                continue
            if x + 1 < width and (index + 1) in support_set:
                gradients.append(
                    (
                        abs(baseline_luma[index] - baseline_luma[index + 1]),
                        abs(normalized_by_index[index] - normalized_by_index[index + 1]),
                    )
                )
    if not gradients:
        return None
    return quantize_metric(
        max(0.0, 1.0 - sum(abs(left - right) for left, right in gradients) / len(gradients) / 255.0)
    )
