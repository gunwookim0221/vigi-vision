# pyright: reportAny=false, reportExplicitAny=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
# ruff: noqa: C901
"""Phase 7E-1D request-relative planning and immutable terminal composition.

The adapters in this module do not acquire a recording or classify pixels.
They consume only strictly reopened schema-6 records admitted by 1C, reuse the
existing C1 target-grid and D1 iteration policies, and publish the closed
Schema-7 identity family through the existing Phase 7E repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, cast

from vigi_vision.recording_search_7e_1c import (
    B4Bridge,
    CommonSessionAcquisition,
    CommonSessionRequest,
    DecodedLocalFrame,
    Decoder,
    Phase7EInvocation,
    admit_frame_then_classify,
    append_schema6_indexes,
    execute_local_targets,
    make_alias_envelope,
    make_decoder_envelope,
    make_target_envelope,
)
from vigi_vision.recording_search_7e_models import Schema6TargetState, StrictIdentityEnvelope
from vigi_vision.recording_search_7e_repository import (
    Phase7ECorruptError,
    Phase7ENotFoundError,
    Phase7ERun,
    PublicationResult,
    PublicationStatus,
    RecordingSearch7ERepository,
)
from vigi_vision.recording_search_7e_validation import Schema6Envelope
from vigi_vision.recording_search_c1_planner import build_coarse_sampling_plan
from vigi_vision.recording_search_d1_planner import maximum_narrowing_iterations
from vigi_vision.recording_search_d2_terminal_models import TerminalResultKind

if TYPE_CHECKING:
    from vigi_vision.recording_search_models import RecordingSearchPolicy


class Phase7E1DError(RuntimeError):
    """Safe base failure for terminal composition."""


class Phase7EIncompleteEvidenceError(Phase7E1DError):
    """Schema 6 does not contain enough comparable evidence to terminalize."""


class Phase7EOperationalEvidenceError(Phase7E1DError):
    """Operational or unusable evidence cannot become a visual result."""


class Phase7EAdapterError(Phase7E1DError):
    """An existing C1/C2/D1/D2 boundary returned an unknown shape."""


class Phase7ETerminalReason(str, Enum):
    """Closed Schema-7 visual reason vocabulary."""

    SUPPORTED_TRANSITION = "SUPPORTED_TRANSITION"
    COMPLETE_PRESENT_GRID = "COMPLETE_PRESENT_GRID"
    BASELINE_ONLY_LOWER_BOUND = "BASELINE_ONLY_LOWER_BOUND"
    VISUAL_INDETERMINATE = "VISUAL_INDETERMINATE"
    INCOMPLETE_VISUAL_EVIDENCE = "INCOMPLETE_VISUAL_EVIDENCE"


@dataclass(frozen=True, slots=True)
class Phase7ECoarsePlanBundle:
    """The Phase 7E C1 plan plus exact target records owned by Schema 5/6."""

    plan: StrictIdentityEnvelope
    coarse_targets: tuple[StrictIdentityEnvelope, ...]
    final_support_targets: tuple[StrictIdentityEnvelope, ...]


@dataclass(frozen=True, slots=True)
class Phase7ETerminalDecision:
    """D2-compatible terminal facts selected from authoritative Schema 6."""

    result_kind: TerminalResultKind
    reason: Phase7ETerminalReason
    selected_observation_ids: tuple[str, ...]
    selected_support_group_ids: tuple[str, ...]
    narrowed_bracket_id: str | None
    interval_start_requested_time_utc: str | None
    interval_end_requested_time_utc: str | None


@dataclass(frozen=True, slots=True)
class Phase7EStatus:
    """Credential-free internal Phase 7 status projection."""

    investigation_id: str
    run_id: str
    schema_version: int
    status: str
    reason_code: str | None
    terminal_result_id: str | None


class TerminalDecisionBoundary(Protocol):
    """Narrow adapter around the existing C2/D1/D2 production composition."""

    def interpret(self, run: Phase7ERun) -> Phase7ETerminalDecision:
        """Return one closed decision from a strictly reopened Schema-6 run."""
        ...


@dataclass(frozen=True, slots=True)
class Phase7ELocalEvidenceAdapter:
    """Run initial or adaptive targets through the existing 1C/A2/B4 loop."""

    repository: RecordingSearch7ERepository
    decoder: Decoder
    classifier: B4Bridge

    def execute(
        self,
        invocation: Phase7EInvocation,
        acquisition: CommonSessionAcquisition,
        targets: tuple[StrictIdentityEnvelope, ...],
    ) -> Phase7ERun:
        """Admit each target from one retained session with strict readbacks."""
        if acquisition.request != invocation.request or not targets:
            raise Phase7EAdapterError
        session_id = acquisition.common_session_id
        pending: list[StrictIdentityEnvelope] = []
        for target in targets:
            invocation.validate(self.repository)
            current = self.repository.reopen_schema6(
                invocation.request.investigation_id,
                invocation.request.run_id,
                ownership=invocation.ownership,
            )
            if current.manifest.payload.get("common_session_id") != session_id:
                raise Phase7EAdapterError
            if _target_is_admitted(current, target.identity):
                continue
            _validate_target_for_session(current, target, invocation.request)
            pending.append(target)
        if not pending:
            return self.repository.reopen_schema6(
                invocation.request.investigation_id,
                invocation.request.run_id,
                ownership=invocation.ownership,
            )
        pending.sort(key=lambda item: str(item.payload["requested_time_utc"]))
        requested_by_target = {
            item.identity: _parse_whole_text(str(item.payload["requested_time_utc"]))
            for item in pending
        }
        ordered = tuple(dict.fromkeys(requested_by_target.values()))
        pass_number = invocation.budget.decoder_passes + 1
        frames = execute_local_targets(
            acquisition,
            self.decoder,
            ordered,
            pass_number=pass_number,
            logical_end=any(
                item.payload["selection_rule"] == "FINAL_STRICTLY_BEFORE_END" for item in pending
            ),
            allow_aliases=True,
            budget=invocation.budget,
        )
        decoder_operation = make_decoder_envelope(
            acquisition,
            pass_number,
            tuple(item.identity for item in pending),
        )
        frame_by_requested_time = dict(zip(ordered, frames, strict=True))
        for target in pending:
            frame = frame_by_requested_time[requested_by_target[target.identity]]
            invocation.validate(self.repository)
            current = self.repository.reopen_schema6(
                invocation.request.investigation_id,
                invocation.request.run_id,
                ownership=invocation.ownership,
            )
            current = _request_schema6_target(self.repository, invocation, current, target)
            alias = _find_frame_alias(current, frame)
            if alias is not None:
                _ = _admit_alias_observation(
                    self.repository,
                    invocation,
                    current,
                    target,
                    decoder_operation,
                    alias,
                )
                continue
            _ = admit_frame_then_classify(
                self.repository,
                acquisition,
                target,
                decoder_operation,
                frame,
                self.classifier,
                classification_attempt_id=f"{decoder_operation.identity}:attempt-1",
                invocation=invocation,
            )
        invocation.validate(self.repository)
        return self.repository.reopen_schema6(
            invocation.request.investigation_id,
            invocation.request.run_id,
            ownership=invocation.ownership,
        )


@dataclass(frozen=True, slots=True)
class Phase7EC1PlannerAdapter:
    """Reuse C1's bounded chronological grid with Phase 7E endpoint rules."""

    def build(
        self,
        request: CommonSessionRequest,
        policy: StrictIdentityEnvelope,
    ) -> Phase7ECoarsePlanBundle:
        """Include S/logical E and construct final support backward from E."""
        if policy.family != "policy":
            raise Phase7EAdapterError
        legacy = _C1PolicyView(request, policy.payload)
        existing_plan = build_coarse_sampling_plan(
            cast("RecordingSearchPolicy", cast("object", legacy))
        )  # existing C1 policy component
        targets = (request.start_utc, *existing_plan.target_times)
        if len(targets) != len(set(targets)) or targets[-1] != request.end_utc:
            raise Phase7EAdapterError
        plan = StrictIdentityEnvelope.from_payload(
            "coarse-plan",
            {
                "investigation_id": request.investigation_id,
                "run_id": request.run_id,
                "channel_id": request.channel_id,
                "policy_id": policy.identity,
                "start_requested_time_utc": _whole_text(request.start_utc),
                "end_requested_time_utc": _whole_text(request.end_utc),
                "target_requested_times_utc": [_whole_text(item) for item in targets],
            },
        )
        coarse = tuple(
            make_target_envelope(
                request,
                plan.identity,
                sequence,
                target,
                kind="COARSE",
                selection_rule=(
                    "FINAL_STRICTLY_BEFORE_END"
                    if target == request.end_utc
                    else "NEAREST_IN_HALF_OPEN_SESSION"
                ),
            )
            for sequence, target in enumerate(targets, start=1)
        )
        count = int(policy.payload["support_count"])
        cadence = int(policy.payload["support_cadence_seconds"])
        support_times = tuple(
            request.end_utc - timedelta(seconds=index * cadence) for index in range(count, 0, -1)
        )
        if not support_times or support_times[0] < request.start_utc:
            raise Phase7EIncompleteEvidenceError
        origin = coarse[-1].identity
        support = tuple(
            make_target_envelope(
                request,
                plan.identity,
                len(coarse) + index + 1,
                target,
                kind="SUPPORT",
                selection_rule="NEAREST_IN_HALF_OPEN_SESSION",
                origin_target_request_id=origin,
            )
            for index, target in enumerate(support_times)
        )
        return Phase7ECoarsePlanBundle(plan, coarse, support)


@dataclass(frozen=True, slots=True)
class Phase7ED2DecisionAdapter:
    """Translate reopened request-relative evidence into the closed D2 outcome."""

    def interpret(  # noqa: PLR0912 - closed terminal precedence table.
        self, run: Phase7ERun
    ) -> Phase7ETerminalDecision:
        """Apply D2 terminal precedence without accepting caller evidence."""
        if (
            not run.is_schema6
            or not isinstance(run.state, Schema6Envelope)
            or run.state.run_state != "RUNNING"
            or run.state.target_state is not Schema6TargetState.OBSERVED
        ):
            raise Phase7ECorruptError
        records = {record.identity: record for record in run.records}
        by_family: dict[str, list[StrictIdentityEnvelope]] = {}
        for record in run.records:
            by_family.setdefault(record.family, []).append(record)
        observations = {
            item.payload["target_request_id"]: item for item in by_family.get("observation", [])
        }
        for alias in by_family.get("alias", []):
            origin = observations.get(alias.payload["alias_of_target_request_id"])
            if origin is None or alias.payload["target_request_id"] in observations:
                raise Phase7ECorruptError
            observations[alias.payload["target_request_id"]] = origin
        for observation in observations.values():
            evidence = observation.payload["classifier_evidence"]
            if evidence["visual_status"] == "unusable":
                raise Phase7EOperationalEvidenceError
        narrowed_ids = run.manifest.payload["indexes"]["narrowed_bracket_ids"]
        if narrowed_ids:
            narrowed = records[narrowed_ids[-1]]
            support_id = narrowed.payload["upper_support_group_id"]
            selected = (
                narrowed.payload["lower_observation_id"],
                narrowed.payload["upper_observation_id"],
            )
            return Phase7ETerminalDecision(
                TerminalResultKind.FOUND,
                Phase7ETerminalReason.SUPPORTED_TRANSITION,
                selected,
                (support_id,),
                narrowed.identity,
                narrowed.payload["interval_start_requested_time_utc"],
                narrowed.payload["interval_end_requested_time_utc"],
            )
        schema5 = next(
            (record for record in run.records if record.family == "schema5-manifest"), None
        )
        if schema5 is None:
            raise Phase7ECorruptError
        coarse_ids = tuple(schema5.payload["coarse_target_request_ids"])
        coarse_observations = tuple(observations.get(item) for item in coarse_ids)
        if any(item is None for item in coarse_observations):
            raise Phase7EIncompleteEvidenceError
        complete = tuple(item for item in coarse_observations if item is not None)
        if len({item.identity for item in complete}) != len(complete):
            raise Phase7EIncompleteEvidenceError
        indeterminate = tuple(
            item for item in complete if item.payload["outcome"] == "INDETERMINATE"
        )
        if indeterminate:
            return Phase7ETerminalDecision(
                TerminalResultKind.INCONCLUSIVE,
                Phase7ETerminalReason.VISUAL_INDETERMINATE,
                tuple(item.identity for item in indeterminate),
                (),
                None,
                None,
                None,
            )
        if all(item.payload["outcome"] == "PRESENT" for item in complete):
            return Phase7ETerminalDecision(
                TerminalResultKind.NOT_FOUND,
                Phase7ETerminalReason.COMPLETE_PRESENT_GRID,
                tuple(item.identity for item in complete),
                (),
                None,
                None,
                None,
            )
        support_ids = run.manifest.payload["indexes"]["support_group_ids"]
        if support_ids and not any(item.payload["outcome"] == "PRESENT" for item in complete):
            support = records[support_ids[-1]]
            return Phase7ETerminalDecision(
                TerminalResultKind.INCONCLUSIVE,
                Phase7ETerminalReason.BASELINE_ONLY_LOWER_BOUND,
                tuple(support.payload["member_observation_ids"]),
                (support.identity,),
                None,
                None,
                None,
            )
        return Phase7ETerminalDecision(
            TerminalResultKind.INCONCLUSIVE,
            Phase7ETerminalReason.INCOMPLETE_VISUAL_EVIDENCE,
            tuple(item.identity for item in complete),
            tuple(support_ids[-1:]),
            None,
            None,
            None,
        )


@dataclass(frozen=True, slots=True)
class Phase7E1DService:
    """Strict Schema-6 reconstruction, D2 interpretation, and Schema-7 commit."""

    repository: RecordingSearch7ERepository
    decision_boundary: TerminalDecisionBoundary = Phase7ED2DecisionAdapter()

    def execute(
        self,
        invocation: Phase7EInvocation,
    ) -> PublicationResult:
        """Terminalize one owned run and strictly reopen the immutable winner."""
        invocation.validate(self.repository)
        invocation.budget.check()
        current = self.repository.reopen_current(
            invocation.request.investigation_id,
            invocation.request.run_id,
            ownership=invocation.ownership,
        )
        if current.is_schema7:
            return PublicationResult(PublicationStatus.REUSED, current)
        if not current.is_schema6:
            raise Phase7EIncompleteEvidenceError
        run = current
        _ = invocation.budget.operation_timeout(
            invocation.request.policy.terminal_interpretation_seconds,
            minimum_start_seconds=0.001,
        )
        decision = self.decision_boundary.interpret(run)
        invocation.validate(self.repository)
        invocation.budget.check()
        source_set = build_source_record_set(run)
        snapshot = build_evidence_snapshot(run, source_set, decision)
        terminal = build_terminal_result(run, source_set, snapshot, decision)
        manifest = build_schema7_manifest(run, source_set, snapshot, terminal)
        _ = invocation.budget.operation_timeout(
            invocation.request.policy.publication_seconds,
            minimum_start_seconds=0.001,
        )
        _ = invocation.budget.operation_timeout(
            invocation.request.policy.strict_readback_seconds,
            minimum_start_seconds=0.001,
        )
        published = self.repository.publish_schema7(
            run.investigation_id,
            run.run_id,
            manifest,
            source_set,
            snapshot,
            terminal,
            expected_schema6_manifest_id=run.manifest_id,
            ownership=invocation.ownership,
        )
        _ = self.repository.reopen_schema7(
            run.investigation_id,
            run.run_id,
            ownership=invocation.ownership,
        )
        return published


def build_source_record_set(run: Phase7ERun) -> StrictIdentityEnvelope:
    """Bind every strictly reopened Schema-6 child in canonical group order."""
    if not run.is_schema6:
        raise Phase7ECorruptError
    payload = run.manifest.payload
    indexes = payload["indexes"]
    schema5 = next(record for record in run.records if record.family == "schema5-manifest")
    groups = [
        {"type": "policy", "ids": [payload["policy_id"]]},
        {"type": "classifier_policy", "ids": [payload["classifier_policy_id"]]},
        {"type": "schema5_manifest", "ids": [schema5.identity]},
        {"type": "coarse_plan", "ids": [payload["plan_id"]]},
        {"type": "replay_operation", "ids": [payload["replay_operation_id"]]},
        {"type": "common_session", "ids": [payload["common_session_id"]]},
        {"type": "target_requests", "ids": list(indexes["target_request_ids"])},
        {"type": "decoder_operations", "ids": list(indexes["decoder_operation_ids"])},
        {"type": "frames", "ids": list(indexes["frame_ids"])},
        {
            "type": "classification_operations",
            "ids": list(indexes["classification_operation_ids"]),
        },
        {"type": "observations", "ids": list(indexes["observation_ids"])},
        {"type": "aliases", "ids": list(indexes["alias_ids"])},
        {"type": "support_groups", "ids": list(indexes["support_group_ids"])},
        {"type": "c2_brackets", "ids": list(indexes["c2_bracket_ids"])},
        {"type": "d1_inputs", "ids": list(indexes["d1_input_ids"])},
        {"type": "d1_histories", "ids": list(indexes["d1_history_ids"])},
        {"type": "narrowed_brackets", "ids": list(indexes["narrowed_bracket_ids"])},
    ]
    if sum(len(group["ids"]) for group in groups) != len(run.records):
        raise Phase7ECorruptError
    return StrictIdentityEnvelope.from_payload(
        "source-record-set",
        {
            "schema_version": 1,
            "investigation_id": run.investigation_id,
            "run_id": run.run_id,
            "schema6_manifest_id": run.manifest_id,
            "record_count": len(run.records),
            "record_groups": groups,
        },
    )


def build_evidence_snapshot(
    run: Phase7ERun,
    source_set: StrictIdentityEnvelope,
    decision: Phase7ETerminalDecision,
) -> StrictIdentityEnvelope:
    """Bind only the observations/support selected by the D2 decision."""
    return StrictIdentityEnvelope.from_payload(
        "evidence-snapshot",
        {
            "schema_version": 1,
            "investigation_id": run.investigation_id,
            "run_id": run.run_id,
            "policy_id": run.manifest.payload["policy_id"],
            "classifier_policy_id": run.manifest.payload["classifier_policy_id"],
            "narrowed_bracket_id": decision.narrowed_bracket_id,
            "selected_observation_ids": list(decision.selected_observation_ids),
            "selected_support_group_ids": list(decision.selected_support_group_ids),
            "source_record_set_id": source_set.identity,
        },
    )


def build_terminal_result(
    run: Phase7ERun,
    source_set: StrictIdentityEnvelope,
    snapshot: StrictIdentityEnvelope,
    decision: Phase7ETerminalDecision,
) -> StrictIdentityEnvelope:
    """Construct the exact request-relative terminal-result payload."""
    return StrictIdentityEnvelope.from_payload(
        "terminal-result",
        {
            "schema_version": 1,
            "investigation_id": run.investigation_id,
            "run_id": run.run_id,
            "result_kind": decision.result_kind.value,
            "reason_code": decision.reason.value,
            "interval_start_requested_time_utc": decision.interval_start_requested_time_utc,
            "interval_end_requested_time_utc": decision.interval_end_requested_time_utc,
            "source_record_set_id": source_set.identity,
            "evidence_snapshot_id": snapshot.identity,
            "common_session_id": run.manifest.payload["common_session_id"],
        },
    )


def build_schema7_manifest(
    run: Phase7ERun,
    source_set: StrictIdentityEnvelope,
    snapshot: StrictIdentityEnvelope,
    terminal: StrictIdentityEnvelope,
) -> StrictIdentityEnvelope:
    """Construct the immutable Schema-7 publication pointer."""
    return StrictIdentityEnvelope.from_payload(
        "schema7-manifest",
        {
            "schema_version": 7,
            "investigation_id": run.investigation_id,
            "run_id": run.run_id,
            "schema6_predecessor_manifest_id": run.manifest_id,
            "source_record_set_id": source_set.identity,
            "evidence_snapshot_id": snapshot.identity,
            "terminal_result_id": terminal.identity,
        },
    )


def read_phase7_status(
    repository: RecordingSearch7ERepository,
    investigation_id: str,
    run_id: str,
) -> Phase7EStatus:
    """Return a safe status derived only from strict repository reopen."""
    try:
        run = repository.reopen_current(investigation_id, run_id)
    except Phase7ENotFoundError:
        return Phase7EStatus(investigation_id, run_id, 0, "UNAVAILABLE", None, None)
    except Phase7ECorruptError:
        return Phase7EStatus(investigation_id, run_id, 0, "CORRUPT", None, None)
    if not run.is_schema7:
        return Phase7EStatus(
            investigation_id,
            run_id,
            run.schema_version,
            run.state.run_state,
            run.state.reason_code,
            None,
        )
    terminal = next(record for record in run.records if record.family == "terminal-result")
    return Phase7EStatus(
        investigation_id,
        run_id,
        7,
        str(terminal.payload["result_kind"]),
        str(terminal.payload["reason_code"]),
        terminal.identity,
    )


def maximum_phase7e_narrowing_iterations(
    interval_seconds: int,
    policy: StrictIdentityEnvelope,
) -> int:
    """Reuse D1's exact finite iteration calculation for request-relative bounds."""
    if policy.family != "policy":
        raise Phase7EAdapterError
    return maximum_narrowing_iterations(
        interval_seconds, int(policy.payload["binary_stop_seconds"])
    )


def _target_is_admitted(run: Phase7ERun, target_id: str) -> bool:
    """Return whether strict Schema 6 already binds this logical target."""
    return any(
        item.family in {"observation", "alias"}
        and item.payload.get("target_request_id") == target_id
        for item in run.records
    )


def _validate_target_for_session(
    run: Phase7ERun,
    target: StrictIdentityEnvelope,
    request: CommonSessionRequest,
) -> None:
    if (
        target.family != "target-request"
        or target.payload.get("investigation_id") != run.investigation_id
        or target.payload.get("run_id") != run.run_id
        or target.payload.get("plan_id") != run.manifest.payload.get("plan_id")
    ):
        raise Phase7EAdapterError
    requested = _parse_whole_text(str(target.payload.get("requested_time_utc")))
    logical_end = target.payload.get("selection_rule") == "FINAL_STRICTLY_BEFORE_END"
    if requested < request.start_utc or requested > request.end_utc:
        raise Phase7EAdapterError
    if requested == request.end_utc and not logical_end:
        raise Phase7EAdapterError
    if requested < request.end_utc and logical_end:
        raise Phase7EAdapterError


def _request_schema6_target(
    repository: RecordingSearch7ERepository,
    invocation: Phase7EInvocation,
    current: Phase7ERun,
    target: StrictIdentityEnvelope,
) -> Phase7ERun:
    """Publish or reuse one REQUESTED row before local decoding starts."""
    if (
        target.family != "target-request"
        or target.payload.get("investigation_id") != current.investigation_id
        or target.payload.get("run_id") != current.run_id
        or target.payload.get("plan_id") != current.manifest.payload.get("plan_id")
        or not isinstance(current.state, Schema6Envelope)
    ):
        raise Phase7EAdapterError
    if current.state.target_state is Schema6TargetState.REQUESTED:
        if current.state.active_target_request_id != target.identity:
            raise Phase7EAdapterError
        return current
    if current.state.target_state is not Schema6TargetState.OBSERVED:
        raise Phase7EAdapterError
    indexes = current.manifest.payload["indexes"]
    indexed = target.identity in indexes["target_request_ids"]
    manifest = (
        current.manifest
        if indexed
        else append_schema6_indexes(current.manifest, target_request_ids=target.identity)
    )
    records = tuple(item for item in current.records if item.family != "schema5-manifest")
    if not indexed:
        records = (*records, target)
    state = Schema6Envelope(
        run_state="RUNNING",
        target_state=Schema6TargetState.REQUESTED,
        active_target_request_id=target.identity,
        active_decoder_operation_id=None,
        active_frame_id=None,
        active_classification_attempt_id=None,
        active_classification_operation_id=None,
        active_observation_id=None,
        reason_code=None,
        attempt_count=0,
        predecessor_target_state=Schema6TargetState.OBSERVED,
    )
    return repository.admit_schema6(
        current.investigation_id,
        current.run_id,
        manifest,
        state,
        records,
        expected_manifest_id=current.manifest_id,
        ownership=invocation.ownership,
    ).run


def _find_frame_alias(
    run: Phase7ERun,
    frame: DecodedLocalFrame,
) -> tuple[StrictIdentityEnvelope, StrictIdentityEnvelope, StrictIdentityEnvelope] | None:
    """Resolve an existing frame/operation/observation for identical RGB24 media."""
    records = {item.identity: item for item in run.records}
    for candidate in run.records:
        if candidate.family != "frame":
            continue
        payload = candidate.payload
        same_session = payload.get("common_session_id") == frame.decode_session_id
        same_position = (
            payload.get("raw_pts") == frame.raw_pts and payload.get("ordinal") == frame.ordinal
        )
        same_pixels = payload.get("rgb24_sha256") == frame.rgb24_sha256
        if same_session and same_pixels and not same_position:
            raise Phase7EOperationalEvidenceError
        if not same_session or not same_position or not same_pixels:
            continue
        observation = next(
            (
                item
                for item in run.records
                if item.family == "observation"
                and item.payload.get("frame_id") == candidate.identity
            ),
            None,
        )
        if observation is None:
            raise Phase7EAdapterError
        operation = records.get(str(observation.payload["classification_operation_id"]))
        if operation is None or operation.family != "classification-operation":
            raise Phase7EAdapterError
        return candidate, operation, observation
    return None


def _admit_alias_observation(  # noqa: PLR0913 - explicit persisted authority inputs.
    repository: RecordingSearch7ERepository,
    invocation: Phase7EInvocation,
    current: Phase7ERun,
    target: StrictIdentityEnvelope,
    decoder: StrictIdentityEnvelope,
    authority: tuple[StrictIdentityEnvelope, StrictIdentityEnvelope, StrictIdentityEnvelope],
) -> Phase7ERun:
    """Persist one explicit alias without reclassifying or adding distinct evidence."""
    if not isinstance(current.state, Schema6Envelope):
        raise Phase7EAdapterError
    frame, operation, observation = authority
    alias = make_alias_envelope(
        invocation.request,
        target.identity,
        frame.identity,
        str(observation.payload["target_request_id"]),
    )
    decoder_indexed = (
        decoder.identity in current.manifest.payload["indexes"]["decoder_operation_ids"]
    )
    manifest = append_schema6_indexes(
        current.manifest,
        **(
            {"alias_ids": alias.identity}
            if decoder_indexed
            else {
                "decoder_operation_ids": decoder.identity,
                "alias_ids": alias.identity,
            }
        ),
    )
    state = Schema6Envelope(
        run_state="RUNNING",
        target_state=Schema6TargetState.OBSERVED,
        active_target_request_id=target.identity,
        active_decoder_operation_id=decoder.identity,
        active_frame_id=frame.identity,
        active_classification_attempt_id=None,
        active_classification_operation_id=operation.identity,
        active_observation_id=observation.identity,
        reason_code=None,
        attempt_count=1,
        predecessor_target_state=Schema6TargetState.REQUESTED,
    )
    records = tuple(item for item in current.records if item.family != "schema5-manifest")
    additions = (alias,) if decoder_indexed else (decoder, alias)
    result = repository.admit_schema6(
        current.investigation_id,
        current.run_id,
        manifest,
        state,
        (*records, *additions),
        expected_manifest_id=current.manifest_id,
        ownership=invocation.ownership,
    ).run
    invocation.validate(repository)
    return repository.reopen_schema6(
        result.investigation_id,
        result.run_id,
        ownership=invocation.ownership,
    )


def _parse_whole_text(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise Phase7EAdapterError from exc
    if _whole_text(parsed) != value:
        raise Phase7EAdapterError
    return parsed


@dataclass(frozen=True, slots=True)
class _C1PolicyView:
    request: CommonSessionRequest
    payload: dict[str, Any]

    @property
    def search_start_utc(self) -> datetime:
        return self.request.start_utc

    @property
    def search_end_utc(self) -> datetime:
        return self.request.end_utc

    @property
    def maximum_requested_span_seconds(self) -> int:
        return int(self.payload["maximum_search_duration_seconds"])

    @property
    def coarse_interval_seconds(self) -> int:
        return int(self.payload["coarse_interval_seconds"])

    @property
    def absence_confirmation_frames(self) -> int:
        return int(self.payload["support_count"])

    @property
    def absence_cadence_seconds(self) -> int:
        return int(self.payload["support_cadence_seconds"])

    @property
    def maximum_consecutive_indeterminate_targets(self) -> int:
        return int(self.payload["maximum_consecutive_indeterminate_targets"])

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        if mode != "json":
            raise ValueError
        return {
            "search_start_utc": _whole_text(self.search_start_utc),
            "search_end_utc": _whole_text(self.search_end_utc),
            "maximum_requested_span_seconds": self.maximum_requested_span_seconds,
            "coarse_interval_seconds": self.coarse_interval_seconds,
            "absence_confirmation_frames": self.absence_confirmation_frames,
            "absence_cadence_seconds": self.absence_cadence_seconds,
            "maximum_consecutive_indeterminate_targets": (
                self.maximum_consecutive_indeterminate_targets
            ),
        }


def _whole_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise Phase7EAdapterError
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "Phase7E1DError",
    "Phase7E1DService",
    "Phase7EAdapterError",
    "Phase7EC1PlannerAdapter",
    "Phase7ECoarsePlanBundle",
    "Phase7ED2DecisionAdapter",
    "Phase7EIncompleteEvidenceError",
    "Phase7ELocalEvidenceAdapter",
    "Phase7EOperationalEvidenceError",
    "Phase7EStatus",
    "Phase7ETerminalDecision",
    "Phase7ETerminalReason",
    "TerminalDecisionBoundary",
    "build_evidence_snapshot",
    "build_schema7_manifest",
    "build_source_record_set",
    "build_terminal_result",
    "maximum_phase7e_narrowing_iterations",
    "read_phase7_status",
]
