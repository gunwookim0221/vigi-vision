# pyright: reportAny=false, reportExplicitAny=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportPrivateUsage=false, reportUnusedCallResult=false
"""Focused Phase 7E-1D orchestration and Schema-7 persistence tests."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from vigi_vision.recording_models import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.recording_search_7e_1c import (
    CommonSessionAcquisition,
    CommonSessionCancelledError,
    CommonSessionDecoderError,
    CommonSessionPolicy,
    CommonSessionRequest,
    DecodedLocalFrame,
    InvocationBudget,
    MediaProbeFacts,
    Phase7EB4Input,
    Phase7EInvocation,
    execute_local_targets,
)
from vigi_vision.recording_search_7e_1d import (
    Phase7E1DService,
    Phase7EC1PlannerAdapter,
    Phase7EIncompleteEvidenceError,
    Phase7ELocalEvidenceAdapter,
    Phase7ETerminalDecision,
    Phase7ETerminalReason,
    build_evidence_snapshot,
    build_schema7_manifest,
    build_source_record_set,
    build_terminal_result,
    maximum_phase7e_narrowing_iterations,
    read_phase7_status,
)
from vigi_vision.recording_search_7e_models import (
    Schema5PhaseState,
    Schema6TargetState,
    StrictIdentityEnvelope,
)
from vigi_vision.recording_search_7e_repository import (
    Phase7EConflictError,
    Phase7ECorruptError,
    Phase7ERun,
    PublicationStatus,
    RecordingSearch7ERepository,
)
from vigi_vision.recording_search_7e_validation import Schema5Envelope, Schema6Envelope
from vigi_vision.recording_search_d2_terminal_models import TerminalResultKind
from vigi_vision.replay import ReplayClip

_DOC = Path(__file__).parents[1] / "docs" / "design" / "object-disappearance-recording-search.md"


def _json_blocks() -> list[object]:
    text = _DOC.read_text(encoding="utf-8")
    result: list[object] = []
    for match in re.finditer(r"```json", text):
        end = text.find("```", match.end())
        if end < 0:
            continue
        try:
            result.append(json.loads(text[match.end() : end]))
        except json.JSONDecodeError:
            continue
    return result


def _vectors() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in _json_blocks():
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, dict) for item in value)
            and all({"family", "expected_id", "payload"} <= set(item) for item in value)
        ):
            result.extend(value)
    return result[:49]


def _binary_fixture() -> dict[str, Any]:
    return next(
        value for value in _json_blocks() if isinstance(value, dict) and "jpeg_files" in value
    )


def _envelope(vector: dict[str, Any]) -> StrictIdentityEnvelope:
    return StrictIdentityEnvelope(
        family=vector["family"],
        identity=vector["expected_id"],
        payload=vector["payload"],
    )


def _state5(state: Schema5PhaseState, operation_id: str | None = None) -> Schema5Envelope:
    return Schema5Envelope(
        run_state="RUNNING",
        phase_state=state,
        active_replay_operation_id=operation_id,
        reason_code=None,
        attempt_count=0 if state is Schema5PhaseState.PLANNED else 1,
    )


def _state6(  # noqa: PLR0913 - mirrors the strict lifecycle envelope.
    state: Schema6TargetState,
    target_id: str,
    *,
    decoder_id: str | None = None,
    frame_id: str | None = None,
    attempt_id: str | None = None,
    operation_id: str | None = None,
    observation_id: str | None = None,
    predecessor: Schema6TargetState | None = None,
) -> Schema6Envelope:
    return Schema6Envelope(
        run_state="RUNNING",
        target_state=state,
        active_target_request_id=target_id,
        active_decoder_operation_id=decoder_id,
        active_frame_id=frame_id,
        active_classification_attempt_id=attempt_id,
        active_classification_operation_id=operation_id,
        active_observation_id=observation_id,
        reason_code=None,
        attempt_count=0 if state is Schema6TargetState.REQUESTED else 1,
        predecessor_target_state=predecessor,
    )


def _manifest_with_indexes(
    final: StrictIdentityEnvelope, **updates: list[str]
) -> StrictIdentityEnvelope:
    payload = dict(final.payload)
    indexes = {key: [] for key in payload["indexes"]}
    indexes.update(updates)
    payload["indexes"] = indexes
    return StrictIdentityEnvelope.from_payload("schema6-manifest", payload)


def _create_golden_schema6(
    tmp_path: Path, *, complete: bool = True, active_index: int = 1
) -> tuple[RecordingSearch7ERepository, Phase7ERun]:
    vectors = _vectors()
    by_id = {item["expected_id"]: item for item in vectors}
    by_family = {item["family"]: item for item in vectors}
    manifest5 = _envelope(by_family["schema5-manifest"])
    policy = _envelope(by_family["policy"])
    plan = _envelope(by_family["coarse-plan"])
    coarse = [_envelope(by_id[item]) for item in manifest5.payload["coarse_target_request_ids"]]
    operation = _envelope(by_family["replay-operation"])
    base = [policy, plan, *coarse]
    repo = RecordingSearch7ERepository(tmp_path)
    repo.create_schema5(manifest5, _state5(Schema5PhaseState.PLANNED), base)
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
    final_manifest = _envelope(by_family["schema6-manifest"])
    final_indexes = final_manifest.payload["indexes"]
    targets = [_envelope(by_id[item]) for item in final_indexes["target_request_ids"]]
    classifier_policy = _envelope(by_family["classifier-policy"])
    session = _envelope(by_family["common-session"])
    common = [policy, plan, operation, classifier_policy, session, *targets]
    active_target = final_indexes["target_request_ids"][active_index]
    requested_manifest = _manifest_with_indexes(
        final_manifest, target_request_ids=list(final_indexes["target_request_ids"])
    )
    requested = _state6(Schema6TargetState.REQUESTED, active_target)
    initial = repo.transition_schema5_to_schema6(
        "inv-01", "run-01", requested_manifest, requested, common
    )
    if not complete:
        return repo, initial.run
    decoder = _envelope(by_id[final_indexes["decoder_operation_ids"][0]])
    decoding_manifest = _manifest_with_indexes(
        final_manifest,
        target_request_ids=list(final_indexes["target_request_ids"]),
        decoder_operation_ids=[decoder.identity],
    )
    decoding = _state6(
        Schema6TargetState.DECODING,
        active_target,
        decoder_id=decoder.identity,
        predecessor=Schema6TargetState.REQUESTED,
    )
    repo.admit_schema6(
        "inv-01",
        "run-01",
        decoding_manifest,
        decoding,
        [*common, decoder],
        expected_manifest_id=requested_manifest.identity,
    )
    frames = [_envelope(by_id[item]) for item in final_indexes["frame_ids"]]
    raw_by_digest = {
        item["sha256"]: base64.b64decode(item["base64"]) for item in _binary_fixture()["jpeg_files"]
    }
    binaries = {frame.identity: raw_by_digest[frame.payload["jpeg_sha256"]] for frame in frames}
    ready_manifest = _manifest_with_indexes(
        final_manifest,
        target_request_ids=list(final_indexes["target_request_ids"]),
        decoder_operation_ids=[decoder.identity],
        frame_ids=list(final_indexes["frame_ids"]),
    )
    active_frame = final_indexes["frame_ids"][-1]
    ready = _state6(
        Schema6TargetState.FRAME_READY,
        active_target,
        decoder_id=decoder.identity,
        frame_id=active_frame,
        predecessor=Schema6TargetState.DECODING,
    )
    repo.admit_schema6(
        "inv-01",
        "run-01",
        ready_manifest,
        ready,
        [*common, decoder, *frames],
        expected_manifest_id=decoding_manifest.identity,
        binary_records=binaries,
    )
    classifying = _state6(
        Schema6TargetState.CLASSIFYING,
        active_target,
        decoder_id=decoder.identity,
        frame_id=active_frame,
        attempt_id="attempt-1",
        predecessor=Schema6TargetState.FRAME_READY,
    )
    repo.admit_schema6(
        "inv-01",
        "run-01",
        ready_manifest,
        classifying,
        [*common, decoder, *frames],
        expected_manifest_id=ready_manifest.identity,
    )
    indexed_families = {
        "classification_operation_ids": "classification-operation",
        "observation_ids": "observation",
        "alias_ids": "alias",
        "support_group_ids": "support-group",
        "c2_bracket_ids": "c2-bracket",
        "d1_input_ids": "d1-input",
        "d1_history_ids": "d1-history",
        "narrowed_bracket_ids": "narrowed-bracket",
    }
    terminal_records: list[StrictIdentityEnvelope] = []
    for key, family in indexed_families.items():
        terminal_records.extend(
            _envelope(by_id[identity])
            for identity in final_indexes[key]
            if by_id[identity]["family"] == family
        )
    active_operation = final_indexes["classification_operation_ids"][-1]
    active_observation = final_indexes["observation_ids"][-1]
    observed = _state6(
        Schema6TargetState.OBSERVED,
        active_target,
        decoder_id=decoder.identity,
        frame_id=active_frame,
        operation_id=active_operation,
        observation_id=active_observation,
        predecessor=Schema6TargetState.CLASSIFYING,
    )
    result = repo.admit_schema6(
        "inv-01",
        "run-01",
        final_manifest,
        observed,
        [*common, decoder, *frames, *terminal_records],
        expected_manifest_id=ready_manifest.identity,
    )
    return repo, result.run


def _found_decision(run: Phase7ERun) -> Phase7ETerminalDecision:
    narrowed_id = run.manifest.payload["indexes"]["narrowed_bracket_ids"][0]
    narrowed = next(item for item in run.records if item.identity == narrowed_id)
    return Phase7ETerminalDecision(
        TerminalResultKind.FOUND,
        Phase7ETerminalReason.SUPPORTED_TRANSITION,
        (
            narrowed.payload["lower_observation_id"],
            narrowed.payload["upper_observation_id"],
        ),
        (narrowed.payload["upper_support_group_id"],),
        narrowed.identity,
        narrowed.payload["interval_start_requested_time_utc"],
        narrowed.payload["interval_end_requested_time_utc"],
    )


def _terminal_records(run: Phase7ERun) -> tuple[StrictIdentityEnvelope, ...]:
    decision = _found_decision(run)
    source = build_source_record_set(run)
    snapshot = build_evidence_snapshot(run, source, decision)
    result = build_terminal_result(run, source, snapshot, decision)
    manifest = build_schema7_manifest(run, source, snapshot, result)
    return manifest, source, snapshot, result


def test_phase7e_c1_adapter_includes_s_logical_e_and_backward_support() -> None:
    policy = _envelope(next(item for item in _vectors() if item["family"] == "policy"))
    request = CommonSessionRequest(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        _utc("2026-07-20T03:00:04Z"),
        CommonSessionPolicy.from_payload(policy.payload),
    )
    bundle = Phase7EC1PlannerAdapter().build(request, policy)
    assert bundle.plan.payload["target_requested_times_utc"] == [
        "2026-07-20T03:00:00Z",
        "2026-07-20T03:00:04Z",
    ]
    assert [item.payload["requested_time_utc"] for item in bundle.final_support_targets] == [
        "2026-07-20T03:00:01Z",
        "2026-07-20T03:00:02Z",
        "2026-07-20T03:00:03Z",
    ]
    assert bundle.coarse_targets[-1].payload["selection_rule"] == "FINAL_STRICTLY_BEFORE_END"
    assert all(
        item.payload["origin_target_request_id"] == bundle.coarse_targets[-1].identity
        for item in bundle.final_support_targets
    )


def test_phase7e_c1_adapter_refuses_to_clamp_short_final_support() -> None:
    policy = _envelope(next(item for item in _vectors() if item["family"] == "policy"))
    request = CommonSessionRequest(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        _utc("2026-07-20T03:00:02Z"),
        CommonSessionPolicy.from_payload(policy.payload),
    )
    with pytest.raises(Phase7EIncompleteEvidenceError):
        Phase7EC1PlannerAdapter().build(request, policy)


def test_d1_iteration_ceiling_reuses_existing_policy() -> None:
    policy = _envelope(next(item for item in _vectors() if item["family"] == "policy"))
    assert maximum_phase7e_narrowing_iterations(300, policy) == 9
    assert maximum_phase7e_narrowing_iterations(1, policy) == 0


def test_schema6_to_schema7_atomic_publish_strict_reopen_and_duplicate_reuse(
    tmp_path: Path,
) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    manifest, source, snapshot, result = _terminal_records(run)
    first = repo.publish_schema7("inv-01", "run-01", manifest, source, snapshot, result)
    assert first.status is PublicationStatus.CREATED
    assert first.run.is_schema7
    assert first.run.result_kind == "FOUND"
    assert (first.run.root / "terminal" / "result.json").is_file()
    assert (first.run.root / "manifests" / f"{run.manifest_id}.json").is_file()
    before = (first.run.root / "manifest.json").read_bytes()
    second = repo.publish_schema7("inv-01", "run-01", manifest, source, snapshot, result)
    assert second.status is PublicationStatus.REUSED
    assert (first.run.root / "manifest.json").read_bytes() == before
    reopened = repo.reopen_schema7("inv-01", "run-01")
    assert reopened.manifest_id == manifest.identity
    assert reopened.result_kind == "FOUND"


def test_schema7_builders_reproduce_the_approved_golden_identities(tmp_path: Path) -> None:
    _repo, run = _create_golden_schema6(tmp_path)
    terminal_records = _terminal_records(run)
    expected = {item["family"]: item["expected_id"] for item in _vectors()}
    assert {item.family: item.identity for item in terminal_records} == {
        item.family: expected[item.family] for item in terminal_records
    }


def test_phase7e_1d_service_uses_owned_cumulative_budget_and_strict_readback(
    tmp_path: Path,
) -> None:
    repo, _run = _create_golden_schema6(tmp_path)
    policy = _envelope(next(item for item in _vectors() if item["family"] == "policy"))
    request = CommonSessionRequest(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        _utc("2026-07-20T03:00:04Z"),
        CommonSessionPolicy.from_payload(policy.payload),
    )
    clock = lambda: 0.0  # noqa: E731 - deterministic immutable test clock.
    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner:
        invocation = Phase7EInvocation(request, owner, InvocationBudget(request.policy, clock))
        result = Phase7E1DService(repo).execute(invocation)
        assert result.status is PublicationStatus.CREATED
        assert (
            result.run.manifest_id
            == repo.reopen_schema7("inv-01", "run-01", ownership=owner).manifest_id
        )
        reused = Phase7E1DService(repo).execute(invocation)
        assert reused.status is PublicationStatus.REUSED
        assert reused.run.manifest_id == result.run.manifest_id


def test_phase7e_1d_cancellation_never_becomes_visual_terminal(tmp_path: Path) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    policy = _envelope(next(item for item in _vectors() if item["family"] == "policy"))
    request = CommonSessionRequest(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        _utc("2026-07-20T03:00:04Z"),
        CommonSessionPolicy.from_payload(policy.payload),
    )
    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner:
        invocation = Phase7EInvocation(
            request,
            owner,
            InvocationBudget(request.policy, lambda: 0.0, cancellation=lambda: True),
        )
        with pytest.raises(CommonSessionCancelledError):
            Phase7E1DService(repo).execute(invocation)
    assert repo.reopen_schema6("inv-01", "run-01").manifest_id == run.manifest_id


def test_schema7_conflicting_duplicate_preserves_winner(tmp_path: Path) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    manifest, source, snapshot, result = _terminal_records(run)
    published = repo.publish_schema7("inv-01", "run-01", manifest, source, snapshot, result)
    changed_payload = dict(result.payload)
    changed_payload["reason_code"] = "VISUAL_INDETERMINATE"
    changed_payload["result_kind"] = "INCONCLUSIVE"
    changed_payload["interval_start_requested_time_utc"] = None
    changed_payload["interval_end_requested_time_utc"] = None
    changed = StrictIdentityEnvelope.from_payload("terminal-result", changed_payload)
    conflicting_manifest = StrictIdentityEnvelope.from_payload(
        "schema7-manifest",
        {**manifest.payload, "terminal_result_id": changed.identity},
    )
    with pytest.raises(Phase7EConflictError):
        repo.publish_schema7("inv-01", "run-01", conflicting_manifest, source, snapshot, changed)
    assert repo.reopen_schema7("inv-01", "run-01").manifest_id == published.run.manifest_id


def _add_unknown_key(value: dict[str, Any]) -> dict[str, Any]:
    return {**value, "unknown": True}


def _replace_identity(value: dict[str, Any]) -> dict[str, Any]:
    return {**value, "identity": "bad"}


def _replace_schema_version(value: dict[str, Any]) -> dict[str, Any]:
    return {**value, "schema_version": 5}


@pytest.mark.parametrize(
    ("relative_path", "mutation"),
    [
        ("terminal/result.json", _add_unknown_key),
        ("terminal/evidence-snapshot.json", _replace_identity),
        ("manifests/schema6", _replace_schema_version),
    ],
)
def test_schema7_strict_reopen_rejects_mutation_without_repair(
    tmp_path: Path,
    relative_path: str,
    mutation: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    manifest, source, snapshot, result = _terminal_records(run)
    published = repo.publish_schema7("inv-01", "run-01", manifest, source, snapshot, result).run
    path = (
        published.root / "manifests" / f"{run.manifest_id}.json"
        if relative_path == "manifests/schema6"
        else published.root / Path(relative_path)
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    changed = mutation(value)
    path.write_text(json.dumps(changed), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(Phase7ECorruptError):
        repo.reopen_schema7("inv-01", "run-01")
    assert path.read_bytes() == before


def test_schema7_strict_reopen_rejects_alias_only_support(tmp_path: Path) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    manifest, source, snapshot, result = _terminal_records(run)
    published = repo.publish_schema7("inv-01", "run-01", manifest, source, snapshot, result).run
    support_id = snapshot.payload["selected_support_group_ids"][0]
    support = next(item for item in run.records if item.identity == support_id)
    path = published.root / "support-groups" / f"{support_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["payload"]["member_frame_ids"] = [support.payload["member_frame_ids"][0]] * 3
    path.write_text(json.dumps(value), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(Phase7ECorruptError):
        repo.reopen_schema7("inv-01", "run-01")
    assert path.read_bytes() == before


def test_schema7_status_is_safe_and_terminal(tmp_path: Path) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    manifest, source, snapshot, result = _terminal_records(run)
    repo.publish_schema7("inv-01", "run-01", manifest, source, snapshot, result)
    status = read_phase7_status(repo, "inv-01", "run-01")
    assert status.status == "FOUND"
    assert status.reason_code == "SUPPORTED_TRANSITION"
    assert status.terminal_result_id == result.identity


class _OneFrameDecoder:
    def __init__(self) -> None:
        self.calls: int = 0

    def decode(
        self,
        session: CommonSessionAcquisition,
        targets: tuple[datetime, ...],
        timeout_seconds: float,
    ) -> tuple[DecodedLocalFrame, ...]:
        assert timeout_seconds > 0
        self.calls += 1
        return tuple(
            DecodedLocalFrame(
                target,
                0,
                0,
                4,
                4,
                bytes(4 * 4 * 3),
                decode_session_id=session.common_session_id,
            )
            for target in targets
        )


class _VisualClassifier:
    def __init__(self) -> None:
        self.calls: int = 0

    def classify(self, authoritative: Phase7EB4Input) -> object:
        self.calls += 1
        template = next(item for item in _vectors() if item["family"] == "classification-operation")
        payload = {
            **template["payload"],
            "investigation_id": authoritative.run.investigation_id,
            "run_id": authoritative.run.run_id,
            "frame_id": authoritative.frame_record.identity,
            "target_request_id": authoritative.target_request.identity,
            "classifier_policy_id": authoritative.run.manifest.payload["classifier_policy_id"],
        }
        return StrictIdentityEnvelope.from_payload("classification-operation", payload)


def _test_acquisition(
    tmp_path: Path,
    run: Phase7ERun,
    request: CommonSessionRequest,
) -> CommonSessionAcquisition:
    session = next(item for item in run.records if item.family == "common-session")
    window = RecordingWindow(1, request.start_utc, request.end_utc)
    segment = RecordingSegment(
        1,
        request.start_utc.date(),
        int(request.start_utc.timestamp()),
        int(request.end_utc.timestamp()),
        request.start_utc,
        request.end_utc,
    )
    replay_path = tmp_path / "replay.mp4"
    replay_path.write_bytes(b"retained")
    return CommonSessionAcquisition(
        request,
        segment,
        ReplayRequest(window, "rtsp://redacted"),
        ReplayClip(1, request.start_utc, request.end_utc, "rtsp://redacted", replay_path, 4),
        MediaProbeFacts(0, 1, 0, 0, 1, 1, 4, width=4, height=4, average_frame_rate_num=1),
        session,
        replay_path,
    )


def test_local_evidence_adapter_reuses_classification_for_repeated_frame_alias(
    tmp_path: Path,
) -> None:
    repo, run = _create_golden_schema6(tmp_path / "runs", complete=False)
    policy_record = next(item for item in run.records if item.family == "policy")
    request = CommonSessionRequest(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        _utc("2026-07-20T03:00:04Z"),
        CommonSessionPolicy.from_payload(policy_record.payload),
    )
    assert isinstance(run.state, Schema6Envelope)
    active_target = next(
        item
        for item in run.records
        if item.family == "target-request" and item.identity == run.state.active_target_request_id
    )
    alias_target = next(
        item
        for item in run.records
        if item.family == "target-request" and item.identity != active_target.identity
    )
    acquisition = _test_acquisition(tmp_path, run, request)
    decoder = _OneFrameDecoder()
    classifier = _VisualClassifier()
    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner:
        invocation = Phase7EInvocation(
            request,
            owner,
            InvocationBudget(request.policy, lambda: 0.0),
        )
        adapter = Phase7ELocalEvidenceAdapter(repo, decoder, classifier)
        adapter.execute(
            invocation,
            acquisition,
            (active_target,),
        )
        result = adapter.execute(invocation, acquisition, (alias_target,))
    assert decoder.calls == 2
    assert classifier.calls == 1
    assert len(result.manifest.payload["indexes"]["decoder_operation_ids"]) == 2
    assert len(result.manifest.payload["indexes"]["frame_ids"]) == 1
    assert len(result.manifest.payload["indexes"]["observation_ids"]) == 1
    assert len(result.manifest.payload["indexes"]["alias_ids"]) == 1
    alias = next(item for item in result.records if item.family == "alias")
    assert alias.payload["target_request_id"] == alias_target.identity
    assert alias.payload["alias_of_target_request_id"] == active_target.identity


def test_local_evidence_adapter_admits_same_pass_physical_alias_once(tmp_path: Path) -> None:
    repo, run = _create_golden_schema6(tmp_path / "runs", complete=False, active_index=0)
    policy_record = next(item for item in run.records if item.family == "policy")
    request = CommonSessionRequest(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        _utc("2026-07-20T03:00:04Z"),
        CommonSessionPolicy.from_payload(policy_record.payload),
    )
    assert isinstance(run.state, Schema6Envelope)
    active_target = next(
        item
        for item in run.records
        if item.family == "target-request" and item.identity == run.state.active_target_request_id
    )
    end_target = next(
        item
        for item in run.records
        if item.family == "target-request"
        and item.payload["selection_rule"] == "FINAL_STRICTLY_BEFORE_END"
    )
    acquisition = _test_acquisition(tmp_path, run, request)
    decoder = _OneFrameDecoder()
    classifier = _VisualClassifier()
    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner:
        invocation = Phase7EInvocation(
            request,
            owner,
            InvocationBudget(request.policy, lambda: 0.0),
        )
        result = Phase7ELocalEvidenceAdapter(repo, decoder, classifier).execute(
            invocation,
            acquisition,
            (active_target, end_target),
        )
    assert decoder.calls == 1
    assert classifier.calls == 1
    assert len(result.manifest.payload["indexes"]["frame_ids"]) == 1
    assert len(result.manifest.payload["indexes"]["observation_ids"]) == 1
    assert len(result.manifest.payload["indexes"]["alias_ids"]) == 1


class _MismatchedAliasDecoder:
    def decode(
        self,
        session: CommonSessionAcquisition,
        targets: tuple[datetime, ...],
        timeout_seconds: float,
    ) -> tuple[DecodedLocalFrame, ...]:
        assert len(targets) == 2
        assert timeout_seconds > 0
        return (
            DecodedLocalFrame(
                targets[0],
                0,
                0,
                4,
                4,
                bytes(4 * 4 * 3),
                decode_session_id=session.common_session_id,
            ),
            DecodedLocalFrame(
                targets[1],
                0,
                0,
                4,
                4,
                bytes([1]) * (4 * 4 * 3),
                decode_session_id=session.common_session_id,
            ),
        )


def test_local_decode_rejects_same_position_with_different_rgb24(tmp_path: Path) -> None:
    _repo, run = _create_golden_schema6(tmp_path / "runs", complete=False, active_index=0)
    policy_record = next(item for item in run.records if item.family == "policy")
    request = CommonSessionRequest(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        _utc("2026-07-20T03:00:04Z"),
        CommonSessionPolicy.from_payload(policy_record.payload),
    )
    acquisition = _test_acquisition(tmp_path, run, request)
    with pytest.raises(CommonSessionDecoderError):
        execute_local_targets(
            acquisition,
            _MismatchedAliasDecoder(),
            (request.start_utc, request.end_utc),
            logical_end=True,
            allow_aliases=True,
            budget=InvocationBudget(request.policy, lambda: 0.0),
        )


def test_status_and_recovery_dispatch_nonterminal_and_terminal_runs(tmp_path: Path) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    running = read_phase7_status(repo, "inv-01", "run-01")
    assert running.schema_version == 6
    assert running.status == "RUNNING"
    assert read_phase7_status(repo, "missing", "run").status == "UNAVAILABLE"
    manifest, source, snapshot, result = _terminal_records(run)
    terminal = repo.publish_schema7("inv-01", "run-01", manifest, source, snapshot, result).run
    recovered = repo.recover_active("inv-01", "run-01")
    assert recovered.manifest_id == terminal.manifest_id
    assert recovered.result_kind == "FOUND"


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00").astimezone(timezone.utc)
