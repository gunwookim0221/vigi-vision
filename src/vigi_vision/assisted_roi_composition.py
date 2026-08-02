from vigi_vision.assisted_roi_predictor import LazyEfficientSamPredictor
from vigi_vision.assisted_roi_service import (
    AssistedRoiSuggestionService,
    ReferenceFrameImageResolver,
)
from vigi_vision.config import AssistedRoiSettings


def build_assisted_roi_service(
    settings: AssistedRoiSettings, resources: ReferenceFrameImageResolver
) -> AssistedRoiSuggestionService | None:
    if not settings.enabled:
        return None
    predictor = LazyEfficientSamPredictor(
        checkpoint_path=settings.checkpoint_path,
        expected_sha256=settings.expected_sha256,
        device_mode=settings.device,
    )
    return AssistedRoiSuggestionService(
        resources=resources,
        predictor=predictor,
        timeout_seconds=settings.inference_timeout_seconds,
    )
