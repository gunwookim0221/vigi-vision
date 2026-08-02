from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from vigi_vision.assisted_roi_geometry import (
    BoundingBox,
    ImageSize,
    Mask,
    MaskCandidate,
    MaskRejection,
    MaskRejectionReason,
    Point,
    ValidatedMaskCandidate,
    binarize_logits,
    mask_matches_size,
    mask_to_bounding_box,
    select_valid_mask_candidate,
    sort_prediction_candidates,
)

Classification = Literal["success", "partial", "failure", "skip"]
PredictionCandidate = MaskCandidate

__all__ = (
    "BoundingBox",
    "FrameOrder",
    "FrameRecord",
    "ImageSize",
    "MaskRejection",
    "Point",
    "PredictionCandidate",
    "ValidatedMaskCandidate",
    "binarize_logits",
    "discover_frames",
    "expand_minimum_box",
    "mask_matches_size",
    "mask_to_bounding_box",
    "order_frames",
    "select_mask_candidate",
    "select_valid_mask_candidate",
    "sort_prediction_candidates",
)


@dataclass(frozen=True, slots=True)
class FrameRecord:
    source_path: Path
    relative_path: str
    resource_id: str | None
    channel_id: int | None
    size: ImageSize | None
    metadata_warning: str | None


@dataclass(frozen=True, slots=True)
class FrameOrder:
    channel_id: int | None = None
    shuffle: bool = False
    seed: int = 0
    limit: int | None = None


def _manifest_metadata(
    path: Path, resource_id: str
) -> tuple[str | None, int | None, ImageSize | None, str | None]:
    manifest = path.parent / "manifest.json"
    if not manifest.is_file():
        return resource_id, None, None, "manifest_missing"
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return resource_id, None, None, "manifest_invalid"
    if not isinstance(raw, dict):
        return resource_id, None, None, "manifest_invalid"
    status = raw.get("status")
    manifest_resource = raw.get("resource_id")
    channel = raw.get("channel_id")
    width = raw.get("width")
    height = raw.get("height")
    if status != "completed" or manifest_resource != resource_id:
        return None, None, None, "manifest_incomplete"
    channel_id = channel if isinstance(channel, int) and not isinstance(channel, bool) else None
    size = (
        ImageSize(width=width, height=height)
        if isinstance(width, int)
        and isinstance(height, int)
        and not isinstance(width, bool)
        and not isinstance(height, bool)
        and width > 0
        and height > 0
        else None
    )
    return resource_id, channel_id, size, None


def discover_frames(input_root: Path) -> tuple[FrameRecord, ...]:
    records: list[FrameRecord] = []
    for path in sorted(input_root.rglob("frame.jpg"), key=lambda item: item.as_posix().casefold()):
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            continue
        resource_id, channel_id, size, warning = _manifest_metadata(path, path.parent.name)
        if warning == "manifest_incomplete":
            continue
        records.append(
            FrameRecord(
                source_path=path,
                relative_path=path.relative_to(input_root).as_posix(),
                resource_id=resource_id,
                channel_id=channel_id,
                size=size,
                metadata_warning=warning,
            )
        )
    return tuple(records)


def order_frames(records: Sequence[FrameRecord], options: FrameOrder) -> tuple[FrameRecord, ...]:
    selected = [
        record
        for record in records
        if options.channel_id is None or record.channel_id == options.channel_id
    ]
    if options.shuffle:
        random.Random(options.seed).shuffle(selected)
    if options.limit is not None:
        selected = selected[: options.limit]
    return tuple(selected)


def _contains_point(mask: Mask, point: Point) -> bool:
    return (
        point.y >= 0
        and point.x >= 0
        and point.y < len(mask)
        and point.x < len(mask[point.y])
        and mask[point.y][point.x]
    )


def select_mask_candidate(
    candidates: Sequence[PredictionCandidate], point: Point
) -> PredictionCandidate | None:
    eligible = [
        (candidate.score, -index, candidate)
        for index, candidate in enumerate(candidates)
        if _contains_point(candidate.mask, point)
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item[0], item[1]))[2]


def expand_minimum_box(box: BoundingBox, size: ImageSize, minimum: int = 4) -> BoundingBox:
    width = min(max(box.width, minimum), size.width)
    height = min(max(box.height, minimum), size.height)
    x = max(0, min(box.x, size.width - width))
    y = max(0, min(box.y, size.height - height))
    return BoundingBox(x=x, y=y, width=width, height=height)
