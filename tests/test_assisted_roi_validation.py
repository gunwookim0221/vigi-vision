from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import tools.assisted_roi_checkpoint as checkpoint_module
from tools.assisted_roi_checkpoint import CheckpointError, checkpoint_info
from tools.assisted_roi_gui import format_prediction_status
from tools.assisted_roi_render import point_marker_geometry
from tools.assisted_roi_report import EnvironmentInfo, render_summary
from tools.assisted_roi_session import (
    SessionDocument,
    SessionFormatError,
    SessionItem,
    merge_session_items,
    metrics_for,
    read_session,
    recommendation_for,
    write_session,
)
from tools.assisted_roi_validation import (
    BoundingBox,
    Classification,
    FrameOrder,
    FrameRecord,
    ImageSize,
    MaskRejection,
    Point,
    PredictionCandidate,
    ValidatedMaskCandidate,
    binarize_logits,
    discover_frames,
    expand_minimum_box,
    mask_matches_size,
    mask_to_bounding_box,
    order_frames,
    select_mask_candidate,
    select_valid_mask_candidate,
    sort_prediction_candidates,
)
from tools.efficient_sam_predictor import EfficientSamPredictor


def _write_frame(root: Path, name: str, channel: int | None = 1) -> Path:
    directory = root / name
    _ = directory.mkdir(parents=True)
    frame = directory / "frame.jpg"
    _ = frame.write_bytes(b"jpeg")
    if channel is not None:
        _ = (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "resource_id": name,
                    "status": "completed",
                    "channel_id": channel,
                    "width": 640,
                    "height": 480,
                }
            ),
            encoding="utf-8",
        )
    return frame


def test_discovery_is_recursive_deterministic_and_keeps_missing_manifest(tmp_path: Path) -> None:
    _ = _write_frame(tmp_path, "z-folder", channel=2)
    missing = _write_frame(tmp_path / "nested", "a-folder", channel=None)
    _ = (tmp_path / "ignored.txt").write_text("not a frame", encoding="utf-8")

    records = discover_frames(tmp_path)

    assert [record.relative_path for record in records] == [
        "nested/a-folder/frame.jpg",
        "z-folder/frame.jpg",
    ]
    assert records[0].source_path == missing
    assert records[0].metadata_warning == "manifest_missing"
    assert records[1].channel_id == 2


def test_discovery_ignores_incomplete_artifact_and_reports_malformed_manifest(
    tmp_path: Path,
) -> None:
    invalid = _write_frame(tmp_path, "invalid", channel=None)
    _ = (invalid.parent / "manifest.json").write_text("{", encoding="utf-8")
    incomplete = _write_frame(tmp_path, "incomplete", channel=None)
    _ = (incomplete.parent / "manifest.json").write_text(
        json.dumps({"resource_id": "incomplete", "status": "failed"}),
        encoding="utf-8",
    )

    records = discover_frames(tmp_path)

    assert [record.relative_path for record in records] == ["invalid/frame.jpg"]
    assert records[0].metadata_warning == "manifest_invalid"


def test_order_frames_applies_channel_limit_and_seeded_shuffle() -> None:
    records = tuple(
        FrameRecord(
            source_path=Path(f"frame-{index}.jpg"),
            relative_path=f"frame-{index}.jpg",
            resource_id=None,
            channel_id=1 if index < 2 else 2,
            size=ImageSize(width=10, height=10),
            metadata_warning=None,
        )
        for index in range(4)
    )

    ordered = order_frames(records, FrameOrder(channel_id=2, shuffle=True, seed=7, limit=1))

    assert len(ordered) == 1
    assert ordered[0].channel_id == 2


def test_point_and_mask_bbox_are_clamped_and_deterministic() -> None:
    candidates = (
        PredictionCandidate(
            mask=((False, False), (False, True)),
            score=0.9,
        ),
        PredictionCandidate(
            mask=((True, True), (True, True)),
            score=0.9,
        ),
    )

    selected = select_mask_candidate(candidates, Point(0, 0))
    box = mask_to_bounding_box(selected.mask, ImageSize(width=2, height=2)) if selected else None

    assert box == BoundingBox(x=0, y=0, width=2, height=2)

    assert mask_to_bounding_box(((False,),), ImageSize(width=1, height=1)) is None
    assert mask_matches_size(((True, False),), ImageSize(width=2, height=2)) is False
    assert expand_minimum_box(BoundingBox(9, 9, 1, 1), ImageSize(10, 10)) == BoundingBox(6, 6, 4, 4)


def test_binarize_logits_uses_zero_threshold() -> None:
    assert binarize_logits(((-0.1, 0.0, 0.2),)) == ((False, True, True),)


def test_candidate_order_keeps_score_and_mask_aligned() -> None:
    candidates = (
        PredictionCandidate(mask=((True,),), score=0.2),
        PredictionCandidate(mask=((False,),), score=0.9),
        PredictionCandidate(mask=((True,),), score=0.5),
    )

    ordered = sort_prediction_candidates(candidates)

    assert [index for index, _candidate in ordered] == [1, 2, 0]
    assert [candidate.score for _index, candidate in ordered] == [0.9, 0.5, 0.2]
    assert ordered[0][1].mask == ((False,),)


def test_valid_candidate_is_selected_after_pathological_higher_score() -> None:
    result = select_valid_mask_candidate(
        (
            PredictionCandidate(mask=((True, True), (True, True)), score=0.9),
            PredictionCandidate(mask=((False, False), (False, True)), score=0.4),
        ),
        Point(1, 1),
        ImageSize(width=2, height=2),
    )

    assert isinstance(result, ValidatedMaskCandidate)
    assert result.mask == ((False, False), (False, True))
    assert result.score == 0.4
    assert result.candidate_index == 1


def test_full_frame_mask_is_rejected_as_background_dominant() -> None:
    result = select_valid_mask_candidate(
        (PredictionCandidate(mask=((True, True), (True, True)), score=0.663),),
        Point(0, 0),
        ImageSize(width=2, height=2),
    )

    assert isinstance(result, MaskRejection)
    assert result.reason == "background-dominant"
    assert result.bbox is None
    assert result.mask_pixel_count == 4
    assert result.coverage_percent == 100.0


def test_point_containment_rejects_mask_without_clicked_point() -> None:
    result = select_valid_mask_candidate(
        (PredictionCandidate(mask=((True, False), (False, False)), score=0.9),),
        Point(1, 1),
        ImageSize(width=2, height=2),
    )

    assert isinstance(result, MaskRejection)
    assert result.reason == "point-not-in-mask"


def test_empty_mask_is_rejected() -> None:
    result = select_valid_mask_candidate(
        (PredictionCandidate(mask=((False, False), (False, False)), score=0.9),),
        Point(1, 1),
        ImageSize(width=2, height=2),
    )

    assert isinstance(result, MaskRejection)
    assert result.reason == "empty-mask"


def test_gui_status_does_not_claim_suggestion_for_rejected_mask() -> None:
    status = format_prediction_status(
        prediction=None,
        reason="background-dominant",
        score=0.663,
        mask_pixel_count=3686400,
        coverage_percent=100.0,
        bbox=None,
    )

    assert "Suggestion ready" not in status
    assert "background-dominant" in status
    assert "mask pixels=3686400" in status
    assert "coverage=100.00%" in status
    assert "bbox=n/a" in status


def test_failure_overlay_marker_geometry_contains_clicked_point() -> None:
    geometry = point_marker_geometry(Point(10, 10), 20, 20)

    assert geometry is not None
    circle, lines = geometry
    assert circle == (6, 6, 14, 14)
    assert (10, 10, 10, 10) not in lines
    assert any(line[0] <= 10 <= line[2] for line in lines)


def test_checkpoint_verification_is_explicit_and_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "efficient_sam_vitt.pt"
    _ = checkpoint.write_bytes(b"checkpoint")
    monkeypatch.setattr(
        checkpoint_module,
        "EXPECTED_EFFICIENT_SAM_TINY_SHA256",
        hashlib.sha256(b"checkpoint").hexdigest(),
    )

    info = checkpoint_info(checkpoint, verify_sha256=True)

    assert info.name == "efficient_sam_vitt.pt"
    assert info.actual_sha256 == info.expected_sha256


def test_checkpoint_mismatch_does_not_expose_file_contents(tmp_path: Path) -> None:
    checkpoint = tmp_path / "efficient_sam_vitt.pt"
    _ = checkpoint.write_bytes(b"secret-like-content")

    with pytest.raises(CheckpointError) as raised:
        _ = checkpoint_info(checkpoint, verify_sha256=True)
    assert raised.value.category == "checkpoint_sha256_mismatch"
    assert "secret-like-content" not in str(raised.value)


def test_session_round_trip_and_duplicate_prevention(tmp_path: Path) -> None:
    record = FrameRecord(
        source_path=tmp_path / "frame.jpg",
        relative_path="frame.jpg",
        resource_id="resource",
        channel_id=1,
        size=ImageSize(width=640, height=480),
        metadata_warning=None,
    )
    document = SessionDocument(
        checkpoint_name="efficient_sam_vitt.pt",
        expected_sha256="expected",
        actual_sha256=None,
        device="cpu",
        items=merge_session_items((record,), ()),
        updated_at_utc="2026-08-01T00:00:00Z",
    )
    path = tmp_path / "session.json"

    write_session(path, document)
    loaded = read_session(path)
    merged = merge_session_items((record,), loaded.items)

    assert loaded == document
    assert len(merged) == 1


def test_session_round_trip_preserves_prediction_evidence(tmp_path: Path) -> None:
    item = SessionItem(
        source_path="channel-1/frame.jpg",
        resource_id="resource",
        channel_id=1,
        source_width=640,
        source_height=480,
        point=Point(12, 34),
        bbox=BoundingBox(x=10, y=20, width=40, height=50),
        classification="failure",
        inference_ms=123.4,
        overlay_path="overlays/evidence.jpg",
        notes="",
        mask_pixel_count=321,
        mask_coverage_percent=0.1045,
        selected_score=0.663,
        failure_reason="background-dominant",
    )
    document = SessionDocument(
        checkpoint_name="efficient_sam_vitt.pt",
        expected_sha256="expected",
        actual_sha256=None,
        device="cpu",
        items=(item,),
        updated_at_utc="2026-08-01T00:00:00Z",
    )

    path = tmp_path / "session.json"
    write_session(path, document)

    assert read_session(path).items[0] == item


def test_invalid_resume_session_is_rejected_without_source_changes(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    _ = path.write_text(json.dumps({"schema_version": 1, "items": []}), encoding="utf-8")

    with pytest.raises(SessionFormatError) as raised:
        _ = read_session(path)

    assert raised.value.reason == "session_invalid"


def test_metrics_and_thresholds_do_not_count_partial_as_success() -> None:
    items = (
        document_item("one", "success", 10.0),
        document_item("two", "partial", 20.0),
        document_item("three", "failure", 30.0),
        document_item("four", "skip", None),
    )

    metrics = metrics_for(items)

    assert metrics.evaluated == 3
    assert metrics.success_rate == 33.33
    assert recommendation_for(evaluated=19, success_rate=100.0) == "insufficient_evidence"
    assert recommendation_for(evaluated=20, success_rate=70.0) == "proceed"
    assert recommendation_for(evaluated=20, success_rate=50.0) == "proceed_with_limitations"
    assert recommendation_for(evaluated=20, success_rate=49.99) == "do_not_proceed"


def test_summary_contains_thresholds_and_no_authenticated_urls() -> None:
    items = (
        replace(
            document_item("channel-1/frame.jpg", "success", 10.0),
            mask_pixel_count=321,
            mask_coverage_percent=0.10,
            selected_score=0.663,
        ),
    )
    document = SessionDocument(
        checkpoint_name="efficient_sam_vitt.pt",
        expected_sha256="expected",
        actual_sha256=None,
        device="cpu",
        items=items,
        updated_at_utc="2026-08-01T00:00:00Z",
    )

    summary = render_summary(
        document,
        metrics_for(items),
        EnvironmentInfo(python_version="3.13", platform="Windows", device="cpu"),
        "artifacts/validation/assisted-roi",
    )

    assert "insufficient_evidence" in summary
    assert "321" in summary
    assert "0.10%" in summary
    assert "0.663" in summary
    assert "rtsp://" not in summary
    assert "secret-like-content" not in summary


def test_predictor_is_lazy_and_does_not_load_model_at_construction(tmp_path: Path) -> None:
    predictor = EfficientSamPredictor(tmp_path / "checkpoint.pt", "cpu")

    assert not predictor.is_loaded


def document_item(
    name: str,
    classification: Classification,
    inference_ms: float | None,
) -> SessionItem:
    return SessionItem(
        source_path=name,
        resource_id=None,
        channel_id=1,
        source_width=640,
        source_height=480,
        point=Point(1, 1) if classification != "skip" else None,
        bbox=None,
        classification=classification,
        inference_ms=inference_ms,
        overlay_path=None,
        notes="",
    )
