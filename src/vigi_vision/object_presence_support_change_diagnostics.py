"""Bounded non-authoritative facts for support-change RCA."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict, cast

from vigi_vision.object_presence_values import quantize_metric

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vigi_vision.object_presence_values import BinaryMask


SUPPORT_CHANGE_FACT_FIELDS = frozenset(
    {
        "baseline_support_pixel_count",
        "valid_support_pixel_count",
        "excluded_support_pixel_count",
        "changed_support_pixel_count",
        "unchanged_support_pixel_count",
        "support_change_ratio",
        "difference_le_24_count",
        "difference_gt_24_le_30_count",
        "difference_gt_30_le_32_count",
        "difference_gt_32_le_34_count",
        "difference_gt_34_le_40_count",
        "difference_gt_40_count",
        "raw_probe_changed_count",
        "normalized_preclip_changed_count",
        "normalized_postclip_changed_count",
        "clipped_low_count",
        "clipped_high_count",
        "non_clipped_count",
        "changed_and_clipped_low_count",
        "changed_and_clipped_high_count",
        "changed_and_non_clipped_count",
        "baseline_background_pixel_count",
        "probe_background_pixel_count",
        "baseline_background_median",
        "probe_background_median",
        "normalization_scale",
        "normalization_offset",
        "alignment_dx",
        "alignment_dy",
        "alignment_rotation_degrees",
        "alignment_overlap",
        "alignment_state",
        "alignment_margin",
        "support_mask_fingerprint",
        "baseline_mask_width",
        "baseline_mask_height",
    }
)

_COUNT_FIELDS = tuple(
    sorted(
        SUPPORT_CHANGE_FACT_FIELDS
        - {
            "support_change_ratio",
            "baseline_background_median",
            "probe_background_median",
            "normalization_scale",
            "normalization_offset",
            "alignment_dx",
            "alignment_dy",
            "alignment_rotation_degrees",
            "alignment_overlap",
            "alignment_state",
            "alignment_margin",
            "support_mask_fingerprint",
            "baseline_mask_width",
            "baseline_mask_height",
        }
    )
)
_HISTOGRAM_FIELDS = (
    "difference_le_24_count",
    "difference_gt_24_le_30_count",
    "difference_gt_30_le_32_count",
    "difference_gt_32_le_34_count",
    "difference_gt_34_le_40_count",
    "difference_gt_40_count",
)
_FLOAT_FIELDS = (
    "support_change_ratio",
    "baseline_background_median",
    "probe_background_median",
    "normalization_scale",
    "normalization_offset",
    "alignment_overlap",
    "alignment_margin",
)
_ALIGNMENT_STATES = frozenset({"aligned", "ambiguous", "not_required", "no_valid_candidate"})
_LUMA_MAXIMUM = 255.0
_MIN_SCALE = 0.5
_MAX_SCALE = 2.0
_SHA256_HEX_LENGTH = 64
_MIN_NORMALIZATION_OFFSET = -2.0 * _LUMA_MAXIMUM
_MAX_NORMALIZATION_OFFSET = _LUMA_MAXIMUM
_MAX_SUPPORT_MASK_PIXELS = 256 * 1024 * 1024
_MAX_ALIGNMENT_TRANSLATION = 16
_MAX_ALIGNMENT_MARGIN = 2.0
_ALIGNMENT_ROTATIONS = frozenset({-10, -5, 0, 5, 10})
_MASK_BITS_PER_BYTE = 8
_MASK_FINGERPRINT_CHUNK_BYTES = 4096


class _SupportChangePayload(TypedDict):
    baseline_support_pixel_count: int
    valid_support_pixel_count: int
    excluded_support_pixel_count: int
    changed_support_pixel_count: int
    unchanged_support_pixel_count: int
    support_change_ratio: float
    difference_le_24_count: int
    difference_gt_24_le_30_count: int
    difference_gt_30_le_32_count: int
    difference_gt_32_le_34_count: int
    difference_gt_34_le_40_count: int
    difference_gt_40_count: int
    raw_probe_changed_count: int
    normalized_preclip_changed_count: int
    normalized_postclip_changed_count: int
    clipped_low_count: int
    clipped_high_count: int
    non_clipped_count: int
    changed_and_clipped_low_count: int
    changed_and_clipped_high_count: int
    changed_and_non_clipped_count: int
    baseline_background_pixel_count: int
    probe_background_pixel_count: int
    baseline_background_median: float
    probe_background_median: float
    normalization_scale: float
    normalization_offset: float
    alignment_dx: int
    alignment_dy: int
    alignment_rotation_degrees: int
    alignment_overlap: float
    alignment_state: str
    alignment_margin: float
    support_mask_fingerprint: str
    baseline_mask_width: int
    baseline_mask_height: int


@dataclass(frozen=True, slots=True)
class SupportChangeFacts:
    """Fixed-shape aggregates from the exact selected support comparison."""

    baseline_support_pixel_count: int
    valid_support_pixel_count: int
    excluded_support_pixel_count: int
    changed_support_pixel_count: int
    unchanged_support_pixel_count: int
    support_change_ratio: float
    difference_le_24_count: int
    difference_gt_24_le_30_count: int
    difference_gt_30_le_32_count: int
    difference_gt_32_le_34_count: int
    difference_gt_34_le_40_count: int
    difference_gt_40_count: int
    raw_probe_changed_count: int
    normalized_preclip_changed_count: int
    normalized_postclip_changed_count: int
    clipped_low_count: int
    clipped_high_count: int
    non_clipped_count: int
    changed_and_clipped_low_count: int
    changed_and_clipped_high_count: int
    changed_and_non_clipped_count: int
    baseline_background_pixel_count: int
    probe_background_pixel_count: int
    baseline_background_median: float
    probe_background_median: float
    normalization_scale: float
    normalization_offset: float
    alignment_dx: int
    alignment_dy: int
    alignment_rotation_degrees: int
    alignment_overlap: float
    alignment_state: str
    alignment_margin: float
    support_mask_fingerprint: str
    baseline_mask_width: int
    baseline_mask_height: int

    def to_payload(self) -> dict[str, object]:
        """Return deterministic JSON-compatible optional facts."""
        return {name: getattr(self, name) for name in sorted(SUPPORT_CHANGE_FACT_FIELDS)}

    @classmethod
    def from_payload(  # noqa: C901, PLR0911 - keep strict schema and arithmetic checks together.
        cls, value: object
    ) -> SupportChangeFacts | None:
        """Defensively validate worker facts without affecting classification."""
        if not isinstance(value, dict):
            return None
        raw = cast("dict[object, object]", value)
        if frozenset(raw) != SUPPORT_CHANGE_FACT_FIELDS:
            return None
        record = cast("Mapping[str, object]", raw)
        counts = tuple(record[name] for name in _COUNT_FIELDS)
        if any(
            type(item) is not int or item < 0 or item > _MAX_SUPPORT_MASK_PIXELS for item in counts
        ):
            return None
        if any(type(record[name]) is not float for name in _FLOAT_FIELDS):
            return None
        floats = tuple(_finite_float(record[name]) for name in _FLOAT_FIELDS)
        if any(item is None for item in floats):
            return None
        ratio, baseline_median, probe_median, scale, offset, overlap, margin = cast(
            "tuple[float, float, float, float, float, float, float]", floats
        )
        values = dict(record)
        if (
            type(record["alignment_dx"]) is not int
            or type(record["alignment_dy"]) is not int
            or type(record["alignment_rotation_degrees"]) is not int
            or not isinstance(record["alignment_state"], str)
            or record["alignment_state"] not in _ALIGNMENT_STATES
            or abs(record["alignment_dx"]) > _MAX_ALIGNMENT_TRANSLATION
            or abs(record["alignment_dy"]) > _MAX_ALIGNMENT_TRANSLATION
            or record["alignment_rotation_degrees"] not in _ALIGNMENT_ROTATIONS
            or type(record["support_mask_fingerprint"]) is not str
            or len(record["support_mask_fingerprint"]) != _SHA256_HEX_LENGTH
            or any(char not in "0123456789abcdef" for char in record["support_mask_fingerprint"])
        ):
            return None
        if (
            type(record["baseline_mask_width"]) is not int
            or type(record["baseline_mask_height"]) is not int
            or record["baseline_mask_width"] <= 0
            or record["baseline_mask_height"] <= 0
        ):
            return None

        def count(name: str) -> int:
            return cast("int", record[name])

        population = count("baseline_support_pixel_count")
        valid = count("valid_support_pixel_count")
        excluded = count("excluded_support_pixel_count")
        changed = count("changed_support_pixel_count")
        unchanged = count("unchanged_support_pixel_count")
        histogram_changed = sum(count(name) for name in _HISTOGRAM_FIELDS[3:])
        mask_area = count("baseline_mask_width") * count("baseline_mask_height")
        if (
            valid == 0
            or population == 0
            or mask_area > _MAX_SUPPORT_MASK_PIXELS
            or population > mask_area
            or population != valid + excluded
            or valid != changed + unchanged
            or sum(count(name) for name in _HISTOGRAM_FIELDS) != valid
            or histogram_changed != changed
            or quantize_metric(changed / valid) != ratio
            or any(
                count(name) > valid
                for name in _COUNT_FIELDS
                if name
                not in {
                    "baseline_support_pixel_count",
                    "valid_support_pixel_count",
                    "excluded_support_pixel_count",
                    "unchanged_support_pixel_count",
                    "baseline_background_pixel_count",
                    "probe_background_pixel_count",
                }
            )
            or count("normalized_postclip_changed_count") != changed
            or count("normalized_postclip_changed_count")
            > count("normalized_preclip_changed_count")
            or count("clipped_low_count") + count("clipped_high_count") + count("non_clipped_count")
            != valid
            or count("changed_and_clipped_low_count")
            + count("changed_and_clipped_high_count")
            + count("changed_and_non_clipped_count")
            != changed
            or count("changed_and_clipped_low_count") > count("clipped_low_count")
            or count("changed_and_clipped_high_count") > count("clipped_high_count")
            or count("changed_and_non_clipped_count") > count("non_clipped_count")
            or count("baseline_background_pixel_count") == 0
            or count("probe_background_pixel_count") == 0
            or not 0.0 <= baseline_median <= _LUMA_MAXIMUM
            or not 0.0 <= probe_median <= _LUMA_MAXIMUM
            or not _MIN_SCALE <= scale <= _MAX_SCALE
            or not _MIN_NORMALIZATION_OFFSET <= offset <= _MAX_NORMALIZATION_OFFSET
            or not 0.0 <= overlap <= 1.0
            or not 0.0 <= margin <= _MAX_ALIGNMENT_MARGIN
        ):
            return None
        try:
            facts = cls(**cast("_SupportChangePayload", cast("object", values)))
        except (TypeError, ValueError):
            return None
        return facts


def baseline_mask_fingerprint(mask: BinaryMask) -> str:
    """Hash dimensions and canonical row-major packed mask bits."""
    digest = hashlib.sha256()
    digest.update(f"phase7e-support-mask-v1\0{mask.width}x{mask.height}\0".encode("ascii"))
    packed = bytearray()
    current_byte = 0
    bit_offset = 0
    for value in (cell for row in mask.rows for cell in row):
        if value:
            current_byte |= 1 << bit_offset
        bit_offset += 1
        if bit_offset == _MASK_BITS_PER_BYTE:
            packed.append(current_byte)
            current_byte = 0
            bit_offset = 0
        if len(packed) == _MASK_FINGERPRINT_CHUNK_BYTES:
            digest.update(packed)
            packed.clear()
    if bit_offset:
        packed.append(current_byte)
    digest.update(packed)
    return digest.hexdigest()


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
