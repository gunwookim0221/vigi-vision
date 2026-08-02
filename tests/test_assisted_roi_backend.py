from __future__ import annotations

import time
from pathlib import Path
from typing import cast, final

import anyio
import pytest
from fastapi.testclient import TestClient

from vigi_vision.assisted_roi_geometry import (
    BoundingBox,
    ImageSize,
    MaskCandidate,
    MaskPreview,
    Point,
    binarize_logits,
    bounding_box_to_mask_preview,
    select_valid_mask_candidate,
)
from vigi_vision.assisted_roi_predictor import LazyEfficientSamPredictor, candidates_from_output
from vigi_vision.assisted_roi_service import (
    AssistedRoiSuggestionService,
    RoiPrediction,
    RoiSuggestionNoValidSuggestionError,
    RoiSuggestionTimeoutError,
    RoiSuggestionUnavailableError,
)
from vigi_vision.config import AssistedRoiSettings, load_assisted_roi_settings
from vigi_vision.reference_frame_api import create_reference_frame_app
from vigi_vision.reference_frame_models import ReferenceFrameRequest, ReferenceFrameResolution
from vigi_vision.reference_frame_resources import ReferenceFrameImageResource


@final
class _FakeResources:
    image: ReferenceFrameImageResource

    def __init__(self, image: ReferenceFrameImageResource) -> None:
        self.image = image

    def resolve_image(self, resource_id: str) -> ReferenceFrameImageResource:
        if resource_id != self.image.resource_id:
            raise RuntimeError
        return self.image


@final
class _FakePredictor:
    bbox: BoundingBox

    def __init__(self, bbox: BoundingBox) -> None:
        self.bbox = bbox
        self.calls: list[tuple[Path, Point, ImageSize]] = []

    def predict(self, image_path: Path, point: Point, size: ImageSize) -> BoundingBox:
        self.calls.append((image_path, point, size))
        return self.bbox

    def predict_with_mask(self, image_path: Path, point: Point, size: ImageSize) -> RoiPrediction:
        self.calls.append((image_path, point, size))
        return RoiPrediction(self.bbox, bounding_box_to_mask_preview(self.bbox, size))


@final
class _FailingPredictor:
    def predict(self, image_path: Path, point: Point, size: ImageSize) -> BoundingBox:
        _ = image_path, point, size
        raise RoiSuggestionNoValidSuggestionError

    def predict_with_mask(self, image_path: Path, point: Point, size: ImageSize) -> RoiPrediction:
        _ = image_path, point, size
        raise RoiSuggestionNoValidSuggestionError


@final
class _MaskPredictor:
    prediction: RoiPrediction

    def __init__(self, prediction: RoiPrediction) -> None:
        self.prediction = prediction

    def predict_with_mask(self, image_path: Path, point: Point, size: ImageSize) -> RoiPrediction:
        _ = image_path, point, size
        return self.prediction


@final
class _SlowPredictor:
    def predict(self, image_path: Path, point: Point, size: ImageSize) -> BoundingBox:
        _ = image_path, point, size
        time.sleep(0.05)
        return BoundingBox(1, 1, 2, 2)

    def predict_with_mask(self, image_path: Path, point: Point, size: ImageSize) -> RoiPrediction:
        _ = image_path, point, size
        time.sleep(0.05)
        box = BoundingBox(1, 1, 2, 2)
        return RoiPrediction(box, bounding_box_to_mask_preview(box, size))


@final
class _BboxOnlyPredictor:
    def predict(self, image_path: Path, point: Point, size: ImageSize) -> BoundingBox:
        _ = image_path, point, size
        return BoundingBox(1, 1, 2, 2)


@final
class _Tensor:
    value: object
    shape: tuple[int, ...]

    def __init__(self, value: object, shape: tuple[int, ...]) -> None:
        self.value = value
        self.shape = shape

    def __getitem__(self, key: object) -> _Tensor:
        if not isinstance(key, tuple):
            raise TypeError
        indices = cast("tuple[int, ...]", key)
        value = self.value
        for index in indices:
            if not isinstance(value, list):
                raise TypeError
            items = cast("list[object]", value)
            value = items[index]
        return _Tensor(value, ())

    def detach(self) -> _Tensor:
        return self

    def cpu(self) -> _Tensor:
        return self

    def tolist(self) -> object:
        return self.value

    def item(self) -> object:
        return self.value

    def to(self, device: str) -> _Tensor:
        _ = device
        return self

    def unsqueeze(self, dimension: int) -> _Tensor:
        _ = dimension
        return self

    def numpy(self) -> object:
        return self.value


@final
class _FakeReferenceService:
    def execute_or_resolve(self, request: ReferenceFrameRequest) -> ReferenceFrameResolution:
        _ = request
        raise AssertionError


def test_binarize_logits_uses_signed_zero_threshold() -> None:
    assert binarize_logits(((-0.1, 0.0, 0.1),)) == ((False, True, True),)


def test_candidate_selection_preserves_score_alignment_and_rejects_full_frame() -> None:
    size = ImageSize(4, 3)
    candidates = (
        MaskCandidate(((True,) * 4,) * 3, 0.99),
        MaskCandidate(
            (
                (False, False, False, False),
                (False, True, False, False),
                (False, False, False, False),
            ),
            0.75,
        ),
    )

    selected = select_valid_mask_candidate(candidates, Point(1, 1), size)

    assert selected.bbox == BoundingBox(1, 1, 1, 1)
    assert selected.score == 0.75
    assert selected.candidate_index == 1


def test_predictor_output_contract_thresholds_logits_and_aligns_scores() -> None:
    logits = _Tensor(
        [[[[[-1.0, 1.0], [-1.0, -1.0]], [[1.0, 1.0], [1.0, 1.0]]]]],
        (1, 1, 2, 2, 2),
    )
    scores = _Tensor([[[0.2, 0.9]]], (1, 1, 2))

    candidates = candidates_from_output(logits, scores, ImageSize(2, 2))

    assert candidates[0].mask == ((False, True), (False, False))
    assert candidates[0].score == 0.2
    assert candidates[1].mask == ((True, True), (True, True))
    assert candidates[1].score == 0.9


def test_service_validates_point_and_returns_source_pixel_bbox(tmp_path: Path) -> None:
    image = ReferenceFrameImageResource("resource-1", tmp_path / "frame.jpg", 100, 80)
    predictor = _FakePredictor(BoundingBox(10, 20, 30, 25))
    service = AssistedRoiSuggestionService(_FakeResources(image), predictor)

    result = anyio.run(service.suggest, "resource-1", Point(15, 22))

    assert result.resource_id == "resource-1"
    assert result.source_width == 100
    assert result.source_height == 80
    assert result.bbox == BoundingBox(10, 20, 30, 25)
    assert result.mask_preview == bounding_box_to_mask_preview(result.bbox, ImageSize(100, 80))
    assert predictor.calls == [(image.jpeg_path, Point(15, 22), ImageSize(100, 80))]


def test_service_does_not_fabricate_mask_for_bbox_only_predictor(tmp_path: Path) -> None:
    image = ReferenceFrameImageResource("resource-1", tmp_path / "frame.jpg", 100, 80)
    service = AssistedRoiSuggestionService(_FakeResources(image), _BboxOnlyPredictor())

    with pytest.raises(RoiSuggestionUnavailableError):
        _ = anyio.run(service.suggest, "resource-1", Point(15, 22))


@pytest.mark.parametrize("point", [Point(-1, 1), Point(100, 1), Point(1, 80)])
def test_service_rejects_points_outside_server_dimensions(tmp_path: Path, point: Point) -> None:
    image = ReferenceFrameImageResource("resource-1", tmp_path / "frame.jpg", 100, 80)
    service = AssistedRoiSuggestionService(
        _FakeResources(image), _FakePredictor(BoundingBox(1, 1, 2, 2))
    )

    with pytest.raises(ValueError, match="point"):
        _ = anyio.run(service.suggest, "resource-1", point)


def test_disabled_service_returns_safe_unavailable_error(tmp_path: Path) -> None:
    image = ReferenceFrameImageResource("resource-1", tmp_path / "frame.jpg", 100, 80)
    service = AssistedRoiSuggestionService(_FakeResources(image), None)

    with pytest.raises(RoiSuggestionUnavailableError):
        _ = anyio.run(service.suggest, "resource-1", Point(1, 1))


def test_service_rejects_mask_preview_that_does_not_match_bbox(tmp_path: Path) -> None:
    image = ReferenceFrameImageResource("resource-1", tmp_path / "frame.jpg", 100, 80)
    prediction = RoiPrediction(
        BoundingBox(10, 20, 30, 25),
        bounding_box_to_mask_preview(BoundingBox(11, 20, 30, 25), ImageSize(100, 80)),
    )
    service = AssistedRoiSuggestionService(_FakeResources(image), _MaskPredictor(prediction))

    with pytest.raises(RoiSuggestionNoValidSuggestionError):
        _ = anyio.run(service.suggest, "resource-1", Point(15, 22))


def test_service_rejects_mask_preview_for_different_source_dimensions(tmp_path: Path) -> None:
    image = ReferenceFrameImageResource("resource-1", tmp_path / "frame.jpg", 100, 80)
    box = BoundingBox(10, 20, 30, 25)
    prediction = RoiPrediction(box, bounding_box_to_mask_preview(box, ImageSize(120, 80)))
    service = AssistedRoiSuggestionService(_FakeResources(image), _MaskPredictor(prediction))

    with pytest.raises(RoiSuggestionNoValidSuggestionError):
        _ = anyio.run(service.suggest, "resource-1", Point(15, 22))


def test_service_rejects_oversized_mask_preview_safely(tmp_path: Path) -> None:
    image = ReferenceFrameImageResource("resource-1", tmp_path / "frame.jpg", 100, 80)
    oversized_row = tuple((10, 11) for _ in range(50_001))
    prediction = RoiPrediction(
        BoundingBox(10, 0, 1, 1),
        MaskPreview(100, 80, (oversized_row, *(() for _ in range(79)))),
    )
    service = AssistedRoiSuggestionService(_FakeResources(image), _MaskPredictor(prediction))

    with pytest.raises(RoiSuggestionNoValidSuggestionError):
        _ = anyio.run(service.suggest, "resource-1", Point(10, 0))


def test_api_returns_bbox_and_bounded_mask_preview(tmp_path: Path) -> None:
    image = ReferenceFrameImageResource("resource-1", tmp_path / "frame.jpg", 100, 80)
    service = AssistedRoiSuggestionService(
        _FakeResources(image), _FakePredictor(BoundingBox(10, 20, 30, 25))
    )
    app = create_reference_frame_app(
        service=_FakeReferenceService(),
        resources=_FakeResources(image),
        suggestion_service=service,
    )
    response = TestClient(app).post(
        "/api/v1/reference-frames/resource-1/roi-suggestions",
        json={"point": {"x": 15, "y": 22}},
    )

    assert response.status_code == 200
    assert response.json()["resource_id"] == "resource-1"
    assert response.json()["source_width"] == 100
    assert response.json()["source_height"] == 80
    assert response.json()["bbox"] == {"x": 10, "y": 20, "width": 30, "height": 25}
    assert response.json()["mask_preview"]["width"] == 100
    assert response.json()["mask_preview"]["height"] == 80
    assert response.json()["mask_preview"]["rows"][20] == [[10, 40]]
    assert response.json()["mask_preview"]["rows"][44] == [[10, 40]]
    assert response.json()["mask_preview"]["rows"][45] == []
    assert "tmp_path" not in response.text
    assert "mask" in response.text


def test_api_disabled_feature_is_deterministic_503(tmp_path: Path) -> None:
    image = ReferenceFrameImageResource("resource-1", tmp_path / "frame.jpg", 100, 80)
    app = create_reference_frame_app(
        service=_FakeReferenceService(), resources=_FakeResources(image), suggestion_service=None
    )

    response = TestClient(app).post(
        "/api/v1/reference-frames/resource-1/roi-suggestions",
        json={"point": {"x": 15, "y": 22}},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "suggestion_unavailable"


def test_api_rejects_point_at_exclusive_source_edge_and_extra_dimensions(tmp_path: Path) -> None:
    image = ReferenceFrameImageResource("resource-1", tmp_path / "frame.jpg", 100, 80)
    service = AssistedRoiSuggestionService(
        _FakeResources(image), _FakePredictor(BoundingBox(10, 20, 30, 25))
    )
    app = create_reference_frame_app(
        service=_FakeReferenceService(),
        resources=_FakeResources(image),
        suggestion_service=service,
    )
    client = TestClient(app)

    edge = client.post(
        "/api/v1/reference-frames/resource-1/roi-suggestions",
        json={"point": {"x": 100, "y": 1}},
    )
    extra = client.post(
        "/api/v1/reference-frames/resource-1/roi-suggestions",
        json={"point": {"x": 1, "y": 1}, "source_width": 100},
    )

    assert edge.status_code == 422
    assert edge.json()["error"]["code"] == "invalid_point"
    assert extra.status_code == 422
    assert extra.json()["error"]["code"] == "invalid_request"


def test_api_maps_no_valid_prediction_without_model_details(tmp_path: Path) -> None:
    image = ReferenceFrameImageResource("resource-1", tmp_path / "frame.jpg", 100, 80)
    service = AssistedRoiSuggestionService(_FakeResources(image), _FailingPredictor())
    app = create_reference_frame_app(
        service=_FakeReferenceService(),
        resources=_FakeResources(image),
        suggestion_service=service,
    )

    response = TestClient(app).post(
        "/api/v1/reference-frames/resource-1/roi-suggestions",
        json={"point": {"x": 1, "y": 1}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_valid_suggestion"
    assert "EfficientSAM" not in response.text


def test_service_translates_bounded_inference_timeout(tmp_path: Path) -> None:
    image = ReferenceFrameImageResource("resource-1", tmp_path / "frame.jpg", 100, 80)
    service = AssistedRoiSuggestionService(
        _FakeResources(image), _SlowPredictor(), timeout_seconds=0.001
    )

    with pytest.raises(RoiSuggestionTimeoutError):
        _ = anyio.run(service.suggest, "resource-1", Point(1, 1))


def test_assisted_settings_do_not_require_openai_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _ = env_file.write_text("VIGI_ASSISTED_ROI_ENABLED=false\n", encoding="utf-8")

    settings = load_assisted_roi_settings(env_file)

    assert settings == AssistedRoiSettings()


def test_lazy_predictor_caches_unavailable_checkpoint_state(tmp_path: Path) -> None:
    predictor = LazyEfficientSamPredictor(
        checkpoint_path=tmp_path / "missing.pt",
        expected_sha256="0" * 64,
        device_mode="cpu",
    )

    with pytest.raises(RoiSuggestionUnavailableError):
        _ = predictor.predict(tmp_path / "frame.jpg", Point(1, 1), ImageSize(10, 10))
    with pytest.raises(RoiSuggestionUnavailableError):
        _ = predictor.predict(tmp_path / "frame.jpg", Point(1, 1), ImageSize(10, 10))

    assert predictor.is_unavailable is True
    assert predictor.is_loaded is False
