from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypeAlias, final

if TYPE_CHECKING:
    from collections.abc import Sequence

Mask: TypeAlias = tuple[tuple[bool, ...], ...]
MaskRun: TypeAlias = tuple[int, int]
MaskPreviewRows: TypeAlias = tuple[tuple[MaskRun, ...], ...]
MaskRejectionReason: TypeAlias = Literal[
    "empty-mask",
    "background-dominant",
    "point-not-in-mask",
    "invalid-mask-shape",
]

_DEFAULT_MAX_COVERAGE_PERCENT: Final = 95.0
MAX_MASK_PREVIEW_RUNS: Final = 50_000


@final
@dataclass(frozen=True, slots=True)
class ImageSize:
    width: int
    height: int

    def __post_init__(self) -> None:
        if (
            type(self.width) is not int
            or type(self.height) is not int
            or self.width <= 0
            or self.height <= 0
        ):
            raise ValueError


@final
@dataclass(frozen=True, slots=True)
class Point:
    x: int
    y: int


@final
@dataclass(frozen=True, slots=True)
class BoundingBox:
    x: int
    y: int
    width: int
    height: int


@final
@dataclass(frozen=True, slots=True)
class MaskPreview:
    width: int
    height: int
    rows: MaskPreviewRows


@final
@dataclass(frozen=True, slots=True)
class MaskCandidate:
    mask: Mask
    score: float


PredictionCandidate: TypeAlias = MaskCandidate


@final
@dataclass(frozen=True, slots=True)
class ValidatedMaskCandidate:
    mask: Mask
    score: float
    candidate_index: int
    bbox: BoundingBox
    mask_pixel_count: int
    coverage_percent: float


@final
@dataclass(frozen=True, slots=True)
class MaskRejection:
    reason: MaskRejectionReason
    score: float
    candidate_index: int
    mask_pixel_count: int
    coverage_percent: float
    bbox: BoundingBox | None


def binarize_logits(logits: Sequence[Sequence[float]]) -> Mask:
    return tuple(tuple(value >= 0.0 for value in row) for row in logits)


def select_valid_mask_candidate(
    candidates: Sequence[MaskCandidate],
    point: Point,
    size: ImageSize,
    max_coverage_percent: float = _DEFAULT_MAX_COVERAGE_PERCENT,
) -> ValidatedMaskCandidate | MaskRejection:
    if not math.isfinite(max_coverage_percent) or max_coverage_percent <= 0.0:
        raise ValueError
    total_pixels = size.width * size.height
    rejections: list[MaskRejection] = []
    ranked = sorted(enumerate(candidates), key=lambda item: (-item[1].score, item[0]))
    for candidate_index, candidate in ranked:
        if not math.isfinite(candidate.score) or not mask_matches_size(candidate.mask, size):
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
        coverage_percent = pixel_count / total_pixels * 100.0
        box = mask_to_bounding_box(candidate.mask, size)
        if pixel_count == 0:
            reason: MaskRejectionReason = "empty-mask"
        elif coverage_percent >= max_coverage_percent:
            reason = "background-dominant"
        elif not _contains_point(candidate.mask, point):
            reason = "point-not-in-mask"
        elif box is None or not _bbox_contains_point(box, point):
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
                bbox=None if reason == "background-dominant" else box,
            )
        )
    if not rejections:
        return MaskRejection("invalid-mask-shape", 0.0, -1, 0, 0.0, None)
    priority: dict[MaskRejectionReason, int] = {
        "background-dominant": 0,
        "point-not-in-mask": 1,
        "empty-mask": 2,
        "invalid-mask-shape": 3,
    }
    return min(rejections, key=lambda item: (priority[item.reason], item.candidate_index))


def mask_to_bounding_box(mask: Mask, size: ImageSize) -> BoundingBox | None:
    coordinates = [
        (x, y)
        for y, row in enumerate(mask)
        for x, value in enumerate(row)
        if value and x < size.width and y < size.height
    ]
    if not coordinates:
        return None
    xs = [coordinate[0] for coordinate in coordinates]
    ys = [coordinate[1] for coordinate in coordinates]
    box = BoundingBox(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
    return clamp_bounding_box(box, size)


def mask_to_preview(
    mask: Mask,
    size: ImageSize,
    max_runs: int = MAX_MASK_PREVIEW_RUNS,
) -> MaskPreview:
    if max_runs <= 0 or not mask_matches_size(mask, size):
        raise ValueError
    rows: list[tuple[MaskRun, ...]] = []
    run_count = 0
    for source_row in mask:
        row_runs: list[MaskRun] = []
        run_start: int | None = None
        for x, value in enumerate((*source_row, False)):
            if value and run_start is None:
                run_start = x
            elif not value and run_start is not None:
                row_runs.append((run_start, x))
                run_count += 1
                if run_count > max_runs:
                    raise ValueError
                run_start = None
        rows.append(tuple(row_runs))
    return MaskPreview(size.width, size.height, tuple(rows))


def bounding_box_to_mask_preview(box: BoundingBox, size: ImageSize) -> MaskPreview:
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
    ):
        raise ValueError
    rows = tuple(
        ((box.x, box.x + box.width),) if box.y <= y < box.y + box.height else ()
        for y in range(size.height)
    )
    return MaskPreview(size.width, size.height, rows)


def mask_preview_to_bounding_box(preview: MaskPreview) -> BoundingBox | None:
    if not mask_preview_matches_size(preview):
        return None
    left: int | None = None
    top: int | None = None
    right: int | None = None
    bottom: int | None = None
    for y, row in enumerate(preview.rows):
        previous_end = 0
        for start, end in row:
            if (
                type(start) is not int
                or type(end) is not int
                or start < 0
                or start >= end
                or end > preview.width
                or start < previous_end
            ):
                return None
            previous_end = end
            left = start if left is None else min(left, start)
            top = y if top is None else min(top, y)
            right = end if right is None else max(right, end)
            bottom = y + 1 if bottom is None else max(bottom, y + 1)
    if left is None or top is None or right is None or bottom is None:
        return None
    return BoundingBox(left, top, right - left, bottom - top)


def mask_preview_matches_size(preview: MaskPreview) -> bool:
    return (
        type(preview.width) is int
        and type(preview.height) is int
        and preview.width > 0
        and preview.height > 0
        and len(preview.rows) == preview.height
    )


def clamp_bounding_box(box: BoundingBox, size: ImageSize) -> BoundingBox:
    width = min(max(box.width, 1), size.width)
    height = min(max(box.height, 1), size.height)
    x = max(0, min(box.x, size.width - width))
    y = max(0, min(box.y, size.height - height))
    return BoundingBox(x, y, width, height)


def mask_matches_size(mask: Mask, size: ImageSize) -> bool:
    return len(mask) == size.height and all(len(row) == size.width for row in mask)


def sort_prediction_candidates(
    candidates: Sequence[MaskCandidate],
) -> tuple[tuple[int, MaskCandidate], ...]:
    return tuple(sorted(enumerate(candidates), key=lambda item: (-item[1].score, item[0])))


def _contains_point(mask: Mask, point: Point) -> bool:
    return 0 <= point.y < len(mask) and 0 <= point.x < len(mask[point.y]) and mask[point.y][point.x]


def _bbox_contains_point(box: BoundingBox, point: Point) -> bool:
    return box.x <= point.x < box.x + box.width and box.y <= point.y < box.y + box.height
