# pyright: reportAny=false, reportExplicitAny=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportPrivateUsage=false, reportUnusedCallResult=false
"""Focused Phase 7E-1D orchestration and Schema-7 persistence tests."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from vigi_vision.recording_models import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.recording_search_7e_1c import (
    CommonSessionAcquirer,
    CommonSessionAcquisition,
    CommonSessionCancelledError,
    CommonSessionDeadlineError,
    CommonSessionDecoderError,
    CommonSessionDecoderTimeoutError,
    CommonSessionPolicy,
    CommonSessionRequest,
    DecodedLocalFrame,
    InvocationBudget,
    MediaProbeFacts,
    Phase7E1CExecutor,
    Phase7EB4Input,
    Phase7EInvocation,
    admit_decoder_operation,
    admit_frame_then_classify,
    execute_local_targets,
    make_decoder_envelope,
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
    Phase7EInProgressError,
    Phase7ERun,
    PublicationStatus,
    RecordingSearch7ERepository,
)
from vigi_vision.recording_search_7e_validation import Schema5Envelope, Schema6Envelope
from vigi_vision.recording_search_d1_models import NarrowingResult
from vigi_vision.recording_search_d1_service import BinaryNarrowingService
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


def test_live_status_is_running_without_reacquiring_execution_authority(tmp_path: Path) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    manifest_before = (run.root / "manifest.json").read_bytes()
    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0):
        status = read_phase7_status(repo, "inv-01", "run-01")
    assert status.schema_version == 6
    assert status.status == "RUNNING"
    assert (run.root / "manifest.json").read_bytes() == manifest_before


def test_status_during_atomic_publication_is_never_false_terminal(
    tmp_path: Path,
) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    manifest, source, snapshot, result = _terminal_records(run)
    observed: list[str] = []

    def publication_check() -> None:
        observed.append(read_phase7_status(repo, "inv-01", "run-01").status)

    published = repo.publish_schema7(
        "inv-01",
        "run-01",
        manifest,
        source,
        snapshot,
        result,
        publication_check=publication_check,
    )
    assert observed
    assert set(observed) <= {"RUNNING"}
    assert published.run.is_schema7
    assert read_phase7_status(repo, "inv-01", "run-01").status == "FOUND"


class _MutableClock:
    def __init__(self) -> None:
        self.value: float = 0.0

    def __call__(self) -> float:
        return self.value


class _AdvanceBeforePublication:
    def __init__(self, clock: _MutableClock, value: float) -> None:
        self.clock: _MutableClock = clock
        self.value: float = value

    def interpret(self, run: Phase7ERun) -> Phase7ETerminalDecision:
        self.clock.value = self.value
        return _found_decision(run)


def test_terminal_interpretation_does_not_start_after_deadline(tmp_path: Path) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    policy_record = next(item for item in run.records if item.family == "policy")
    request = CommonSessionRequest(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        _utc("2026-07-20T03:00:04Z"),
        CommonSessionPolicy.from_payload(policy_record.payload),
    )
    clock = _MutableClock()
    budget = InvocationBudget(request.policy, clock)
    clock.value = budget.deadline
    with (
        repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner,
        pytest.raises(CommonSessionDeadlineError),
    ):
        Phase7E1DService(repo).execute(Phase7EInvocation(request, owner, budget))
    assert repo.reopen_schema6("inv-01", "run-01").manifest_id == run.manifest_id


def test_publication_requires_reserved_strict_readback_budget(tmp_path: Path) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    policy_record = next(item for item in run.records if item.family == "policy")
    request = CommonSessionRequest(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        _utc("2026-07-20T03:00:04Z"),
        CommonSessionPolicy.from_payload(policy_record.payload),
    )
    clock = _MutableClock()
    budget = InvocationBudget(request.policy, clock)
    remaining_without_readback = (
        budget.deadline
        - request.policy.cleanup_reserve_seconds
        - request.policy.strict_readback_seconds
    )
    boundary = _AdvanceBeforePublication(clock, remaining_without_readback)
    with (
        repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner,
        pytest.raises(CommonSessionDeadlineError),
    ):
        Phase7E1DService(repo, decision_boundary=boundary).execute(
            Phase7EInvocation(request, owner, budget)
        )
    assert repo.reopen_schema6("inv-01", "run-01").manifest_id == run.manifest_id


def test_readback_timeout_preserves_committed_schema7_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    policy_record = next(item for item in run.records if item.family == "policy")
    request = CommonSessionRequest(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        _utc("2026-07-20T03:00:04Z"),
        CommonSessionPolicy.from_payload(policy_record.payload),
    )
    clock = _MutableClock()
    budget = InvocationBudget(request.policy, clock)
    original = repo.publish_schema7

    def publish_with_expired_readback(
        *args: Any,  # noqa: ANN401 - transparent repository hook.
        **kwargs: Any,  # noqa: ANN401 - transparent repository hook.
    ) -> object:
        readback_check = kwargs["readback_check"]

        def expire_then_check() -> None:
            clock.value = budget.deadline
            assert callable(readback_check)
            readback_check()

        kwargs["readback_check"] = expire_then_check
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, "publish_schema7", publish_with_expired_readback)
    with (
        repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner,
        pytest.raises(CommonSessionDeadlineError),
    ):
        Phase7E1DService(repo).execute(Phase7EInvocation(request, owner, budget))
    committed = repo.reopen_schema7("inv-01", "run-01")
    assert committed.is_schema7
    assert committed.result_kind == "FOUND"


def test_publication_timeout_does_not_commit_schema7(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    policy_record = next(item for item in run.records if item.family == "policy")
    request = CommonSessionRequest(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        _utc("2026-07-20T03:00:04Z"),
        CommonSessionPolicy.from_payload(policy_record.payload),
    )
    clock = _MutableClock()
    budget = InvocationBudget(request.policy, clock)
    original = repo.publish_schema7

    def publish_with_timeout(
        *args: Any,  # noqa: ANN401 - transparent repository hook.
        **kwargs: Any,  # noqa: ANN401 - transparent repository hook.
    ) -> object:
        publication_check = kwargs["publication_check"]
        checks = 0

        def expire_during_publication() -> None:
            nonlocal checks
            checks += 1
            if checks == 2:
                clock.value = request.policy.publication_seconds
            assert callable(publication_check)
            publication_check()

        kwargs["publication_check"] = expire_during_publication
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, "publish_schema7", publish_with_timeout)
    with (
        repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner,
        pytest.raises(CommonSessionDeadlineError),
    ):
        Phase7E1DService(repo).execute(Phase7EInvocation(request, owner, budget))
    assert repo.reopen_schema6("inv-01", "run-01").manifest_id == run.manifest_id


def test_cancellation_after_commit_preserves_schema7_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, run = _create_golden_schema6(tmp_path)
    policy_record = next(item for item in run.records if item.family == "policy")
    request = CommonSessionRequest(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        _utc("2026-07-20T03:00:04Z"),
        CommonSessionPolicy.from_payload(policy_record.payload),
    )
    cancelled = False
    budget = InvocationBudget(request.policy, lambda: 0.0, cancellation=lambda: cancelled)
    original = repo.publish_schema7

    def publish_with_cancellation(
        *args: Any,  # noqa: ANN401 - transparent repository hook.
        **kwargs: Any,  # noqa: ANN401 - transparent repository hook.
    ) -> object:
        readback_check = kwargs["readback_check"]

        def cancel_then_check() -> None:
            nonlocal cancelled
            cancelled = True
            assert callable(readback_check)
            readback_check()

        kwargs["readback_check"] = cancel_then_check
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, "publish_schema7", publish_with_cancellation)
    with (
        repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner,
        pytest.raises(CommonSessionCancelledError),
    ):
        Phase7E1DService(repo).execute(Phase7EInvocation(request, owner, budget))
    assert repo.reopen_schema7("inv-01", "run-01").result_kind == "FOUND"


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


class _TimelineDecoder:
    def __init__(self) -> None:
        self.session_ids: list[str] = []

    def decode(
        self,
        session: CommonSessionAcquisition,
        targets: tuple[datetime, ...],
        timeout_seconds: float,
    ) -> tuple[DecodedLocalFrame, ...]:
        assert timeout_seconds > 0
        self.session_ids.append(session.common_session_id)
        result: list[DecodedLocalFrame] = []
        for target in targets:
            offset = int((target - session.request.start_utc).total_seconds())
            selected = min(offset, int(session.request.duration_seconds) - 1)
            result.append(
                DecodedLocalFrame(
                    target,
                    selected,
                    selected,
                    4,
                    4,
                    bytes([selected]) * (4 * 4 * 3),
                    decode_session_id=session.common_session_id,
                )
            )
        return tuple(result)


class _TimelineClassifier:
    def __init__(self, present_times: set[str] | None = None) -> None:
        self.present_times: set[str] = present_times or {"2026-07-20T03:00:00Z"}

    def classify(self, authoritative: Phase7EB4Input) -> object:
        assert authoritative.frame_record in authoritative.run.records
        assert (
            authoritative.run.frame_bytes[authoritative.frame_record.identity]
            == authoritative.frame_jpeg_bytes
        )
        outcome = (
            "PRESENT"
            if authoritative.target_request.payload["requested_time_utc"] in self.present_times
            else "ABSENT"
        )
        template = next(
            item
            for item in _vectors()
            if item["family"] == "classification-operation"
            and item["payload"]["outcome"] == outcome
        )
        return StrictIdentityEnvelope.from_payload(
            "classification-operation",
            {
                **template["payload"],
                "investigation_id": authoritative.run.investigation_id,
                "run_id": authoritative.run.run_id,
                "frame_id": authoritative.frame_record.identity,
                "target_request_id": authoritative.target_request.identity,
                "classifier_policy_id": authoritative.run.manifest.payload["classifier_policy_id"],
            },
        )


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


def test_1d_runs_real_c1_c2_d1_d2_path_from_retained_common_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    acquisition = _test_acquisition(tmp_path, run, request)
    decoder = _TimelineDecoder()
    adapter = Phase7ELocalEvidenceAdapter(
        repo,
        decoder,
        _TimelineClassifier(),
    )
    first_target = Phase7EC1PlannerAdapter().build(request, policy_record).coarse_targets[0]
    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner:
        intermediate = adapter.execute(
            Phase7EInvocation(
                request,
                owner,
                InvocationBudget(request.policy, lambda: 0.0),
            ),
            acquisition,
            (first_target,),
        )
    first_observation = next(
        item
        for item in intermediate.records
        if item.family == "observation"
        and item.payload["target_request_id"] == first_target.identity
    )
    narrowing_calls = 0
    original_narrow = BinaryNarrowingService.narrow

    def track_narrow(
        *args: Any,  # noqa: ANN401 - transparent service spy.
        **kwargs: Any,  # noqa: ANN401 - transparent service spy.
    ) -> object:
        nonlocal narrowing_calls
        narrowing_calls += 1
        return original_narrow(*args, **kwargs)

    monkeypatch.setattr(BinaryNarrowingService, "narrow", track_narrow)
    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner:
        result = Phase7E1DService(repo, local_evidence=adapter).execute(
            Phase7EInvocation(
                request,
                owner,
                InvocationBudget(request.policy, lambda: 0.0),
            ),
            acquisition,
        )
    assert result.run.is_schema7
    assert result.run.result_kind == "FOUND"
    reopened = repo.reopen_schema7("inv-01", "run-01")
    assert reopened.result_kind == "FOUND"
    assert narrowing_calls == 1
    assert any(item.identity == first_observation.identity for item in reopened.records)
    assert set(decoder.session_ids) == {acquisition.common_session_id}
    assert acquisition.retained_mp4_path == tmp_path / "replay.mp4"
    families = {item.family for item in reopened.records}
    assert {
        "support-group",
        "c2-bracket",
        "d1-input",
        "d1-history",
        "narrowed-bracket",
    } <= families
    support = next(item for item in reopened.records if item.family == "support-group")
    assert len(set(support.payload["member_frame_ids"])) == 3
    assert len(set(support.payload["member_observation_ids"])) == 3
    logical_end = next(
        item
        for item in reopened.records
        if item.family == "target-request"
        and item.payload["selection_rule"] == "FINAL_STRICTLY_BEFORE_END"
    )
    alias = next(
        item
        for item in reopened.records
        if item.family == "alias" and item.payload["target_request_id"] == logical_end.identity
    )
    assert alias.payload["frame_id"] == support.payload["member_frame_ids"][-1]
    terminal = next(item for item in reopened.records if item.family == "terminal-result")
    assert terminal.payload["interval_start_requested_time_utc"] == "2026-07-20T03:00:00Z"
    assert terminal.payload["interval_end_requested_time_utc"] == "2026-07-20T03:00:01Z"


def test_1d_from_actual_1c_output_admits_all_decoder_target_dependencies(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start at 1C's real zero-evidence state, not a terminal-ready fixture."""
    vectors = _vectors()
    by_family = {item["family"]: item for item in vectors}
    policy_record = _envelope(by_family["policy"])
    policy = CommonSessionPolicy.from_payload(policy_record.payload)
    request = CommonSessionRequest.from_start_and_duration(
        "inv-01",
        "run-01",
        1,
        _utc("2026-07-20T03:00:00Z"),
        duration_seconds=5,
        policy=policy,
    )
    planned = Phase7EC1PlannerAdapter().build(request, policy_record)
    plan = planned.plan
    targets = planned.coarse_targets
    target_ids = tuple(item.identity for item in targets)
    schema5_template = _envelope(by_family["schema5-manifest"])
    schema5 = StrictIdentityEnvelope.from_payload(
        "schema5-manifest",
        {
            **schema5_template.payload,
            "policy_id": policy_record.identity,
            "plan_id": plan.identity,
            "coarse_target_request_ids": list(target_ids),
        },
    )
    classifier_policy = _envelope(by_family["classifier-policy"])
    start = request.start_utc
    segment = RecordingSegment(
        1,
        date(2026, 7, 20),
        int(start.timestamp()),
        int((start + timedelta(seconds=30)).timestamp()),
        start,
        start + timedelta(seconds=30),
    )
    replay_path = tmp_path / "replay.mp4"
    replay_calls = 0

    class Planner:
        def find_segments_for_window(self, window: RecordingWindow) -> tuple[RecordingSegment, ...]:
            _ = window
            return (segment,)

        def plan_for_segment(
            self, segment: RecordingSegment, window: RecordingWindow
        ) -> ReplayRequest:
            _ = segment
            return ReplayRequest(window, "rtsp://redacted.example/replay")

    class Extractor:
        def extract(self, replay_request: ReplayRequest) -> ReplayClip:
            nonlocal replay_calls
            replay_calls += 1
            replay_path.write_bytes(b"one-retained-session")
            return ReplayClip(
                replay_request.window.channel_id,
                replay_request.window.start_utc,
                replay_request.window.end_utc,
                replay_request.replay_url,
                replay_path,
                replay_request.window.duration_seconds,
            )

    class Probe:
        def probe(self, path: Path, timeout_seconds: float) -> MediaProbeFacts:
            _ = (path, timeout_seconds)
            return MediaProbeFacts(
                selected_video_stream_index=0,
                video_stream_count=1,
                audio_stream_count=0,
                container_start_pts=0,
                time_base_num=1,
                time_base_den=1,
                duration_ticks=5,
                codec="h264",
                profile="High",
                pixel_format="yuv420p",
                width=8,
                height=8,
                average_frame_rate_num=1,
                average_frame_rate_den=1,
            )

    repository = RecordingSearch7ERepository(tmp_path / "runs", lock_timeout_seconds=0)
    acquirer = CommonSessionAcquirer(cast("Any", Planner()), Extractor(), Probe())
    executor = Phase7E1CExecutor(repository, acquirer)
    admission = executor.execute(
        request,
        schema5,
        (policy_record, plan, *targets),
        classifier_policy,
        targets,
    )
    initial = admission.run
    assert initial.is_schema6
    assert set(initial.manifest.payload["indexes"]["target_request_ids"]) == set(target_ids)
    assert initial.manifest.payload["indexes"]["decoder_operation_ids"] == []

    events: list[str] = []
    original_admit_schema6 = repository.admit_schema6

    def traced_admit_schema6(
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> object:
        records = args[4] if len(args) > 4 else kwargs.get("records", ())
        if any(
            isinstance(item, StrictIdentityEnvelope) and item.family == "decoder-operation"
            for item in records
        ):
            events.append("operation-admitted")
        return original_admit_schema6(*args, **kwargs)

    class TracingDecoder:
        def __init__(self, delegate: _TimelineDecoder) -> None:
            self.delegate: _TimelineDecoder = delegate

        def decode(
            self,
            session: CommonSessionAcquisition,
            targets: tuple[datetime, ...],
            timeout_seconds: float,
        ) -> tuple[DecodedLocalFrame, ...]:
            events.append("decoder-callback")
            return self.delegate.decode(session, targets, timeout_seconds)

    monkeypatch.setattr(repository, "admit_schema6", traced_admit_schema6)
    decoder = _TimelineDecoder()
    adapter = Phase7ELocalEvidenceAdapter(
        repository,
        TracingDecoder(decoder),
        _TimelineClassifier(present_times={"2026-07-20T03:00:00Z", "2026-07-20T03:00:01Z"}),
    )
    narrowing_results: list[NarrowingResult] = []
    original_narrow = BinaryNarrowingService.narrow

    def trace_narrow(
        *args: Any,  # noqa: ANN401 - transparent service spy.
        **kwargs: Any,  # noqa: ANN401 - transparent service spy.
    ) -> NarrowingResult:
        result = original_narrow(*args, **kwargs)
        narrowing_results.append(result)
        return result

    monkeypatch.setattr(BinaryNarrowingService, "narrow", trace_narrow)
    with executor.invocation(request) as invocation:
        result = Phase7E1DService(repository, local_evidence=adapter).execute(
            invocation,
            admission.acquisition,
        )

    assert result.run.is_schema7
    assert result.run.result_kind == "FOUND"
    assert narrowing_results
    narrowing = narrowing_results[0]
    narrowing_bracket = narrowing.narrowed_bracket
    assert narrowing_bracket is not None
    reopened = repository.reopen_schema7("inv-01", "run-01")
    assert replay_calls == 1
    assert events.index("operation-admitted") < events.index("decoder-callback")
    assert events.count("decoder-callback") >= 2
    steps = narrowing_bracket.history
    assert steps
    midpoint_step = steps[0]
    assert midpoint_step.entry_kind.value == "PRESENT_TRANSITION"
    midpoint = next(item for item in midpoint_step.evidence if item.role == "MIDPOINT")
    midpoint_target = next(
        item
        for item in reopened.records
        if item.family == "target-request" and item.identity == midpoint.probe_request_id
    )
    assert midpoint_target.payload["kind"] == "BINARY"
    assert midpoint_step.midpoint_requested_time_utc == _utc("2026-07-20T03:00:01Z")
    assert midpoint.probe_request_id == midpoint_target.identity
    frame = next(item for item in reopened.records if item.identity == midpoint.canonical_frame_id)
    assert frame.family == "frame"
    assert frame.payload["target_request_id"] == midpoint_target.identity
    decoder = next(
        item for item in reopened.records if item.identity == midpoint.acquisition_operation_id
    )
    assert decoder.family == "decoder-operation"
    assert midpoint_target.identity in decoder.payload["target_request_ids"]
    classification = next(
        item for item in reopened.records if item.identity == midpoint.classification_operation_id
    )
    assert classification.family == "classification-operation"
    observation = next(
        item for item in reopened.records if item.identity == midpoint.observation_id
    )
    assert observation.family == "observation"
    assert observation.payload["target_request_id"] == midpoint_target.identity
    assert midpoint_step.bracket_before.lower_requested_time_utc == _utc("2026-07-20T03:00:00Z")
    assert midpoint_step.bracket_before.upper_requested_time_utc == _utc("2026-07-20T03:00:02Z")
    assert (
        midpoint_step.bracket_after.lower_requested_time_utc
        == midpoint_step.midpoint_requested_time_utc
    )
    assert midpoint_step.bracket_after.upper_requested_time_utc == _utc("2026-07-20T03:00:02Z")
    history = next(item for item in reopened.records if item.family == "d1-history")
    assert history.payload["steps"]
    source_set = next(item for item in reopened.records if item.family == "source-record-set")
    assert source_set.identity == reopened.manifest.payload["source_record_set_id"]
    source_ids = {
        identity for group in source_set.payload["record_groups"] for identity in group["ids"]
    }
    assert source_ids == {
        item.identity
        for item in reopened.records
        if item.family
        not in {
            "schema6-manifest",
            "source-record-set",
            "evidence-snapshot",
            "terminal-result",
            "schema7-manifest",
        }
    }
    common_sessions = [item for item in reopened.records if item.family == "common-session"]
    assert len(common_sessions) == 1
    decoder_operations = [item for item in reopened.records if item.family == "decoder-operation"]
    assert decoder_operations
    assert {item.payload["common_session_id"] for item in decoder_operations} == {
        common_sessions[0].identity
    }
    initial_target_ids = {
        item.identity for item in initial.records if item.family == "target-request"
    }
    midpoint_targets = {
        evidence.target_id
        for step in steps
        for evidence in step.evidence
        if evidence.role == "MIDPOINT"
    }
    assert midpoint_targets
    assert midpoint_targets.isdisjoint(initial_target_ids)


def test_decoder_operation_is_idempotent_before_decode_under_same_owner(tmp_path: Path) -> None:
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
    acquisition = _test_acquisition(tmp_path, run, request)
    target = next(
        item
        for item in run.records
        if item.family == "target-request"
        and isinstance(run.state, Schema6Envelope)
        and item.identity == run.state.active_target_request_id
    )
    operation = make_decoder_envelope(acquisition, 1, (target.identity,))
    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner:
        invocation = Phase7EInvocation(
            request,
            owner,
            InvocationBudget(request.policy, lambda: 0.0),
        )
        first = admit_decoder_operation(
            repo,
            acquisition,
            target,
            operation,
            invocation=invocation,
        )
        second = admit_decoder_operation(
            repo,
            acquisition,
            target,
            operation,
            invocation=invocation,
        )
    assert first.manifest_id == second.manifest_id
    assert isinstance(second.state, Schema6Envelope)
    assert second.state.target_state is Schema6TargetState.DECODING
    assert second.state.active_decoder_operation_id == operation.identity


def test_new_owner_rejects_preexisting_decoder_operation_without_decode(
    tmp_path: Path,
) -> None:
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
    acquisition = _test_acquisition(tmp_path, run, request)
    target = next(
        item
        for item in run.records
        if item.family == "target-request"
        and isinstance(run.state, Schema6Envelope)
        and item.identity == run.state.active_target_request_id
    )
    operation = make_decoder_envelope(acquisition, 1, (target.identity,))
    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner_a:
        admit_decoder_operation(
            repo,
            acquisition,
            target,
            operation,
            invocation=Phase7EInvocation(
                request,
                owner_a,
                InvocationBudget(request.policy, lambda: 0.0),
            ),
        )

    decoder = _OneFrameDecoder()

    def expect_interrupted(invocation: Phase7EInvocation) -> bool:
        try:
            Phase7ELocalEvidenceAdapter(repo, decoder, _VisualClassifier()).execute(
                invocation,
                acquisition,
                (target,),
            )
        except CommonSessionCancelledError:
            return True
        return False

    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner_b:
        assert expect_interrupted(
            Phase7EInvocation(
                request,
                owner_b,
                InvocationBudget(request.policy, lambda: 0.0),
            )
        )
    assert decoder.calls == 0
    before_recovery = repo.reopen_schema6("inv-01", "run-01")
    assert isinstance(before_recovery.state, Schema6Envelope)
    assert before_recovery.state.target_state is Schema6TargetState.DECODING

    recovered = repo.recover_active("inv-01", "run-01")
    assert isinstance(recovered.state, Schema6Envelope)
    assert recovered.state.target_state is Schema6TargetState.INTERRUPTED
    assert recovered.manifest.payload["indexes"]["frame_ids"] == []
    assert recovered.manifest.payload["indexes"]["observation_ids"] == []


def test_late_decoder_output_cannot_cross_recovery_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    acquisition = _test_acquisition(tmp_path, run, request)
    target = next(
        item
        for item in run.records
        if item.family == "target-request"
        and isinstance(run.state, Schema6Envelope)
        and item.identity == run.state.active_target_request_id
    )
    captured_args: tuple[object, ...] = ()
    captured_kwargs: dict[str, object] = {}

    def fail_before_frame_admission(*args: object, **kwargs: object) -> Phase7ERun:
        nonlocal captured_args, captured_kwargs
        captured_args = args
        captured_kwargs = kwargs
        raise RuntimeError from None

    monkeypatch.setattr(
        "vigi_vision.recording_search_7e_1d.admit_frame_then_classify",
        fail_before_frame_admission,
    )
    decoder_a = _OneFrameDecoder()

    def expect_frame_admission_interrupt(owner: Phase7EInvocation) -> bool:
        try:
            Phase7ELocalEvidenceAdapter(repo, decoder_a, _VisualClassifier()).execute(
                owner,
                acquisition,
                (target,),
            )
        except RuntimeError:
            return True
        return False

    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner_a:
        assert expect_frame_admission_interrupt(
            Phase7EInvocation(
                request,
                owner_a,
                InvocationBudget(request.policy, lambda: 0.0),
            )
        )
    assert decoder_a.calls == 1
    decoder_b = _OneFrameDecoder()

    def expect_second_owner_interrupt(owner: Phase7EInvocation) -> bool:
        try:
            Phase7ELocalEvidenceAdapter(repo, decoder_b, _VisualClassifier()).execute(
                owner,
                acquisition,
                (target,),
            )
        except CommonSessionCancelledError:
            return True
        return False

    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner_b:
        assert expect_second_owner_interrupt(
            Phase7EInvocation(
                request,
                owner_b,
                InvocationBudget(request.policy, lambda: 0.0),
            )
        )
    assert decoder_b.calls == 0

    recovered = repo.recover_active("inv-01", "run-01")
    assert isinstance(recovered.state, Schema6Envelope)
    assert recovered.state.target_state is Schema6TargetState.INTERRUPTED
    assert recovered.manifest.payload["indexes"]["frame_ids"] == []
    assert recovered.manifest.payload["indexes"]["observation_ids"] == []

    late_admission = cast("Callable[..., Phase7ERun]", admit_frame_then_classify)

    def expect_late_rejection() -> bool:
        try:
            late_admission(*captured_args, **captured_kwargs)
        except Phase7EInProgressError:
            return True
        return False

    assert expect_late_rejection()


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (CommonSessionDecoderError, "decoder_failed"),
        (CommonSessionDecoderTimeoutError, "decoder_timeout"),
        (CommonSessionCancelledError, "interrupted"),
        (RuntimeError, "decoder_failed"),
    ],
)
def test_decoder_failure_is_persisted_after_durable_intent(
    tmp_path: Path,
    failure: type[BaseException],
    reason: str,
) -> None:
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
    acquisition = _test_acquisition(tmp_path, run, request)
    target = next(
        item
        for item in run.records
        if item.family == "target-request"
        and isinstance(run.state, Schema6Envelope)
        and item.identity == run.state.active_target_request_id
    )

    class FailingDecoder:
        def decode(
            self,
            session: CommonSessionAcquisition,
            targets: tuple[datetime, ...],
            timeout_seconds: float,
        ) -> tuple[DecodedLocalFrame, ...]:
            _ = (session, targets, timeout_seconds)
            raise failure

    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner:
        invocation = Phase7EInvocation(
            request,
            owner,
            InvocationBudget(request.policy, lambda: 0.0),
        )
        with pytest.raises(failure):
            Phase7ELocalEvidenceAdapter(repo, FailingDecoder(), _VisualClassifier()).execute(
                invocation,
                acquisition,
                (target,),
            )
    reopened = repo.reopen_schema6("inv-01", "run-01")
    assert isinstance(reopened.state, Schema6Envelope)
    assert reopened.state.target_state is Schema6TargetState.ACQUISITION_FAILED
    assert reopened.state.reason_code == reason
    assert len(reopened.manifest.payload["indexes"]["decoder_operation_ids"]) == 1
    assert reopened.manifest.payload["indexes"]["frame_ids"] == []


def test_cancellation_after_intent_before_decoder_is_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    acquisition = _test_acquisition(tmp_path, run, request)
    target = next(
        item
        for item in run.records
        if item.family == "target-request"
        and isinstance(run.state, Schema6Envelope)
        and item.identity == run.state.active_target_request_id
    )
    cancelled = False
    decoder_calls = 0
    original_admit = admit_decoder_operation

    def admit_then_cancel(
        repository: RecordingSearch7ERepository,
        acquisition: CommonSessionAcquisition,
        target_request: StrictIdentityEnvelope,
        decoder_operation: StrictIdentityEnvelope,
        *,
        invocation: Phase7EInvocation,
    ) -> Phase7ERun:
        nonlocal cancelled
        result = original_admit(
            repository,
            acquisition,
            target_request,
            decoder_operation,
            invocation=invocation,
        )
        cancelled = True
        return result

    class UnexpectedDecoder:
        def decode(
            self,
            session: CommonSessionAcquisition,
            targets: tuple[datetime, ...],
            timeout_seconds: float,
        ) -> tuple[DecodedLocalFrame, ...]:
            nonlocal decoder_calls
            _ = (session, targets, timeout_seconds)
            decoder_calls += 1
            raise AssertionError

    monkeypatch.setattr(
        "vigi_vision.recording_search_7e_1d.admit_decoder_operation", admit_then_cancel
    )
    with repo.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner:
        invocation = Phase7EInvocation(
            request,
            owner,
            InvocationBudget(request.policy, lambda: 0.0, cancellation=lambda: cancelled),
        )
        with pytest.raises(CommonSessionCancelledError):
            Phase7ELocalEvidenceAdapter(repo, UnexpectedDecoder(), _VisualClassifier()).execute(
                invocation,
                acquisition,
                (target,),
            )
    reopened = repo.reopen_schema6("inv-01", "run-01")
    assert isinstance(reopened.state, Schema6Envelope)
    assert reopened.state.target_state is Schema6TargetState.ACQUISITION_FAILED
    assert reopened.state.reason_code == "interrupted"
    assert decoder_calls == 0
    assert len(reopened.manifest.payload["indexes"]["decoder_operation_ids"]) == 1


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
