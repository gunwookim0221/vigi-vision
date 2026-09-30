"""Bounded non-authoritative facts for foreground-retention RCA."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

_FACT_FIELDS = frozenset(
    {
        "baseline_support_pixel_count",
        "retention_support_population_pixel_count",
        "eligible_baseline_foreground_pixel_count",
        "retained_foreground_pixel_count",
        "absolute_floor_rejection_count",
        "relative_retention_rejection_count",
        "baseline_contrast_histogram",
        "probe_contrast_histogram",
        "baseline_background_pixel_count",
        "probe_background_pixel_count",
        "baseline_background_median",
        "probe_background_median",
        "normalization_scale",
        "normalization_offset",
        "normalization_clipped_low_count",
        "normalization_clipped_high_count",
        "absolute_contrast_floor",
        "relative_retention_fraction",
        "present_foreground_retention_threshold",
    }
)
FOREGROUND_RETENTION_FACT_FIELDS = _FACT_FIELDS
_LUMA_MAXIMUM = 255.0
_MIN_NORMALIZATION_SCALE = 0.5
_MAX_NORMALIZATION_SCALE = 2.0
_HISTOGRAM_BUCKET_COUNT = 5


@dataclass(frozen=True, slots=True)
class ForegroundRetentionFacts:
    """Fixed-size numeric aggregates from one selected support comparison."""

    baseline_support_pixel_count: int
    retention_support_population_pixel_count: int
    eligible_baseline_foreground_pixel_count: int
    retained_foreground_pixel_count: int
    absolute_floor_rejection_count: int
    relative_retention_rejection_count: int
    baseline_contrast_histogram: tuple[int, int, int, int, int]
    probe_contrast_histogram: tuple[int, int, int, int, int]
    baseline_background_pixel_count: int
    probe_background_pixel_count: int
    baseline_background_median: float
    probe_background_median: float
    normalization_scale: float
    normalization_offset: float
    normalization_clipped_low_count: int
    normalization_clipped_high_count: int
    absolute_contrast_floor: float
    relative_retention_fraction: float
    present_foreground_retention_threshold: float

    def to_payload(self) -> dict[str, object]:
        """Return the closed JSON-compatible diagnostic payload."""
        return {
            "baseline_support_pixel_count": self.baseline_support_pixel_count,
            "retention_support_population_pixel_count": (
                self.retention_support_population_pixel_count
            ),
            "eligible_baseline_foreground_pixel_count": (
                self.eligible_baseline_foreground_pixel_count
            ),
            "retained_foreground_pixel_count": self.retained_foreground_pixel_count,
            "absolute_floor_rejection_count": self.absolute_floor_rejection_count,
            "relative_retention_rejection_count": self.relative_retention_rejection_count,
            "baseline_contrast_histogram": list(self.baseline_contrast_histogram),
            "probe_contrast_histogram": list(self.probe_contrast_histogram),
            "baseline_background_pixel_count": self.baseline_background_pixel_count,
            "probe_background_pixel_count": self.probe_background_pixel_count,
            "baseline_background_median": self.baseline_background_median,
            "probe_background_median": self.probe_background_median,
            "normalization_scale": self.normalization_scale,
            "normalization_offset": self.normalization_offset,
            "normalization_clipped_low_count": self.normalization_clipped_low_count,
            "normalization_clipped_high_count": self.normalization_clipped_high_count,
            "absolute_contrast_floor": self.absolute_contrast_floor,
            "relative_retention_fraction": self.relative_retention_fraction,
            "present_foreground_retention_threshold": self.present_foreground_retention_threshold,
        }

    @classmethod
    def from_payload(  # noqa: PLR0911
        cls, value: object
    ) -> ForegroundRetentionFacts | None:
        """Validate a fixed-shape worker or repository payload."""
        if not isinstance(value, dict):
            return None
        raw_record = cast("dict[object, object]", value)
        if frozenset(raw_record) != _FACT_FIELDS:
            return None
        record = cast("Mapping[str, object]", raw_record)
        integer_names = (
            "baseline_support_pixel_count",
            "retention_support_population_pixel_count",
            "eligible_baseline_foreground_pixel_count",
            "retained_foreground_pixel_count",
            "absolute_floor_rejection_count",
            "relative_retention_rejection_count",
            "baseline_background_pixel_count",
            "probe_background_pixel_count",
            "normalization_clipped_low_count",
            "normalization_clipped_high_count",
        )
        integer_values = tuple(record[name] for name in integer_names)
        if any(type(item) is not int or item < 0 for item in integer_values):
            return None
        histograms = (
            _histogram(record.get("baseline_contrast_histogram")),
            _histogram(record.get("probe_contrast_histogram")),
        )
        if histograms[0] is None or histograms[1] is None:
            return None
        numeric_values = tuple(
            _finite_float(record[name])
            for name in (
                "baseline_background_median",
                "probe_background_median",
                "normalization_scale",
                "normalization_offset",
                "absolute_contrast_floor",
                "relative_retention_fraction",
                "present_foreground_retention_threshold",
            )
        )
        if any(item is None for item in numeric_values):
            return None
        ints = cast("tuple[int, ...]", integer_values)
        (
            baseline_count,
            population_count,
            eligible_count,
            retained_count,
            floor_count,
            relative_count,
        ) = ints[:6]
        baseline_ring_count, probe_ring_count, clipped_low, clipped_high = ints[6:]
        numeric = numeric_values
        if any(item is None for item in numeric):
            return None
        normalized_values = cast("tuple[float, ...]", numeric)
        baseline_median = normalized_values[0]
        probe_median = normalized_values[1]
        scale = normalized_values[2]
        offset = normalized_values[3]
        floor = normalized_values[4]
        fraction = normalized_values[5]
        present_threshold = normalized_values[6]
        medians = (baseline_median, probe_median, scale, offset)
        if (
            baseline_count < population_count
            or eligible_count > population_count
            or retained_count + floor_count + relative_count != eligible_count
            or not baseline_ring_count
            or not probe_ring_count
            or sum(histograms[0]) != population_count
            or sum(histograms[1]) != population_count
            or clipped_low > population_count
            or clipped_high > population_count
            or not 0.0 <= medians[0] <= _LUMA_MAXIMUM
            or not 0.0 <= medians[1] <= _LUMA_MAXIMUM
            or not _MIN_NORMALIZATION_SCALE <= medians[2] <= _MAX_NORMALIZATION_SCALE
            or floor <= 0.0
            or not 0.0 <= fraction <= 1.0
            or not 0.0 <= present_threshold <= 1.0
        ):
            return None
        return cls(
            baseline_count,
            population_count,
            eligible_count,
            retained_count,
            floor_count,
            relative_count,
            histograms[0],
            histograms[1],
            baseline_ring_count,
            probe_ring_count,
            medians[0],
            medians[1],
            medians[2],
            medians[3],
            clipped_low,
            clipped_high,
            floor,
            fraction,
            present_threshold,
        )


def _histogram(value: object) -> tuple[int, int, int, int, int] | None:
    if not isinstance(value, list):
        return None
    items = cast("list[object]", value)
    if len(items) != _HISTOGRAM_BUCKET_COUNT:
        return None
    if any(type(item) is not int or item < 0 for item in items):
        return None
    values = cast("tuple[int, ...]", tuple(items))
    return values[0], values[1], values[2], values[3], values[4]


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None
