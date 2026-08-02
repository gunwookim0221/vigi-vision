from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from tools.assisted_roi_validation import (
    BoundingBox,
    ImageSize,
    Mask,
    MaskRejection,
    Point,
    PredictionCandidate,
    ValidatedMaskCandidate,
    binarize_logits,
    expand_minimum_box,
    sort_prediction_candidates,
    select_valid_mask_candidate,
)


@dataclass(frozen=True, slots=True)
class ModelPrediction:
    mask: Mask
    bbox: BoundingBox
    score: float
    size: ImageSize
    elapsed_ms: float
    mask_pixel_count: int
    coverage_percent: float
    candidate_index: int
    candidate_count: int


@dataclass(frozen=True, slots=True)
class PredictorUnavailableError(Exception):
    def __str__(self) -> str:
        return "EfficientSAM runtime is unavailable"


@dataclass(frozen=True, slots=True)
class PredictionFailedError(Exception):
    def __str__(self) -> str:
        return "EfficientSAM inference failed"


@dataclass(frozen=True, slots=True)
class PredictionRejectedError(Exception):
    reason: str
    score: float | None = None
    mask_pixel_count: int | None = None
    coverage_percent: float | None = None
    bbox: BoundingBox | None = None
    candidate_index: int | None = None
    candidate_count: int | None = None
    elapsed_ms: float | None = None

    def __str__(self) -> str:
        return self.reason


class EfficientSamPredictor:
    def __init__(self, checkpoint: Path, device: str) -> None:
        self._checkpoint = checkpoint
        self._device_name = device
        self._model = None
        self._torch = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None and self._torch is not None

    def _load_model(self):
        if self._model is not None and self._torch is not None:
            return self._model, self._torch
        try:
            import torch
            from efficient_sam.efficient_sam import build_efficient_sam
        except (ImportError, ModuleNotFoundError) as error:
            raise PredictorUnavailableError from error
        device = self._device_name
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self._device_name = device
        if device == "cuda" and not torch.cuda.is_available():
            raise PredictorUnavailableError
        model = build_efficient_sam(
            encoder_patch_embed_dim=192,
            encoder_num_heads=3,
            checkpoint=str(self._checkpoint),
        )
        self._model = model.to(device).eval()
        self._torch = torch
        return self._model, self._torch

    def predict(self, image_path: Path, point: Point) -> ModelPrediction:
        started = time.perf_counter()
        try:
            from PIL import Image
            from torchvision.transforms.functional import to_tensor

            model, torch = self._load_model()
            with Image.open(image_path) as opened:
                image = opened.convert("RGB")
            size = ImageSize(width=image.width, height=image.height)
            if point.x < 0 or point.y < 0 or point.x >= size.width or point.y >= size.height:
                raise PredictionFailedError
            image_tensor = to_tensor(image).unsqueeze(0).to(self._device_name)
            input_points = torch.tensor(
                [[[[point.x, point.y]]]], dtype=torch.int64, device=self._device_name
            )
            input_labels = torch.tensor([[[1]]], dtype=torch.int64, device=self._device_name)
            with torch.inference_mode():
                logits, scores = model(image_tensor, input_points, input_labels)
            logits_shape = tuple(int(dimension) for dimension in logits.shape)
            scores_shape = tuple(int(dimension) for dimension in scores.shape)
            if (
                len(logits_shape) != 5
                or len(scores_shape) != 3
                or logits_shape[:3] != scores_shape
                or logits_shape[3:] != (size.height, size.width)
                or logits_shape[2] == 0
            ):
                raise PredictionRejectedError(
                    "invalid-output-shape", elapsed_ms=(time.perf_counter() - started) * 1000.0
                )
            raw_candidates = tuple(
                PredictionCandidate(
                    mask=binarize_logits(logits[0, 0, index].detach().cpu().numpy().tolist()),
                    score=float(scores[0, 0, index].detach().cpu().item()),
                )
                for index in range(int(logits.shape[2]))
            )
            candidates = tuple(candidate for _index, candidate in sort_prediction_candidates(raw_candidates))
            selected = select_valid_mask_candidate(candidates, point, size)
            if isinstance(selected, MaskRejection):
                raise PredictionRejectedError(
                    reason=selected.reason,
                    score=selected.score,
                    mask_pixel_count=selected.mask_pixel_count,
                    coverage_percent=selected.coverage_percent,
                    bbox=selected.bbox,
                    candidate_index=selected.candidate_index,
                    candidate_count=len(candidates),
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )
            if not isinstance(selected, ValidatedMaskCandidate):
                raise PredictionFailedError
            return ModelPrediction(
                mask=selected.mask,
                bbox=expand_minimum_box(selected.bbox, size),
                score=selected.score,
                size=size,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                mask_pixel_count=selected.mask_pixel_count,
                coverage_percent=selected.coverage_percent,
                candidate_index=selected.candidate_index,
                candidate_count=len(candidates),
            )
        except (ImportError, ModuleNotFoundError) as error:
            raise PredictorUnavailableError from error
        except (OSError, RuntimeError, TypeError, ValueError, IndexError) as error:
            raise PredictionFailedError from error
