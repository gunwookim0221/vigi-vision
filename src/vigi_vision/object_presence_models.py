"""Public Phase 7B model surface."""

from vigi_vision.object_presence_evidence import ClassificationResult, RawComparison
from vigi_vision.object_presence_values import (
    BinaryMask,
    ClassificationFailureReason,
    ClassificationOperationalError,
    ClassificationOutcome,
    DecodedRgbImage,
    MaskRows,
    RgbPixel,
    RgbRows,
    VisualReason,
    VisualStatus,
    quantize_metric,
)

__all__ = [
    "BinaryMask",
    "ClassificationFailureReason",
    "ClassificationOperationalError",
    "ClassificationOutcome",
    "ClassificationResult",
    "DecodedRgbImage",
    "MaskRows",
    "RawComparison",
    "RgbPixel",
    "RgbRows",
    "VisualReason",
    "VisualStatus",
    "quantize_metric",
]
