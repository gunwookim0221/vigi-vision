from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import tkinter as tk

from tools.assisted_roi_render import OverlayRequest, render_frame, write_overlay
from tools.assisted_roi_report import EnvironmentInfo, render_summary
from tools.assisted_roi_session import SessionDocument, SessionItem, metrics_for, write_session
from tools.assisted_roi_validation import BoundingBox, FrameRecord, Point
from tools.efficient_sam_predictor import (
    EfficientSamPredictor,
    ModelPrediction,
    PredictionFailedError,
    PredictionRejectedError,
    PredictorUnavailableError,
)


@dataclass(frozen=True, slots=True)
class GuiContext:
    records: tuple[FrameRecord, ...]
    document: SessionDocument
    predictor: EfficientSamPredictor
    output_root: Path
    session_path: Path
    summary_path: Path
    environment: EnvironmentInfo


def _format_bbox(bbox: BoundingBox | None) -> str:
    if bbox is None:
        return "n/a"
    return f"({bbox.x}, {bbox.y}, {bbox.width}, {bbox.height})"


def format_prediction_status(
    prediction: ModelPrediction | None,
    *,
    reason: str | None = None,
    score: float | None = None,
    mask_pixel_count: int | None = None,
    coverage_percent: float | None = None,
    bbox: BoundingBox | None = None,
) -> str:
    if prediction is not None:
        score = prediction.score
        mask_pixel_count = prediction.mask_pixel_count
        coverage_percent = prediction.coverage_percent
        bbox = prediction.bbox
    score_text = "n/a" if score is None else f"{score:.3f}"
    pixels_text = "n/a" if mask_pixel_count is None else str(mask_pixel_count)
    coverage_text = "n/a" if coverage_percent is None else f"{coverage_percent:.2f}%"
    details = (
        f"score={score_text}; mask pixels={pixels_text}; coverage={coverage_text}; "
        f"bbox={_format_bbox(bbox)}"
    )
    if prediction is not None:
        return f"Suggestion ready; {details}. Classify the result."
    return f"Safe result: {reason or 'inference-failed'}; {details}. No valid suggestion exists."


class ValidationApp:
    def __init__(self, context: GuiContext) -> None:
        from PIL import Image

        self._context = context
        self._records = context.records
        self._items = list(context.document.items)
        self._root = tk.Tk()
        self._root.title("EfficientSAM-Ti Assisted ROI Validation")
        self._root.protocol("WM_DELETE_WINDOW", self._quit)
        self._root.bind("<Left>", lambda _event: self._previous())
        self._root.bind("<Right>", lambda _event: self._next())
        self._index = 0
        self._image: Image.Image | None = None
        self._photo = None
        self._prediction: ModelPrediction | None = None
        self._busy = False
        self._closed = False
        self._scale = (1.0, 1.0)
        self._status = tk.StringVar(value="Tap an object in the frame to run inference.")
        self._classification = tk.StringVar(value="skip")
        self._notes = tk.StringVar(value="")
        self._canvas = tk.Canvas(self._root, width=900, height=700, background="#1a1a1a")
        self._canvas.grid(row=0, column=0, columnspan=6, padx=8, pady=8)
        self._canvas.bind("<Button-1>", self._on_click)
        tk.Label(self._root, textvariable=self._status, anchor="w").grid(
            row=1, column=0, columnspan=6, sticky="ew", padx=8
        )
        for column, label, command in (
            (0, "Previous", self._previous),
            (1, "Next", self._next),
            (2, "Retry", self._retry),
            (3, "Clear", self._clear),
            (4, "Quit", self._quit),
        ):
            tk.Button(self._root, text=label, command=command).grid(
                row=2, column=column, padx=4, pady=4
            )
        for column, label in enumerate(("success", "partial", "failure", "skip")):
            tk.Radiobutton(
                self._root,
                text=label,
                value=label,
                variable=self._classification,
                command=self._classify,
            ).grid(row=3, column=column, padx=4, pady=4)
        tk.Label(self._root, text="Notes").grid(row=4, column=0, padx=4, pady=4)
        tk.Entry(self._root, textvariable=self._notes, width=70).grid(
            row=4, column=1, columnspan=5, sticky="ew", padx=4, pady=4
        )
        self._load(0)

    def _item_index(self, record: FrameRecord) -> int:
        for index, item in enumerate(self._items):
            if item.source_path == record.relative_path:
                return index
        self._items.append(
            SessionItem(
                source_path=record.relative_path,
                resource_id=record.resource_id,
                channel_id=record.channel_id,
                source_width=record.size.width if record.size else 0,
                source_height=record.size.height if record.size else 0,
                point=None,
                bbox=None,
                classification="skip",
                inference_ms=None,
                overlay_path=None,
                notes=record.metadata_warning or "",
            )
        )
        return len(self._items) - 1

    def _current(self) -> tuple[FrameRecord, int, SessionItem]:
        record = self._records[self._index]
        item_index = self._item_index(record)
        return record, item_index, self._items[item_index]

    def _save(self) -> None:
        from datetime import datetime, timezone

        document = replace(
            self._context.document,
            items=tuple(self._items),
            updated_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        self._context = replace(self._context, document=document)
        try:
            write_session(self._context.session_path, document)
            metrics = metrics_for(document.items)
            self._context.summary_path.write_text(
                render_summary(
                    document,
                    metrics,
                    self._context.environment,
                    self._context.output_root.as_posix(),
                ),
                encoding="utf-8",
            )
        except OSError:
            self._status.set("Validation output could not be saved safely.")

    def _load(self, index: int) -> None:
        from PIL import Image, ImageTk

        self._index = index % len(self._records)
        record, item_index, item = self._current()
        try:
            with Image.open(record.source_path) as opened:
                self._image = opened.convert("RGB")
        except (OSError, ValueError):
            self._image = None
            self._status.set("Frame could not be opened safely.")
            return
        if item.source_width != self._image.width or item.source_height != self._image.height:
            self._items[item_index] = replace(
                item, source_width=self._image.width, source_height=self._image.height
            )
        self._prediction = None
        self._classification.set(item.classification)
        self._notes.set(item.notes)
        self._render(ImageTk)
        self._status.set(f"Frame {self._index + 1}/{len(self._records)}: {record.relative_path}")

    def _render(self, image_tk) -> None:
        if self._image is None:
            return
        _record, _item_index, item = self._current()
        displayed = render_frame(self._image, self._prediction, item.point)
        displayed.thumbnail((900, 700))
        self._scale = (displayed.width / self._image.width, displayed.height / self._image.height)
        self._photo = image_tk.PhotoImage(displayed)
        self._canvas.config(width=displayed.width, height=displayed.height)
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, image=self._photo, anchor="nw")

    def _replace_current(self, item: SessionItem) -> None:
        _record, item_index, _old = self._current()
        self._items[item_index] = item

    def _on_click(self, event) -> None:
        if self._busy or self._image is None:
            return
        source_x = max(0, min(self._image.width - 1, int(event.x / self._scale[0])))
        source_y = max(0, min(self._image.height - 1, int(event.y / self._scale[1])))
        record, _item_index_value, item = self._current()
        point = Point(source_x, source_y)
        self._replace_current(
            replace(
                item,
                point=point,
                bbox=None,
                classification="skip",
                inference_ms=None,
                overlay_path=None,
                mask_pixel_count=None,
                mask_coverage_percent=None,
                selected_score=None,
                failure_reason=None,
            )
        )
        self._prediction = None
        self._classification.set("skip")
        self._save()
        self._status.set(f"Running EfficientSAM-Ti at ({source_x}, {source_y})...")
        self._busy = True

        def infer() -> None:
            result: ModelPrediction | None = None
            error: Exception | None = None
            try:
                result = self._context.predictor.predict(record.source_path, point)
            except (
                PredictorUnavailableError,
                PredictionFailedError,
                PredictionRejectedError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as caught:
                error = caught
            if not self._closed:
                try:
                    self._root.after(0, lambda: self._finish(result, error))
                except tk.TclError:
                    self._closed = True

        threading.Thread(target=infer, daemon=True).start()

    def _finish(self, result: ModelPrediction | None, error: Exception | None) -> None:
        self._busy = False
        record, _item_index_value, item = self._current()
        if error is not None or result is None:
            if isinstance(error, PredictionRejectedError):
                updated = replace(
                    item,
                    bbox=None,
                    inference_ms=error.elapsed_ms,
                    mask_pixel_count=error.mask_pixel_count,
                    mask_coverage_percent=error.coverage_percent,
                    selected_score=error.score,
                    failure_reason=error.reason,
                    overlay_path=None,
                )
                reason = error.reason
                status = format_prediction_status(
                    None,
                    reason=reason,
                    score=error.score,
                    mask_pixel_count=error.mask_pixel_count,
                    coverage_percent=error.coverage_percent,
                    bbox=None,
                )
            else:
                reason = (
                    "model-runtime-unavailable"
                    if isinstance(error, PredictorUnavailableError)
                    else "inference-failed"
                )
                updated = replace(
                    item,
                    bbox=None,
                    inference_ms=None,
                    mask_pixel_count=None,
                    mask_coverage_percent=None,
                    selected_score=None,
                    failure_reason=reason,
                    overlay_path=None,
                )
                status = format_prediction_status(None, reason=reason)
            self._prediction = None
            self._replace_current(updated)
            overlay = write_overlay(
                OverlayRequest(
                    output_root=self._context.output_root,
                    relative_path=record.relative_path,
                    image=self._image,
                    prediction=None,
                    point=updated.point,
                )
            )
            if overlay is not None:
                self._replace_current(replace(self._current()[2], overlay_path=overlay))
            self._save()
            from PIL import ImageTk

            self._render(ImageTk)
            self._status.set(status)
            return
        self._prediction = result
        self._replace_current(
            replace(
                item,
                source_width=result.size.width,
                source_height=result.size.height,
                bbox=result.bbox,
                inference_ms=result.elapsed_ms,
                mask_pixel_count=result.mask_pixel_count,
                mask_coverage_percent=result.coverage_percent,
                selected_score=result.score,
                failure_reason=None,
            )
        )
        overlay = write_overlay(
            OverlayRequest(
                output_root=self._context.output_root,
                relative_path=record.relative_path,
                image=self._image,
                prediction=result,
                point=item.point,
            )
        )
        if overlay is not None:
            self._replace_current(replace(self._current()[2], overlay_path=overlay))
        self._save()
        from PIL import ImageTk

        self._render(ImageTk)
        self._status.set(format_prediction_status(result))

    def _classify(self) -> None:
        record, _item_index_value, item = self._current()
        classification = self._classification.get()
        if classification not in {"success", "partial", "failure", "skip"}:
            return
        self._replace_current(replace(item, classification=classification, notes=self._notes.get()))
        self._save()
        self._status.set(f"Classified {record.relative_path} as {classification}.")

    def _clear(self) -> None:
        _record, _item_index_value, item = self._current()
        self._prediction = None
        self._replace_current(
            replace(
                item,
                point=None,
                bbox=None,
                classification="skip",
                inference_ms=None,
                overlay_path=None,
                mask_pixel_count=None,
                mask_coverage_percent=None,
                selected_score=None,
                failure_reason=None,
            )
        )
        self._classification.set("skip")
        self._save()
        from PIL import ImageTk

        self._render(ImageTk)
        self._status.set("Cleared this frame; tap again to retry.")

    def _retry(self) -> None:
        self._prediction = None
        self._status.set("Tap an object to retry inference.")
        from PIL import ImageTk

        self._render(ImageTk)

    def _previous(self) -> None:
        if not self._busy:
            self._load(self._index - 1)

    def _next(self) -> None:
        if not self._busy:
            self._load(self._index + 1)

    def _quit(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._save()
        self._root.destroy()

    def run(self) -> None:
        self._root.mainloop()
