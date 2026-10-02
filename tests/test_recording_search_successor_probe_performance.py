"""Per-probe timing diagnostics remain optional and non-authoritative."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import vigi_vision.recording_search_successor_diagnostics as diagnostics_module
import vigi_vision.recording_search_successor_execution as execution_module
import vigi_vision.replay as replay_module
from test_recording_search_7e_b4_process import _values
from test_recording_search_successor_execution import (
    ANCHOR,
    _confirmed,
    _IndeterminateClassifier,
    _service,
    _ThreeMidpointClassifier,
)
from test_recording_search_successor_search_evidence import _comparison
from vigi_vision.object_presence_values import ClassificationOutcome, DecodedRgbImage
from vigi_vision.recording_models import RecordingWindow, ReplayRequest
from vigi_vision.recording_search_7e_b4_process import StaticMaskWorkerSpec
from vigi_vision.recording_search_successor_classification import SuccessorClassifierResult
from vigi_vision.recording_search_successor_diagnostics import (
    ProbePerformanceCapture,
    SuccessorDiagnosticError,
    SuccessorDiagnosticRepository,
    performance_run_scope,
    probe_performance_scope,
)
from vigi_vision.recording_search_successor_evidence import SuccessorEvidenceRepository

_INVESTIGATION_ID = "object-disappearance-v3-ch1-20260904T051732Z"
_RUN_ID = "search-run-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
_OBSERVATION_ID = "successor-observation-v1-" + "a" * 64


class _AlignedClassifier:
    policy_identity = "successor-aligned-test-classifier-v1"

    def classify(
        self,
        _baseline: object,
        probe: DecodedRgbImage,
        _width: int,
        _height: int,
        _roi: object,
        _correlation_id: str,
    ) -> SuccessorClassifierResult:
        outcome = (
            ClassificationOutcome.ABSENT if probe.pixels[0][0][0] else ClassificationOutcome.PRESENT
        )
        return SuccessorClassifierResult(outcome, comparison=_comparison())


def _record() -> dict[str, object]:
    return {
        "version": "phase7e-probe-performance-v1",
        "diagnostic_kind": "probe_performance",
        "investigation_id": _INVESTIGATION_ID,
        "run_id": _RUN_ID,
        "plan_id": "successor-plan-v1-test",
        "target_id": "successor-target-v1-test",
        "observation_id": _OBSERVATION_ID,
        "requested_time_utc": "2026-09-04T05:18:00Z",
        "selected_frame_time_utc": "2026-09-04T05:18:00Z",
        "observation_role": "coarse",
        "reference_identity": "successor-authority-v1-test",
        "reference_frame_resource_id": "reference-frame-resource-v1-test",
        "roi_identity": "successor-roi-v1-test",
        "classifier_policy_identity": "successor-policy-v1-test",
        "replay_total_ms": 800,
        "replay_first_output_ms": 100,
        "replay_process_exit_ms": 620,
        "replay_cleanup_tail_ms": 180,
        "probe_total_ms": 1_800,
        "classifier_total_ms": 900,
        "request_decoded_ms": 70,
        "child_startup_ms": 300,
        "preprocessing_ms": 90,
        "inference_ms": 420,
        "child_inference_ms": 400,
        "alignment_elapsed_ms": 230,
        "ipc_result_ms": 710,
        "b4_cleanup_ms": 25,
        "classifier_invocation_count": 1,
        "decoder_calls": 2,
        "segmentation_calls": 3,
        "baseline_segmentation_calls": 1,
        "candidate_segmentation_calls": 2,
        "alignment_comparisons": 450,
        "alignment_translation_candidates": 9,
        "alignment_rotation_candidates": 5,
        "alignment_scale_candidates": 1,
        "duplicate_processing_count": 0,
    }


def _capture_record(capture: ProbePerformanceCapture) -> dict[str, object]:
    return capture.record(
        investigation_id=_INVESTIGATION_ID,
        run_id=_RUN_ID,
        observation_id=_OBSERVATION_ID,
        selected_frame_time_utc="2026-09-04T05:18:00Z",
        reference_identity="successor-authority-v1-test",
        reference_frame_resource_id="reference-frame-resource-v1-test",
        roi_identity="successor-roi-v1-test",
        classifier_policy_identity="successor-policy-v1-test",
    )


def test_replay_arithmetic_and_probe_total_use_deterministic_monotonic_values() -> None:
    clock_values = iter((12.0, 13.8))
    with performance_run_scope():
        with probe_performance_scope(
            plan_id="successor-plan-v1-test",
            target_id="successor-target-v1-test",
            requested_time_utc=ANCHOR,
            observation_role="coarse",
            clock=lambda: next(clock_values),
        ) as capture:
            assert capture is not None
            capture.record_replay_progress("first_output", 100)
            capture.record_replay_progress("process_exited", 1_250, exit_code=0)
            capture.record_replay_progress("cleanup_completed", 1_800)
        completed = diagnostics_module.completed_probe_performance_captures()

    record = _capture_record(completed[0])
    assert record["replay_first_output_ms"] == 100
    assert record["replay_process_exit_ms"] == 1_250
    assert record["replay_total_ms"] == 1_800
    assert record["replay_cleanup_tail_ms"] == 550
    assert record["replay_cleanup_tail_ms"] == (
        record["replay_total_ms"] - record["replay_process_exit_ms"]
    )
    assert record["probe_total_ms"] == 1_800


def test_unobserved_replay_stages_are_null_not_fabricated_zero() -> None:
    clock_values = iter((4.0, 4.0))
    with performance_run_scope():
        with probe_performance_scope(
            plan_id="successor-plan-v1-test",
            target_id="successor-target-v1-test",
            requested_time_utc=ANCHOR,
            observation_role="anchor",
            clock=lambda: next(clock_values),
        ) as capture:
            assert capture is not None
            capture.record_replay_progress("process_exited", 150, exit_code=None)
            capture.record_replay_progress("cleanup_completed", 300)
        record = _capture_record(diagnostics_module.completed_probe_performance_captures()[0])

    assert record["probe_total_ms"] == 0
    assert record["replay_total_ms"] == 300
    assert record["replay_first_output_ms"] is None
    assert record["replay_process_exit_ms"] is None
    assert record["replay_cleanup_tail_ms"] is None
    assert record["classifier_total_ms"] is None
    assert record["alignment_elapsed_ms"] is None
    assert record["alignment_comparisons"] is None


def test_replay_lifecycle_events_reach_the_current_probe_without_new_process_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed_values = iter((100.1, 100.7, 101.0))
    stages: list[str] = []
    logged_stages: list[str] = []
    clock_values = iter((2.0, 4.0))
    request = ReplayRequest(
        RecordingWindow(1, ANCHOR, ANCHOR + timedelta(seconds=10)),
        "rtsp://redacted.example/replay",
    )
    monkeypatch.setattr(replay_module, "perf_counter", lambda: next(elapsed_values))
    monkeypatch.setattr(
        replay_module,
        "_safe_replay_log",
        lambda payload: logged_stages.append(str(payload["stage"])),
    )

    with performance_run_scope():
        with probe_performance_scope(
            plan_id="successor-plan-v1-test",
            target_id="successor-target-v1-test",
            requested_time_utc=ANCHOR,
            observation_role="coarse",
            clock=lambda: next(clock_values),
        ) as capture:
            assert capture is not None
            lifecycle = replay_module._ReplayLifecycle(
                request,
                tmp_path / "replay.mp4",
                30.0,
                None,
                performance_capture=capture,
                started_at=100.0,
            )
            for stage, exit_code in (
                ("first_output", None),
                ("process_exited", 0),
                ("cleanup_completed", None),
            ):
                stages.append(stage)
                lifecycle._emit(stage, exit_code=exit_code)
        record = _capture_record(diagnostics_module.completed_probe_performance_captures()[0])

    assert logged_stages == stages
    assert record["replay_first_output_ms"] == 100
    assert record["replay_process_exit_ms"] == 700
    assert record["replay_total_ms"] == 1_000
    assert record["replay_cleanup_tail_ms"] == 300


def test_production_b4_timing_sink_is_captured_without_changing_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline, probe, baseline_mask, probe_mask, roi, policy = _values()
    event: dict[str, object] = {
        "event": "phase7e.classifier_timing",
        "stage": "completed",
        "startup_ms": 35,
        "request_decoded_ms": 12,
        "preprocessing_ms": 19,
        "inference_ms": 80,
        "child_inference_ms": 74,
        "alignment_elapsed_ms": 51,
        "ipc_result_ms": 117,
        "cleanup_ms": 4,
        "decoder_calls": 2,
        "segmentation_calls": 3,
        "baseline_segmentation_calls": 1,
        "candidate_segmentation_calls": 2,
        "alignment_comparisons": 450,
        "alignment_translation_candidates": 90,
        "alignment_rotation_candidates": 5,
        "alignment_scale_candidates": 1,
        "duplicate_processing_count": 0,
    }

    def fake_b4(**kwargs: object) -> SimpleNamespace:
        sink = kwargs.get("timing_sink")
        assert callable(sink)
        sink(event)
        return SimpleNamespace(
            outcome=ClassificationOutcome.PRESENT,
            reason_code=None,
            comparison=None,
        )

    monkeypatch.setattr(execution_module, "run_b4_in_process", fake_b4)
    classifier = execution_module.SuccessorB4Classifier(
        policy,
        StaticMaskWorkerSpec(baseline_mask, probe_mask),
    )
    clock_values = iter((5.0, 5.9))
    with performance_run_scope():
        with probe_performance_scope(
            plan_id="successor-plan-v1-test",
            target_id="successor-target-v1-test",
            requested_time_utc=ANCHOR,
            observation_role="coarse",
            clock=lambda: next(clock_values),
        ):
            result = classifier.classify(
                baseline,
                probe,
                32,
                32,
                roi,
                "performance-test",
            )
        record = _capture_record(diagnostics_module.completed_probe_performance_captures()[0])

    assert result.outcome is ClassificationOutcome.PRESENT
    assert record["classifier_total_ms"] == result.elapsed_ms
    assert record["child_startup_ms"] == 35
    assert record["preprocessing_ms"] == 19
    assert record["inference_ms"] == 80
    assert record["alignment_elapsed_ms"] == 51
    assert record["ipc_result_ms"] == 117
    assert record["b4_cleanup_ms"] == 4
    assert record["alignment_comparisons"] == 450


def test_existing_b4_timing_fields_map_without_claiming_an_arithmetic_sum() -> None:
    clock_values = iter((1.0, 2.0))
    event = {
        "event": "phase7e.classifier_timing",
        "stage": "completed",
        "startup_ms": 300,
        "request_decoded_ms": 70,
        "preprocessing_ms": 90,
        "inference_ms": 420,
        "child_inference_ms": 400,
        "alignment_elapsed_ms": 230,
        "ipc_result_ms": 710,
        "cleanup_ms": 25,
        "decoder_calls": 2,
        "segmentation_calls": 3,
        "baseline_segmentation_calls": 1,
        "candidate_segmentation_calls": 2,
        "alignment_comparisons": 450,
        "alignment_translation_candidates": 9,
        "alignment_rotation_candidates": 5,
        "alignment_scale_candidates": 1,
        "duplicate_processing_count": 0,
    }
    with performance_run_scope():
        with probe_performance_scope(
            plan_id="successor-plan-v1-test",
            target_id="successor-target-v1-test",
            requested_time_utc=ANCHOR,
            observation_role="coarse",
            clock=lambda: next(clock_values),
        ) as capture:
            assert capture is not None
            capture.record_classifier_timing(event)
            capture.record_classifier_total(900)
        record = _capture_record(diagnostics_module.completed_probe_performance_captures()[0])

    assert record["child_startup_ms"] == 300
    assert record["request_decoded_ms"] == 70
    assert record["preprocessing_ms"] == 90
    assert record["inference_ms"] == 420
    assert record["child_inference_ms"] == 400
    assert record["alignment_elapsed_ms"] == 230
    assert record["ipc_result_ms"] == 710
    assert record["b4_cleanup_ms"] == 25
    assert record["classifier_total_ms"] == 900
    assert record["alignment_comparisons"] == 450
    assert record["ipc_result_ms"] > record["inference_ms"]
    assert "stage_sum_ms" not in record
    assert sum(
        cast_int(record[key])
        for key in ("child_startup_ms", "preprocessing_ms", "inference_ms", "alignment_elapsed_ms")
    ) > cast_int(record["classifier_total_ms"])


def test_malformed_captured_timing_discards_only_the_optional_capture() -> None:
    clock_values = iter((1.0, 2.0))
    with performance_run_scope():
        with probe_performance_scope(
            plan_id="successor-plan-v1-test",
            target_id="successor-target-v1-test",
            requested_time_utc=ANCHOR,
            observation_role="coarse",
            clock=lambda: next(clock_values),
        ) as capture:
            assert capture is not None
            capture.record_classifier_timing(
                {"event": "phase7e.classifier_timing", "startup_ms": True}
            )
        assert diagnostics_module.completed_probe_performance_captures() == ()


def test_timing_sidecar_is_strict_bounded_immutable_and_path_separate(tmp_path: Path) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    record = _record()
    repository.publish_probe_performance(record)
    repository.publish_probe_performance(record)

    directory = tmp_path / _INVESTIGATION_ID / _RUN_ID / "diagnostics" / "probe-performance-v1"
    path = directory / f"{_OBSERVATION_ID}.json"
    assert path.is_file()
    assert path.stat().st_size <= 4096
    assert json.loads(path.read_text(encoding="ascii")) == record
    assert repository.read_probe_performance(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) == record
    assert len(tuple(directory.glob("*.json"))) == 1

    conflict = dict(record, probe_total_ms=1_801)
    with pytest.raises(SuccessorDiagnosticError, match="diagnostic_conflict"):
        repository.publish_probe_performance(conflict)
    assert repository.read_probe_performance(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) == record

    path.write_text("{", encoding="ascii")
    with pytest.raises(SuccessorDiagnosticError, match="diagnostic_corrupt"):
        repository.publish_probe_performance(record)
    assert path.read_text(encoding="ascii") == "{"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("probe_total_ms", -1),
        ("probe_total_ms", 86_400_001),
        ("probe_total_ms", True),
        ("probe_total_ms", 1.5),
        ("ipc_result_ms", 10**100),
        ("alignment_comparisons", False),
        ("preprocessing_ms", 2.5),
        ("replay_cleanup_tail_ms", 181),
    ],
)
def test_malformed_timing_facts_are_rejected_without_publication(
    tmp_path: Path, field: str, value: object
) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    malformed = dict(_record())
    malformed[field] = value
    with pytest.raises(SuccessorDiagnosticError, match="diagnostic_corrupt"):
        repository.publish_probe_performance(malformed)
    assert repository.read_probe_performance(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) is None


def test_missing_required_timing_is_rejected(tmp_path: Path) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    malformed = dict(_record())
    del malformed["probe_total_ms"]
    with pytest.raises(SuccessorDiagnosticError, match="diagnostic_corrupt"):
        repository.publish_probe_performance(malformed)


def test_synthetic_six_probe_run_binds_final_observations_and_roles(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        ANCHOR + timedelta(minutes=20),
        _ThreeMidpointClassifier(),
    )
    service.evidence_repository = SuccessorEvidenceRepository(service.publisher.root)
    confirmed = replace(
        _confirmed(tmp_path),
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:37:32",
        run_id=_RUN_ID,
        now_utc=ANCHOR + timedelta(hours=1),
    )

    terminal = service.execute(prepared)
    evidence = service.evidence_repository.read(confirmed.investigation_id, _RUN_ID)
    repository = SuccessorDiagnosticRepository(service.publisher.root)
    directory = (
        service.publisher.root
        / confirmed.investigation_id
        / _RUN_ID
        / "diagnostics"
        / "probe-performance-v1"
    )
    paths = tuple(directory.glob("*.json"))
    records = tuple(json.loads(path.read_text(encoding="ascii")) for path in paths)
    assert evidence is not None
    evidence_ids = {
        item["observation_id"]
        for item in evidence["entries"]
        if item.get("role") != "baseline_link" and isinstance(item.get("observation_id"), str)
    }

    assert terminal.status == "INCONCLUSIVE"
    assert len(records) == 6
    assert len(evidence_ids) == 6
    assert {path.stem for path in paths} == evidence_ids
    assert {item["observation_role"] for item in records} == {"anchor", "coarse", "narrowing"}
    assert sum(item["observation_role"] == "anchor" for item in records) == 1
    assert sum(item["observation_role"] == "coarse" for item in records) == 2
    assert sum(item["observation_role"] == "narrowing" for item in records) == 3
    for path, record in zip(paths, records, strict=True):
        assert path.stem == record["observation_id"]
        assert record["observation_id"] in evidence_ids
        assert (
            repository.read_probe_performance(
                confirmed.investigation_id, _RUN_ID, record["observation_id"]
            )
            == record
        )
        assert type(record["probe_total_ms"]) is int
        assert record["probe_total_ms"] >= 0


def test_performance_persistence_failure_keeps_terminal_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(
        tmp_path,
        ANCHOR + timedelta(minutes=20),
        _ThreeMidpointClassifier(),
    )
    service.evidence_repository = SuccessorEvidenceRepository(service.publisher.root)
    confirmed = replace(
        _confirmed(tmp_path),
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:37:32",
        run_id=_RUN_ID,
        now_utc=ANCHOR + timedelta(hours=1),
    )

    def fail(_self: object, _record: object) -> None:
        message = "diagnostic write failure"
        raise OSError(message)

    monkeypatch.setattr(SuccessorDiagnosticRepository, "publish_probe_performance", fail)
    terminal = service.execute(prepared)
    evidence = service.evidence_repository.read(confirmed.investigation_id, _RUN_ID)

    assert terminal.status == "INCONCLUSIVE"
    assert evidence is not None
    assert evidence["terminal_status"] == terminal.status
    assert service.publisher.read(confirmed.investigation_id, _RUN_ID)["status"] == terminal.status
    assert not (
        service.publisher.root
        / confirmed.investigation_id
        / _RUN_ID
        / "diagnostics"
        / "probe-performance-v1"
    ).exists()


@pytest.mark.parametrize(
    ("case", "absent_after", "classifier", "expected_status"),
    [
        ("present", ANCHOR + timedelta(hours=2), None, "NOT_FOUND"),
        ("aligned-absent", ANCHOR + timedelta(minutes=15), _AlignedClassifier, "FOUND"),
        (
            "indeterminate",
            ANCHOR + timedelta(minutes=20),
            _IndeterminateClassifier,
            "INCONCLUSIVE",
        ),
        (
            "bounded-inconclusive",
            ANCHOR + timedelta(minutes=20),
            _ThreeMidpointClassifier,
            "INCONCLUSIVE",
        ),
    ],
)
def test_diagnostic_publication_on_off_preserves_authoritative_semantics(  # noqa: PLR0913
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    absent_after: datetime,
    classifier: object,
    expected_status: str,
) -> None:
    def run(root: Path, *, disable_publication: bool) -> dict[str, object]:
        root.mkdir(parents=True, exist_ok=True)
        selected_classifier = classifier() if callable(classifier) else None
        service = _service(root, absent_after, selected_classifier)
        service.evidence_repository = SuccessorEvidenceRepository(service.publisher.root)
        confirmed = replace(
            _confirmed(root),
            jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
        )
        prepared = service.prepare(
            confirmed,
            search_end_time_text="2026-09-04T14:37:32",
            run_id=_RUN_ID,
            now_utc=ANCHOR + timedelta(hours=1),
        )
        if disable_publication:
            with monkeypatch.context() as context:
                context.setattr(execution_module, "persist_probe_performance", lambda _record: None)
                terminal = service.execute(prepared)
        else:
            terminal = service.execute(prepared)
        performance_directory = (
            service.publisher.root
            / confirmed.investigation_id
            / _RUN_ID
            / "diagnostics"
            / "probe-performance-v1"
        )
        if disable_publication:
            assert not performance_directory.exists()
        else:
            assert tuple(performance_directory.glob("*.json"))
        return terminal.as_record()

    without_diagnostics = run(tmp_path / f"{case}-off", disable_publication=True)
    with_diagnostics = run(tmp_path / f"{case}-on", disable_publication=False)

    assert without_diagnostics == with_diagnostics
    assert without_diagnostics["status"] == expected_status
    if case == "aligned-absent":
        observations = without_diagnostics["coarse_observations"]
        assert isinstance(observations, list)
        assert any(
            isinstance(item, dict)
            and isinstance(item.get("comparison"), dict)
            and item["comparison"].get("baseline_support_alignment_dx") == 0
            and item["comparison"].get("baseline_support_alignment_state") == "aligned"
            for item in observations
        )
    if case == "bounded-inconclusive":
        assert without_diagnostics["reason_code"] == "midpoint_indeterminate"
        assert without_diagnostics["last_present_observation_id"] is not None
        assert without_diagnostics["first_absent_observation_id"] is not None


def cast_int(value: object) -> int:
    assert type(value) is int
    return value
