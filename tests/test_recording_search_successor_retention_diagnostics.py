"""Optional foreground-retention sidecars remain separate from search authority."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_recording_search_successor_execution as successor_execution_test
import vigi_vision.recording_search_successor_execution as execution_module
from test_object_presence_retention_diagnostics import _metrics_with_facts
from test_recording_search_successor_execution import (
    ANCHOR,
    _Classifier,
    _confirmed,
    _service,
)
from vigi_vision.investigation_confirmation_models import ConfirmationRoi, RoiProvenance
from vigi_vision.object_presence_comparator import _LumaNormalization, _make_support_change_facts
from vigi_vision.object_presence_models import BinaryMask
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.object_presence_retention_diagnostics import ForegroundRetentionFacts
from vigi_vision.object_presence_support_change_diagnostics import SupportChangeFacts
from vigi_vision.object_presence_values import DecodedRgbImage
from vigi_vision.recording_search_7e_b4_process import StaticMaskWorkerSpec
from vigi_vision.recording_search_successor_classification import SuccessorClassifierResult
from vigi_vision.recording_search_successor_diagnostics import (
    SuccessorDiagnosticError,
    SuccessorDiagnosticRepository,
    diagnostic_scope,
    persist_foreground_retention,
    persist_support_change,
)
from vigi_vision.recording_search_successor_evidence import SuccessorEvidenceRepository
from vigi_vision.recording_search_successor_execution import (
    SuccessorB4Classifier,
    SuccessorExecutionError,
    SuccessorExecutionService,
)

_INVESTIGATION_ID = "object-disappearance-v3-ch1-20260904T051732Z"
_RUN_ID = "search-run-dddddddddddddddddddddddddddddddd"
_OBSERVATION_ID = f"successor-observation-v1-{'a' * 64}"
_FACTS = _metrics_with_facts(
    (60.0, 80.0, 90.0, 120.0, 240.0),
    (50.0, 80.0, 60.0, 60.0, 80.0),
    (40.0,) * 5,
    (40.0,) * 5,
)[1]
_SUPPORT_FACTS = _make_support_change_facts(
    (100.0,) * 4,
    (100.0,) * 4,
    (100.0,) * 4,
    _LumaNormalization(100.0, 100.0, 1.0, ()),
    "e" * 64,
    4,
    4,
    4,
    1,
    1,
    0,
    0,
    0,
    1.0,
    "not_required",
    0.0,
)
assert isinstance(_SUPPORT_FACTS, SupportChangeFacts)


def _support_record(facts: SupportChangeFacts = _SUPPORT_FACTS) -> dict[str, object]:
    return {
        "version": "phase7e-support-change-v1",
        "diagnostic_kind": "support_change",
        "investigation_id": _INVESTIGATION_ID,
        "run_id": _RUN_ID,
        "plan_id": "successor-plan-test",
        "target_id": "successor-target-test",
        "observation_id": _OBSERVATION_ID,
        "requested_time_utc": "2026-09-04T05:18:00Z",
        "reference_identity": f"successor-authority-v1-{'b' * 64}",
        "reference_frame_resource_id": "NVR_Channel_01-Reference-Frame",
        "baseline_frame_digest": "a" * 64,
        "probe_frame_digest": "b" * 64,
        "roi_identity": f"successor-roi-v1-{'c' * 64}",
        "roi_x": 0,
        "roi_y": 0,
        "roi_width": 4,
        "roi_height": 4,
        "classifier_policy_identity": f"policy-{'d' * 64}",
        **facts.to_payload(),
    }


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


def test_reused_final_observations_keep_one_unchanged_sidecar_each(tmp_path: Path) -> None:
    first = _successful_run(tmp_path, include_diagnostics=True)
    directory = (
        first.service.publisher.root
        / first.investigation_id
        / first.run_id
        / "diagnostics"
        / "foreground-retention-v1"
    )
    before = {path.name: path.read_bytes() for path in directory.glob("*.json")}

    second = _successful_run(tmp_path, include_diagnostics=True)

    after = {path.name: path.read_bytes() for path in directory.glob("*.json")}
    assert second.terminal == first.terminal
    assert second.evidence == first.evidence
    assert after == before


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


def test_support_change_repository_is_separate_bounded_and_idempotent(tmp_path: Path) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    record = _support_record()

    repository.publish_support_change(record)
    repository.publish_support_change(record)

    path = (
        tmp_path
        / _INVESTIGATION_ID
        / _RUN_ID
        / "diagnostics"
        / "support-change-v1"
        / f"{_OBSERVATION_ID}.json"
    )
    assert path.is_file()
    assert path.stat().st_size <= 4096
    assert repository.read_support_change(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) == record
    assert repository.read(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) is None
    assert repository.read_retention(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) is None


def test_concurrent_identical_support_change_publication_is_atomic(tmp_path: Path) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    record = _support_record()

    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(repository.publish_support_change, record) for _ in range(8)]
        for future in futures:
            future.result()

    directory = tmp_path / _INVESTIGATION_ID / _RUN_ID / "diagnostics" / "support-change-v1"
    paths = tuple(directory.glob("*.json"))
    assert len(paths) == 1
    assert json.loads(paths[0].read_text(encoding="ascii")) == record


def test_support_change_conflict_and_corrupt_record_are_never_overwritten(
    tmp_path: Path,
) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    record = _support_record()
    repository.publish_support_change(record)
    changed = dict(record, normalization_offset=1.0)

    with pytest.raises(SuccessorDiagnosticError, match="diagnostic_conflict"):
        repository.publish_support_change(changed)
    assert repository.read_support_change(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) == record

    path = (
        tmp_path
        / _INVESTIGATION_ID
        / _RUN_ID
        / "diagnostics"
        / "support-change-v1"
        / f"{_OBSERVATION_ID}.json"
    )
    path.write_text("{", encoding="ascii")
    with pytest.raises(SuccessorDiagnosticError, match="diagnostic_corrupt"):
        repository.publish_support_change(record)
    assert path.read_text(encoding="ascii") == "{"


def test_malformed_support_change_record_is_rejected_without_publication(tmp_path: Path) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)
    malformed = dict(_support_record(), normalization_scale=10**1000)

    with pytest.raises(SuccessorDiagnosticError, match="diagnostic_corrupt"):
        repository.publish_support_change(malformed)
    assert repository.read_support_change(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) is None


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


def test_support_change_optional_write_failure_is_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SuccessorDiagnosticRepository(tmp_path)

    def fail_publish(_record: dict[str, object]) -> None:
        raise OSError

    monkeypatch.setattr(repository, "publish_support_change", fail_publish)
    with diagnostic_scope(repository, _INVESTIGATION_ID, _RUN_ID):
        persist_support_change(
            plan_id="successor-plan-test",
            target_id="successor-target-test",
            observation_id=_OBSERVATION_ID,
            requested_time_utc="2026-09-04T05:18:00Z",
            reference_identity=f"successor-authority-v1-{'b' * 64}",
            reference_frame_resource_id="reference-frame-resource-v1-test",
            baseline_frame_digest="a" * 64,
            probe_frame_digest="b" * 64,
            roi_identity=f"successor-roi-v1-{'c' * 64}",
            roi_x=0,
            roi_y=0,
            roi_width=4,
            roi_height=4,
            classifier_policy_identity=f"policy-{'d' * 64}",
            facts=_SUPPORT_FACTS,
        )
    assert repository.read_support_change(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID) is None


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
        return replace(
            result,
            retention_diagnostic=_FACTS,
            support_change_diagnostic=_SUPPORT_FACTS,
        )


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
    assert not tuple(
        (
            repository.root
            / run.investigation_id
            / run.run_id
            / "diagnostics"
            / "support-change-v1"
        ).glob("*.json")
    )
    assert run.service.publisher.read(run.investigation_id, run.run_id) == run.terminal
    assert run.service.evidence_repository.read(run.investigation_id, run.run_id) == run.evidence


def test_legacy_provisional_sidecar_is_ignored_and_not_migrated(tmp_path: Path) -> None:
    run = _successful_run(tmp_path, include_diagnostics=False)
    repository = SuccessorDiagnosticRepository(run.service.publisher.root)
    legacy_record = _record()
    legacy_ids = {str(entry["observation_id"]) for entry in run.evidence["entries"]}
    assert _OBSERVATION_ID not in legacy_ids

    repository.publish_retention(legacy_record)

    legacy_path = (
        repository.root
        / run.investigation_id
        / run.run_id
        / "diagnostics"
        / "foreground-retention-v1"
        / f"{_OBSERVATION_ID}.json"
    )
    assert repository.read_retention(run.investigation_id, run.run_id, _OBSERVATION_ID) == (
        legacy_record
    )
    assert tuple(legacy_path.parent.glob("*.json")) == (legacy_path,)
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

    support_directory = (
        repository.root / run.investigation_id / run.run_id / "diagnostics" / "support-change-v1"
    )
    assert tuple(support_directory.glob("*.json"))

    assert run.service.publisher.read(run.investigation_id, run.run_id) == run.terminal
    assert run.service.evidence_repository.read(run.investigation_id, run.run_id) == run.evidence


def test_retention_sidecars_bind_to_final_authoritative_observation_ids(tmp_path: Path) -> None:
    run = _successful_run(tmp_path, include_diagnostics=True)
    repository = SuccessorDiagnosticRepository(run.service.publisher.root)
    directory = (
        repository.root
        / run.investigation_id
        / run.run_id
        / "diagnostics"
        / "foreground-retention-v1"
    )
    evidence_by_id = {
        str(entry["observation_id"]): entry
        for entry in run.evidence["entries"]
        if isinstance(entry, dict) and entry.get("role") != "baseline_link"
    }
    paths = tuple(directory.glob("*.json"))
    sidecar_ids = {path.stem for path in paths}

    assert paths
    assert sidecar_ids <= set(evidence_by_id)
    assert any(entry["role"] == "anchor" for entry in evidence_by_id.values())
    anchor_ids = {
        observation_id
        for observation_id, entry in evidence_by_id.items()
        if entry["role"] == "anchor"
    }
    midpoint_ids = {
        observation_id
        for observation_id, entry in evidence_by_id.items()
        if str(entry["target_id"]).startswith("successor-midpoint-target-v1-")
    }
    assert anchor_ids <= sidecar_ids
    assert midpoint_ids
    assert midpoint_ids <= sidecar_ids
    for path in paths:
        observation_id = path.stem
        diagnostic = json.loads(path.read_text(encoding="ascii"))
        evidence_observation = evidence_by_id[observation_id]
        assert diagnostic["observation_id"] == observation_id
        assert diagnostic["investigation_id"] == run.investigation_id
        assert diagnostic["run_id"] == run.run_id
        assert diagnostic["plan_id"] == evidence_observation["plan_id"]
        assert diagnostic["target_id"] == evidence_observation["target_id"]
        evidence_time = datetime.fromisoformat(
            str(evidence_observation["requested_time_utc"]).replace("Z", "+00:00")
        )
        assert diagnostic["requested_time_utc"] == evidence_time.isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        assert diagnostic["reference_identity"] == evidence_observation["authority_identity"]
        assert (
            diagnostic["reference_frame_resource_id"]
            == evidence_observation["reference_frame_resource_id"]
        )
        assert diagnostic["roi_identity"] == evidence_observation["roi_identity"]
        assert (
            diagnostic["classifier_policy_identity"]
            == evidence_observation["classifier_policy_identity"]
        )
        assert {key: diagnostic[key] for key in _FACTS.to_payload()} == _FACTS.to_payload()


def test_support_change_sidecars_bind_to_final_ids_and_coexist_with_retention(
    tmp_path: Path,
) -> None:
    run = _successful_run(tmp_path, include_diagnostics=True)
    repository = SuccessorDiagnosticRepository(run.service.publisher.root)
    diagnostics = run.service.publisher.root / run.investigation_id / run.run_id / "diagnostics"
    support_directory = diagnostics / "support-change-v1"
    retention_directory = diagnostics / "foreground-retention-v1"
    evidence_by_id = {
        str(entry["observation_id"]): entry
        for entry in run.evidence["entries"]
        if isinstance(entry, dict) and entry.get("role") != "baseline_link"
    }
    support_paths = tuple(support_directory.glob("*.json"))
    retention_ids = {path.stem for path in retention_directory.glob("*.json")}
    support_ids = {path.stem for path in support_paths}

    assert support_ids
    assert support_ids == retention_ids
    assert support_ids <= set(evidence_by_id)
    anchor_ids = {
        observation_id
        for observation_id, entry in evidence_by_id.items()
        if entry["role"] == "anchor"
    }
    coarse_ids = {
        observation_id
        for observation_id, entry in evidence_by_id.items()
        if str(entry["target_id"]).startswith("successor-target-v1-")
    }
    midpoint_ids = {
        observation_id
        for observation_id, entry in evidence_by_id.items()
        if str(entry["target_id"]).startswith("successor-midpoint-target-v1-")
    }
    assert anchor_ids
    assert coarse_ids
    assert midpoint_ids
    assert anchor_ids <= support_ids
    assert coarse_ids <= support_ids
    assert midpoint_ids <= support_ids
    for path in support_paths:
        observation_id = path.stem
        diagnostic = repository.read_support_change(
            run.investigation_id, run.run_id, observation_id
        )
        assert diagnostic is not None
        assert diagnostic["observation_id"] == observation_id == path.stem
        assert observation_id in evidence_by_id
        assert diagnostic["probe_frame_digest"] == evidence_by_id[observation_id]["digest"]
        assert diagnostic["baseline_frame_digest"] == hashlib.sha256(b"baseline").hexdigest()
        assert diagnostic["support_mask_fingerprint"] == _SUPPORT_FACTS.support_mask_fingerprint

    assert (
        repository.read_retention(run.investigation_id, run.run_id, next(iter(support_ids)))
        is not None
    )


def test_real_b4_execution_persists_diagnostics_against_final_evidence_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def decode(_self: object, payload: bytes, width: int, height: int) -> object:
        rows = tuple(
            tuple(
                (
                    (180 + (x + y) % 5,) * 3
                    if payload == b"absent" or not (12 <= x < 20 and 12 <= y < 20)
                    else (52 + (x + y) % 5,) * 3
                )
                for x in range(width)
            )
            for y in range(height)
        )
        return SimpleNamespace(image=DecodedRgbImage.from_rows(rows), integrity=None)

    def decode_frame(_self: object, request: object) -> object:
        clip_path = request.clip_path  # type: ignore[attr-defined]
        output_path = request.output_path  # type: ignore[attr-defined]
        payload = clip_path.read_bytes()
        output_path.write_bytes(payload)
        return successor_execution_test.DecodedFrameEvidence(
            output_path,
            request.target_offset_seconds,  # type: ignore[attr-defined]
            32,
            32,
            successor_execution_test.TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
            (),
        )

    monkeypatch.setattr(successor_execution_test._MediaDecoder, "decode", decode)
    monkeypatch.setattr(successor_execution_test._FrameDecoder, "decode", decode_frame)
    width = 32
    height = 32
    roi = ConfirmationRoi(
        x=4,
        y=4,
        width=24,
        height=24,
        coordinate_space="source_pixels",
        provenance=RoiProvenance.MANUAL,
    )
    mask = BinaryMask.from_rows(
        tuple(tuple(12 <= x < 20 and 12 <= y < 20 for x in range(width)) for y in range(height))
    )
    policy = ObjectPresenceDecisionPolicy(
        classifier_policy_version="test-support-change-production-execution",
        classifier_preprocessing_version="test-support-change-production-execution",
        baseline_support_mode=True,
        minimum_mask_overlap_for_comparison=0.1,
        minimum_comparison_area=64,
        minimum_roi_pixels=64,
        minimum_clipped_mask_pixels=1,
    )
    classifier = SuccessorB4Classifier(policy, StaticMaskWorkerSpec(mask, mask))
    service = _service(tmp_path, ANCHOR + timedelta(hours=1), classifier)
    service.evidence_repository = SuccessorEvidenceRepository(service.publisher.root)
    confirmed = replace(
        _confirmed(tmp_path),
        source_width=width,
        source_height=height,
        roi=roi,
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:21:32",
        run_id=_RUN_ID,
        now_utc=ANCHOR + timedelta(hours=1),
    )

    terminal = service.execute(prepared)

    evidence = service.evidence_repository.read(confirmed.investigation_id, _RUN_ID)
    assert evidence is not None
    assert terminal.status == evidence["terminal_status"]
    evidence_by_id = {
        str(item["observation_id"]): item
        for item in evidence["entries"]
        if isinstance(item, dict) and item.get("role") != "baseline_link"
    }
    assert any(item["role"] == "anchor" for item in evidence_by_id.values())
    assert any(
        str(item["target_id"]).startswith("successor-target-v1-")
        for item in evidence_by_id.values()
    )
    repository = SuccessorDiagnosticRepository(service.publisher.root)
    diagnostics_root = service.publisher.root / confirmed.investigation_id / _RUN_ID / "diagnostics"
    support_paths = tuple((diagnostics_root / "support-change-v1").glob("*.json"))
    retention_paths = tuple((diagnostics_root / "foreground-retention-v1").glob("*.json"))
    assert support_paths
    assert {path.stem for path in support_paths} == {path.stem for path in retention_paths}
    assert {path.stem for path in support_paths} <= set(evidence_by_id)
    for path in support_paths:
        record = repository.read_support_change(confirmed.investigation_id, _RUN_ID, path.stem)
        assert record is not None
        assert record["observation_id"] == path.stem == evidence_by_id[path.stem]["observation_id"]
        assert repository.read_retention(confirmed.investigation_id, _RUN_ID, path.stem) is not None
    assert not tuple((diagnostics_root / "support-change-v1").glob("*.tmp"))


def test_retention_identity_mismatch_discards_diagnostic_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _successful_run(tmp_path / "expected", include_diagnostics=True)

    with monkeypatch.context() as patcher:
        patcher.setattr(
            execution_module,
            "_retention_identity_matches_final_observation",
            lambda *_args, **_kwargs: False,
        )
        actual = _successful_run(tmp_path / "identity-mismatch", include_diagnostics=True)

    assert actual.terminal == expected.terminal
    assert actual.evidence == expected.evidence
    directory = (
        actual.service.publisher.root
        / actual.investigation_id
        / actual.run_id
        / "diagnostics"
        / "foreground-retention-v1"
    )
    assert not tuple(directory.glob("*.json"))
    support_directory = (
        actual.service.publisher.root
        / actual.investigation_id
        / actual.run_id
        / "diagnostics"
        / "support-change-v1"
    )
    assert not tuple(support_directory.glob("*.json"))


def test_failure_before_final_observation_binding_leaves_no_retention_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15), _FactsClassifier())
    service.evidence_repository = SuccessorEvidenceRepository(service.publisher.root)
    confirmed = replace(_confirmed(tmp_path), jpeg_sha256=hashlib.sha256(b"baseline").hexdigest())
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id=_RUN_ID,
        now_utc=ANCHOR + timedelta(hours=1),
    )

    def fail_binding(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError

    monkeypatch.setattr(execution_module, "_with_anchor_observation", fail_binding)
    with pytest.raises(SuccessorExecutionError):
        service.execute(prepared)

    directory = (
        service.publisher.root
        / confirmed.investigation_id
        / _RUN_ID
        / "diagnostics"
        / "foreground-retention-v1"
    )
    assert not tuple(directory.glob("*.json"))


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
    support_directory = (
        actual.service.publisher.root
        / actual.investigation_id
        / actual.run_id
        / "diagnostics"
        / "support-change-v1"
    )
    assert tuple(support_directory.glob("*.json"))


def test_support_change_repository_failure_leaves_retention_and_execution_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _successful_run(tmp_path / "expected", include_diagnostics=True)

    def fail_publish(_self: SuccessorDiagnosticRepository, _record: dict[str, object]) -> None:
        raise OSError

    with monkeypatch.context() as patcher:
        patcher.setattr(SuccessorDiagnosticRepository, "publish_support_change", fail_publish)
        actual = _successful_run(tmp_path / "write-failed", include_diagnostics=True)

    assert actual.terminal == expected.terminal
    assert actual.evidence == expected.evidence
    retention_directory = (
        actual.service.publisher.root
        / actual.investigation_id
        / actual.run_id
        / "diagnostics"
        / "foreground-retention-v1"
    )
    assert tuple(retention_directory.glob("*.json"))
    support_directory = retention_directory.parent / "support-change-v1"
    assert not tuple(support_directory.glob("*.json"))
