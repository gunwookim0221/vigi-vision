"""Pure mask, luma, and closed-matrix metric helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from vigi_vision.object_presence_evidence import RawComparison
from vigi_vision.object_presence_models import (
    ClassificationFailureReason,
    ClassificationOperationalError,
    DecodedRgbImage,
    VisualReason,
    VisualStatus,
    quantize_metric,
)

if TYPE_CHECKING:
    from vigi_vision.investigation_confirmation_models import ConfirmationRoi


@dataclass(frozen=True, slots=True)
class ComparisonMeasurements:
    """Measurements available at one closed terminal gate."""

    roi_pixel_count: int
    baseline_mask_pixel_count: int | None = None
    probe_mask_pixel_count: int | None = None
    mask_intersection_pixel_count: int | None = None
    mask_union_pixel_count: int | None = None
    baseline_mask_coverage: float | None = None
    probe_mask_coverage: float | None = None
    mask_iou: float | None = None
    effective_comparison_area: int | None = None
    roi_luma_ncc: float | None = None


def clipped_masks(
    baseline: tuple[tuple[bool, ...], ...],
    probe: tuple[tuple[bool, ...], ...],
    roi: ConfirmationRoi,
) -> tuple[tuple[tuple[bool, ...], ...], tuple[tuple[bool, ...], ...]]:
    """Return exact half-open ROI views without mutating caller data."""
    y_slice = slice(roi.y, roi.y + roi.height)
    x_slice = slice(roi.x, roi.x + roi.width)
    return (
        tuple(row[x_slice] for row in baseline[y_slice]),
        tuple(row[x_slice] for row in probe[y_slice]),
    )


def mask_count(mask: tuple[tuple[bool, ...], ...]) -> int:
    """Count true pixels in a clipped mask."""
    return sum(sum(row) for row in mask)


def contains_prompt(mask: tuple[tuple[bool, ...], ...], roi: ConfirmationRoi) -> bool:
    """Check the deterministic center prompt in a clipped mask."""
    return mask[(roi.height - 1) // 2][(roi.width - 1) // 2]


def mask_intersection(
    baseline: tuple[tuple[bool, ...], ...], probe: tuple[tuple[bool, ...], ...]
) -> int:
    """Count exact boolean intersection pixels."""
    return sum(
        1
        for baseline_row, probe_row in zip(baseline, probe, strict=True)
        for baseline_value, probe_value in zip(baseline_row, probe_row, strict=True)
        if baseline_value and probe_value
    )


def roi_luma(image: DecodedRgbImage, roi: ConfirmationRoi) -> tuple[float, ...]:
    """Extract deterministic integer-rounded luma for every ROI pixel."""
    return tuple(
        float((299 * red + 587 * green + 114 * blue + 500) // 1000)
        for row in image.pixels[roi.y : roi.y + roi.height]
        for red, green, blue in row[roi.x : roi.x + roi.width]
    )


def mean_centered_ncc(baseline: tuple[float, ...], probe: tuple[float, ...]) -> float | None:
    """Compute float64 mean-centered NCC, or None for zero variance."""
    if len(baseline) != len(probe) or not baseline:
        fail(ClassificationFailureReason.INVALID_INPUT_SHAPE)
    baseline_mean = sum(baseline) / len(baseline)
    probe_mean = sum(probe) / len(probe)
    centered_baseline = tuple(value - baseline_mean for value in baseline)
    centered_probe = tuple(value - probe_mean for value in probe)
    baseline_variance = sum(value * value for value in centered_baseline)
    probe_variance = sum(value * value for value in centered_probe)
    if not math.isfinite(baseline_variance) or not math.isfinite(probe_variance):
        fail(ClassificationFailureReason.INVALID_NUMERIC_INPUT)
    if baseline_variance <= 0.0 or probe_variance <= 0.0:
        return None
    numerator = sum(
        left * right for left, right in zip(centered_baseline, centered_probe, strict=True)
    )
    ncc = numerator / math.sqrt(baseline_variance * probe_variance)
    if not math.isfinite(ncc) or not -1.0 <= ncc <= 1.0:
        fail(ClassificationFailureReason.INVALID_NUMERIC_INPUT)
    return ncc


def unusable(measurements: ComparisonMeasurements, reason: VisualReason) -> RawComparison:
    """Build one matrix row with only measurements valid at its terminal gate."""
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
        roi_luma_ncc=measurements.roi_luma_ncc,
        visual_status=VisualStatus.UNUSABLE,
        unusable_reason=reason,
    )


def invalid_mask(roi_pixel_count: int) -> RawComparison:
    """Return the minimal invalid-mask matrix row."""
    return unusable(ComparisonMeasurements(roi_pixel_count), VisualReason.INVALID_MASK)


def background_dominant(measurements: ComparisonMeasurements) -> RawComparison:
    """Return the background-dominant matrix row."""
    return unusable(measurements, VisualReason.BACKGROUND_DOMINANT)


def fail(reason: ClassificationFailureReason) -> NoReturn:
    """Raise one fixed operational classification failure."""
    raise ClassificationOperationalError(reason)


def ratio(value: int, total: int) -> float:
    """Compute one finite quantized ratio."""
    try:
        return quantize_metric(value / total)
    except (ArithmeticError, ValueError) as error:
        raise ClassificationOperationalError(
            ClassificationFailureReason.INVALID_NUMERIC_INPUT
        ) from error
