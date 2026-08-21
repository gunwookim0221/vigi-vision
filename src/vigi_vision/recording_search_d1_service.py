"""Sequential A2/B4-backed non-terminal binary narrowing for Phase 7D-1."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Generic, TypeVar

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_a2_models import ProbeFrameRequestRecord, ProbeRequestStatus
from vigi_vision.recording_search_b3_models import ClassifyRecordingProbeRequest
from vigi_vision.recording_search_b4_models import ClassificationOperationalError
from vigi_vision.recording_search_c1_models import CoarseSampleStatus
from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
from vigi_vision.recording_search_d1_history import (
    D1BracketState,
    HistoryEntryKind,
    HistoryEvidence,
    NarrowingHistoryEntry,
    history_digest,
    narrowed_bracket_id,
)
from vigi_vision.recording_search_d1_identity import (
    D1LowerBoundReference,
    D1SupportGroup,
    build_d1_input_bracket,
    d1_input_bracket_id,
    support_group_id,
)
from vigi_vision.recording_search_d1_models import (
    NarrowedBracket,
    NarrowingProbeEvidence,
    NarrowingResult,
    NarrowingState,
    NarrowingStatus,
    NarrowingStopReason,
    NarrowingTarget,
)
from vigi_vision.recording_search_d1_planner import (
    maximum_narrowing_iterations,
    midpoint_target,
    support_target_id,
)
from vigi_vision.recording_search_d1_support import (
    NarrowingEvidenceStore,
    NarrowingHandle,
    NarrowingHost,
    bound_from_evidence,
    require_successful_result,
)
from vigi_vision.recording_search_models import (
    RecordingSearchArtifactError,
    RecordingSearchBaselineError,
    RecordingSearchManifestCorruptError,
)

if TYPE_CHECKING:
    from vigi_vision.recording_search_models import RecordingSearchPolicy


HandleT = TypeVar("HandleT", bound=NarrowingHandle)


@dataclass(frozen=True, slots=True)
class BinaryNarrowingService(Generic[HandleT]):
    """Run one explicit active-handle narrowing invocation."""

    host: NarrowingHost[HandleT]
    evidence_store: NarrowingEvidenceStore[HandleT]

    def narrow(  # noqa: C901, PLR0911, PLR0912, PLR0915 - bounded state machine
        self,
        handle: HandleT,
        bracket: CoarseCandidateBracket,
        policy: RecordingSearchPolicy,
    ) -> NarrowingResult:
        """Return a typed narrowed bracket without terminal persistence."""
        if type(bracket) is not CoarseCandidateBracket:
            raise TypeError
        try:
            with self.host.a2_mutation(handle):
                self._require_active(handle)
                self.evidence_store.validate_bracket(handle, bracket, policy)
                state = self.evidence_store.load_state(handle, bracket)
                state = _attach_d1_context(state, bracket, handle, policy)
        except RecordingSearchBaselineError:
            return _safe(NarrowingStatus.INTERRUPTED, "inactive_run_handle", None)
        except (RecordingSearchManifestCorruptError, RecordingSearchArtifactError, ValueError):
            return _safe(NarrowingStatus.CORRUPT, "authoritative_evidence_invalid", None)

        maximum_iterations = maximum_narrowing_iterations(
            state.interval_seconds, policy.binary_stop_resolution_seconds
        )
        while True:
            if state.interval_seconds <= policy.binary_stop_resolution_seconds:
                return _ready(state, NarrowingStopReason.TARGET_PRECISION_REACHED)
            if state.iteration >= maximum_iterations:
                return _ready(state, NarrowingStopReason.MAXIMUM_ITERATIONS)
            target = midpoint_target(state, policy, state.iteration)
            if target is None:
                stopped = _record_operational_stop(state, None, "no_distinct_midpoint")
                return _safe(NarrowingStatus.INDETERMINATE, "no_distinct_midpoint", stopped)
            if not self._digest_matches(handle, state):
                return _safe(NarrowingStatus.CORRUPT, "stale_authoritative_evidence", state)
            try:
                evidence = self._probe(handle, target.target_id, target.requested_time_utc)
            except RecordingSearchManifestCorruptError:
                return _safe(NarrowingStatus.CORRUPT, "authoritative_evidence_invalid", state)
            if evidence.status is not CoarseSampleStatus.SUCCESS or evidence.state is None:
                status = (
                    NarrowingStatus.INTERRUPTED
                    if evidence.status is CoarseSampleStatus.INTERRUPTED
                    else NarrowingStatus.INDETERMINATE
                )
                stopped = _record_operational_stop(state, target, _safe_reason(evidence.status))
                return _safe(status, _safe_reason(evidence.status), stopped)
            match evidence.state:
                case ClassificationOutcome.PRESENT:
                    before = state
                    advanced = self._advance_present(state, target.target_id, evidence)
                    try:
                        state = _record_present_transition(before, advanced, target, evidence)
                    except ValueError:
                        stopped = _record_operational_stop(
                            before, target, "history_evidence_incomplete"
                        )
                        return _safe(
                            NarrowingStatus.INDETERMINATE,
                            "history_evidence_incomplete",
                            stopped,
                        )
                case ClassificationOutcome.ABSENT:
                    try:
                        support = self._support(handle, target, evidence, policy)
                    except RecordingSearchManifestCorruptError:
                        return _safe(
                            NarrowingStatus.CORRUPT, "authoritative_evidence_invalid", state
                        )
                    if support is None:
                        stopped = _record_operational_stop(
                            state, target, "absence_support_unusable"
                        )
                        return _safe(
                            NarrowingStatus.INDETERMINATE, "absence_support_unusable", stopped
                        )
                    before = state
                    advanced = self._advance_absent(state, target.target_id, evidence, support)
                    try:
                        state = _record_absent_transition(
                            before, advanced, target, evidence, support, policy
                        )
                    except ValueError:
                        stopped = _record_operational_stop(
                            before, target, "history_evidence_incomplete"
                        )
                        return _safe(
                            NarrowingStatus.INDETERMINATE,
                            "history_evidence_incomplete",
                            stopped,
                        )
                case ClassificationOutcome.INDETERMINATE:
                    try:
                        stopped = _record_visual_stop(state, target, evidence)
                    except ValueError:
                        stopped = _record_operational_stop(
                            state, target, "history_evidence_incomplete"
                        )
                        return _safe(
                            NarrowingStatus.INDETERMINATE,
                            "history_evidence_incomplete",
                            stopped,
                        )
                    return _safe(NarrowingStatus.INDETERMINATE, "visual_indeterminate", stopped)
            refreshed = self._refresh_digest(handle, state)
            if refreshed is None:
                return _safe(NarrowingStatus.CORRUPT, "stale_authoritative_evidence", state)
            state = refreshed

    def _probe(
        self,
        handle: HandleT,
        target_id: str,
        requested_time_utc: datetime,
    ) -> NarrowingProbeEvidence:
        with self.host.a2_mutation(handle):
            self._require_active(handle)
            existing = self.evidence_store.find_existing(handle, requested_time_utc, target_id)
        if existing is not None:
            return existing
        try:
            requests = self.host.acquire_targets(handle, (requested_time_utc,))
        except RecordingSearchBaselineError:
            return _failure(target_id, requested_time_utc, CoarseSampleStatus.INTERRUPTED)
        except RecordingSearchArtifactError:
            return _failure(target_id, requested_time_utc, CoarseSampleStatus.ACQUISITION_FAILED)
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(target_id, requested_time_utc, CoarseSampleStatus.UNEXPECTED_ERROR)
        if len(requests) != 1 or requests[0].requested_time_utc != requested_time_utc:
            return _failure(target_id, requested_time_utc, CoarseSampleStatus.UNEXPECTED_ERROR)
        return self._classify_request(handle, target_id, requests[0])

    def _classify_request(
        self,
        handle: HandleT,
        target_id: str,
        request: ProbeFrameRequestRecord,
    ) -> NarrowingProbeEvidence:
        if request.status is not ProbeRequestStatus.SUCCEEDED:
            return self._resolve(handle, request, target_id)
        try:
            result = self.host.classify(
                handle,
                ClassifyRecordingProbeRequest(
                    investigation_id=handle.investigation_id,
                    search_run_id=handle.search_run_id,
                    probe_request_id=request.probe_request_id,
                ),
            )
            require_successful_result(result, request)
        except ClassificationOperationalError:
            return _failure(
                target_id, request.requested_time_utc, CoarseSampleStatus.CLASSIFICATION_FAILED
            )
        except (RecordingSearchBaselineError, RecordingSearchArtifactError):
            return _failure(target_id, request.requested_time_utc, CoarseSampleStatus.INTERRUPTED)
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                target_id, request.requested_time_utc, CoarseSampleStatus.UNEXPECTED_ERROR
            )
        return self._resolve(handle, request, target_id)

    def _resolve(
        self,
        handle: HandleT,
        request: ProbeFrameRequestRecord,
        target_id: str,
    ) -> NarrowingProbeEvidence:
        try:
            with self.host.a2_mutation(handle):
                self._require_active(handle)
                return self.evidence_store.resolve_request(handle, request, target_id)
        except RecordingSearchBaselineError:
            return _failure(target_id, request.requested_time_utc, CoarseSampleStatus.INTERRUPTED)
        except (RecordingSearchArtifactError, ValueError):
            return _failure(
                target_id, request.requested_time_utc, CoarseSampleStatus.UNEXPECTED_ERROR
            )

    def _support(  # noqa: C901, PLR0911 - bounded support policy
        self,
        handle: HandleT,
        target: NarrowingTarget,
        midpoint: NarrowingProbeEvidence,
        policy: RecordingSearchPolicy,
    ) -> tuple[NarrowingProbeEvidence, ...] | None:
        support_times = tuple(
            midpoint.requested_time_utc + index * timedelta(seconds=policy.absence_cadence_seconds)
            for index in range(policy.absence_confirmation_frames)
        )
        if support_times[-1] > policy.search_end_utc:
            return None
        support: list[NarrowingProbeEvidence] = [midpoint]
        existing: list[NarrowingProbeEvidence | None] = [midpoint]
        try:
            with self.host.a2_mutation(handle):
                self._require_active(handle)
                existing.extend(
                    self.evidence_store.find_existing(
                        handle,
                        requested_time_utc,
                        support_target_id(target, requested_time_utc),
                    )
                    for requested_time_utc in support_times[1:]
                )
        except RecordingSearchManifestCorruptError:
            raise
        except (RecordingSearchBaselineError, ValueError):
            return None
        try:
            requests = self.host.acquire_targets(handle, support_times)
            if len(requests) != len(support_times):
                return None
        except RecordingSearchManifestCorruptError:
            raise
        except (
            RecordingSearchBaselineError,
            RecordingSearchArtifactError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            return None
        if requests[0].probe_request_id != midpoint.probe_request_id:
            return None
        for requested_time_utc, request in zip(support_times[1:], requests[1:], strict=True):
            target_id = support_target_id(target, requested_time_utc)
            if request.requested_time_utc != requested_time_utc:
                return None
            evidence = existing[len(support)]
            if evidence is None:
                evidence = self._classify_request(handle, target_id, request)
            support.append(evidence)
        if not _valid_absence_support(
            support,
            policy.absence_confirmation_frames,
            policy.absence_cadence_seconds,
        ):
            return None
        return tuple(support)

    def _advance_present(
        self,
        state: NarrowingState,
        target_id: str,
        evidence: NarrowingProbeEvidence,
    ) -> NarrowingState:
        return replace(
            state,
            lower_bound_utc=evidence.requested_time_utc,
            lower_evidence=bound_from_evidence(evidence),
            target_ids=(*state.target_ids, target_id),
            evidence=(*state.evidence, evidence),
            iteration=state.iteration + 1,
        )

    def _advance_absent(
        self,
        state: NarrowingState,
        target_id: str,
        midpoint: NarrowingProbeEvidence,
        support: tuple[NarrowingProbeEvidence, ...],
    ) -> NarrowingState:
        return replace(
            state,
            upper_bound_utc=midpoint.requested_time_utc,
            upper_support_evidence=tuple(bound_from_evidence(item) for item in support),
            target_ids=(*state.target_ids, target_id, *(item.target_id for item in support[1:])),
            evidence=(*state.evidence, *support),
            iteration=state.iteration + 1,
        )

    def _digest_matches(self, handle: HandleT, state: NarrowingState) -> bool:
        try:
            with self.host.a2_mutation(handle):
                self._require_active(handle)
                return self.evidence_store.current_manifest_digest(handle) == state.manifest_digest
        except (RecordingSearchBaselineError, RecordingSearchManifestCorruptError, ValueError):
            return False

    def _refresh_digest(
        self,
        handle: HandleT,
        state: NarrowingState,
    ) -> NarrowingState | None:
        try:
            with self.host.a2_mutation(handle):
                self._require_active(handle)
                digest = self.evidence_store.current_manifest_digest(handle)
        except (RecordingSearchBaselineError, RecordingSearchManifestCorruptError, ValueError):
            return None
        return replace(state, manifest_digest=digest)

    @staticmethod
    def _require_active(handle: HandleT) -> None:
        if handle.closed:
            raise RecordingSearchBaselineError


def narrow_active_bracket(
    service: NarrowingHost[HandleT],
    handle: HandleT,
    bracket: CoarseCandidateBracket,
    policy: RecordingSearchPolicy,
    evidence_store: NarrowingEvidenceStore[HandleT],
) -> NarrowingResult:
    """Compose the internal D1 service from the existing A2/B4 host and store."""
    return BinaryNarrowingService(service, evidence_store).narrow(handle, bracket, policy)


def execute_binary_narrowing(
    service: NarrowingHost[HandleT],
    handle: HandleT,
    bracket: CoarseCandidateBracket,
    policy: RecordingSearchPolicy,
    evidence_store: NarrowingEvidenceStore[HandleT],
) -> NarrowingResult:
    """Expose the internal production-composed D1 boundary without a public route."""
    return narrow_active_bracket(service, handle, bracket, policy, evidence_store)


def _ready(state: NarrowingState, stop_reason: NarrowingStopReason) -> NarrowingResult:
    narrowed_id = None
    if (
        state.d1_input_bracket is not None
        and state.history_digest is not None
        and state.source_bracket is not None
    ):
        narrowed_id = narrowed_bracket_id(
            state.d1_input_bracket,
            state.history,
            _bracket_state(state),
            state.history_digest,
            state.iteration,
            state.interval_seconds,
            stop_reason.value,
            state.manifest_digest,
            source_bracket=state.source_bracket,
        )
    return NarrowingResult(
        status=NarrowingStatus.READY,
        narrowed_bracket=NarrowedBracket(
            investigation_id=state.investigation_id,
            search_run_id=state.search_run_id,
            phase6_confirmation_id=state.phase6_confirmation_id,
            baseline_identity=state.baseline_identity,
            source_bracket_id=state.source_bracket_id,
            policy_version=state.policy_version,
            lower_bound_utc=state.lower_bound_utc,
            upper_bound_utc=state.upper_bound_utc,
            lower_evidence=state.lower_evidence,
            upper_support_evidence=state.upper_support_evidence,
            target_ids=state.target_ids,
            evidence=state.evidence,
            iterations=state.iteration,
            achieved_precision_seconds=state.interval_seconds,
            stop_reason=stop_reason,
            manifest_digest=state.manifest_digest,
            d1_input_bracket=state.d1_input_bracket,
            source_bracket=state.source_bracket,
            upper_support_group_id=state.upper_support_group_id,
            history=state.history,
            history_digest=state.history_digest,
            narrowed_bracket_id=narrowed_id,
        ),
        history=state.history,
    )


def _safe(
    status: NarrowingStatus,
    reason: str,
    state: NarrowingState | None,
) -> NarrowingResult:
    return NarrowingResult(
        status=status,
        current_state=state,
        safe_reason=reason,
        history=() if state is None else state.history,
    )


def _attach_d1_context(
    state: NarrowingState,
    bracket: CoarseCandidateBracket,
    handle: NarrowingHandle,
    policy: RecordingSearchPolicy,
) -> NarrowingState:
    try:
        input_bracket = build_d1_input_bracket(
            bracket,
            phase6_confirmation_id=handle.phase6_confirmation_id,
            baseline_identity=handle.baseline_identity,
            policy=policy,
        )
        if state.source_bracket_id != input_bracket.source_revision.c2_bracket_id:
            return state
        identity = d1_input_bracket_id(input_bracket)
        return replace(
            state,
            d1_input_bracket=input_bracket,
            source_bracket=bracket,
            upper_support_group_id=input_bracket.upper_support.support_group_id,
            history_digest=history_digest(input_bracket, identity, state.history),
        )
    except ValueError:
        return state


def _bracket_state(state: NarrowingState) -> D1BracketState:
    if state.upper_support_group_id is None:
        raise ValueError
    lower = state.lower_evidence
    if lower.is_baseline:
        reference = D1LowerBoundReference(
            kind="PHASE6_BASELINE",
            target_id=None,
            requested_time_utc=lower.requested_time_utc,
            observation_id=lower.observation_id,
            probe_request_id=None,
            canonical_frame_id=None,
        )
    else:
        reference = D1LowerBoundReference(
            kind="PRESENT_PROBE",
            target_id=lower.target_id,
            requested_time_utc=lower.requested_time_utc,
            observation_id=lower.observation_id,
            probe_request_id=lower.probe_request_id,
            canonical_frame_id=lower.canonical_frame_id,
        )
    return D1BracketState(
        lower_requested_time_utc=state.lower_bound_utc,
        upper_requested_time_utc=state.upper_bound_utc,
        lower_reference=reference,
        upper_support_group_id=state.upper_support_group_id,
    )


def _history_evidence(
    evidence: NarrowingProbeEvidence,
    role: str,
) -> HistoryEvidence:
    if (
        evidence.state is None
        or evidence.observation_id is None
        or evidence.canonical_frame_id is None
        or evidence.operation_id is None
        or evidence.classification_operation_id is None
        or evidence.decode_session_id is None
        or evidence.decoded_frame_utc is None
        or evidence.decoded_pts is None
        or evidence.decoded_ordinal is None
        or evidence.alias_id is not None
    ):
        raise ValueError
    return HistoryEvidence(
        role=role,
        target_id=evidence.target_id,
        probe_request_id=evidence.probe_request_id or "",
        observation_id=evidence.observation_id,
        canonical_frame_id=evidence.canonical_frame_id,
        acquisition_operation_id=evidence.operation_id,
        classification_operation_id=evidence.classification_operation_id,
        decode_session_id=evidence.decode_session_id,
        decoded_frame_utc=evidence.decoded_frame_utc,
        decoded_pts=evidence.decoded_pts,
        decoded_ordinal=evidence.decoded_ordinal,
        classification=evidence.state,
        requested_time_utc=evidence.requested_time_utc,
    )


def _append_history(
    state: NarrowingState,
    entry: NarrowingHistoryEntry,
) -> NarrowingState:
    if state.d1_input_bracket is None:
        return state
    entries = (*state.history, entry)
    value = d1_input_bracket_id(state.d1_input_bracket)
    return replace(
        state,
        upper_support_group_id=entry.bracket_after.upper_support_group_id,
        history=entries,
        history_digest=history_digest(state.d1_input_bracket, value, entries),
    )


def _record_present_transition(
    before: NarrowingState,
    after: NarrowingState,
    target: NarrowingTarget,
    evidence: NarrowingProbeEvidence,
) -> NarrowingState:
    if before.d1_input_bracket is None:
        return after
    bracket_before = _bracket_state(before)
    bracket_after = _bracket_state(after)
    midpoint = _history_evidence(evidence, "MIDPOINT")
    entry = NarrowingHistoryEntry(
        iteration=before.iteration,
        entry_kind=HistoryEntryKind.PRESENT_TRANSITION,
        target_id=target.target_id,
        midpoint_requested_time_utc=target.requested_time_utc,
        bracket_before=bracket_before,
        evidence=(midpoint,),
        classification=ClassificationOutcome.PRESENT,
        support_group_id=None,
        support_indexes=(),
        bracket_after=bracket_after,
        visual_stop_reason=None,
        operational_stop_reason=None,
    )
    return _append_history(after, entry)


def _record_absent_transition(  # noqa: PLR0913 - mirrors the transition inputs
    before: NarrowingState,
    after: NarrowingState,
    target: NarrowingTarget,
    midpoint: NarrowingProbeEvidence,
    support: tuple[NarrowingProbeEvidence, ...],
    policy: RecordingSearchPolicy,
) -> NarrowingState:
    if before.d1_input_bracket is None:
        return after
    input_bracket = before.d1_input_bracket
    support_group = D1SupportGroup(
        support_group_id="pending",
        origin_target_id=target.target_id,
        support_count=len(support),
        cadence_seconds=policy.absence_cadence_seconds,
        requested_support_times=tuple(item.requested_time_utc for item in support),
        probe_request_ids=tuple(item.probe_request_id or "" for item in support),
        observation_ids=tuple(item.observation_id or "" for item in support),
        canonical_frame_ids=tuple(item.canonical_frame_id or "" for item in support),
        decode_session_id=support[0].decode_session_id or "",
        decoded_frame_utc=tuple(
            item.decoded_frame_utc for item in support if item.decoded_frame_utc
        ),
        decoded_pts=tuple(item.decoded_pts or -1 for item in support),
        decoded_ordinals=tuple(item.decoded_ordinal or -1 for item in support),
        origin_midpoint_requested_time_utc=midpoint.requested_time_utc,
    )
    group_id = support_group_id(
        investigation_id=before.investigation_id,
        search_run_id=before.search_run_id,
        phase6_confirmation_id=before.phase6_confirmation_id,
        baseline_identity=before.baseline_identity,
        plan_id=input_bracket.plan_id,
        policy_identity=input_bracket.policy_identity,
        source_revision=input_bracket.source_revision,
        d1_input_bracket_id=d1_input_bracket_id(input_bracket),
        iteration=before.iteration,
        group=support_group,
    )
    midpoint_evidence = _history_evidence(midpoint, "MIDPOINT")
    support_evidence = tuple(_history_evidence(item, "ABSENCE_SUPPORT") for item in support)
    entry = NarrowingHistoryEntry(
        iteration=before.iteration,
        entry_kind=HistoryEntryKind.ABSENT_TRANSITION,
        target_id=target.target_id,
        midpoint_requested_time_utc=target.requested_time_utc,
        bracket_before=_bracket_state(before),
        evidence=(midpoint_evidence, *support_evidence),
        classification=ClassificationOutcome.ABSENT,
        support_group_id=group_id,
        support_indexes=tuple(range(len(support))),
        bracket_after=replace(
            _bracket_state(after),
            upper_support_group_id=group_id,
        ),
        visual_stop_reason=None,
        operational_stop_reason=None,
    )
    return _append_history(after, entry)


def _record_visual_stop(
    state: NarrowingState,
    target: NarrowingTarget,
    evidence: NarrowingProbeEvidence,
) -> NarrowingState:
    if state.d1_input_bracket is None:
        return state
    entry = NarrowingHistoryEntry(
        iteration=state.iteration,
        entry_kind=HistoryEntryKind.VISUAL_STOP,
        target_id=target.target_id,
        midpoint_requested_time_utc=target.requested_time_utc,
        bracket_before=_bracket_state(state),
        evidence=(_history_evidence(evidence, "MIDPOINT"),),
        classification=ClassificationOutcome.INDETERMINATE,
        support_group_id=None,
        support_indexes=(),
        bracket_after=_bracket_state(state),
        visual_stop_reason="visual_indeterminate",
        operational_stop_reason=None,
    )
    return _append_history(state, entry)


def _record_operational_stop(
    state: NarrowingState,
    target: NarrowingTarget | None,
    reason: str,
) -> NarrowingState:
    if state.d1_input_bracket is None:
        return state
    midpoint = state.lower_bound_utc if target is None else target.requested_time_utc
    target_id = "no-distinct-midpoint" if target is None else target.target_id
    entry = NarrowingHistoryEntry(
        iteration=state.iteration,
        entry_kind=HistoryEntryKind.OPERATIONAL_STOP,
        target_id=target_id,
        midpoint_requested_time_utc=midpoint,
        bracket_before=_bracket_state(state),
        evidence=(),
        classification=None,
        support_group_id=None,
        support_indexes=(),
        bracket_after=_bracket_state(state),
        visual_stop_reason=None,
        operational_stop_reason=reason,
    )
    return _append_history(state, entry)


def _failure(
    target_id: str, requested_time_utc: datetime, status: CoarseSampleStatus
) -> NarrowingProbeEvidence:
    return NarrowingProbeEvidence(
        target_id=target_id,
        requested_time_utc=requested_time_utc,
        status=status,
        probe_request_id=f"unadmitted-{target_id}",
    )


def _safe_reason(status: CoarseSampleStatus) -> str:
    return {
        CoarseSampleStatus.RECORDING_UNAVAILABLE: "recording_unavailable",
        CoarseSampleStatus.ACQUISITION_FAILED: "acquisition_failed",
        CoarseSampleStatus.TIMEOUT: "acquisition_timeout",
        CoarseSampleStatus.CLASSIFICATION_FAILED: "classification_failed",
        CoarseSampleStatus.INTERRUPTED: "interrupted",
        CoarseSampleStatus.UNEXPECTED_ERROR: "unexpected_error",
    }.get(status, "narrowing_evidence_unusable")


def _valid_absence_support(
    support: list[NarrowingProbeEvidence],
    expected_count: int,
    cadence_seconds: int,
) -> bool:
    if len(support) != expected_count or any(
        item.status is not CoarseSampleStatus.SUCCESS
        or item.state is not ClassificationOutcome.ABSENT
        or item.alias_id is not None
        or item.observation_id is None
        or item.canonical_frame_id is None
        or item.decode_session_id is None
        or item.decoded_frame_utc is None
        or item.decoded_pts is None
        or item.decoded_ordinal is None
        for item in support
    ):
        return False
    observations = tuple(item.observation_id for item in support)
    requested_times = tuple(item.requested_time_utc for item in support)
    probe_requests = tuple(item.probe_request_id for item in support)
    frames = tuple(item.canonical_frame_id for item in support)
    classification_operations = tuple(item.classification_operation_id for item in support)
    sessions = tuple(item.decode_session_id for item in support)
    decoded_times = tuple(_required_time(item.decoded_frame_utc) for item in support)
    decoded_pts = tuple(_required_int(item.decoded_pts) for item in support)
    decoded_ordinals = tuple(_required_int(item.decoded_ordinal) for item in support)
    return (
        all(
            requested_times[index]
            == requested_times[0] + index * timedelta(seconds=cadence_seconds)
            for index in range(expected_count)
        )
        and len(set(probe_requests)) == expected_count
        and len(set(observations)) == expected_count
        and len(set(frames)) == expected_count
        and len(set(classification_operations)) == expected_count
        and len(set(sessions)) == 1
        and all(left < right for left, right in pairwise(decoded_times))
        and all(left < right for left, right in pairwise(decoded_pts))
        and all(left < right for left, right in pairwise(decoded_ordinals))
    )


def _required_time(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError
    return value


def _required_int(value: int | None) -> int:
    if value is None:
        raise ValueError
    return value
