# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnusedCallResult=false, reportAttributeAccessIssue=false, reportPrivateUsage=false
# ruff: noqa: I001, SIM102
"""Focused persistence and strict-reopen coverage for Phase 7E-1B."""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any

import pytest

from vigi_vision.recording_search_7e_models import (
    Schema5PhaseState,
    Schema6TargetState,
    StrictIdentityEnvelope,
)
from vigi_vision.recording_search_7e_repository import (
    Phase7ECorruptError,
    Phase7EConflictError,
    Phase7ERepositoryError,
    PublicationStatus,
    RecordingSearch7ERepository,
)
from vigi_vision.recording_search_7e_validation import (
    Phase7EValidationError,
    Schema5Envelope,
    Schema6Envelope,
)


_DOC = Path(__file__).parents[1] / "docs" / "design" / "object-disappearance-recording-search.md"


def _vectors() -> list[dict[str, Any]]:
    text = _DOC.read_text(encoding="utf-8")
    vectors: list[dict[str, Any]] = []
    for match in re.finditer(r"```json", text):
        end = text.find("```", match.end())
        if end < 0:
            continue
        try:
            value = json.loads(text[match.end() : end])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            if value and all({"family", "expected_id", "payload"} <= set(item) for item in value):
                vectors.extend(value)
    return vectors


def _fixture() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    vectors = _vectors()
    by_family: dict[str, list[dict[str, Any]]] = {}
    for vector in vectors:
        by_family.setdefault(vector["family"], []).append(vector)
    schema5 = by_family["schema5-manifest"][0]
    payload = schema5["payload"]
    targets = [
        vector
        for vector in by_family["target-request"]
        if vector["expected_id"] in payload["coarse_target_request_ids"]
    ]
    records = [
        by_family["policy"][0],
        by_family["coarse-plan"][0],
        *targets,
    ]
    return schema5, by_family["coarse-plan"][0], records


def _envelopes(records: list[dict[str, Any]]) -> list[StrictIdentityEnvelope]:
    return [
        StrictIdentityEnvelope(
            family=record["family"],
            identity=record["expected_id"],
            payload=record["payload"],
        )
        for record in records
    ]


def _planned() -> Schema5Envelope:
    return Schema5Envelope(
        run_state="RUNNING",
        phase_state=Schema5PhaseState.PLANNED,
        active_replay_operation_id=None,
        reason_code=None,
        attempt_count=0,
    )


def test_schema5_create_duplicate_reuse_and_strict_reopen(tmp_path: Path) -> None:
    schema5, _, raw_records = _fixture()
    repo = RecordingSearch7ERepository(tmp_path)
    manifest = StrictIdentityEnvelope(
        family=schema5["family"], identity=schema5["expected_id"], payload=schema5["payload"]
    )
    records = _envelopes(raw_records)

    first = repo.create_schema5(manifest, _planned(), records)
    assert first.status is PublicationStatus.CREATED
    before = (first.run.root / "manifest.json").read_bytes()
    second = repo.create_schema5(manifest, _planned(), records)
    assert second.status is PublicationStatus.REUSED
    assert (first.run.root / "manifest.json").read_bytes() == before
    reopened = repo.reopen_schema5("inv-01", "run-01")
    assert reopened.manifest_id == manifest.identity
    assert reopened.state.phase_state is Schema5PhaseState.PLANNED


def test_schema5_conflicting_duplicate_fails_closed(tmp_path: Path) -> None:
    schema5, _, raw_records = _fixture()
    repo = RecordingSearch7ERepository(tmp_path)
    records = _envelopes(raw_records)
    manifest = StrictIdentityEnvelope(
        family=schema5["family"], identity=schema5["expected_id"], payload=schema5["payload"]
    )
    repo.create_schema5(manifest, _planned(), records)
    changed = dict(schema5["payload"])
    changed["coarse_target_request_ids"] = [raw_records[-1]["expected_id"]]
    changed_manifest = StrictIdentityEnvelope.from_payload("schema5-manifest", changed)
    with pytest.raises(Phase7EConflictError):
        repo.create_schema5(changed_manifest, _planned(), [records[0], records[1], records[-1]])


def test_strict_reopen_rejects_unindexed_child_without_mutation(tmp_path: Path) -> None:
    schema5, _, raw_records = _fixture()
    repo = RecordingSearch7ERepository(tmp_path)
    manifest = StrictIdentityEnvelope(
        family=schema5["family"], identity=schema5["expected_id"], payload=schema5["payload"]
    )
    result = repo.create_schema5(manifest, _planned(), _envelopes(raw_records))
    rogue = result.run.root / "requests" / ("rr-target-request-v1-" + "0" * 64 + ".json")
    rogue.write_text(
        json.dumps({"family": "target-request", "identity": rogue.stem, "payload": {}}),
        encoding="utf-8",
    )
    before = rogue.read_bytes()
    with pytest.raises(Phase7ECorruptError):
        repo.reopen_schema5("inv-01", "run-01")
    assert rogue.read_bytes() == before


def test_schema5_to_schema6_requires_acquired_predecessor(tmp_path: Path) -> None:
    schema5, _, raw_records = _fixture()
    repo = RecordingSearch7ERepository(tmp_path)
    manifest = StrictIdentityEnvelope(
        family=schema5["family"], identity=schema5["expected_id"], payload=schema5["payload"]
    )
    repo.create_schema5(manifest, _planned(), _envelopes(raw_records))
    schema6_payload = {
        "schema_version": 6,
        "investigation_id": "inv-01",
        "run_id": "run-01",
        "schema5_predecessor_manifest_id": manifest.identity,
        "policy_id": schema5["payload"]["policy_id"],
        "classifier_policy_id": "rr-classifier-policy-v1-" + "0" * 64,
        "plan_id": schema5["payload"]["plan_id"],
        "replay_operation_id": "rr-replay-operation-v1-" + "0" * 64,
        "common_session_id": "rr-common-session-v1-" + "0" * 64,
        "indexes": {
            key: []
            for key in (
                "target_request_ids",
                "decoder_operation_ids",
                "frame_ids",
                "classification_operation_ids",
                "observation_ids",
                "alias_ids",
                "support_group_ids",
                "c2_bracket_ids",
                "d1_input_ids",
                "d1_history_ids",
                "narrowed_bracket_ids",
            )
        },
    }
    proposal = StrictIdentityEnvelope.from_payload("schema6-manifest", schema6_payload)
    state = Schema6Envelope(
        run_state="RUNNING",
        target_state=Schema6TargetState.REQUESTED,
        active_target_request_id=None,
        active_decoder_operation_id=None,
        active_frame_id=None,
        active_classification_attempt_id=None,
        active_classification_operation_id=None,
        active_observation_id=None,
        reason_code=None,
        attempt_count=0,
        predecessor_target_state=None,
    )
    with pytest.raises(Phase7EValidationError):
        repo.transition_schema5_to_schema6(
            "inv-01", "run-01", proposal, state, _envelopes(raw_records)
        )


def _state5(phase: Schema5PhaseState, operation_id: str | None = None) -> Schema5Envelope:
    if phase is Schema5PhaseState.PLANNED:
        return _planned()
    if phase is Schema5PhaseState.ACQUIRING:
        return Schema5Envelope(
            run_state="RUNNING",
            phase_state=phase,
            active_replay_operation_id=operation_id,
            reason_code=None,
            attempt_count=1,
        )
    return Schema5Envelope(
        run_state="RUNNING",
        phase_state=phase,
        active_replay_operation_id=operation_id,
        reason_code=None,
        attempt_count=1,
    )


def test_schema5_lifecycle_and_recovery_preserve_owned_operation(tmp_path: Path) -> None:
    schema5, _, raw_records = _fixture()
    vectors = {vector["family"]: vector for vector in _vectors()}
    repo = RecordingSearch7ERepository(tmp_path)
    manifest = StrictIdentityEnvelope(
        family=schema5["family"], identity=schema5["expected_id"], payload=schema5["payload"]
    )
    base = _envelopes(raw_records)
    operation = StrictIdentityEnvelope(
        family=vectors["replay-operation"]["family"],
        identity=vectors["replay-operation"]["expected_id"],
        payload=vectors["replay-operation"]["payload"],
    )
    repo.create_schema5(manifest, _planned(), base)
    acquiring = repo.admit_schema5(
        "inv-01",
        "run-01",
        manifest,
        _state5(Schema5PhaseState.ACQUIRING, operation.identity),
        [*base, operation],
    )
    assert acquiring.status is PublicationStatus.CREATED
    interrupted = repo.recover_active("inv-01", "run-01")
    assert interrupted.state.phase_state is Schema5PhaseState.INTERRUPTED
    assert interrupted.state.active_replay_operation_id == operation.identity
    assert (interrupted.root / "operations" / f"{operation.identity}.json").is_file()


def test_schema5_to_schema6_publishes_archive_and_reuses_identical_retry(tmp_path: Path) -> None:
    schema5, _, raw_records = _fixture()
    vectors = {vector["family"]: vector for vector in _vectors()}
    repo = RecordingSearch7ERepository(tmp_path)
    manifest = StrictIdentityEnvelope(
        family=schema5["family"], identity=schema5["expected_id"], payload=schema5["payload"]
    )
    base = _envelopes(raw_records)
    operation = StrictIdentityEnvelope(
        family=vectors["replay-operation"]["family"],
        identity=vectors["replay-operation"]["expected_id"],
        payload=vectors["replay-operation"]["payload"],
    )
    repo.create_schema5(manifest, _planned(), base)
    repo.admit_schema5(
        "inv-01",
        "run-01",
        manifest,
        _state5(Schema5PhaseState.ACQUIRING, operation.identity),
        [*base, operation],
    )
    acquired = _state5(Schema5PhaseState.ACQUIRED, operation.identity)
    repo.admit_schema5("inv-01", "run-01", manifest, acquired, [*base, operation])
    schema6 = vectors["schema6-manifest"]
    schema6_payload = dict(schema6["payload"])
    schema6_payload["indexes"] = {
        key: (
            [vector["expected_id"] for vector in _vectors() if vector["family"] == "target-request"]
            if key == "target_request_ids"
            else []
        )
        for key in schema6_payload["indexes"]
    }
    proposal = StrictIdentityEnvelope(
        family=schema6["family"],
        identity=StrictIdentityEnvelope.from_payload("schema6-manifest", schema6_payload).identity,
        payload=schema6_payload,
    )
    all_targets = [
        StrictIdentityEnvelope(
            family=vector["family"], identity=vector["expected_id"], payload=vector["payload"]
        )
        for vector in _vectors()
        if vector["family"] == "target-request"
    ]
    schema6_records = [
        base[0],
        base[1],
        operation,
        StrictIdentityEnvelope(
            family=vectors["classifier-policy"]["family"],
            identity=vectors["classifier-policy"]["expected_id"],
            payload=vectors["classifier-policy"]["payload"],
        ),
        StrictIdentityEnvelope(
            family=vectors["common-session"]["family"],
            identity=vectors["common-session"]["expected_id"],
            payload=vectors["common-session"]["payload"],
        ),
        *all_targets,
    ]
    state = Schema6Envelope(
        run_state="RUNNING",
        target_state=Schema6TargetState.REQUESTED,
        active_target_request_id=all_targets[0].identity,
        active_decoder_operation_id=None,
        active_frame_id=None,
        active_classification_attempt_id=None,
        active_classification_operation_id=None,
        active_observation_id=None,
        reason_code=None,
        attempt_count=0,
        predecessor_target_state=None,
    )
    first = repo.transition_schema5_to_schema6(
        "inv-01",
        "run-01",
        proposal,
        state,
        schema6_records,
        expected_schema5_manifest_id=manifest.identity,
    )
    assert first.status is PublicationStatus.CREATED
    assert (first.run.root / "manifests" / f"{manifest.identity}.json").is_file()
    assert first.run.is_schema6
    retry = repo.transition_schema5_to_schema6("inv-01", "run-01", proposal, state, schema6_records)
    assert retry.status is PublicationStatus.REUSED
    assert retry.run.manifest_id == proposal.identity


def test_schema6_increment_persists_decoder_and_frame_bytes(tmp_path: Path) -> None:
    schema5, _, raw_records = _fixture()
    vectors = {vector["family"]: vector for vector in _vectors()}
    repo = RecordingSearch7ERepository(tmp_path)
    manifest5 = StrictIdentityEnvelope(
        family=schema5["family"], identity=schema5["expected_id"], payload=schema5["payload"]
    )
    base = _envelopes(raw_records)
    operation = StrictIdentityEnvelope(
        family=vectors["replay-operation"]["family"],
        identity=vectors["replay-operation"]["expected_id"],
        payload=vectors["replay-operation"]["payload"],
    )
    repo.create_schema5(manifest5, _planned(), base)
    repo.admit_schema5(
        "inv-01",
        "run-01",
        manifest5,
        _state5(Schema5PhaseState.ACQUIRING, operation.identity),
        [*base, operation],
    )
    repo.admit_schema5(
        "inv-01",
        "run-01",
        manifest5,
        _state5(Schema5PhaseState.ACQUIRED, operation.identity),
        [*base, operation],
    )
    targets = [
        StrictIdentityEnvelope(
            family=vector["family"], identity=vector["expected_id"], payload=vector["payload"]
        )
        for vector in _vectors()
        if vector["family"] == "target-request"
    ]
    classifier_policy = StrictIdentityEnvelope(
        family=vectors["classifier-policy"]["family"],
        identity=vectors["classifier-policy"]["expected_id"],
        payload=vectors["classifier-policy"]["payload"],
    )
    common_session = StrictIdentityEnvelope(
        family=vectors["common-session"]["family"],
        identity=vectors["common-session"]["expected_id"],
        payload=vectors["common-session"]["payload"],
    )
    base_payload = dict(vectors["schema6-manifest"]["payload"])
    indexes = {key: [] for key in base_payload["indexes"]}
    indexes["target_request_ids"] = [target.identity for target in targets]
    base_payload["indexes"] = indexes
    manifest6 = StrictIdentityEnvelope.from_payload("schema6-manifest", base_payload)
    state_requested = Schema6Envelope(
        run_state="RUNNING",
        target_state=Schema6TargetState.REQUESTED,
        active_target_request_id=targets[0].identity,
        active_decoder_operation_id=None,
        active_frame_id=None,
        active_classification_attempt_id=None,
        active_classification_operation_id=None,
        active_observation_id=None,
        reason_code=None,
        attempt_count=0,
        predecessor_target_state=None,
    )
    common = [base[0], base[1], *targets, operation, classifier_policy, common_session]
    repo.transition_schema5_to_schema6("inv-01", "run-01", manifest6, state_requested, common)
    decoder = StrictIdentityEnvelope(
        family=vectors["decoder-operation"]["family"],
        identity=vectors["decoder-operation"]["expected_id"],
        payload=vectors["decoder-operation"]["payload"],
    )
    payload_decoding = dict(base_payload)
    payload_decoding["indexes"] = dict(indexes)
    payload_decoding["indexes"]["decoder_operation_ids"] = [decoder.identity]
    manifest_decoding = StrictIdentityEnvelope.from_payload("schema6-manifest", payload_decoding)
    state_decoding = state_requested.model_copy(
        update={
            "target_state": Schema6TargetState.DECODING,
            "active_decoder_operation_id": decoder.identity,
            "attempt_count": 1,
            "predecessor_target_state": Schema6TargetState.REQUESTED,
        }
    )
    repo.admit_schema6(
        "inv-01",
        "run-01",
        manifest_decoding,
        state_decoding,
        [*common, decoder],
        expected_manifest_id=manifest6.identity,
    )
    raw = b"jpeg-placeholder"
    frame_payload = dict(vectors["frame"]["payload"])
    frame_payload.update(
        {"jpeg_size_bytes": len(raw), "jpeg_sha256": hashlib.sha256(raw).hexdigest()}
    )
    frame_payload["decoder_operation_id"] = decoder.identity
    frame = StrictIdentityEnvelope.from_payload("frame", frame_payload)
    payload_ready = dict(payload_decoding)
    payload_ready["indexes"] = dict(payload_decoding["indexes"])
    payload_ready["indexes"]["frame_ids"] = [frame.identity]
    manifest_ready = StrictIdentityEnvelope.from_payload("schema6-manifest", payload_ready)
    state_ready = state_decoding.model_copy(
        update={
            "target_state": Schema6TargetState.FRAME_READY,
            "active_frame_id": frame.identity,
            "predecessor_target_state": Schema6TargetState.DECODING,
        }
    )
    result = repo.admit_schema6(
        "inv-01",
        "run-01",
        manifest_ready,
        state_ready,
        [*common, decoder, frame],
        expected_manifest_id=manifest_decoding.identity,
        binary_records={frame.identity: raw},
    )
    assert result.status is PublicationStatus.CREATED
    assert (result.run.root / "frames" / f"{frame.identity}.jpg").read_bytes() == raw
    assert (
        repo.reopen_schema6("inv-01", "run-01").state.target_state is Schema6TargetState.FRAME_READY
    )


def test_successor_failure_removes_only_invocation_owned_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema5, _, raw_records = _fixture()
    vectors = {vector["family"]: vector for vector in _vectors()}
    repo = RecordingSearch7ERepository(tmp_path)
    manifest = StrictIdentityEnvelope(
        family=schema5["family"], identity=schema5["expected_id"], payload=schema5["payload"]
    )
    base = _envelopes(raw_records)
    operation = StrictIdentityEnvelope(
        family=vectors["replay-operation"]["family"],
        identity=vectors["replay-operation"]["expected_id"],
        payload=vectors["replay-operation"]["payload"],
    )
    repo.create_schema5(manifest, _planned(), base)
    original = repo._replace_manifest

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise Phase7ERepositoryError

    monkeypatch.setattr(repo, "_replace_manifest", fail)
    with pytest.raises(Phase7ERepositoryError):
        repo.admit_schema5(
            "inv-01",
            "run-01",
            manifest,
            _state5(Schema5PhaseState.ACQUIRING, operation.identity),
            [*base, operation],
        )
    monkeypatch.setattr(repo, "_replace_manifest", original)
    reopened = repo.reopen_schema5("inv-01", "run-01")
    assert reopened.state.phase_state is Schema5PhaseState.PLANNED
    assert not (reopened.root / "operations" / f"{operation.identity}.json").exists()
    assert not tuple((tmp_path / ".staging").iterdir())


def test_recovery_removes_owned_journaled_unindexed_file(tmp_path: Path) -> None:
    schema5, _, raw_records = _fixture()
    vectors = {vector["family"]: vector for vector in _vectors()}
    repo = RecordingSearch7ERepository(tmp_path)
    manifest = StrictIdentityEnvelope(
        family=schema5["family"], identity=schema5["expected_id"], payload=schema5["payload"]
    )
    base = _envelopes(raw_records)
    operation = StrictIdentityEnvelope(
        family=vectors["replay-operation"]["family"],
        identity=vectors["replay-operation"]["expected_id"],
        payload=vectors["replay-operation"]["payload"],
    )
    repo.create_schema5(manifest, _planned(), base)
    repo.admit_schema5(
        "inv-01",
        "run-01",
        manifest,
        _state5(Schema5PhaseState.ACQUIRING, operation.identity),
        [*base, operation],
    )
    staging = tmp_path / ".staging" / "crashed-publication"
    staging.mkdir(parents=True)
    (staging / "target.json").write_text(
        json.dumps({"investigation_id": "inv-01", "run_id": "run-01"}), encoding="utf-8"
    )
    relative = "operations/rr-replay-operation-v1-" + "1" * 64 + ".json"
    owned = repo.run_path("inv-01", "run-01") / relative
    owned.parent.mkdir(parents=True, exist_ok=True)
    owned.write_bytes(b"owned-unindexed")
    (staging / "publication.json").write_text(
        json.dumps(
            {
                "investigation_id": "inv-01",
                "run_id": "run-01",
                "manifest_id": "rr-schema5-manifest-v1-" + "0" * 64,
                "paths": [relative],
            }
        ),
        encoding="utf-8",
    )
    recovered = repo.recover_active("inv-01", "run-01")
    assert recovered.state.phase_state is Schema5PhaseState.INTERRUPTED
    assert not owned.exists()
    assert not staging.exists()
