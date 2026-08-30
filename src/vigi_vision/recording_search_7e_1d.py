# pyright: reportAny=false, reportExplicitAny=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
# ruff: noqa: C901
"""Phase 7E-1D request-relative planning and immutable terminal composition.

The adapters in this module do not acquire a recording or classify pixels.
They consume only strictly reopened schema-6 records admitted by 1C, reuse the
existing C1 target-grid and D1 iteration policies, and publish the closed
Schema-7 identity family through the existing Phase 7E repository.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, cast

from vigi_vision.object_presence_values import ClassificationOutcome, VisualReason
from vigi_vision.recording_search_7e_1c import (
    B4Bridge,
    CommonSessionAcquisition,
    CommonSessionCapacityError,
    CommonSessionDeadlineError,
    CommonSessionDecoderError,
    CommonSessionError,
    CommonSessionRequest,
    DecodedLocalFrame,
    Decoder,
    Phase7EInvocation,
    admit_decoder_failure,
    admit_decoder_operation,
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
    Phase7EInProgressError,
    Phase7ENotFoundError,
    Phase7ERun,
    PublicationResult,
    PublicationStatus,
    RecordingSearch7ERepository,
)
from vigi_vision.recording_search_7e_validation import Schema6Envelope
from vigi_vision.recording_search_a2_models import ProbeFrameRequestRecord, ProbeRequestStatus
from vigi_vision.recording_search_b4_models import (
    ClassificationPublicationOutcome,
    PublishedClassificationResult,
)
from vigi_vision.recording_search_c1_models import (
    CoarseSampleResult,
    CoarseSampleStatus,
    CoarseSamplingResult,
    CoarseSupportResult,
)
from vigi_vision.recording_search_c1_planner import (
    CoarseSamplingIdentity,
    SupportDirection,
    build_coarse_sampling_plan,
    confirmation_run_id_for,
    support_target_times,
)
from vigi_vision.recording_search_c2_interpreter import interpret_coarse_evidence
from vigi_vision.recording_search_c2_models import (
    CoarseCandidateBracket,
    CoarseEvidenceSnapshot,
    CoarseInterpretationStatus,
    CoarseTargetEvidence,
)
from vigi_vision.recording_search_c2_support import coarse_target_id
from vigi_vision.recording_search_d1_history import (
    D1BracketState,
    history_digest,
    narrowed_bracket_id,
)
from vigi_vision.recording_search_d1_identity import (
    D1SourceRevision,
    d1_input_bracket_id,
    policy_identity,
    source_bracket_identity,
)
from vigi_vision.recording_search_d1_models import (
    NarrowingBoundEvidence,
    NarrowingProbeEvidence,
    NarrowingResult,
    NarrowingState,
    NarrowingStatus,
)
from vigi_vision.recording_search_d1_planner import maximum_narrowing_iterations
from vigi_vision.recording_search_d1_service import BinaryNarrowingService
from vigi_vision.recording_search_d2_c2_adapter import adapt_c2_result
from vigi_vision.recording_search_d2_d1_adapter import adapt_d1_result
from vigi_vision.recording_search_d2_enums import D2EvidenceRole
from vigi_vision.recording_search_d2_evidence import (
    D2EvidenceReference,
    D2EvidenceSnapshot,
    D2SourceRevision,
    D2SupportGroup,
)
from vigi_vision.recording_search_d2_terminal_interpreter import interpret_terminal
from vigi_vision.recording_search_d2_terminal_models import (
    FoundResult,
    InconclusiveResult,
    NotFoundResult,
    OperationalOutcome,
    TerminalInputSnapshot,
    TerminalResultKind,
)
from vigi_vision.recording_search_models import RecordingSearchPolicy, default_policy

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from vigi_vision.recording_search_b3_models import ClassifyRecordingProbeRequest

_DIGEST_HEX_LENGTH = 64


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

    def execute(  # noqa: PLR0912, PLR0915
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
        pending_by_identity: dict[str, StrictIdentityEnvelope] = {}
        for target in targets:
            invocation.validate(self.repository)
            current = self.repository.reopen_schema6(
                invocation.request.investigation_id,
                invocation.request.run_id,
                ownership=invocation.ownership,
            )
            if current.manifest.payload.get("common_session_id") != session_id:
                raise Phase7EAdapterError
            existing = next(
                (
                    item
                    for item in current.records
                    if item.family == "target-request" and item.identity == target.identity
                ),
                None,
            )
            if existing is not None and existing != target:
                raise Phase7EAdapterError
            if _target_is_admitted(current, target.identity):
                continue
            _validate_target_for_session(current, target, invocation.request)
            previous = pending_by_identity.get(target.identity)
            if previous is not None:
                if previous != target:
                    raise Phase7EAdapterError
                continue
            pending_by_identity[target.identity] = target
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
        current = self.repository.reopen_schema6(
            invocation.request.investigation_id,
            invocation.request.run_id,
            ownership=invocation.ownership,
        )
        current = _request_schema6_targets(
            self.repository,
            invocation,
            current,
            pending,
            session_id=session_id,
        )
        if (
            isinstance(current.state, Schema6Envelope)
            and current.state.target_state is Schema6TargetState.OBSERVED
        ):
            current = _request_schema6_target(self.repository, invocation, current, pending[0])
        decoder_operation: StrictIdentityEnvelope
        if (
            isinstance(current.state, Schema6Envelope)
            and current.state.target_state is Schema6TargetState.DECODING
        ):
            active_operation_id = current.state.active_decoder_operation_id
            persisted_operation = next(
                (
                    item
                    for item in current.records
                    if item.family == "decoder-operation" and item.identity == active_operation_id
                ),
                None,
            )
            if persisted_operation is None:
                raise Phase7EAdapterError
            decoder_operation = persisted_operation
            persisted_pass = decoder_operation.payload.get("pass_number")
            if type(persisted_pass) is not int or persisted_pass <= 0:
                raise Phase7EAdapterError
            pass_number = persisted_pass
            if invocation.budget.decoder_passes == 0:
                prior_operations = tuple(
                    item
                    for item in current.records
                    if item.family == "decoder-operation"
                    and item.identity != decoder_operation.identity
                )
                invocation.budget.decoder_passes = len(prior_operations)
                invocation.budget.selected_rgb24_frames = sum(
                    len(item.payload.get("target_request_ids", ())) for item in prior_operations
                )
        else:
            decoder_operation = make_decoder_envelope(
                acquisition,
                pass_number,
                tuple(item.identity for item in pending),
            )
        if pass_number > invocation.request.policy.maximum_decoder_passes:
            raise CommonSessionCapacityError
        invocation.validate(self.repository)
        current = admit_decoder_operation(
            self.repository,
            acquisition,
            pending[0],
            decoder_operation,
            invocation=invocation,
        )
        try:
            # Keep post-admission authority/budget checks inside the failure
            # boundary so a deadline or cancellation after durable DECODING
            # intent is attributed to that exact operation before returning.
            invocation.validate(self.repository)
            frames = execute_local_targets(
                acquisition,
                self.decoder,
                ordered,
                pass_number=pass_number,
                logical_end=any(
                    item.payload["selection_rule"] == "FINAL_STRICTLY_BEFORE_END"
                    for item in pending
                ),
                allow_aliases=True,
                budget=invocation.budget,
            )
        except CommonSessionError as primary:
            with suppress(Exception):
                _ = admit_decoder_failure(
                    self.repository,
                    acquisition,
                    pending[0],
                    decoder_operation,
                    primary.code,
                    invocation=invocation,
                )
            raise
        except Exception as unexpected:
            primary = CommonSessionDecoderError()
            with suppress(Exception):
                _ = admit_decoder_failure(
                    self.repository,
                    acquisition,
                    pending[0],
                    decoder_operation,
                    primary.code,
                    invocation=invocation,
                )
            raise primary from unexpected
        frame_by_requested_time = dict(zip(ordered, frames, strict=True))
        for target in pending:
            frame = frame_by_requested_time[requested_by_target[target.identity]]
            invocation.validate(self.repository)
            current = self.repository.reopen_schema6(
                invocation.request.investigation_id,
                invocation.request.run_id,
                ownership=invocation.ownership,
            )
            if not isinstance(current.state, Schema6Envelope):
                raise Phase7EAdapterError
            if current.state.target_state is Schema6TargetState.OBSERVED:
                current = _request_schema6_target(self.repository, invocation, current, target)
            elif current.state.target_state not in {
                Schema6TargetState.REQUESTED,
                Schema6TargetState.DECODING,
            }:
                raise Phase7EAdapterError
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
            cast("RecordingSearchPolicy", cast("object", legacy)),
            support_direction=SupportDirection.BACKWARD_FROM_END,
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
            for sequence, target in enumerate(targets)
        )
        support_times = support_target_times(existing_plan, request.end_utc)
        if not support_times or support_times[0] < request.start_utc:
            raise Phase7EIncompleteEvidenceError
        origin = coarse[-1].identity
        support = tuple(
            make_target_envelope(
                request,
                plan.identity,
                len(coarse) + index,
                target,
                kind="SUPPORT",
                selection_rule="NEAREST_IN_HALF_OPEN_SESSION",
                origin_target_request_id=origin,
            )
            for index, target in enumerate(support_times)
        )
        return Phase7ECoarsePlanBundle(plan, coarse, support)


@dataclass(frozen=True, slots=True)
class Phase7EAdaptiveOrchestrator:
    """Compose existing C1, C2, D1, and D2 over one retained local session."""

    repository: RecordingSearch7ERepository
    local_evidence: Phase7ELocalEvidenceAdapter

    def execute(  # noqa: PLR0915 - explicit composition of approved boundaries.
        self,
        invocation: Phase7EInvocation,
        acquisition: CommonSessionAcquisition,
    ) -> Phase7ERun:
        """Progress an actual 1C Schema-6 run to strictly persisted D2 evidence."""
        invocation.validate(self.repository)
        run = self.repository.reopen_schema6(
            invocation.request.investigation_id,
            invocation.request.run_id,
            ownership=invocation.ownership,
        )
        _require_acquisition_binding(run, invocation, acquisition)
        policy_record = _one_family(run, "policy")
        policy = _legacy_policy(invocation.request, policy_record)
        plan = build_coarse_sampling_plan(
            policy,
            support_direction=SupportDirection.BACKWARD_FROM_END,
        )
        schema5 = _one_family(run, "schema5-manifest")
        bundle = Phase7EC1PlannerAdapter().build(invocation.request, policy_record)
        if bundle.plan.identity != run.manifest.payload["plan_id"] or tuple(
            schema5.payload["coarse_target_request_ids"]
        ) != tuple(item.identity for item in bundle.coarse_targets):
            raise Phase7ECorruptError
        coarse = bundle.coarse_targets
        logical_end = next(
            item for item in coarse if item.payload["selection_rule"] == "FINAL_STRICTLY_BEFORE_END"
        )
        preceding_coarse = tuple(item for item in coarse if item.identity != logical_end.identity)
        run = self.local_evidence.execute(invocation, acquisition, preceding_coarse)
        # Decode backward support first so E aliases the authoritative E-cadence
        # frame. Existing C2/D2 support evidence therefore remains non-alias and
        # the logical endpoint still resolves to the last eligible frame before E.
        run = self.local_evidence.execute(
            invocation,
            acquisition,
            (*bundle.final_support_targets, logical_end),
        )
        if _target_outcome(run, logical_end.identity) == ClassificationOutcome.ABSENT.value:
            support = bundle.final_support_targets
            expected = support_target_times(plan, invocation.request.end_utc)
            if (
                tuple(
                    _parse_whole_text(str(item.payload["requested_time_utc"])) for item in support
                )
                != expected
            ):
                raise Phase7EIncompleteEvidenceError
            if any(not _target_is_admitted(run, item.identity) for item in support):
                raise Phase7EIncompleteEvidenceError
        c2_snapshot = _build_c2_snapshot(run, invocation.request, policy, plan)
        c2 = interpret_coarse_evidence(c2_snapshot)
        if c2.status is CoarseInterpretationStatus.BRACKET_READY:
            if c2.bracket is None:
                raise Phase7EAdapterError
            run, bracket, c2_record = _persist_c2_bracket(
                self.repository,
                invocation,
                run,
                c2.bracket,
            )
            handle = _Phase7ENarrowingHandle(
                invocation,
                acquisition,
                self.local_evidence,
                baseline_identity=c2_snapshot.identity.baseline_identity,
            )
            host = _Phase7ENarrowingHost(handle)
            store = _Phase7ENarrowingStore(handle, bracket, policy)
            narrowing = BinaryNarrowingService(host, store).narrow(handle, bracket, policy)
            if narrowing.status is not NarrowingStatus.READY or narrowing.narrowed_bracket is None:
                raise Phase7EIncompleteEvidenceError
            run, phase7_support_id = _persist_d1_result(
                self.repository,
                invocation,
                c2_record,
                narrowing,
            )
            terminal_narrowing = _rebase_narrowing_for_terminal(
                narrowing,
                _manifest_digest(run),
            )
            terminal_bracket_value = terminal_narrowing.narrowed_bracket
            if terminal_bracket_value is None:
                raise Phase7EIncompleteEvidenceError
            terminal_bracket = replace(
                bracket,
                manifest_digest=terminal_bracket_value.manifest_digest,
            )
            persisted_c2 = replace(c2, bracket=terminal_bracket)
            d2_snapshot = _build_d2_snapshot(
                run,
                c2_snapshot,
                persisted_c2,
                terminal_narrowing,
                policy,
            )
            terminal_input = TerminalInputSnapshot(
                d2_snapshot,
                plan,
                policy,
                adapt_c2_result(persisted_c2, d2_snapshot),
                adapt_d1_result(terminal_narrowing, d2_snapshot),
                terminal_bracket_value.d1_input_bracket,
            )
            d2 = interpret_terminal(terminal_input)
            if not isinstance(d2, FoundResult):
                raise Phase7EOperationalEvidenceError
            narrowed_id = run.manifest.payload["indexes"]["narrowed_bracket_ids"][-1]
            narrowed_record = next(item for item in run.records if item.identity == narrowed_id)
            if narrowed_record.payload["upper_support_group_id"] != phase7_support_id:
                raise Phase7EAdapterError
            return run
        d2_snapshot = _build_d2_snapshot(run, c2_snapshot, c2, None, policy)
        d2 = interpret_terminal(
            TerminalInputSnapshot(
                d2_snapshot,
                plan,
                policy,
                adapt_c2_result(c2, d2_snapshot),
            )
        )
        if not isinstance(d2, (NotFoundResult, InconclusiveResult)):
            if isinstance(d2, OperationalOutcome):
                raise Phase7EOperationalEvidenceError
            raise Phase7EIncompleteEvidenceError
        return run


@dataclass(slots=True)
class _Phase7ENarrowingHandle:
    invocation: Phase7EInvocation
    acquisition: CommonSessionAcquisition
    local_evidence: Phase7ELocalEvidenceAdapter
    baseline_identity: str
    target_by_time: dict[datetime, StrictIdentityEnvelope] = field(default_factory=dict)

    @property
    def investigation_id(self) -> str:
        return self.invocation.request.investigation_id

    @property
    def search_run_id(self) -> str:
        return self.invocation.request.run_id

    @property
    def phase6_confirmation_id(self) -> str:
        return self.invocation.request.investigation_id

    @property
    def closed(self) -> bool:
        return not self.invocation.ownership.active


@dataclass(frozen=True, slots=True)
class _Phase7ENarrowingHost:
    handle: _Phase7ENarrowingHandle

    @contextmanager
    def a2_mutation(self, handle: _Phase7ENarrowingHandle) -> Generator[None, None, None]:
        _require_same_handle(self.handle, handle)
        handle.invocation.validate(handle.local_evidence.repository)
        yield
        handle.invocation.validate(handle.local_evidence.repository)

    def acquire_targets(
        self,
        handle: _Phase7ENarrowingHandle,
        requested_times: tuple[datetime, ...],
    ) -> tuple[ProbeFrameRequestRecord, ...]:
        _require_same_handle(self.handle, handle)
        run = handle.local_evidence.repository.reopen_schema6(
            handle.investigation_id,
            handle.search_run_id,
            ownership=handle.invocation.ownership,
        )
        targets: list[StrictIdentityEnvelope] = []
        origin: StrictIdentityEnvelope | None = None
        for requested in requested_times:
            target = _target_at(run, requested)
            if target is None:
                sequence = (
                    max(
                        int(item.payload["sequence"])
                        for item in run.records
                        if item.family == "target-request"
                    )
                    + 1
                )
                target = make_target_envelope(
                    handle.invocation.request,
                    str(run.manifest.payload["plan_id"]),
                    sequence,
                    requested,
                    kind="BINARY" if origin is None else "SUPPORT",
                    selection_rule="NEAREST_IN_HALF_OPEN_SESSION",
                    origin_target_request_id=None if origin is None else origin.identity,
                )
            if origin is None:
                origin = target
            handle.target_by_time[requested] = target
            targets.append(target)
        _ = handle.local_evidence.execute(handle.invocation, handle.acquisition, tuple(targets))
        return tuple(_synthetic_probe_request(handle, target) for target in targets)

    def classify(
        self,
        handle: _Phase7ENarrowingHandle,
        request: ClassifyRecordingProbeRequest,
    ) -> PublishedClassificationResult:
        _require_same_handle(self.handle, handle)
        run = handle.local_evidence.repository.reopen_schema6(
            handle.investigation_id,
            handle.search_run_id,
            ownership=handle.invocation.ownership,
        )
        evidence = _target_evidence(run, request.probe_request_id, request.probe_request_id)
        if (
            evidence.state is None
            or evidence.observation_id is None
            or evidence.canonical_frame_id is None
        ):
            raise Phase7EOperationalEvidenceError
        observation = next(item for item in run.records if item.identity == evidence.observation_id)
        reason = observation.payload["reason_code"]
        return PublishedClassificationResult(
            ClassificationPublicationOutcome.REUSED,
            evidence.observation_id,
            evidence.alias_id,
            request.probe_request_id,
            evidence.canonical_frame_id,
            evidence.state,
            None if reason is None else VisualReason(str(reason)),
        )


@dataclass(frozen=True, slots=True)
class _Phase7ENarrowingStore:
    handle: _Phase7ENarrowingHandle
    bracket: CoarseCandidateBracket
    policy: RecordingSearchPolicy

    def validate_bracket(
        self,
        handle: _Phase7ENarrowingHandle,
        bracket: CoarseCandidateBracket,
        policy: RecordingSearchPolicy,
    ) -> None:
        _require_same_handle(self.handle, handle)
        run = _strict_schema6(handle)
        if (
            bracket != self.bracket
            or policy != self.policy
            or _manifest_digest(run) != bracket.manifest_digest
        ):
            raise ValueError

    def load_state(
        self,
        handle: _Phase7ENarrowingHandle,
        bracket: CoarseCandidateBracket,
    ) -> NarrowingState:
        self.validate_bracket(handle, bracket, self.policy)
        run = _strict_schema6(handle)
        lower = (
            NarrowingBoundEvidence(
                target_id="source-baseline",
                requested_time_utc=bracket.last_present_requested_time_utc,
                state=ClassificationOutcome.PRESENT,
                observation_id=bracket.last_present_observation_id,
                probe_request_id=None,
                canonical_frame_id=None,
                operation_id=None,
                decode_session_id=None,
                decoded_frame_utc=None,
                decoded_pts=None,
                decoded_ordinal=None,
                is_baseline=True,
            )
            if bracket.last_present_is_baseline
            else _bound_from_target(
                _target_evidence(
                    run,
                    str(bracket.last_present_probe_request_id),
                    str(bracket.last_present_target_id or bracket.last_present_probe_request_id),
                )
            )
        )
        upper = tuple(
            _bound_from_target(_target_evidence(run, target_id, target_id))
            for target_id in bracket.support_probe_request_ids
        )
        return NarrowingState(
            handle.investigation_id,
            handle.search_run_id,
            handle.phase6_confirmation_id,
            handle.baseline_identity,
            source_bracket_identity(bracket),
            self.policy.policy_version,
            bracket.last_present_requested_time_utc,
            bracket.first_absent_requested_time_utc,
            lower,
            upper,
            (),
            (),
            0,
            _manifest_digest(run),
        )

    def find_existing(
        self,
        handle: _Phase7ENarrowingHandle,
        requested_time_utc: datetime,
        target_id: str,
    ) -> NarrowingProbeEvidence | None:
        _require_same_handle(self.handle, handle)
        run = _strict_schema6(handle)
        target = handle.target_by_time.get(requested_time_utc) or _target_at(
            run, requested_time_utc
        )
        if target is None or not _target_is_admitted(run, target.identity):
            return None
        handle.target_by_time[requested_time_utc] = target
        return _target_evidence(run, target.identity, target_id)

    def resolve_request(
        self,
        handle: _Phase7ENarrowingHandle,
        request: ProbeFrameRequestRecord,
        target_id: str,
    ) -> NarrowingProbeEvidence:
        _require_same_handle(self.handle, handle)
        return _target_evidence(_strict_schema6(handle), request.probe_request_id, target_id)

    def current_manifest_digest(self, handle: _Phase7ENarrowingHandle) -> str:
        _require_same_handle(self.handle, handle)
        return _manifest_digest(_strict_schema6(handle))


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


def _require_acquisition_binding(
    run: Phase7ERun,
    invocation: Phase7EInvocation,
    acquisition: CommonSessionAcquisition,
) -> None:
    if (
        acquisition.request != invocation.request
        or acquisition.common_session_id != run.manifest.payload.get("common_session_id")
        or acquisition.session.identity != acquisition.common_session_id
        or acquisition.retained_mp4_path is None
    ):
        raise Phase7EAdapterError


def _one_family(run: Phase7ERun, family: str) -> StrictIdentityEnvelope:
    values = tuple(item for item in run.records if item.family == family)
    if len(values) != 1:
        raise Phase7ECorruptError
    return values[0]


def _legacy_policy(
    request: CommonSessionRequest,
    policy: StrictIdentityEnvelope,
) -> RecordingSearchPolicy:
    if policy.family != "policy":
        raise Phase7EAdapterError
    return default_policy(request.start_utc, request.end_utc).model_copy(
        update={
            "maximum_requested_span_seconds": int(
                policy.payload["maximum_search_duration_seconds"]
            ),
            "coarse_interval_seconds": int(policy.payload["coarse_interval_seconds"]),
            "binary_stop_resolution_seconds": int(policy.payload["binary_stop_seconds"]),
            "absence_confirmation_frames": int(policy.payload["support_count"]),
            "absence_cadence_seconds": int(policy.payload["support_cadence_seconds"]),
            "maximum_consecutive_indeterminate_targets": int(
                policy.payload["maximum_consecutive_indeterminate_targets"]
            ),
        }
    )


def _target_at(run: Phase7ERun, requested: datetime) -> StrictIdentityEnvelope | None:
    matches = tuple(
        item
        for item in run.records
        if item.family == "target-request"
        and _parse_whole_text(str(item.payload["requested_time_utc"])) == requested
    )
    admitted = tuple(item for item in matches if _target_is_admitted(run, item.identity))
    candidates = admitted or matches
    if not candidates:
        return None
    binary = tuple(item for item in candidates if item.payload["kind"] == "BINARY")
    selected = binary or candidates
    if len(selected) != 1:
        raise Phase7ECorruptError
    return selected[0]


def _target_outcome(run: Phase7ERun, target_id: str) -> str:
    return _target_binding(run, target_id)[2].payload["outcome"]


def _target_binding(
    run: Phase7ERun,
    target_id: str,
) -> tuple[
    StrictIdentityEnvelope,
    StrictIdentityEnvelope,
    StrictIdentityEnvelope,
    StrictIdentityEnvelope | None,
]:
    aliases = tuple(
        item
        for item in run.records
        if item.family == "alias" and item.payload["target_request_id"] == target_id
    )
    if len(aliases) > 1:
        raise Phase7ECorruptError
    alias = aliases[0] if aliases else None
    canonical_target = (
        str(alias.payload["alias_of_target_request_id"]) if alias is not None else target_id
    )
    observations = tuple(
        item
        for item in run.records
        if item.family == "observation" and item.payload["target_request_id"] == canonical_target
    )
    if len(observations) != 1:
        raise Phase7ECorruptError
    observation = observations[0]
    frame = next(
        (item for item in run.records if item.identity == observation.payload["frame_id"]),
        None,
    )
    operation = next(
        (
            item
            for item in run.records
            if item.identity == observation.payload["classification_operation_id"]
        ),
        None,
    )
    if frame is None or frame.family != "frame" or operation is None:
        raise Phase7ECorruptError
    decoder = next(
        (item for item in run.records if item.identity == frame.payload["decoder_operation_id"]),
        None,
    )
    if decoder is None or decoder.family != "decoder-operation":
        raise Phase7ECorruptError
    return frame, operation, observation, alias


def _target_evidence(
    run: Phase7ERun,
    target_request_id: str,
    target_id: str,
) -> NarrowingProbeEvidence:
    target = next(
        (item for item in run.records if item.identity == target_request_id),
        None,
    )
    if target is None or target.family != "target-request":
        raise Phase7ECorruptError
    frame, operation, observation, alias = _target_binding(run, target_request_id)
    return NarrowingProbeEvidence(
        target_id=target_id,
        requested_time_utc=_parse_whole_text(str(target.payload["requested_time_utc"])),
        status=CoarseSampleStatus.SUCCESS,
        state=ClassificationOutcome(str(observation.payload["outcome"])),
        probe_request_id=target_request_id,
        observation_id=observation.identity,
        alias_id=None if alias is None else alias.identity,
        canonical_frame_id=frame.identity,
        operation_id=str(frame.payload["decoder_operation_id"]),
        classification_operation_id=operation.identity,
        decode_session_id=str(frame.payload["common_session_id"]),
        decoded_frame_utc=_parse_whole_text(str(frame.payload["estimated_requested_time_utc"])),
        decoded_pts=int(frame.payload["raw_pts"]),
        decoded_ordinal=int(frame.payload["ordinal"]),
    )


def _bound_from_target(value: NarrowingProbeEvidence) -> NarrowingBoundEvidence:
    if value.state is None or value.observation_id is None:
        raise Phase7ECorruptError
    return NarrowingBoundEvidence(
        value.target_id,
        value.requested_time_utc,
        value.state,
        value.observation_id,
        value.probe_request_id,
        value.canonical_frame_id,
        value.operation_id,
        value.decode_session_id,
        value.decoded_frame_utc,
        value.decoded_pts,
        value.decoded_ordinal,
    )


def _coarse_evidence(
    run: Phase7ERun,
    target: StrictIdentityEnvelope,
    *,
    origin: datetime | None = None,
    confirmation_id: str | None = None,
    identity: CoarseSamplingIdentity | None = None,
) -> CoarseTargetEvidence:
    evidence = _target_evidence(run, target.identity, target.identity)
    return CoarseTargetEvidence(
        evidence.requested_time_utc,
        evidence.status,
        evidence.state,
        evidence.probe_request_id,
        evidence.observation_id,
        evidence.canonical_frame_id,
        evidence.decode_session_id,
        evidence.decoded_frame_utc,
        evidence.decoded_pts,
        evidence.decoded_ordinal,
        evidence.alias_id is not None,
        origin,
        confirmation_id,
        identity,
    )


def _build_c2_snapshot(
    run: Phase7ERun,
    request: CommonSessionRequest,
    policy: RecordingSearchPolicy,
    plan: object,
) -> CoarseEvidenceSnapshot:
    if not run.is_schema6:
        raise Phase7ECorruptError
    typed_plan = cast("Any", plan)
    operations = tuple(item for item in run.records if item.family == "classification-operation")
    baselines = {str(item.payload["baseline_identity"]) for item in operations}
    if len(baselines) != 1:
        raise Phase7EIncompleteEvidenceError
    baseline_identity = baselines.pop()
    identity = CoarseSamplingIdentity(
        run.investigation_id,
        run.run_id,
        run.investigation_id,
        baseline_identity,
    )
    coarse_targets = tuple(_target_at(run, target) for target in typed_plan.target_times)
    if any(item is None for item in coarse_targets):
        raise Phase7EIncompleteEvidenceError
    coarse = tuple(cast("StrictIdentityEnvelope", item) for item in coarse_targets)
    evidence = tuple(_coarse_evidence(run, item) for item in coarse)
    initial_target = _target_at(run, request.start_utc)
    if initial_target is None:
        raise Phase7EIncompleteEvidenceError
    initial_evidence = _coarse_evidence(run, initial_target)
    if initial_evidence.classification is not ClassificationOutcome.PRESENT:
        raise Phase7EIncompleteEvidenceError
    samples = tuple(
        CoarseSampleResult(
            item.requested_time_utc,
            CoarseSampleStatus.SUCCESS,
            item.probe_request_id,
            item.classification,
        )
        for item in evidence
    )
    support_results: tuple[CoarseSupportResult, ...] = ()
    support_evidence: tuple[CoarseTargetEvidence, ...] = ()
    if evidence[-1].classification is ClassificationOutcome.ABSENT:
        expected = support_target_times(typed_plan, request.end_utc)
        support = tuple(_target_at(run, target) for target in expected)
        if any(item is None for item in support):
            raise Phase7EIncompleteEvidenceError
        typed_support = tuple(cast("StrictIdentityEnvelope", item) for item in support)
        confirmation_id = confirmation_run_id_for(typed_plan, request.end_utc, identity)
        support_evidence = tuple(
            _coarse_evidence(
                run,
                item,
                origin=request.end_utc,
                confirmation_id=confirmation_id,
                identity=identity,
            )
            for item in typed_support
        )
        support_samples = tuple(
            CoarseSampleResult(
                item.requested_time_utc,
                CoarseSampleStatus.SUCCESS,
                item.probe_request_id,
                item.classification,
            )
            for item in support_evidence
        )
        support_results = (
            CoarseSupportResult(
                identity,
                request.end_utc,
                confirmation_id,
                tuple(range(len(support_samples))),
                support_samples,
            ),
        )
    execution = CoarseSamplingResult(
        identity,
        typed_plan,
        samples,
        complete=True,
        support_results=support_results,
    )
    return CoarseEvidenceSnapshot(
        run.investigation_id,
        run.run_id,
        identity,
        typed_plan,
        policy.policy_version,
        typed_plan.absence_confirmation_frames,
        typed_plan.absence_cadence_seconds,
        baseline_identity,
        _manifest_digest(run),
        execution,
        (initial_evidence, *evidence, *support_evidence),
        typed_plan.maximum_consecutive_indeterminate_targets,
        None,
        initial_evidence,
    )


def _append_analysis_records(
    repository: RecordingSearch7ERepository,
    invocation: Phase7EInvocation,
    records: tuple[StrictIdentityEnvelope, ...],
) -> Phase7ERun:
    current = repository.reopen_schema6(
        invocation.request.investigation_id,
        invocation.request.run_id,
        ownership=invocation.ownership,
    )
    index_names = {
        "support-group": "support_group_ids",
        "c2-bracket": "c2_bracket_ids",
        "d1-input": "d1_input_ids",
        "d1-history": "d1_history_ids",
        "narrowed-bracket": "narrowed_bracket_ids",
    }
    existing = {item.identity for item in current.records}
    additions = tuple(item for item in records if item.identity not in existing)
    if not additions:
        return current
    if not isinstance(current.state, Schema6Envelope):
        raise Phase7ECorruptError
    manifest = append_schema6_indexes(
        current.manifest,
        **{index_names[item.family]: item.identity for item in additions},
    )
    children = tuple(item for item in current.records if item.family != "schema5-manifest")
    result = repository.admit_schema6(
        current.investigation_id,
        current.run_id,
        manifest,
        current.state,
        (*children, *additions),
        expected_manifest_id=current.manifest_id,
        ownership=invocation.ownership,
    ).run
    invocation.validate(repository)
    return repository.reopen_schema6(
        result.investigation_id,
        result.run_id,
        ownership=invocation.ownership,
    )


def _persist_c2_bracket(
    repository: RecordingSearch7ERepository,
    invocation: Phase7EInvocation,
    run: Phase7ERun,
    bracket: CoarseCandidateBracket,
) -> tuple[Phase7ERun, CoarseCandidateBracket, StrictIdentityEnvelope]:
    members = tuple(_target_evidence(run, item, item) for item in bracket.support_probe_request_ids)
    support = StrictIdentityEnvelope.from_payload(
        "support-group",
        {
            "investigation_id": run.investigation_id,
            "run_id": run.run_id,
            "origin_target_request_id": str(
                next(
                    item.payload["origin_target_request_id"]
                    for item in run.records
                    if item.identity == bracket.support_probe_request_ids[0]
                )
            ),
            "member_target_request_ids": [item.probe_request_id for item in members],
            "member_frame_ids": [item.canonical_frame_id for item in members],
            "member_observation_ids": [item.observation_id for item in members],
            "outcome": "SUPPORTED_ABSENT",
        },
    )
    c2_record = StrictIdentityEnvelope.from_payload(
        "c2-bracket",
        {
            "investigation_id": run.investigation_id,
            "run_id": run.run_id,
            "lower_observation_id": bracket.last_present_observation_id,
            "upper_observation_id": bracket.support_observation_ids[0],
            "upper_support_group_id": support.identity,
            "status": "BRACKET_READY",
        },
    )
    fresh = _append_analysis_records(repository, invocation, (support, c2_record))
    return (
        fresh,
        replace(
            bracket,
            manifest_digest=_manifest_digest(fresh),
            support_group_id=support.identity,
        ),
        c2_record,
    )


def _persist_d1_result(
    repository: RecordingSearch7ERepository,
    invocation: Phase7EInvocation,
    c2_record: StrictIdentityEnvelope,
    narrowing: NarrowingResult,
) -> tuple[Phase7ERun, str]:
    narrowed = narrowing.narrowed_bracket
    if narrowed is None or not narrowed.upper_support_evidence:
        raise Phase7EIncompleteEvidenceError
    run = repository.reopen_schema6(
        invocation.request.investigation_id,
        invocation.request.run_id,
        ownership=invocation.ownership,
    )
    support_members = narrowed.upper_support_evidence
    support = StrictIdentityEnvelope.from_payload(
        "support-group",
        {
            "investigation_id": run.investigation_id,
            "run_id": run.run_id,
            "origin_target_request_id": str(support_members[0].probe_request_id),
            "member_target_request_ids": [item.probe_request_id for item in support_members],
            "member_frame_ids": [item.canonical_frame_id for item in support_members],
            "member_observation_ids": [item.observation_id for item in support_members],
            "outcome": "SUPPORTED_ABSENT",
        },
    )
    d1_input = StrictIdentityEnvelope.from_payload(
        "d1-input",
        {
            "investigation_id": run.investigation_id,
            "run_id": run.run_id,
            "c2_bracket_id": c2_record.identity,
            "policy_id": run.manifest.payload["policy_id"],
        },
    )
    history = StrictIdentityEnvelope.from_payload(
        "d1-history",
        {
            "investigation_id": run.investigation_id,
            "run_id": run.run_id,
            "d1_input_id": d1_input.identity,
            "steps": [entry.to_payload() for entry in narrowing.history],
        },
    )
    stop_reason = {
        "target_precision_reached": "TARGET_PRECISION_REACHED",
        "no_distinct_midpoint": "NO_DISTINCT_MIDPOINT",
        "maximum_iterations": "MAXIMUM_ITERATIONS",
    }[narrowed.stop_reason.value]
    record = StrictIdentityEnvelope.from_payload(
        "narrowed-bracket",
        {
            "investigation_id": run.investigation_id,
            "run_id": run.run_id,
            "d1_input_id": d1_input.identity,
            "d1_history_id": history.identity,
            "lower_observation_id": narrowed.lower_evidence.observation_id,
            "upper_observation_id": support_members[0].observation_id,
            "upper_support_group_id": support.identity,
            "interval_start_requested_time_utc": _whole_text(narrowed.lower_bound_utc),
            "interval_end_requested_time_utc": _whole_text(narrowed.upper_bound_utc),
            "stop_reason": stop_reason,
        },
    )
    return _append_analysis_records(
        repository,
        invocation,
        (support, d1_input, history, record),
    ), support.identity


def _build_d2_snapshot(
    run: Phase7ERun,
    c2_snapshot: CoarseEvidenceSnapshot,
    c2_result: object,
    narrowing: NarrowingResult | None,
    policy: RecordingSearchPolicy,
) -> D2EvidenceSnapshot:
    evidence_manifest_digest = _manifest_digest(run)
    if narrowing is not None and narrowing.narrowed_bracket is not None:
        # D1 source identities are bound to the manifest revision captured when
        # C2 was persisted; appending D1 records must not rewrite that source
        # revision used by terminal validation.
        evidence_manifest_digest = narrowing.narrowed_bracket.manifest_digest
    baseline = D2EvidenceReference(
        role=D2EvidenceRole.BASELINE,
        target_id=None,
        requested_time_utc=c2_snapshot.plan.search_start_utc,
        acquisition_operation_id=None,
        probe_request_id=None,
        classification_operation_id=None,
        observation_id=c2_snapshot.baseline_observation_id,
        canonical_frame_id=None,
        alias_id=None,
        decode_session_id=None,
        decoded_frame_utc=None,
        decoded_pts=None,
        decoded_ordinal=None,
        support_group_id=None,
        support_index=None,
        is_phase6_baseline=True,
        classification=ClassificationOutcome.PRESENT,
    )
    coarse_refs: list[D2EvidenceReference] = []
    for item in c2_snapshot.targets:
        if item.origin_coarse_target_utc is not None or item.is_alias:
            continue
        coarse_refs.append(_d2_reference(run, item, D2EvidenceRole.COARSE_TARGET))
    d1_refs: list[D2EvidenceReference] = []
    support_refs: list[D2EvidenceReference] = []
    groups: list[D2SupportGroup] = []
    bracket = getattr(c2_result, "bracket", None)
    source_c2 = "phase7e-c2-" + evidence_manifest_digest
    if isinstance(bracket, CoarseCandidateBracket):
        source_c2 = source_bracket_identity(bracket)
        support_items = tuple(
            next(item for item in c2_snapshot.targets if item.probe_request_id == target_id)
            for target_id in bracket.support_probe_request_ids
        )
        group_id = str(bracket.support_group_id)
        support_refs.extend(
            _d2_reference(
                run,
                item,
                D2EvidenceRole.ABSENCE_SUPPORT,
                support_group_id=group_id,
                support_index=index,
            )
            for index, item in enumerate(support_items)
        )
        groups.append(_d2_group(group_id, support_refs[-len(support_items) :], c2_snapshot))
    source_d1 = source_c2
    if narrowing is not None and narrowing.narrowed_bracket is not None:
        narrowed = narrowing.narrowed_bracket
        source_d1 = narrowed.source_bracket_id
        used = {item.observation_id for item in (*coarse_refs, *support_refs)}
        final_observations = {item.observation_id for item in narrowed.upper_support_evidence}
        for evidence in narrowed.evidence:
            if (
                evidence.observation_id in used
                or evidence.observation_id in final_observations
                or evidence.alias_id is not None
            ):
                continue
            d1_refs.append(_d2_from_narrowing(evidence, D2EvidenceRole.D1_MIDPOINT))
            used.add(evidence.observation_id)
        final_members = narrowed.upper_support_evidence
        final_group = str(narrowed.upper_support_group_id)
        final_refs = tuple(
            _d2_from_bound(
                run,
                item,
                support_group_id=final_group,
                support_index=index,
            )
            for index, item in enumerate(final_members)
        )
        superseded_groups = {
            item.support_group_id
            for item in support_refs
            if item.observation_id in final_observations and item.support_group_id is not None
        }
        support_refs = [
            item
            for item in support_refs
            if item.support_group_id not in superseded_groups
            and item.observation_id not in {x.observation_id for x in final_refs}
        ]
        support_refs.extend(final_refs)
        groups = [
            item
            for item in groups
            if item.support_group_id != final_group
            and item.support_group_id not in superseded_groups
        ]
        groups.append(_d2_group(final_group, final_refs, c2_snapshot))
    return D2EvidenceSnapshot(
        run.investigation_id,
        run.run_id,
        run.investigation_id,
        c2_snapshot.baseline_observation_id,
        c2_snapshot.plan.plan_id,
        policy_identity(policy),
        D2SourceRevision(evidence_manifest_digest, source_c2, source_d1),
        (baseline, *coarse_refs, *d1_refs, *support_refs),
        tuple(groups),
    )


def _rebase_narrowing_for_terminal(
    narrowing: NarrowingResult,
    manifest_digest: str,
) -> NarrowingResult:
    """Bind the in-memory D2 proposal to the final persisted Schema-6 revision."""
    narrowed = narrowing.narrowed_bracket
    if narrowed is None or narrowed.d1_input_bracket is None or narrowed.source_bracket is None:
        raise Phase7EIncompleteEvidenceError
    source = replace(narrowed.source_bracket, manifest_digest=manifest_digest)
    source_id = source_bracket_identity(source)
    input_bracket = replace(
        narrowed.d1_input_bracket,
        source_revision=D1SourceRevision(source_id, manifest_digest),
    )
    input_id = d1_input_bracket_id(input_bracket)
    history = narrowed.history
    history_id = history_digest(input_bracket, input_id, history)
    lower_reference = (
        history[-1].bracket_after.lower_reference if history else input_bracket.lower_bound
    )
    final_bracket = D1BracketState(
        narrowed.lower_bound_utc,
        narrowed.upper_bound_utc,
        lower_reference,
        narrowed.upper_support_group_id or "",
    )
    narrowed_id = narrowed_bracket_id(
        input_bracket,
        history,
        final_bracket,
        history_id,
        narrowed.iterations,
        narrowed.achieved_precision_seconds,
        narrowed.stop_reason.value,
        manifest_digest,
        source_bracket=source,
    )
    return replace(
        narrowing,
        narrowed_bracket=replace(
            narrowed,
            source_bracket_id=source_id,
            manifest_digest=manifest_digest,
            d1_input_bracket=input_bracket,
            source_bracket=source,
            history_digest=history_id,
            narrowed_bracket_id=narrowed_id,
        ),
    )


def _d2_reference(
    run: Phase7ERun,
    value: CoarseTargetEvidence,
    role: D2EvidenceRole,
    *,
    support_group_id: str | None = None,
    support_index: int | None = None,
) -> D2EvidenceReference:
    evidence = _target_evidence(
        run,
        str(value.probe_request_id),
        coarse_target_id(
            run.investigation_id,
            run.run_id,
            value.requested_time_utc,
        ),
    )
    return _d2_from_narrowing(
        evidence,
        role,
        support_group_id=support_group_id,
        support_index=support_index,
    )


def _d2_from_narrowing(
    value: NarrowingProbeEvidence,
    role: D2EvidenceRole,
    *,
    support_group_id: str | None = None,
    support_index: int | None = None,
) -> D2EvidenceReference:
    return D2EvidenceReference(
        role=role,
        target_id=value.target_id,
        requested_time_utc=value.requested_time_utc,
        acquisition_operation_id=value.operation_id,
        probe_request_id=value.probe_request_id,
        classification_operation_id=value.classification_operation_id,
        observation_id=value.observation_id,
        canonical_frame_id=value.canonical_frame_id,
        alias_id=value.alias_id,
        decode_session_id=value.decode_session_id,
        decoded_frame_utc=value.decoded_frame_utc,
        decoded_pts=value.decoded_pts,
        decoded_ordinal=value.decoded_ordinal,
        support_group_id=support_group_id,
        support_index=support_index,
        is_phase6_baseline=False,
        classification=value.state,
    )


def _d2_from_bound(
    run: Phase7ERun,
    value: NarrowingBoundEvidence,
    *,
    support_group_id: str,
    support_index: int,
) -> D2EvidenceReference:
    observation = next(
        (item for item in run.records if item.identity == value.observation_id),
        None,
    )
    if observation is None or observation.family != "observation":
        raise Phase7ECorruptError
    return D2EvidenceReference(
        role=D2EvidenceRole.ABSENCE_SUPPORT,
        target_id=value.target_id,
        requested_time_utc=value.requested_time_utc,
        acquisition_operation_id=value.operation_id,
        probe_request_id=value.probe_request_id,
        classification_operation_id=str(observation.payload["classification_operation_id"]),
        observation_id=value.observation_id,
        canonical_frame_id=value.canonical_frame_id,
        alias_id=None,
        decode_session_id=value.decode_session_id,
        decoded_frame_utc=value.decoded_frame_utc,
        decoded_pts=value.decoded_pts,
        decoded_ordinal=value.decoded_ordinal,
        support_group_id=support_group_id,
        support_index=support_index,
        is_phase6_baseline=False,
        classification=value.state,
    )


def _d2_group(
    group_id: str,
    members: tuple[D2EvidenceReference, ...] | list[D2EvidenceReference],
    snapshot: CoarseEvidenceSnapshot,
) -> D2SupportGroup:
    values = tuple(members)
    return D2SupportGroup(
        group_id,
        str(values[0].target_id),
        len(values),
        snapshot.absence_cadence_seconds,
        str(values[0].decode_session_id),
        tuple(str(item.target_id) for item in values),
        tuple(str(item.observation_id) for item in values),
        tuple(str(item.canonical_frame_id) for item in values),
    )


def _synthetic_probe_request(
    handle: _Phase7ENarrowingHandle,
    target: StrictIdentityEnvelope,
) -> ProbeFrameRequestRecord:
    evidence = _target_evidence(_strict_schema6(handle), target.identity, target.identity)
    return ProbeFrameRequestRecord.model_construct(
        record_type="probe_frame_request",
        probe_request_id=target.identity,
        investigation_id=handle.investigation_id,
        search_run_id=handle.search_run_id,
        operation_id=str(evidence.operation_id),
        channel_id=handle.invocation.request.channel_id,
        requested_time_utc=evidence.requested_time_utc,
        status=ProbeRequestStatus.SUCCEEDED,
        canonical_frame_id=evidence.canonical_frame_id,
        alias_of_probe_request_id=evidence.alias_id,
        failure_reason=None,
        created_at_utc=evidence.requested_time_utc,
        completed_at_utc=evidence.requested_time_utc,
    )


def _strict_schema6(handle: _Phase7ENarrowingHandle) -> Phase7ERun:
    handle.invocation.validate(handle.local_evidence.repository)
    return handle.local_evidence.repository.reopen_schema6(
        handle.investigation_id,
        handle.search_run_id,
        ownership=handle.invocation.ownership,
    )


def _require_same_handle(
    expected: _Phase7ENarrowingHandle,
    actual: _Phase7ENarrowingHandle,
) -> None:
    if actual is not expected:
        raise Phase7EAdapterError


def _manifest_digest(run: Phase7ERun) -> str:
    digest = run.manifest_id.rsplit("-", 1)[-1]
    if len(digest) != _DIGEST_HEX_LENGTH:
        raise Phase7ECorruptError
    return digest


def _operation_check(
    invocation: Phase7EInvocation,
    timeout_seconds: float,
) -> Callable[[], None]:
    deadline = invocation.budget.monotonic_clock() + timeout_seconds

    def check() -> None:
        invocation.validate(invocation.ownership.repository)
        if invocation.budget.monotonic_clock() >= deadline:
            raise CommonSessionDeadlineError

    return check


def _lazy_operation_check(
    invocation: Phase7EInvocation,
    ceiling_seconds: float,
) -> Callable[[], None]:
    active: Callable[[], None] | None = None

    def check() -> None:
        nonlocal active
        if active is None:
            timeout = invocation.budget.operation_timeout(
                ceiling_seconds,
                minimum_start_seconds=0.001,
            )
            active = _operation_check(invocation, timeout)
        active()

    return check


@dataclass(frozen=True, slots=True)
class Phase7E1DService:
    """Strict Schema-6 reconstruction, D2 interpretation, and Schema-7 commit."""

    repository: RecordingSearch7ERepository
    decision_boundary: TerminalDecisionBoundary = Phase7ED2DecisionAdapter()
    local_evidence: Phase7ELocalEvidenceAdapter | None = None

    def execute(
        self,
        invocation: Phase7EInvocation,
        acquisition: CommonSessionAcquisition | None = None,
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
        indexes = run.manifest.payload["indexes"]
        if not indexes["narrowed_bracket_ids"] and (
            acquisition is not None or self.local_evidence is not None
        ):
            if acquisition is None or self.local_evidence is None:
                raise Phase7EIncompleteEvidenceError
            run = Phase7EAdaptiveOrchestrator(
                self.repository,
                self.local_evidence,
            ).execute(invocation, acquisition)
        interpretation_timeout = invocation.budget.operation_timeout(
            invocation.request.policy.terminal_interpretation_seconds,
            minimum_start_seconds=0.001,
        )
        interpretation_check = _operation_check(invocation, interpretation_timeout)
        interpretation_check()
        decision = self.decision_boundary.interpret(run)
        interpretation_check()
        invocation.validate(self.repository)
        invocation.budget.check()
        source_set = build_source_record_set(run)
        snapshot = build_evidence_snapshot(run, source_set, decision)
        terminal = build_terminal_result(run, source_set, snapshot, decision)
        manifest = build_schema7_manifest(run, source_set, snapshot, terminal)
        publication_timeout = invocation.budget.operation_timeout(
            invocation.request.policy.publication_seconds,
            minimum_start_seconds=0.001,
            downstream_reserve_seconds=invocation.request.policy.strict_readback_seconds,
        )
        publication_check = _operation_check(invocation, publication_timeout)
        readback_check = _lazy_operation_check(
            invocation,
            invocation.request.policy.strict_readback_seconds,
        )
        return self.repository.publish_schema7(
            run.investigation_id,
            run.run_id,
            manifest,
            source_set,
            snapshot,
            terminal,
            expected_schema6_manifest_id=run.manifest_id,
            ownership=invocation.ownership,
            publication_check=publication_check,
            readback_check=readback_check,
        )


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
        run = repository.inspect_current_read_only(investigation_id, run_id)
    except Phase7EInProgressError:
        return Phase7EStatus(investigation_id, run_id, 0, "RUNNING", None, None)
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
    try:
        requested = _parse_whole_text(str(target.payload.get("requested_time_utc")))
    except (TypeError, ValueError) as exc:
        raise Phase7EAdapterError from exc
    sequence = target.payload.get("sequence")
    if type(sequence) is not int or sequence < 0:
        raise Phase7EAdapterError
    if target.payload.get("kind") not in {"COARSE", "SUPPORT", "BINARY"}:
        raise Phase7EAdapterError
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


def _request_schema6_targets(  # noqa: PLR0912, PLR0915
    repository: RecordingSearch7ERepository,
    invocation: Phase7EInvocation,
    current: Phase7ERun,
    targets: Sequence[StrictIdentityEnvelope],
    *,
    session_id: str,
) -> Phase7ERun:
    """Admit and strictly reopen every target before a decoder operation."""
    if not isinstance(current.state, Schema6Envelope):
        raise Phase7EAdapterError
    if current.manifest.payload.get("common_session_id") != session_id:
        raise Phase7EAdapterError
    ordered = tuple(sorted(targets, key=_target_sort_key))
    if not ordered or len({item.identity for item in ordered}) != len(ordered):
        raise Phase7EAdapterError
    if len(ordered) > invocation.request.policy.maximum_targets_per_decoder_pass:
        raise Phase7EAdapterError
    for target in ordered:
        _validate_target_for_session(current, target, invocation.request)
    _validate_target_batch(ordered, current)

    records_by_id = {
        item.identity: item for item in current.records if item.family == "target-request"
    }
    indexes = current.manifest.payload["indexes"]["target_request_ids"]
    missing: list[StrictIdentityEnvelope] = []
    for target in ordered:
        existing = records_by_id.get(target.identity)
        if existing is not None and existing != target:
            raise Phase7EAdapterError
        if target.identity not in indexes:
            missing.append(target)
        elif existing is None:
            raise Phase7ECorruptError

    if current.state.target_state is Schema6TargetState.REQUESTED:
        if missing:
            raise Phase7EAdapterError
        if current.state.active_target_request_id not in {item.identity for item in ordered}:
            raise Phase7EAdapterError
        reopened = current
    elif current.state.target_state is Schema6TargetState.OBSERVED:
        if missing:
            manifest = current.manifest
            for target in missing:
                manifest = append_schema6_indexes(manifest, target_request_ids=target.identity)
            records = tuple(item for item in current.records if item.family != "schema5-manifest")
            records = (*records, *missing)
            state = Schema6Envelope(
                run_state="RUNNING",
                target_state=Schema6TargetState.REQUESTED,
                active_target_request_id=ordered[0].identity,
                active_decoder_operation_id=None,
                active_frame_id=None,
                active_classification_attempt_id=None,
                active_classification_operation_id=None,
                active_observation_id=None,
                reason_code=None,
                attempt_count=0,
                predecessor_target_state=Schema6TargetState.OBSERVED,
            )
            reopened = repository.admit_schema6(
                current.investigation_id,
                current.run_id,
                manifest,
                state,
                records,
                expected_manifest_id=current.manifest_id,
                ownership=invocation.ownership,
            ).run
        else:
            reopened = current
    elif current.state.target_state is Schema6TargetState.DECODING:
        if missing:
            raise Phase7EAdapterError
        active_operation_id = current.state.active_decoder_operation_id
        if not isinstance(active_operation_id, str) or not active_operation_id:
            raise Phase7EAdapterError
        operation = next(
            (
                item
                for item in current.records
                if item.family == "decoder-operation" and item.identity == active_operation_id
            ),
            None,
        )
        if operation is None or tuple(operation.payload.get("target_request_ids", ())) != tuple(
            item.identity for item in ordered
        ):
            raise Phase7EAdapterError
        if current.state.active_target_request_id not in {item.identity for item in ordered}:
            raise Phase7EAdapterError
        reopened = current
    else:
        raise Phase7EAdapterError

    reopened_targets = {
        item.identity: item for item in reopened.records if item.family == "target-request"
    }
    reopened_ids = set(reopened.manifest.payload["indexes"]["target_request_ids"])
    for target in ordered:
        if target.identity not in reopened_ids or reopened_targets.get(target.identity) != target:
            raise Phase7ECorruptError
    return reopened


def _target_sort_key(target: StrictIdentityEnvelope) -> tuple[str, int, str]:
    """Return the deterministic decoder order for one target request."""
    sequence = target.payload.get("sequence")
    if type(sequence) is not int or sequence < 0:
        raise Phase7EAdapterError
    return str(target.payload.get("requested_time_utc")), sequence, target.identity


def _validate_target_batch(
    targets: Sequence[StrictIdentityEnvelope],
    current: Phase7ERun,
) -> None:
    """Validate closed support/logical-end ordering before admission."""
    logical_end = tuple(
        target
        for target in targets
        if target.payload.get("selection_rule") == "FINAL_STRICTLY_BEFORE_END"
    )
    if len(logical_end) > 1:
        raise Phase7EAdapterError
    support = tuple(target for target in targets if target.payload.get("kind") == "SUPPORT")
    support_times = tuple(str(target.payload["requested_time_utc"]) for target in support)
    if len(set(support_times)) != len(support_times) or support_times != tuple(
        sorted(support_times)
    ):
        raise Phase7EAdapterError
    if support:
        origins = [target.payload.get("origin_target_request_id") for target in support]
        if any(type(origin) is not str or not origin for origin in origins):
            raise Phase7EAdapterError
        if len(set(origins)) != 1:
            raise Phase7EAdapterError
        origin = origins[0]
        known_targets = {
            item.identity for item in current.records if item.family == "target-request"
        }
        if origin not in known_targets and not any(item.identity == origin for item in targets):
            raise Phase7EAdapterError
    for target in logical_end:
        if target.payload.get("kind") not in {"COARSE", "BINARY"}:
            raise Phase7EAdapterError


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
        predecessor_target_state=current.state.target_state,
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
