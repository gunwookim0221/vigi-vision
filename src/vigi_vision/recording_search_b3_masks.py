"""Pathless production mask inference for immutable classification snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, Protocol

import anyio
from anyio.to_thread import run_sync

from vigi_vision.assisted_roi_geometry import (
    MAX_MASK_PREVIEW_RUNS,
    ImageSize,
    MaskPreview,
    Point,
    mask_preview_matches_size,
    mask_preview_to_bounding_box,
)
from vigi_vision.assisted_roi_predictor import LazyEfficientSamPredictor
from vigi_vision.assisted_roi_service import (
    AssistedRoiSuggestionService,
    RoiPrediction,
    RoiSuggestionInferenceError,
    RoiSuggestionNoValidSuggestionError,
    RoiSuggestionUnavailableError,
)
from vigi_vision.object_presence_models import BinaryMask, DecodedRgbImage
from vigi_vision.recording_search_b3_models import (
    ClassificationPreparationError,
    ClassificationPreparationReason,
    ClassificationSnapshot,
)

if TYPE_CHECKING:
    from vigi_vision.investigation_confirmation_models import ConfirmationRoi
    from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy


class MaskPredictor(Protocol):
    """Run the existing production mask predictor on decoded RGB values."""

    def predict_from_rgb(self, image: DecodedRgbImage, point: Point, size: ImageSize) -> object:
        """Return a source-aligned mask or validated ROI prediction."""
        ...


@dataclass(frozen=True, slots=True)
class LimitedRgbMaskPredictor:
    """Route RGB inference through the existing assisted-ROI limiter."""

    service: AssistedRoiSuggestionService

    def predict_from_rgb(self, image: DecodedRgbImage, point: Point, size: ImageSize) -> object:
        """Run one in-memory prediction under the shared limiter."""
        return anyio.run(self._predict, image, point, size)

    async def _predict(self, image: DecodedRgbImage, point: Point, size: ImageSize) -> object:
        predictor = self.service.predictor
        if not isinstance(predictor, LazyEfficientSamPredictor):
            raise RoiSuggestionUnavailableError
        return await run_sync(
            predictor.predict_from_rgb,
            image,
            point,
            size,
            limiter=self.service.limiter,
        )


def predict_masks(
    snapshot: ClassificationSnapshot,
    predictor: MaskPredictor | None,
) -> tuple[BinaryMask, BinaryMask]:
    """Produce two validated source-aligned masks from immutable RGB values."""
    return predict_masks_for_images(
        snapshot.baseline_image,
        snapshot.probe_image,
        snapshot.source_width,
        snapshot.source_height,
        snapshot.confirmed_roi,
        snapshot.policy,
        predictor,
    )


def predict_masks_for_images(  # noqa: PLR0913
    baseline_image: DecodedRgbImage,
    probe_image: DecodedRgbImage,
    source_width: int,
    source_height: int,
    roi: ConfirmationRoi,
    policy: ObjectPresenceDecisionPolicy,
    predictor: MaskPredictor | None,
) -> tuple[BinaryMask, BinaryMask]:
    """Produce masks from decoded pixels without requiring a persistence snapshot.

    This is the persistence-neutral B4 preparation boundary.  The legacy
    snapshot path and the Phase 7E adapter both call this function so that
    prompt geometry, mask validation, and safe predictor error mapping remain
    identical.
    """
    if predictor is None:
        _fail(ClassificationPreparationReason.CLASSIFIER_UNAVAILABLE)
    size = ImageSize(source_width, source_height)
    point = Point(
        roi.x + (roi.width - 1) // 2,
        roi.y + (roi.height - 1) // 2,
    )
    try:
        baseline = _prediction_to_mask(
            predictor.predict_from_rgb(baseline_image, point, size),
            size,
            point,
            policy,
        )
        probe = _prediction_to_mask(
            predictor.predict_from_rgb(probe_image, point, size),
            size,
            point,
            policy,
        )
    except ClassificationPreparationError:
        raise
    except RoiSuggestionUnavailableError:
        _fail(ClassificationPreparationReason.CLASSIFIER_UNAVAILABLE)
    except RoiSuggestionNoValidSuggestionError:
        _fail(ClassificationPreparationReason.INVALID_CLASSIFIER_OUTPUT)
    except RoiSuggestionInferenceError:
        _fail(ClassificationPreparationReason.CLASSIFIER_EXECUTION_FAILED)
    except Exception:  # noqa: BLE001 - predictor failures are one safe category.
        _fail(ClassificationPreparationReason.CLASSIFIER_EXECUTION_FAILED)
    return baseline, probe


def _prediction_to_mask(
    prediction: object,
    size: ImageSize,
    point: Point,
    policy: ObjectPresenceDecisionPolicy,
) -> BinaryMask:
    if isinstance(prediction, BinaryMask):
        if (
            prediction.width != size.width
            or prediction.height != size.height
            or not _mask_is_usable(prediction, point, policy)
        ):
            _fail(ClassificationPreparationReason.INVALID_CLASSIFIER_OUTPUT)
        return prediction
    if type(prediction) is not RoiPrediction:
        _fail(ClassificationPreparationReason.INVALID_CLASSIFIER_OUTPUT)
    try:
        preview: MaskPreview = prediction.mask_preview
        if preview.width != size.width or preview.height != size.height:
            _fail(ClassificationPreparationReason.INVALID_CLASSIFIER_OUTPUT)
        box = mask_preview_to_bounding_box(preview)
        if (
            not mask_preview_matches_size(preview)
            or len(preview.rows) != size.height
            or sum(len(row) for row in preview.rows) > MAX_MASK_PREVIEW_RUNS
            or box is None
            or prediction.bbox != box
        ):
            _fail(ClassificationPreparationReason.INVALID_CLASSIFIER_OUTPUT)
        rows = tuple(
            tuple(any(start <= x < end for start, end in row) for x in range(size.width))
            for row in preview.rows
        )
        mask = BinaryMask.from_rows(rows)
    except (IndexError, TypeError, ValueError):
        _fail(ClassificationPreparationReason.INVALID_CLASSIFIER_OUTPUT)
    if not _mask_is_usable(mask, point, policy):
        _fail(ClassificationPreparationReason.INVALID_CLASSIFIER_OUTPUT)
    return mask


def _mask_is_usable(mask: BinaryMask, point: Point, policy: ObjectPresenceDecisionPolicy) -> bool:
    pixel_count = sum(sum(row) for row in mask.rows)
    total = mask.width * mask.height
    return (
        pixel_count > 0
        and pixel_count / total < policy.maximum_roi_mask_coverage_ratio
        and mask.rows[point.y][point.x]
    )


def _fail(reason: ClassificationPreparationReason) -> NoReturn:
    raise ClassificationPreparationError(reason)
