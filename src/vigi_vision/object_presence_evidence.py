"""Strict Phase 7B raw-comparison matrix and pure result model."""

from __future__ import annotations

import math
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator

from vigi_vision.object_presence_values import (
    ClassificationOutcome,
    VisualReason,
    VisualStatus,
    quantize_metric,
)


class RawComparison(BaseModel):
    """Strict closed evidence matrix for one successful visual comparison."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )

    baseline_mask_pixel_count: StrictInt | None
    probe_mask_pixel_count: StrictInt | None
    roi_pixel_count: StrictInt = Field(gt=0)
    mask_intersection_pixel_count: StrictInt | None
    mask_union_pixel_count: StrictInt | None
    baseline_mask_coverage: StrictFloat | None
    probe_mask_coverage: StrictFloat | None
    mask_iou: StrictFloat | None
    effective_comparison_area: StrictInt | None
    roi_luma_ncc: StrictFloat | None
    visual_status: VisualStatus
    unusable_reason: VisualReason | None

    @model_validator(mode="after")
    def validate_closed_matrix(self) -> RawComparison:
        """Reject non-finite values and every forbidden field combination."""
        _validate_metrics(self)
        _validate_consistency(self)
        match self.visual_status:
            case VisualStatus.COMPARABLE:
                if self.unusable_reason is not None:
                    raise ValueError
                _require_complete_comparable(self)
            case VisualStatus.UNUSABLE:
                if self.unusable_reason is None:
                    raise ValueError
                if self.unusable_reason is VisualReason.INSUFFICIENT_VISUAL_EVIDENCE:
                    raise ValueError
                _validate_unusable_row(self, self.unusable_reason)
        return self

    def validate_against(self, minimum_mask_overlap: float) -> RawComparison:
        """Validate policy-dependent overlap terminal rows."""
        if not math.isfinite(minimum_mask_overlap) or not 0.0 <= minimum_mask_overlap <= 1.0:
            raise ValueError
        if self.unusable_reason is VisualReason.INSUFFICIENT_MASK_OVERLAP and (
            self.mask_iou is None or self.mask_iou >= minimum_mask_overlap
        ):
            raise ValueError
        if self.unusable_reason is VisualReason.INSUFFICIENT_COMPARISON_AREA and (
            self.mask_iou is None or self.mask_iou < minimum_mask_overlap
        ):
            raise ValueError
        return self


class ClassificationResult(BaseModel):
    """Pure successful classification with its bounded raw evidence."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )

    outcome: ClassificationOutcome
    reason_code: VisualReason | None
    comparison: RawComparison

    @model_validator(mode="after")
    def validate_outcome(self) -> ClassificationResult:
        """Require the closed outcome/reason relationship."""
        match self.outcome:
            case ClassificationOutcome.INDETERMINATE:
                if self.reason_code is None:
                    raise ValueError
                if self.comparison.visual_status is VisualStatus.COMPARABLE:
                    if self.reason_code is not VisualReason.INSUFFICIENT_VISUAL_EVIDENCE:
                        raise ValueError
                elif self.reason_code is not self.comparison.unusable_reason:
                    raise ValueError
            case ClassificationOutcome.PRESENT | ClassificationOutcome.ABSENT:
                if (
                    self.reason_code is not None
                    or self.comparison.visual_status is VisualStatus.UNUSABLE
                ):
                    raise ValueError
        return self

    @property
    def raw_comparison(self) -> RawComparison:
        """Expose evidence under the design's descriptive name."""
        return self.comparison


def _validate_metrics(comparison: RawComparison) -> None:
    _validate_metric_ranges(comparison)
    _validate_count_ranges(comparison)


def _validate_metric_ranges(comparison: RawComparison) -> None:
    for value in (
        comparison.baseline_mask_coverage,
        comparison.probe_mask_coverage,
        comparison.mask_iou,
        comparison.roi_luma_ncc,
    ):
        if value is not None and not math.isfinite(value):
            raise ValueError
    for value in (
        comparison.baseline_mask_coverage,
        comparison.probe_mask_coverage,
        comparison.mask_iou,
    ):
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError
    if comparison.roi_luma_ncc is not None and not -1.0 <= comparison.roi_luma_ncc <= 1.0:
        raise ValueError


def _validate_count_ranges(comparison: RawComparison) -> None:
    for value in (
        comparison.baseline_mask_pixel_count,
        comparison.probe_mask_pixel_count,
        comparison.mask_intersection_pixel_count,
        comparison.mask_union_pixel_count,
        comparison.effective_comparison_area,
    ):
        if value is not None and value < 0:
            raise ValueError
        if value is not None and value > comparison.roi_pixel_count:
            raise ValueError
    if comparison.effective_comparison_area == 0:
        raise ValueError
    if (
        comparison.mask_intersection_pixel_count is not None
        and comparison.mask_union_pixel_count is not None
        and comparison.mask_intersection_pixel_count > comparison.mask_union_pixel_count
    ):
        raise ValueError


def _require_complete_comparable(comparison: RawComparison) -> None:
    required = (
        comparison.baseline_mask_pixel_count,
        comparison.probe_mask_pixel_count,
        comparison.mask_intersection_pixel_count,
        comparison.mask_union_pixel_count,
        comparison.baseline_mask_coverage,
        comparison.probe_mask_coverage,
        comparison.mask_iou,
        comparison.effective_comparison_area,
        comparison.roi_luma_ncc,
    )
    if any(value is None for value in required):
        raise ValueError
    if comparison.effective_comparison_area != comparison.mask_intersection_pixel_count:
        raise ValueError


def _validate_consistency(comparison: RawComparison) -> None:
    _validate_coverages(comparison)
    _validate_pairwise_metrics(comparison)
    _validate_effective_area(comparison)


def _validate_coverages(comparison: RawComparison) -> None:
    counts = (
        comparison.baseline_mask_pixel_count,
        comparison.probe_mask_pixel_count,
    )
    coverages = (
        comparison.baseline_mask_coverage,
        comparison.probe_mask_coverage,
    )
    for count, coverage in zip(counts, coverages, strict=True):
        if count is not None and count == 0:
            raise ValueError
        if (
            count is not None
            and coverage is not None
            and coverage != quantize_metric(count / comparison.roi_pixel_count)
        ):
            raise ValueError


def _validate_pairwise_metrics(comparison: RawComparison) -> None:
    counts = (
        comparison.baseline_mask_pixel_count,
        comparison.probe_mask_pixel_count,
    )
    intersection = comparison.mask_intersection_pixel_count
    union = comparison.mask_union_pixel_count
    if intersection is not None and union is not None:
        if counts[0] is None or counts[1] is None:
            raise ValueError
        if union == 0 or union != counts[0] + counts[1] - intersection:
            raise ValueError
        if comparison.mask_iou is not None and comparison.mask_iou != quantize_metric(
            intersection / union
        ):
            raise ValueError


def _validate_effective_area(comparison: RawComparison) -> None:
    if (
        comparison.effective_comparison_area is not None
        and comparison.mask_intersection_pixel_count is not None
        and comparison.effective_comparison_area != comparison.mask_intersection_pixel_count
    ):
        raise ValueError


def _validate_unusable_row(comparison: RawComparison, reason: VisualReason) -> None:
    fields = {
        "baseline_mask_pixel_count": comparison.baseline_mask_pixel_count,
        "probe_mask_pixel_count": comparison.probe_mask_pixel_count,
        "mask_intersection_pixel_count": comparison.mask_intersection_pixel_count,
        "mask_union_pixel_count": comparison.mask_union_pixel_count,
        "baseline_mask_coverage": comparison.baseline_mask_coverage,
        "probe_mask_coverage": comparison.probe_mask_coverage,
        "mask_iou": comparison.mask_iou,
        "effective_comparison_area": comparison.effective_comparison_area,
        "roi_luma_ncc": comparison.roi_luma_ncc,
    }
    allowed_by_reason: dict[VisualReason, set[str]] = {
        VisualReason.INVALID_MASK: set(),
        VisualReason.BACKGROUND_DOMINANT: {
            "baseline_mask_pixel_count",
            "probe_mask_pixel_count",
            "baseline_mask_coverage",
            "probe_mask_coverage",
        },
        VisualReason.INSUFFICIENT_MASK_OVERLAP: {
            "baseline_mask_pixel_count",
            "probe_mask_pixel_count",
            "mask_intersection_pixel_count",
            "mask_union_pixel_count",
            "baseline_mask_coverage",
            "probe_mask_coverage",
            "mask_iou",
        },
        VisualReason.INSUFFICIENT_COMPARISON_AREA: {
            "baseline_mask_pixel_count",
            "probe_mask_pixel_count",
            "mask_intersection_pixel_count",
            "mask_union_pixel_count",
            "baseline_mask_coverage",
            "probe_mask_coverage",
            "mask_iou",
            "effective_comparison_area",
        },
        VisualReason.ZERO_LUMA_VARIANCE: {
            "baseline_mask_pixel_count",
            "probe_mask_pixel_count",
            "mask_intersection_pixel_count",
            "mask_union_pixel_count",
            "baseline_mask_coverage",
            "probe_mask_coverage",
            "mask_iou",
            "effective_comparison_area",
        },
        VisualReason.INSUFFICIENT_VISUAL_EVIDENCE: set(),
    }
    allowed = allowed_by_reason[reason]
    if any(name not in allowed and value is not None for name, value in fields.items()):
        raise ValueError
    if reason in {
        VisualReason.BACKGROUND_DOMINANT,
        VisualReason.INSUFFICIENT_MASK_OVERLAP,
        VisualReason.INSUFFICIENT_COMPARISON_AREA,
        VisualReason.ZERO_LUMA_VARIANCE,
    } and any(fields[name] is None for name in allowed):
        raise ValueError
