from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tools.assisted_roi_validation import Point
from tools.efficient_sam_predictor import ModelPrediction


class ImageLike(Protocol):
    width: int
    height: int

    def convert(self, mode: str) -> ImageLike: ...


@dataclass(frozen=True, slots=True)
class OverlayRequest:
    output_root: Path
    relative_path: str
    image: ImageLike
    prediction: ModelPrediction | None
    point: Point | None = None


def point_marker_geometry(
    point: Point, width: int, height: int
) -> tuple[tuple[int, int, int, int], tuple[tuple[int, int, int, int], ...]] | None:
    if not (0 <= point.x < width and 0 <= point.y < height):
        return None
    radius = max(4, min(10, min(width, height) // 40))
    return (
        (point.x - radius, point.y - radius, point.x + radius, point.y + radius),
        (
            (point.x - radius - 3, point.y, point.x + radius + 3, point.y),
            (point.x, point.y - radius - 3, point.x, point.y + radius + 3),
        ),
    )


def render_frame(
    image: ImageLike,
    prediction: ModelPrediction | None,
    point: Point | None = None,
) -> ImageLike:
    from PIL import Image, ImageDraw
    import numpy as np

    displayed = image.convert("RGBA")
    if prediction is not None:
        mask = np.asarray(prediction.mask, dtype=np.uint8) * 95
        if mask.shape == (displayed.height, displayed.width):
            overlay = Image.new("RGBA", displayed.size, (0, 170, 255, 0))
            overlay.putalpha(Image.fromarray(mask, mode="L"))
            displayed = Image.alpha_composite(displayed, overlay)
        drawer = ImageDraw.Draw(displayed)
        box = prediction.bbox
        drawer.rectangle(
            (box.x, box.y, box.x + box.width - 1, box.y + box.height - 1),
            outline="red",
            width=4,
        )
    geometry = point_marker_geometry(point, displayed.width, displayed.height) if point else None
    if geometry is not None:
        drawer = ImageDraw.Draw(displayed)
        circle, lines = geometry
        drawer.ellipse(circle, outline="yellow", width=3)
        for line in lines:
            drawer.line(line, fill="yellow", width=2)
    return displayed


def write_overlay(request: OverlayRequest) -> str | None:
    relative_name = hashlib.sha256(request.relative_path.encode("utf-8")).hexdigest()[:16] + ".jpg"
    output = request.output_root / "overlays" / relative_name
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        image = render_frame(request.image, request.prediction, request.point)
        image.convert("RGB").save(output, quality=92)
    except (ImportError, OSError, ValueError, TypeError):
        if output.is_file():
            output.unlink(missing_ok=True)
        return None
    return output.relative_to(request.output_root).as_posix()
