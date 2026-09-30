"""Optional foreground-retention sidecars remain separate from search authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

import pytest

from test_object_presence_retention_diagnostics import _metrics_with_facts
from test_recording_search_successor_execution import (
    ANCHOR,
    _Classifier,
    _confirmed,
    _service,
)
from vigi_vision.object_presence_retention_diagnostics import ForegroundRetentionFacts
from vigi_vision.object_presence_values import DecodedRgbImage
from vigi_vision.recording_search_successor_classification import SuccessorClassifierResult
from vigi_vision.recording_search_successor_diagnostics import (
    SuccessorDiagnosticError,
    SuccessorDiagnosticRepository,
    diagnostic_scope,
    persist_foreground_retention,
)
from vigi_vision.recording_search_successor_evidence import SuccessorEvidenceRepository
from vigi_vision.recording_search_successor_execution import SuccessorExecutionService

_INVESTIGATION_ID = "object-disappearance-v3-ch1-20260904T051732Z"
_RUN_ID = "search-run-dddddddddddddddddddddddddddddddd"
_OBSERVATION_ID = f"successor-observation-v1-{'a' * 64}"
_FACTS = _metrics_with_facts(
    (60.0, 80.0, 90.0, 120.0, 240.0),
    (50.0, 80.0, 60.0, 60.0, 80.0),
    (40.0,) * 5,
    (40.0,) * 5,
)[1]


def _record(facts: ForegroundRetentionFacts = _FACTS) -> dict[str, object]:
    return {
        "version": "phase7e-foreground-retention-v1",
        "diagnostic_kind": "foreground_retention",
        "investigation_id": _INVESTIGATION_ID,
        "run_id": _RUN_ID,
        "plan_id": "successor-plan-test",
        "target_id": "successor-target-test",
        "observation_id": _OBSERVATION_ID,
        "requested_time_utc": "2026-09-04T05:18:00Z",
        "reference_identity": f"successor-authority-v1-{'b' * 64}",
        "reference_frame_resource_id": "NVR_Channel_01-Reference-Frame",
        "roi_identity": f"successor-roi-v1-{'c' * 64}",
        "classifier_policy_identity": f"policy-{'d' * 64}",
        **facts.to_payload(),
    }


def test_retention_repository_is_distinct_bounded_and_readable(tmp_path: Path) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    record = _record()

    repository.publish_retention(record)

    path = (
        tmp_path
        / _INVESTIGATION_ID
        / _RUN_ID
        / "diagnostics"
        / "foreground-retention-v1"
        / f"{_OBSERVATION_ID}.json"
    )
    assert path.is_file()
    assert path.stat().st_size <= 4096
    assert json.loads(path.read_text(encoding="ascii")) == record
    assert repository.read_retention(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) == record
    assert repository.read(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) is None
    assert set(record) == set(_record())


def test_retention_duplicate_publication_is_idempotent(tmp_path: Path) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    record = _record()

    repository.publish_retention(record)
    repository.publish_retention(record)

    directory = tmp_path / _INVESTIGATION_ID / _RUN_ID / "diagnostics" / "foreground-retention-v1"
    assert len(tuple(directory.glob("*.json"))) == 1
    assert not tuple(directory.glob("*.tmp"))


def test_retention_conflict_and_partial_file_are_rejected(tmp_path: Path) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    record = _record()
    repository.publish_retention(record)
    changed = dict(record, normalization_offset=float(record["normalization_offset"]) + 1.0)

    with pytest.raises(SuccessorDiagnosticError, match="diagnostic_conflict"):
        repository.publish_retention(changed)
    assert repository.read_retention(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) == record

    path = (
        tmp_path
        / _INVESTIGATION_ID
        / _RUN_ID
        / "diagnostics"
        / "foreground-retention-v1"
        / f"{_OBSERVATION_ID}.json"
    )
    path.write_text("{", encoding="ascii")
    with pytest.raises(SuccessorDiagnosticError, match="diagnostic_corrupt"):
        repository.read_retention(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID)


def test_b4_and_retention_diagnostic_types_do_not_share_a_path(tmp_path: Path) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    retention = _record()
    b4 = {
        "version": "phase7e-b4-failure-diagnostic-v1",
        "investigation_id": _INVESTIGATION_ID,
        "run_id": _RUN_ID,
        "plan_id": "successor-plan-test",
        "target_id": "successor-target-test",
        "observation_id": _OBSERVATION_ID,
        "requested_time_utc": "2026-09-04T05:18:00Z",
        "classifier_stage": "probe_inference",
        "classifier_error_code": "worker_execution_failed",
        "startup_elapsed_ms": None,
        "inference_elapsed_ms": None,
        "ipc_result_elapsed_ms": None,
        "cleanup_elapsed_ms": None,
        "total_elapsed_ms": None,
        "timeout_phase": None,
        "child_started": True,
        "child_exit_code": None,
        "result_received": False,
        "cleanup_status": "completed",
    }

    repository.publish(b4)
    repository.publish_retention(retention)

    assert repository.read(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) == b4
    assert repository.read_retention(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) == retention
    diagnostic_directory = tmp_path / _INVESTIGATION_ID / _RUN_ID / "diagnostics"
    assert (diagnostic_directory / f"{_OBSERVATION_ID}.json").is_file()
    assert (diagnostic_directory / "foreground-retention-v1" / f"{_OBSERVATION_ID}.json").is_file()


def test_optional_persistence_failure_does_not_change_classifier_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)

    def fail_publish(_record: dict[str, object]) -> None:
        raise OSError

    monkeypatch.setattr(repository, "publish_retention", fail_publish)
    with diagnostic_scope(repository, _INVESTIGATION_ID, _RUN_ID):
        persist_foreground_retention(
            plan_id="successor-plan-test",
            target_id="successor-target-test",
            observation_id=_OBSERVATION_ID,
            requested_time_utc="2026-09-04T05:18:00Z",
            reference_identity=f"successor-authority-v1-{'b' * 64}",
            reference_frame_resource_id="reference-frame-resource-v1-test",
            roi_identity=f"successor-roi-v1-{'c' * 64}",
            classifier_policy_identity=f"policy-{'d' * 64}",
            facts=_FACTS,
        )
    assert repository.read_retention(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) is None


class _FactsClassifier(_Classifier):
    def classify(  # noqa: PLR0913 - retain the existing classifier protocol.
        self,
        baseline: object,
        probe: DecodedRgbImage,
        width: int,
        height: int,
        roi: object,
        correlation_id: str,
    ) -> SuccessorClassifierResult:
        result = super().classify(baseline, probe, width, height, roi, correlation_id)
        return replace(result, retention_diagnostic=_FACTS)


@dataclass(frozen=True, slots=True)
class _SuccessfulRun:
    service: SuccessorExecutionService
    investigation_id: str
    run_id: str
    terminal: dict[str, object]
    evidence: dict[str, object]


def _successful_run(tmp_path: Path, *, include_diagnostics: bool) -> _SuccessfulRun:
    tmp_path.mkdir(parents=True, exist_ok=True)
    classifier = _FactsClassifier() if include_diagnostics else _Classifier()
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15), classifier)
    service.evidence_repository = SuccessorEvidenceRepository(service.publisher.root)
    confirmed = replace(_confirmed(tmp_path), jpeg_sha256=hashlib.sha256(b"baseline").hexdigest())
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id=_RUN_ID,
        now_utc=ANCHOR + timedelta(hours=1),
    )
    result = service.execute(prepared)
    terminal = service.publisher.read(confirmed.investigation_id, _RUN_ID)
    evidence = service.evidence_repository.read(confirmed.investigation_id, _RUN_ID)
    assert terminal is not None
    assert evidence is not None
    assert result.status == terminal["status"]
    return _SuccessfulRun(service, confirmed.investigation_id, _RUN_ID, terminal, evidence)


def test_historical_run_and_reopen_remain_valid_without_retention_sidecars(
    tmp_path: Path,
) -> None:
    run = _successful_run(tmp_path, include_diagnostics=False)
    repository = SuccessorDiagnosticRepository(run.service.publisher.root)

    assert not tuple(
        (
            repository.root
            / run.investigation_id
            / run.run_id
            / "diagnostics"
            / "foreground-retention-v1"
        ).glob("*.json")
    )
    assert run.service.publisher.read(run.investigation_id, run.run_id) == run.terminal
    assert run.service.evidence_repository.read(run.investigation_id, run.run_id) == run.evidence


def test_retention_sidecar_removal_does_not_change_reopen_or_public_result(
    tmp_path: Path,
) -> None:
    run = _successful_run(tmp_path, include_diagnostics=True)
    repository = SuccessorDiagnosticRepository(run.service.publisher.root)
    directory = (
        repository.root
        / run.investigation_id
        / run.run_id
        / "diagnostics"
        / "foreground-retention-v1"
    )
    paths = tuple(directory.glob("*.json"))
    assert paths
    for path in paths:
        path.unlink()

    assert run.service.publisher.read(run.investigation_id, run.run_id) == run.terminal
    assert run.service.evidence_repository.read(run.investigation_id, run.run_id) == run.evidence


def test_retention_repository_failure_leaves_execution_and_evidence_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _successful_run(tmp_path / "expected", include_diagnostics=True)

    def fail_publish(_self: SuccessorDiagnosticRepository, _record: dict[str, object]) -> None:
        raise OSError

    with monkeypatch.context() as patcher:
        patcher.setattr(SuccessorDiagnosticRepository, "publish_retention", fail_publish)
        actual = _successful_run(tmp_path / "write-failed", include_diagnostics=True)

    assert actual.terminal == expected.terminal
    assert actual.evidence == expected.evidence
    assert actual.terminal["status"] == expected.terminal["status"]
