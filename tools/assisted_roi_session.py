from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Literal, Sequence

from tools.assisted_roi_validation import BoundingBox, Classification, FrameRecord, Point

Recommendation = Literal[
    "proceed",
    "proceed_with_limitations",
    "do_not_proceed",
    "insufficient_evidence",
]


@dataclass(frozen=True, slots=True)
class SessionItem:
    source_path: str
    resource_id: str | None
    channel_id: int | None
    source_width: int
    source_height: int
    point: Point | None
    bbox: BoundingBox | None
    classification: Classification
    inference_ms: float | None
    overlay_path: str | None
    notes: str
    mask_pixel_count: int | None = None
    mask_coverage_percent: float | None = None
    selected_score: float | None = None
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SessionDocument:
    checkpoint_name: str
    expected_sha256: str
    actual_sha256: str | None
    device: str
    items: tuple[SessionItem, ...]
    updated_at_utc: str


@dataclass(frozen=True, slots=True)
class MetricSummary:
    total: int
    evaluated: int
    success: int
    partial: int
    failure: int
    skipped: int
    success_rate: float
    average_inference_ms: float | None
    median_inference_ms: float | None
    p95_inference_ms: float | None
    recommendation: Recommendation


@dataclass(frozen=True, slots=True)
class ChannelSummary:
    channel_id: int | None
    total: int
    evaluated: int
    success: int
    partial: int
    failure: int
    skipped: int
    success_rate: float


@dataclass(frozen=True, slots=True)
class SessionFormatError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def _point_to_json(point: Point | None) -> dict[str, int] | None:
    return None if point is None else {"x": point.x, "y": point.y}


def _bbox_to_json(box: BoundingBox | None) -> dict[str, int] | None:
    return (
        None if box is None else {"x": box.x, "y": box.y, "width": box.width, "height": box.height}
    )


def _item_to_json(item: SessionItem) -> dict[str, str | int | float | None | dict[str, int]]:
    return {
        "source_path": item.source_path,
        "resource_id": item.resource_id,
        "channel_id": item.channel_id,
        "source_width": item.source_width,
        "source_height": item.source_height,
        "point": _point_to_json(item.point),
        "bbox": _bbox_to_json(item.bbox),
        "classification": item.classification,
        "inference_ms": item.inference_ms,
        "overlay_path": item.overlay_path,
        "notes": item.notes,
        "mask_pixel_count": item.mask_pixel_count,
        "mask_coverage_percent": item.mask_coverage_percent,
        "selected_score": item.selected_score,
        "failure_reason": item.failure_reason,
    }


def _parse_point(raw: dict[str, int] | None) -> Point | None:
    return None if raw is None else Point(x=int(raw["x"]), y=int(raw["y"]))


def _parse_bbox(raw: dict[str, int] | None) -> BoundingBox | None:
    return (
        None
        if raw is None
        else BoundingBox(
            x=int(raw["x"]),
            y=int(raw["y"]),
            width=int(raw["width"]),
            height=int(raw["height"]),
        )
    )


def _item_from_json(raw: dict[str, str | int | float | None | dict[str, int]]) -> SessionItem:
    classification = raw["classification"]
    if classification not in {"success", "partial", "failure", "skip"}:
        raise SessionFormatError("classification_invalid")
    point = raw["point"]
    bbox = raw["bbox"]
    return SessionItem(
        source_path=str(raw["source_path"]),
        resource_id=None if raw["resource_id"] is None else str(raw["resource_id"]),
        channel_id=None if raw["channel_id"] is None else int(raw["channel_id"]),
        source_width=int(raw["source_width"]),
        source_height=int(raw["source_height"]),
        point=_parse_point(point if isinstance(point, dict) else None),
        bbox=_parse_bbox(bbox if isinstance(bbox, dict) else None),
        classification=classification,
        inference_ms=None if raw["inference_ms"] is None else float(raw["inference_ms"]),
        overlay_path=None if raw["overlay_path"] is None else str(raw["overlay_path"]),
        notes=str(raw["notes"]),
        mask_pixel_count=None
        if raw.get("mask_pixel_count") is None
        else int(raw["mask_pixel_count"]),
        mask_coverage_percent=(
            None
            if raw.get("mask_coverage_percent") is None
            else float(raw["mask_coverage_percent"])
        ),
        selected_score=None if raw.get("selected_score") is None else float(raw["selected_score"]),
        failure_reason=None if raw.get("failure_reason") is None else str(raw["failure_reason"]),
    )


def new_session_item(record: FrameRecord) -> SessionItem:
    width = record.size.width if record.size is not None else 0
    height = record.size.height if record.size is not None else 0
    return SessionItem(
        source_path=record.relative_path,
        resource_id=record.resource_id,
        channel_id=record.channel_id,
        source_width=width,
        source_height=height,
        point=None,
        bbox=None,
        classification="skip",
        inference_ms=None,
        overlay_path=None,
        notes=record.metadata_warning or "",
    )


def merge_session_items(
    records: Sequence[FrameRecord], existing: Sequence[SessionItem]
) -> tuple[SessionItem, ...]:
    known = {item.source_path for item in existing}
    additions = tuple(
        new_session_item(record) for record in records if record.relative_path not in known
    )
    return tuple(existing) + additions


def write_session(path: Path, document: SessionDocument) -> None:
    payload = {
        "schema_version": 1,
        "checkpoint_name": document.checkpoint_name,
        "expected_sha256": document.expected_sha256,
        "actual_sha256": document.actual_sha256,
        "device": document.device,
        "updated_at_utc": document.updated_at_utc,
        "items": [_item_to_json(item) for item in document.items],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_session(path: Path) -> SessionDocument:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SessionFormatError("session_invalid") from error
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise SessionFormatError("session_invalid")
    items = raw.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise SessionFormatError("session_invalid")
    try:
        return SessionDocument(
            checkpoint_name=str(raw["checkpoint_name"]),
            expected_sha256=str(raw["expected_sha256"]),
            actual_sha256=None if raw["actual_sha256"] is None else str(raw["actual_sha256"]),
            device=str(raw["device"]),
            items=tuple(_item_from_json(item) for item in items),
            updated_at_utc=str(raw["updated_at_utc"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SessionFormatError("session_invalid") from error


def recommendation_for(evaluated: int, success_rate: float) -> Recommendation:
    if evaluated < 20:
        return "insufficient_evidence"
    if success_rate >= 70.0:
        return "proceed"
    if success_rate >= 50.0:
        return "proceed_with_limitations"
    return "do_not_proceed"


def _round_rate(success: int, evaluated: int) -> float:
    return 0.0 if evaluated == 0 else round(success / evaluated * 100.0, 2)


def _timing(items: Sequence[SessionItem]) -> tuple[float | None, float | None, float | None]:
    values = sorted(item.inference_ms for item in items if item.inference_ms is not None)
    if not values:
        return None, None, None
    index = max(0, math.ceil(len(values) * 0.95) - 1)
    return mean(values), median(values), values[index]


def metrics_for(items: Sequence[SessionItem]) -> MetricSummary:
    counts = {"success": 0, "partial": 0, "failure": 0, "skip": 0}
    for item in items:
        counts[item.classification] += 1
    evaluated = counts["success"] + counts["partial"] + counts["failure"]
    average, median_value, p95 = _timing(items)
    rate = _round_rate(counts["success"], evaluated)
    return MetricSummary(
        total=len(items),
        evaluated=evaluated,
        success=counts["success"],
        partial=counts["partial"],
        failure=counts["failure"],
        skipped=counts["skip"],
        success_rate=rate,
        average_inference_ms=average,
        median_inference_ms=median_value,
        p95_inference_ms=p95,
        recommendation=recommendation_for(evaluated, rate),
    )


def channel_metrics_for(items: Sequence[SessionItem]) -> tuple[ChannelSummary, ...]:
    channel_ids = sorted({item.channel_id for item in items if item.channel_id is not None})
    summaries: list[ChannelSummary] = []
    for channel_id in channel_ids:
        channel_items = tuple(item for item in items if item.channel_id == channel_id)
        metrics = metrics_for(channel_items)
        summaries.append(
            ChannelSummary(
                channel_id=channel_id,
                total=metrics.total,
                evaluated=metrics.evaluated,
                success=metrics.success,
                partial=metrics.partial,
                failure=metrics.failure,
                skipped=metrics.skipped,
                success_rate=metrics.success_rate,
            )
        )
    return tuple(summaries)
