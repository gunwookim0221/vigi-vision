"""Opt-in production-path assisted ROI readiness and inference validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TypedDict, cast

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from vigi_vision.assisted_roi_predictor import LazyEfficientSamPredictor
from vigi_vision.assisted_roi_service import AssistedRoiSuggestionService
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.reference_frame_api import create_reference_frame_app
from vigi_vision.reference_frame_models import ReferenceFrameRequest, ReferenceFrameResolution
from vigi_vision.reference_frame_resources import ReferenceFrameImageResource


class _BoundingBoxResponse(TypedDict):
    x: int
    y: int
    width: int
    height: int


class _MaskPreviewResponse(TypedDict):
    width: int
    height: int


class _SuggestionResponse(TypedDict):
    resource_id: str
    source_width: int
    source_height: int
    bbox: _BoundingBoxResponse
    mask_preview: _MaskPreviewResponse


class _FixtureResources:
    def __init__(self, image: ReferenceFrameImageResource) -> None:
        self._image: ReferenceFrameImageResource = image

    def resolve_image(self, resource_id: str) -> ReferenceFrameImageResource:
        if resource_id != self._image.resource_id:
            raise AssertionError
        return self._image


class _UnusedReferenceFrameService:
    def execute_or_resolve(self, request: ReferenceFrameRequest) -> ReferenceFrameResolution:
        _ = request
        raise AssertionError


def test_production_assisted_roi_readiness_fixture_inference_and_api(
    tmp_path: Path,
) -> None:
    checkpoint_value = os.environ.get("VIGI_TEST_ASSISTED_ROI_CHECKPOINT")
    if checkpoint_value is None:
        pytest.skip("Set VIGI_TEST_ASSISTED_ROI_CHECKPOINT to run with the external model weights")
    checkpoint = Path(checkpoint_value)
    assert checkpoint.is_file()

    fixture_path = tmp_path / "synthetic-object.png"
    with Image.new("RGB", (320, 240), color=(32, 32, 32)) as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 40, 240, 200), fill=(210, 45, 35), outline=(250, 250, 250), width=4)
        image.save(fixture_path)

    resource = ReferenceFrameImageResource("local-fixture", fixture_path, 320, 240)
    resolver = _FixtureResources(resource)
    predictor = LazyEfficientSamPredictor(
        checkpoint_path=checkpoint,
        expected_sha256=ObjectPresenceDecisionPolicy(
            minimum_mask_overlap_for_comparison=0.1
        ).checkpoint_sha256,
        device_mode="cpu",
    )
    try:
        predictor.ensure_ready()
        assert predictor.is_loaded
        assert not predictor.is_unavailable

        suggestion_service = AssistedRoiSuggestionService(resolver, predictor)
        app = create_reference_frame_app(
            service=_UnusedReferenceFrameService(),
            resources=resolver,
            suggestion_service=suggestion_service,
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/reference-frames/local-fixture/roi-suggestions",
                json={"point": {"x": 160, "y": 120}},
            )

        assert response.status_code == 200, response.text
        result = cast("_SuggestionResponse", response.json())
        assert result["resource_id"] == "local-fixture"
        assert result["source_width"] == 320
        assert result["source_height"] == 240
        bbox = result["bbox"]
        assert bbox["x"] <= 160 < bbox["x"] + bbox["width"]
        assert bbox["y"] <= 120 < bbox["y"] + bbox["height"]
        assert result["mask_preview"]["width"] == 320
        assert result["mask_preview"]["height"] == 240
        assert "synthetic-object.png" not in response.text
    finally:
        predictor.close()
