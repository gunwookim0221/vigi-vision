from __future__ import annotations

import collections.abc
import hashlib
import importlib
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn, Protocol, cast, final

from typing_extensions import Self, override

from vigi_vision.assisted_roi_geometry import (
    BoundingBox,
    ImageSize,
    MaskCandidate,
    Point,
    ValidatedMaskCandidate,
    mask_to_preview,
    select_valid_mask_candidate,
)
from vigi_vision.assisted_roi_service import (
    RoiPrediction,
    RoiPredictor,
    RoiSuggestionInferenceError,
    RoiSuggestionNoValidSuggestionError,
    RoiSuggestionUnavailableError,
)

if TYPE_CHECKING:
    from vigi_vision.object_presence_models import DecodedRgbImage

_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_ARTIFACT_ROOT_NAMES = frozenset({"artifacts", "reference-frames"})


class _Tensor(Protocol):
    @property
    def shape(self) -> collections.abc.Sequence[int]: ...

    def to(self, device: str) -> _Tensor: ...

    def unsqueeze(self, dimension: int) -> _Tensor: ...

    def detach(self) -> _Tensor: ...

    def cpu(self) -> _Tensor: ...

    def numpy(self) -> object: ...

    def tolist(self) -> object: ...

    def item(self) -> object: ...

    def __getitem__(self, key: object) -> _Tensor: ...


class _InferenceContext(Protocol):
    def __enter__(self) -> None: ...

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None: ...


class _TorchModule(Protocol):
    int64: object
    cuda: _Cuda

    def tensor(self, data: object, *, dtype: object, device: str) -> _Tensor: ...

    def inference_mode(self) -> _InferenceContext: ...


class _Model(Protocol):
    def to(self, device: str) -> _Model: ...

    def eval(self) -> _Model: ...

    def __call__(
        self, image: _Tensor, points: _Tensor, labels: _Tensor
    ) -> tuple[_Tensor, _Tensor]: ...


class _EfficientSamFactory(Protocol):
    def __call__(
        self, *, encoder_patch_embed_dim: int, encoder_num_heads: int, checkpoint: str
    ) -> _Model: ...


class _Cuda(Protocol):
    def is_available(self) -> bool: ...


class _EfficientSamModule(Protocol):
    build_efficient_sam: _EfficientSamFactory


class _FunctionalModule(Protocol):
    to_tensor: _ToTensor


class _Image(Protocol):
    width: int
    height: int

    def convert(self, mode: str) -> _Image: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None: ...


class _ImageModule(Protocol):
    def open(self, path: Path) -> _Image: ...

    def frombytes(self, mode: str, size: tuple[int, int], data: bytes) -> _Image: ...


class _ToTensor(Protocol):
    def __call__(self, image: _Image) -> _Tensor: ...


@final
@dataclass(slots=True)
class LazyEfficientSamPredictor(RoiPredictor):
    checkpoint_path: Path | None
    expected_sha256: str | None
    device_mode: str
    _model: _Model | None = field(default=None, init=False, repr=False)
    _torch: _TorchModule | None = field(default=None, init=False, repr=False)
    _device: str | None = field(default=None, init=False, repr=False)
    _unavailable: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def is_loaded(self) -> bool:
        return self._model is not None and self._torch is not None

    @property
    def is_unavailable(self) -> bool:
        return self._unavailable

    @override
    def predict(self, image_path: Path, point: Point, size: ImageSize) -> BoundingBox:
        return self.predict_with_mask(image_path, point, size).bbox

    def predict_with_mask(self, image_path: Path, point: Point, size: ImageSize) -> RoiPrediction:
        try:
            with self._lock:
                model, torch, device, image_module, to_tensor = self._runtime()
                image = _read_image(image_module, image_path, size)
                image_tensor = to_tensor(image).unsqueeze(0).to(device)
                return _predict_image(model, torch, device, image_tensor, point, size)
        except (RoiSuggestionNoValidSuggestionError, RoiSuggestionUnavailableError):
            raise
        except (
            ImportError,
            ModuleNotFoundError,
            OSError,
            ValueError,
            RuntimeError,
            TypeError,
        ) as error:
            if self._model is None:
                self._unavailable = True
                raise RoiSuggestionUnavailableError from error
            raise RoiSuggestionInferenceError from error

    def predict_from_rgb(
        self, image: DecodedRgbImage, point: Point, size: ImageSize
    ) -> RoiPrediction:
        """Predict from an already decoded RGB value without reopening a path."""
        try:
            with self._lock:
                model, torch, device, image_module, to_tensor = self._runtime()
                image_value = _image_from_rgb(image_module, image)
                image_tensor = to_tensor(image_value).unsqueeze(0).to(device)
                return _predict_image(model, torch, device, image_tensor, point, size)
        except (RoiSuggestionNoValidSuggestionError, RoiSuggestionUnavailableError):
            raise
        except (
            ImportError,
            ModuleNotFoundError,
            OSError,
            ValueError,
            RuntimeError,
            TypeError,
        ) as error:
            if self._model is None:
                self._unavailable = True
                raise RoiSuggestionUnavailableError from error
            raise RoiSuggestionInferenceError from error

    def close(self) -> None:
        with self._lock:
            self._model = None
            self._torch = None
            self._device = None

    def _runtime(self) -> tuple[_Model, _TorchModule, str, _ImageModule, _ToTensor]:
        if self._unavailable:
            raise RoiSuggestionUnavailableError
        if self._model is not None and self._torch is not None and self._device is not None:
            image_module, to_tensor = _load_image_dependencies()
            return self._model, self._torch, self._device, image_module, to_tensor
        try:
            _verify_checkpoint(self.checkpoint_path, self.expected_sha256)
            torch = _load_torch()
            efficient_sam = importlib.import_module("efficient_sam.efficient_sam")
            factory = cast("_EfficientSamModule", cast("object", efficient_sam)).build_efficient_sam
            device = _select_device(torch, self.device_mode)
            model = (
                factory(
                    encoder_patch_embed_dim=192,
                    encoder_num_heads=3,
                    checkpoint=str(self.checkpoint_path),
                )
                .to(device)
                .eval()
            )
            image_module, to_tensor = _load_image_dependencies()
        except (
            ImportError,
            ModuleNotFoundError,
            OSError,
            ValueError,
            RuntimeError,
            TypeError,
        ) as error:
            self._unavailable = True
            raise RoiSuggestionUnavailableError from error
        self._model = model
        self._torch = torch
        self._device = device
        return model, torch, device, image_module, to_tensor


def _load_torch() -> _TorchModule:
    return cast("_TorchModule", cast("object", importlib.import_module("torch")))


def _load_image_dependencies() -> tuple[_ImageModule, _ToTensor]:
    pillow = cast("_ImageModule", cast("object", importlib.import_module("PIL.Image")))
    transforms = cast(
        "_FunctionalModule",
        cast("object", importlib.import_module("torchvision.transforms.functional")),
    )
    return pillow, transforms.to_tensor


def _select_device(torch: _TorchModule, mode: str) -> Literal["cpu", "cuda"]:
    if mode not in {"cpu", "cuda", "auto"}:
        raise ValueError
    if mode == "cpu":
        return "cpu"
    if mode == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError
        return "cuda"
    return "cuda" if torch.cuda.is_available() else "cpu"


def _verify_checkpoint(path: Path | None, expected_sha256: str | None) -> None:
    if (
        path is None
        or expected_sha256 is None
        or _SHA256_PATTERN.fullmatch(expected_sha256) is None
    ):
        raise ValueError
    if path.is_symlink():
        raise OSError
    resolved = path.resolve()
    if not resolved.is_file():
        raise OSError
    if any(part.casefold() in _ARTIFACT_ROOT_NAMES for part in resolved.parts):
        raise OSError
    digest = hashlib.sha256()
    with resolved.open("rb") as checkpoint:
        for chunk in iter(lambda: checkpoint.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest().casefold() != expected_sha256.casefold():
        raise ValueError


def _read_image(image_module: _ImageModule, path: Path, size: ImageSize) -> _Image:
    with image_module.open(path) as opened:
        image = opened.convert("RGB")
    if image.width != size.width or image.height != size.height:
        raise ValueError
    return image


def _image_from_rgb(image_module: _ImageModule, image: DecodedRgbImage) -> _Image:
    payload = bytes(channel for row in image.pixels for pixel in row for channel in pixel)
    return image_module.frombytes("RGB", (image.width, image.height), payload)


def _predict_image(  # noqa: PLR0913 - adapter keeps the model boundary explicit.
    model: _Model,
    torch: _TorchModule,
    device: str,
    image_tensor: _Tensor,
    point: Point,
    size: ImageSize,
) -> RoiPrediction:
    input_points = torch.tensor([[[[point.x, point.y]]]], dtype=torch.int64, device=device)
    input_labels = torch.tensor([[[1]]], dtype=torch.int64, device=device)
    with torch.inference_mode():
        logits, scores = model(image_tensor, input_points, input_labels)
    candidates = candidates_from_output(logits, scores, size)
    selected = select_valid_mask_candidate(candidates, point, size)
    if not isinstance(selected, ValidatedMaskCandidate):
        _raise_no_valid_suggestion()
    try:
        preview = mask_to_preview(selected.mask, size)
    except ValueError:
        _raise_no_valid_suggestion()
    return RoiPrediction(selected.bbox, preview)


def candidates_from_output(
    logits: _Tensor, scores: _Tensor, size: ImageSize
) -> tuple[MaskCandidate, ...]:
    logits_shape = _shape(logits)
    scores_shape = _shape(scores)
    if (
        len(logits_shape) != _LOGITS_DIMENSIONS
        or len(scores_shape) != _SCORE_DIMENSIONS
        or logits_shape[:3] != scores_shape
        or logits_shape[3:] != (size.height, size.width)
        or logits_shape[0:2] != (1, 1)
        or logits_shape[2] <= 0
    ):
        raise ValueError
    candidates: list[MaskCandidate] = []
    for index in range(logits_shape[2]):
        raw_mask = logits[0, 0, index].detach().cpu().tolist()
        raw_score = scores[0, 0, index].detach().cpu().item()
        if not isinstance(raw_score, (int, float)) or not math.isfinite(float(raw_score)):
            raise ValueError
        mask = _mask_from_values(raw_mask, size)
        candidates.append(MaskCandidate(mask, float(raw_score)))
    return tuple(candidates)


def _shape(tensor: _Tensor) -> tuple[int, ...]:
    values = tuple(tensor.shape)
    if not all(type(value) is int for value in values):
        raise ValueError
    return values


def _mask_from_values(raw: object, size: ImageSize) -> tuple[tuple[bool, ...], ...]:
    if not isinstance(raw, collections.abc.Sequence) or len(raw) != size.height:
        raise ValueError
    rows: list[tuple[bool, ...]] = []
    for raw_row in raw:
        if not isinstance(raw_row, collections.abc.Sequence) or len(raw_row) != size.width:
            raise ValueError
        row: list[bool] = []
        for value in raw_row:
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise TypeError
            row.append(float(value) >= 0.0)
        rows.append(tuple(row))
    return tuple(rows)


_LOGITS_DIMENSIONS = 5
_SCORE_DIMENSIONS = 3


def _raise_no_valid_suggestion() -> NoReturn:
    raise RoiSuggestionNoValidSuggestionError
