from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, final, runtime_checkable

import anyio
from anyio.to_thread import run_sync
from typing_extensions import override

from vigi_vision.assisted_roi_geometry import (
    MAX_MASK_PREVIEW_RUNS,
    BoundingBox,
    ImageSize,
    MaskPreview,
    Point,
    mask_preview_matches_size,
    mask_preview_to_bounding_box,
)
from vigi_vision.reference_frame_models import (
    ReferenceFrameError,
    ReferenceFrameResourceCorruptError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vigi_vision.reference_frame_resources import ReferenceFrameImageResource


class ReferenceFrameImageResolver(Protocol):
    def resolve_image(self, resource_id: str) -> ReferenceFrameImageResource: ...


class RoiSuggestionExecutionBoundary(Protocol):
    async def suggest(self, resource_id: str, point: Point) -> RoiSuggestion: ...


class RoiPredictor(Protocol):
    def predict(self, image_path: Path, point: Point, size: ImageSize) -> BoundingBox: ...


@runtime_checkable
class RoiMaskPredictor(Protocol):
    def predict_with_mask(
        self, image_path: Path, point: Point, size: ImageSize
    ) -> RoiPrediction: ...


@runtime_checkable
class _ClosableRoiPredictor(Protocol):
    def close(self) -> None: ...


class RoiSuggestionUnavailableError(ReferenceFrameError):
    @override
    def __str__(self) -> str:
        return "Assisted ROI suggestions are unavailable."


class RoiSuggestionInvalidPointError(ReferenceFrameError, ValueError):
    @override
    def __str__(self) -> str:
        return "The assisted ROI point is outside the reference-frame image."


class RoiSuggestionNoValidSuggestionError(ReferenceFrameError):
    @override
    def __str__(self) -> str:
        return "No valid assisted ROI suggestion is available for this point."


class RoiSuggestionTimeoutError(ReferenceFrameError):
    @override
    def __str__(self) -> str:
        return "Assisted ROI inference timed out safely."


class RoiSuggestionInferenceError(ReferenceFrameError):
    @override
    def __str__(self) -> str:
        return "Assisted ROI inference failed safely."


@final
@dataclass(frozen=True, slots=True)
class RoiPrediction:
    bbox: BoundingBox
    mask_preview: MaskPreview


@final
@dataclass(frozen=True, slots=True)
class RoiSuggestion:
    resource_id: str
    source_width: int
    source_height: int
    bbox: BoundingBox
    mask_preview: MaskPreview


@final
@dataclass(frozen=True, slots=True)
class AssistedRoiSuggestionService:
    resources: ReferenceFrameImageResolver = field(repr=False)
    predictor: RoiPredictor | RoiMaskPredictor | None = field(repr=False)
    timeout_seconds: float = 30.0
    limiter: anyio.CapacityLimiter = field(
        default_factory=lambda: anyio.CapacityLimiter(1), repr=False
    )

    async def suggest(self, resource_id: str, point: Point) -> RoiSuggestion:
        if self.predictor is None:
            raise RoiSuggestionUnavailableError
        try:
            image = await run_sync(self._resolve_image, resource_id)
        except ReferenceFrameError:
            raise
        except Exception as error:
            raise ReferenceFrameResourceCorruptError from error
        size = _image_size(image)
        _validate_point(point, size)
        try:
            with anyio.fail_after(self.timeout_seconds):
                prediction = await run_sync(
                    self._predict,
                    image.jpeg_path,
                    point,
                    size,
                    limiter=self.limiter,
                    abandon_on_cancel=True,
                )
        except TimeoutError as error:
            raise RoiSuggestionTimeoutError from error
        except (RoiSuggestionNoValidSuggestionError, RoiSuggestionUnavailableError):
            raise
        except ReferenceFrameError:
            raise
        except Exception as error:
            raise RoiSuggestionInferenceError from error
        _validate_prediction(prediction, point, size)
        return RoiSuggestion(
            resource_id,
            size.width,
            size.height,
            prediction.bbox,
            prediction.mask_preview,
        )

    def close(self) -> None:
        if isinstance(self.predictor, _ClosableRoiPredictor):
            self.predictor.close()

    def _resolve_image(self, resource_id: str) -> ReferenceFrameImageResource:
        return self.resources.resolve_image(resource_id)

    def _predict(self, image_path: Path, point: Point, size: ImageSize) -> RoiPrediction:
        predictor = self.predictor
        if predictor is None:
            raise RoiSuggestionUnavailableError
        if not isinstance(predictor, RoiMaskPredictor):
            raise RoiSuggestionUnavailableError
        return predictor.predict_with_mask(image_path, point, size)


def _image_size(image: ReferenceFrameImageResource) -> ImageSize:
    if image.width is None or image.height is None:
        raise ReferenceFrameResourceCorruptError
    try:
        return ImageSize(image.width, image.height)
    except ValueError:
        raise ReferenceFrameResourceCorruptError from None


def _validate_point(point: Point, size: ImageSize) -> None:
    if (
        type(point.x) is not int
        or type(point.y) is not int
        or point.x < 0
        or point.y < 0
        or point.x >= size.width
        or point.y >= size.height
    ):
        raise RoiSuggestionInvalidPointError


def _validate_bbox(box: BoundingBox, point: Point, size: ImageSize) -> None:
    if (
        type(box.x) is not int
        or type(box.y) is not int
        or type(box.width) is not int
        or type(box.height) is not int
        or box.x < 0
        or box.y < 0
        or box.width <= 0
        or box.height <= 0
        or box.x + box.width > size.width
        or box.y + box.height > size.height
        or not (box.x <= point.x < box.x + box.width)
        or not (box.y <= point.y < box.y + box.height)
    ):
        raise RoiSuggestionNoValidSuggestionError


def _validate_prediction(prediction: RoiPrediction, point: Point, size: ImageSize) -> None:
    _validate_bbox(prediction.bbox, point, size)
    preview = prediction.mask_preview
    if (
        not mask_preview_matches_size(preview)
        or preview.width != size.width
        or preview.height != size.height
    ):
        raise RoiSuggestionNoValidSuggestionError
    run_count = 0
    for row in preview.rows:
        run_count += len(row)
        if run_count > MAX_MASK_PREVIEW_RUNS:
            raise RoiSuggestionNoValidSuggestionError
    if mask_preview_to_bounding_box(preview) != prediction.bbox:
        raise RoiSuggestionNoValidSuggestionError
