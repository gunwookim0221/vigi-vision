"""Immutable Phase 7B input values and closed classification vocabulary."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Final, NoReturn, TypeAlias

from typing_extensions import override

RgbPixel: TypeAlias = tuple[int, int, int]
RgbRows: TypeAlias = tuple[tuple[RgbPixel, ...], ...]
MaskRows: TypeAlias = tuple[tuple[bool, ...], ...]
_METRIC_DECIMAL_PLACES: Final = 6
_RGB_CHANNEL_COUNT: Final = 3
_MAX_CHANNEL_VALUE: Final = 255


class ClassificationOutcome(str, Enum):
    """The three successful visual classification states."""

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    INDETERMINATE = "INDETERMINATE"


class VisualStatus(str, Enum):
    """Whether a raw comparison reached the numeric comparison stage."""

    COMPARABLE = "comparable"
    UNUSABLE = "unusable"


class VisualReason(str, Enum):
    """Closed visual reasons that can produce an indeterminate result."""

    INVALID_MASK = "invalid_mask"
    BACKGROUND_DOMINANT = "background_dominant"
    INSUFFICIENT_MASK_OVERLAP = "insufficient_mask_overlap"
    INSUFFICIENT_COMPARISON_AREA = "insufficient_comparison_area"
    ZERO_LUMA_VARIANCE = "zero_luma_variance"
    INSUFFICIENT_VISUAL_EVIDENCE = "insufficient_visual_evidence"


class ClassificationFailureReason(str, Enum):
    """Closed operational failures that publish no visual comparison."""

    INVALID_INPUT_SHAPE = "invalid_input_shape"
    INVALID_GEOMETRY = "invalid_geometry"
    INVALID_MASK_STRUCTURE = "invalid_mask_structure"
    INVALID_NUMERIC_INPUT = "invalid_numeric_input"
    INVALID_CLASSIFIER_OUTPUT = "invalid_classifier_output"
    UNSUPPORTED_CHANNEL_LAYOUT = "unsupported_channel_layout"
    PREPROCESSING_FAILED = "preprocessing_failed"


@dataclass(frozen=True, slots=True)
class ClassificationOperationalError(Exception):
    """Safe typed failure for an invalid pure-classification input."""

    reason: ClassificationFailureReason

    @override
    def __str__(self) -> str:
        """Return the fixed safe failure category."""
        return f"Object-presence classification failed: {self.reason.value}."


@dataclass(frozen=True, slots=True)
class DecodedRgbImage:
    """Immutable, already decoded RGB pixels in source-row order."""

    pixels: RgbRows

    def __post_init__(self) -> None:
        """Reject non-rectangular or out-of-domain RGB rows."""
        _validate_rgb_rows(self.pixels)

    @classmethod
    def from_rows(cls, rows: RgbRows) -> DecodedRgbImage:
        """Parse nested rows into an immutable RGB image."""
        return cls(tuple(tuple(pixel for pixel in row) for row in rows))

    @property
    def height(self) -> int:
        """Return the source image height."""
        return len(self.pixels)

    @property
    def width(self) -> int:
        """Return the source image width."""
        return len(self.pixels[0])


@dataclass(frozen=True, slots=True)
class BinaryMask:
    """Immutable source-sized boolean mask."""

    rows: MaskRows

    def __post_init__(self) -> None:
        """Reject non-rectangular or non-boolean mask rows."""
        _validate_mask_rows(self.rows)

    @classmethod
    def from_rows(cls, rows: MaskRows) -> BinaryMask:
        """Parse nested rows into an immutable boolean mask."""
        return cls(tuple(tuple(value for value in row) for row in rows))

    @property
    def height(self) -> int:
        """Return the mask height."""
        return len(self.rows)

    @property
    def width(self) -> int:
        """Return the mask width."""
        return len(self.rows[0])


def quantize_metric(value: float) -> float:
    """Round one finite metric to six decimal places using ties-to-even."""
    if not math.isfinite(value):
        raise ValueError
    rounded = round(value, _METRIC_DECIMAL_PLACES)
    if not math.isfinite(rounded):
        raise ValueError
    return rounded


def fail(reason: ClassificationFailureReason) -> NoReturn:
    """Raise one fixed operational classification failure."""
    raise ClassificationOperationalError(reason)


def _validate_rgb_rows(rows: RgbRows) -> None:
    if type(rows) is not tuple or not rows or any(type(row) is not tuple for row in rows):
        raise ValueError
    width = len(rows[0])
    if width == 0 or any(len(row) != width for row in rows):
        raise ValueError
    for row in rows:
        for pixel in row:
            if type(pixel) is not tuple or len(pixel) != _RGB_CHANNEL_COUNT:
                raise ValueError
            if any(
                type(channel) is not int or not 0 <= channel <= _MAX_CHANNEL_VALUE
                for channel in pixel
            ):
                raise ValueError


def _validate_mask_rows(rows: MaskRows) -> None:
    if type(rows) is not tuple or not rows or any(type(row) is not tuple for row in rows):
        raise ValueError
    width = len(rows[0])
    if width == 0 or any(len(row) != width for row in rows):
        raise ValueError
    if any(type(value) is not bool for row in rows for value in row):
        raise ValueError
