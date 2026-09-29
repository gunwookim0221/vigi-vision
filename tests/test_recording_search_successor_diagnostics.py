"""Non-authoritative B4 diagnostics through the successor execution boundary."""

# Deterministic failure fixtures intentionally use closed literal error codes.
# ruff: noqa: EM101, FBT001, PLR0913

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import vigi_vision.recording_search_successor_execution as execution_module
from test_recording_search_7e_b4_process import _values
from test_recording_search_successor_execution import ANCHOR, _confirmed, _service
from vigi_vision.object_presence_models import BinaryMask
from vigi_vision.recording_search_7e_b4_process import (
    B4ProcessError,
    B4ProcessTimeout,
    StaticMaskWorkerSpec,
)
from vigi_vision.recording_search_successor_classification import SuccessorClassificationError
from vigi_vision.recording_search_successor_diagnostics import (
    SuccessorDiagnosticError,
    SuccessorDiagnosticRepository,
    diagnostic_scope,
    persist_failure,
)
from vigi_vision.recording_search_successor_evidence import SuccessorEvidenceRepository
from vigi_vision.recording_search_successor_execution import SuccessorB4Classifier

_RUN_ID = "search-run-dddddddddddddddddddddddddddddddd"


class _OperationalFailure:
    policy_identity = "successor-test-classifier-v1"

    def __init__(
        self, event: dict[str, object] | None = None, reason: str = "classifier_failed"
    ) -> None:
        self.event = event or {
            "stage": "failure",
            "failure_phase": "probe_inference",
            "error_code": "worker_execution_failed",
            "child_started": True,
            "result_received": False,
            "cleanup_status": "completed",
            "startup_ms": 35,
            "inference_ms": 17,
            "cleanup_ms": 2,
            "raw_stderr": "password=secret rtsp://sensitive.example/absolute/path",
        }
        self.reason = reason

    def classify(self, *_args: object) -> object:
        raise SuccessorClassificationError(
            self.reason,
            diagnostic=self.event,
        )


def _failed_run(
    tmp_path: Path,
    classifier: _OperationalFailure | None = None,
    expected_reason: str = "classifier_failed",
) -> tuple[SuccessorDiagnosticRepository, str, dict[str, object]]:
    service = _service(tmp_path, ANCHOR + timedelta(hours=1), classifier or _OperationalFailure())
    service.evidence_repository = SuccessorEvidenceRepository(service.publisher.root)
    confirmed = replace(_confirmed(tmp_path), jpeg_sha256=hashlib.sha256(b"baseline").hexdigest())
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id=_RUN_ID,
        now_utc=ANCHOR + timedelta(hours=1),
    )
    terminal = service.execute(prepared)
    assert terminal.status == "INCONCLUSIVE"
    assert terminal.reason_code == expected_reason
    reopened = service.publisher.read(confirmed.investigation_id, _RUN_ID)
    assert reopened is not None
    evidence = service.evidence_repository.read(confirmed.investigation_id, _RUN_ID)
    assert evidence is not None
    assert evidence["terminal_status"] == "INCONCLUSIVE"
    return (
        SuccessorDiagnosticRepository(service.publisher.root),
        confirmed.investigation_id,
        reopened,
    )


def test_four_failures_persist_distinct_safe_sidecars_without_changing_terminal(
    tmp_path: Path,
) -> None:
    repository, investigation_id, terminal = _failed_run(tmp_path)
    observations = terminal["coarse_observations"]
    assert isinstance(observations, list)
    failed = [item for item in observations if item["state"] == "CLASSIFIER_FAILED"]
    assert len(failed) == 4
    directory = repository.root / investigation_id / _RUN_ID / "diagnostics"
    paths = list(directory.glob("*.json"))
    assert len(paths) == 4
    records = [json.loads(path.read_text(encoding="ascii")) for path in paths]
    assert {item["target_id"] for item in records} == {item["target_id"] for item in failed}
    assert {item["requested_time_utc"] for item in records} == {
        item["requested_time_utc"] for item in failed
    }
    assert all(item["classifier_stage"] == "probe_inference" for item in records)
    assert all(item["classifier_error_code"] == "worker_execution_failed" for item in records)
    assert all(item["child_started"] is True for item in records)
    assert all(item["result_received"] is False for item in records)
    assert all(item["cleanup_status"] == "completed" for item in records)
    assert all(item["startup_elapsed_ms"] == 35 for item in records)
    assert all(item["inference_elapsed_ms"] == 17 for item in records)
    assert all(item["cleanup_elapsed_ms"] == 2 for item in records)
    assert all(item["total_elapsed_ms"] is not None for item in records)
    assert "password" not in "".join(path.read_text(encoding="ascii") for path in paths)
    assert "rtsp" not in "".join(path.read_text(encoding="ascii") for path in paths)
    assert terminal["first_absent_time_utc"] is None
    assert repository.read(investigation_id, _RUN_ID, records[0]["observation_id"]) == records[0]


def test_sidecar_is_immutable_and_corruption_never_becomes_valid(tmp_path: Path) -> None:
    repository, investigation_id, _ = _failed_run(tmp_path)
    path = next((repository.root / investigation_id / _RUN_ID / "diagnostics").glob("*.json"))
    record = json.loads(path.read_text(encoding="ascii"))
    repository.publish(record)
    changed = dict(record, classifier_error_code="worker_abnormal_exit")
    with pytest.raises(SuccessorDiagnosticError, match="diagnostic_conflict"):
        repository.publish(changed)
    assert repository.read(investigation_id, _RUN_ID, record["observation_id"]) == record
    path.write_text("{", encoding="ascii")
    with pytest.raises(SuccessorDiagnosticError, match="diagnostic_corrupt"):
        repository.read(investigation_id, _RUN_ID, record["observation_id"])


def test_concurrent_identical_publication_is_atomic_and_idempotent(tmp_path: Path) -> None:
    repository, investigation_id, _ = _failed_run(tmp_path)
    directory = repository.root / investigation_id / _RUN_ID / "diagnostics"
    record = json.loads(next(directory.glob("*.json")).read_text(encoding="ascii"))
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(repository.publish, (record, record, record, record)))
    assert repository.read(investigation_id, _RUN_ID, record["observation_id"]) == record
    assert not list(directory.glob("*.tmp"))


@pytest.mark.parametrize(
    ("phase", "code", "started", "received", "cleanup", "exit_code", "reason"),
    [
        ("startup", "worker_start_failed", False, False, None, None, "classifier_failed"),
        (
            "result_validation",
            "malformed_worker_protocol",
            True,
            False,
            "completed",
            0,
            "classifier_failed",
        ),
        ("process_exit", "worker_abnormal_exit", True, False, "completed", 7, "classifier_failed"),
        ("cleanup", "worker_execution_failed", True, True, "failed", 0, "classifier_failed"),
        (
            "probe_inference",
            "classifier_timeout",
            True,
            False,
            "completed",
            0,
            "classifier_timeout",
        ),
    ],
)
def test_failure_taxonomy_survives_execution_without_changing_public_state(
    tmp_path: Path,
    phase: str,
    code: str,
    started: bool,
    received: bool,
    cleanup: str | None,
    exit_code: int | None,
    reason: str,
) -> None:
    event: dict[str, object] = {
        "failure_phase": phase,
        "error_code": code,
        "child_started": started,
        "result_received": received,
        "cleanup_status": cleanup,
        "child_exit_code": exit_code,
        "timeout_stage": "inference" if reason == "classifier_timeout" else None,
    }
    repository, investigation_id, terminal = _failed_run(
        tmp_path, _OperationalFailure(event, reason), reason
    )
    path = next((repository.root / investigation_id / _RUN_ID / "diagnostics").glob("*.json"))
    record = json.loads(path.read_text(encoding="ascii"))
    assert record["classifier_stage"] == phase
    assert record["classifier_error_code"] == code
    assert record["child_started"] is started
    assert record["result_received"] is received
    assert record["cleanup_status"] == cleanup
    assert record["child_exit_code"] == exit_code
    assert record["timeout_phase"] == ("inference" if reason == "classifier_timeout" else None)
    assert terminal["reason_code"] == reason
    assert all(item["state"] == reason.upper() for item in terminal["coarse_observations"][1:])


def test_optional_persistence_failure_does_not_change_authoritative_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(_self: object, _record: object) -> None:
        raise OSError("password=secret")

    monkeypatch.setattr(SuccessorDiagnosticRepository, "publish", fail)
    _, _, terminal = _failed_run(tmp_path)
    assert terminal["status"] == "INCONCLUSIVE"
    assert terminal["reason_code"] == "classifier_failed"
    assert "password" not in caplog.text
    assert "diagnostic_unavailable" in caplog.text


def test_missing_sidecar_does_not_affect_historical_reopen(tmp_path: Path) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    assert (
        repository.read(
            "object-disappearance-v3-ch2-20260926T041736Z",
            _RUN_ID,
            "successor-observation-v1-" + "a" * 64,
        )
        is None
    )


def test_success_and_same_run_reopen_never_create_or_append_diagnostics(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id=_RUN_ID,
        now_utc=ANCHOR + timedelta(hours=1),
    )
    first = service.execute(prepared)
    second = service.execute(prepared)
    assert first.status == second.status == "FOUND"
    assert first.terminal_result_id == second.terminal_result_id
    assert service.publisher.read(confirmed.investigation_id, _RUN_ID)["status"] == "FOUND"
    assert not (
        service.publisher.root / confirmed.investigation_id / _RUN_ID / "diagnostics"
    ).exists()


def test_unknown_event_fields_and_values_are_not_persisted(tmp_path: Path) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    investigation_id = "object-disappearance-v3-ch2-20260926T041736Z"
    observation_id = "successor-observation-v1-" + "b" * 64
    with diagnostic_scope(repository, investigation_id, _RUN_ID):
        persist_failure(
            plan_id="successor-plan-v1-test",
            target_id="successor-target-v1-test",
            observation_id=observation_id,
            requested_time_utc="2026-09-26T04:27:36Z",
            total_elapsed_ms=20099,
            public_reason="classifier_failed",
            event={
                "error_code": ["password=secret"],
                "failure_phase": {"url": "rtsp://unsafe"},
                "timeout_stage": ["password=secret"],
                "cleanup_status": {"url": "rtsp://unsafe"},
            },
        )
    loaded = repository.read(investigation_id, _RUN_ID, observation_id)
    assert loaded is not None
    assert loaded["classifier_stage"] == "unknown"
    assert loaded["classifier_error_code"] == "classifier_failed"
    assert loaded["timeout_phase"] is None
    assert loaded["cleanup_status"] is None
    assert "secret" not in json.dumps(loaded)
    assert "unsafe" not in json.dumps(loaded)


@pytest.mark.parametrize(
    ("failure", "reason", "phase", "child_started"),
    [
        (B4ProcessError("worker_start_failed"), "classifier_failed", "startup", False),
        (B4ProcessTimeout(stage="startup"), "classifier_timeout", "startup", None),
        (B4ProcessTimeout(stage="inference"), "classifier_timeout", "probe_inference", None),
    ],
)
def test_production_adapter_preserves_safe_failure_code_without_timing_event(
    monkeypatch: pytest.MonkeyPatch,
    failure: B4ProcessError,
    reason: str,
    phase: str,
    child_started: bool | None,
) -> None:
    baseline, probe, baseline_mask, probe_mask, roi, policy = _values()

    def fail(**_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(execution_module, "run_b4_in_process", fail)
    classifier = SuccessorB4Classifier(policy, StaticMaskWorkerSpec(baseline_mask, probe_mask))
    with pytest.raises(SuccessorClassificationError) as raised:
        classifier.classify(baseline, probe, 32, 32, roi, "failure-correlation")
    assert raised.value.reason == reason
    assert raised.value.diagnostic is not None
    assert raised.value.diagnostic["error_code"] == failure.code
    assert raised.value.diagnostic["failure_phase"] == phase
    if child_started is not None:
        assert raised.value.diagnostic["child_started"] is child_started


def test_real_spawned_b4_failure_reaches_successor_adapter_with_safe_details() -> None:
    baseline, probe, _baseline_mask, _probe_mask, roi, policy = _values()
    empty = BinaryMask.from_rows(tuple(tuple(False for _ in range(32)) for _ in range(32)))
    classifier = SuccessorB4Classifier(policy, StaticMaskWorkerSpec(empty, empty))
    with pytest.raises(SuccessorClassificationError) as raised:
        classifier.classify(baseline, probe, 32, 32, roi, "real-spawned-failure")
    assert raised.value.reason == "classifier_failed"
    details = raised.value.diagnostic
    assert details is not None
    assert details["error_code"] == "invalid_classifier_output"
    assert details["failure_phase"] == "result_validation"
    assert details["child_started"] is True
    assert details["result_received"] is False
    assert details["cleanup_status"] == "completed"
