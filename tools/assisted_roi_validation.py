from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

Classification = Literal["success", "partial", "failure", "skip"]
Mask = tuple[tuple[bool, ...], ...]
MaskRejectionReason = Literal[
    "empty-mask",
    "background-dominant",
    "point-not-in-mask",
    "invalid-mask-shape",
]


@dataclass(frozen=True, slots=True)
class ImageSize:
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class BoundingBox:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class PredictionCandidate:
    mask: Mask
    score: float


@dataclass(frozen=True, slots=True)
class ValidatedMaskCandidate:
    mask: Mask
    score: float
    candidate_index: int
    bbox: BoundingBox
    mask_pixel_count: int
    coverage_percent: float


@dataclass(frozen=True, slots=True)
class MaskRejection:
    reason: MaskRejectionReason
    score: float
    candidate_index: int
    mask_pixel_count: int
    coverage_percent: float
    bbox: BoundingBox | None


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


def binarize_logits(logits: Sequence[Sequence[float]]) -> Mask:
    return tuple(tuple(value >= 0.0 for value in row) for row in logits)


def sort_prediction_candidates(
    candidates: Sequence[PredictionCandidate],
) -> tuple[tuple[int, PredictionCandidate], ...]:
    return tuple(
        sorted(
            enumerate(candidates),
            key=lambda pair: (-pair[1].score, pair[0]),
        )
    )


def select_valid_mask_candidate(
    candidates: Sequence[PredictionCandidate],
    point: Point,
    size: ImageSize,
    max_coverage_percent: float = 95.0,
) -> ValidatedMaskCandidate | MaskRejection:
    rejections: list[MaskRejection] = []
    total_pixels = size.width * size.height
    for candidate_index, candidate in sort_prediction_candidates(candidates):
        if not mask_matches_size(candidate.mask, size):
            rejections.append(
                MaskRejection(
                    reason="invalid-mask-shape",
                    score=candidate.score,
                    candidate_index=candidate_index,
                    mask_pixel_count=0,
                    coverage_percent=0.0,
                    bbox=None,
                )
            )
            continue
        pixel_count = sum(sum(row) for row in candidate.mask)
        coverage_percent = 0.0 if total_pixels == 0 else pixel_count / total_pixels * 100.0
        box = mask_to_bounding_box(candidate.mask, size)
        if pixel_count == 0:
            reason: MaskRejectionReason = "empty-mask"
        elif coverage_percent >= max_coverage_percent:
            reason = "background-dominant"
        elif not _contains_point(candidate.mask, point):
            reason = "point-not-in-mask"
        else:
            if box is None:
                reason = "empty-mask"
            else:
                return ValidatedMaskCandidate(
                    mask=candidate.mask,
                    score=candidate.score,
                    candidate_index=candidate_index,
                    bbox=box,
                    mask_pixel_count=pixel_count,
                    coverage_percent=coverage_percent,
                )
        rejections.append(
            MaskRejection(
                reason=reason,
                score=candidate.score,
                candidate_index=candidate_index,
                mask_pixel_count=pixel_count,
                coverage_percent=coverage_percent,
                bbox=box if reason != "background-dominant" else None,
            )
        )
    if rejections:
        priority = {
            "background-dominant": 0,
            "point-not-in-mask": 1,
            "empty-mask": 2,
            "invalid-mask-shape": 3,
        }
        return min(
            rejections,
            key=lambda rejection: (priority[rejection.reason], rejection.candidate_index),
        )
    return MaskRejection(
        reason="invalid-mask-shape",
        score=0.0,
        candidate_index=-1,
        mask_pixel_count=0,
        coverage_percent=0.0,
        bbox=None,
    )


def mask_to_bounding_box(mask: Mask, size: ImageSize) -> BoundingBox | None:
    points = (
        (x, y)
        for y, row in enumerate(mask)
        for x, value in enumerate(row)
        if value and x < size.width and y < size.height
    )
    coordinates = list(points)
    if not coordinates:
        return None
    xs, ys = zip(*coordinates)
    return BoundingBox(
        x=min(xs),
        y=min(ys),
        width=max(xs) - min(xs) + 1,
        height=max(ys) - min(ys) + 1,
    )


def mask_matches_size(mask: Mask, size: ImageSize) -> bool:
    return len(mask) == size.height and all(len(row) == size.width for row in mask)


def expand_minimum_box(box: BoundingBox, size: ImageSize, minimum: int = 4) -> BoundingBox:
    width = min(max(box.width, minimum), size.width)
    height = min(max(box.height, minimum), size.height)
    x = max(0, min(box.x, size.width - width))
    y = max(0, min(box.y, size.height - height))
    return BoundingBox(x=x, y=y, width=width, height=height)
