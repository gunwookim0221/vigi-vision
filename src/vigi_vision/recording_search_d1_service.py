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

    def narrow(  # noqa: C901, PLR0911, PLR0912 - bounded state machine
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
                return _safe(NarrowingStatus.INDETERMINATE, "no_distinct_midpoint", state)
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
                return _safe(status, _safe_reason(evidence.status), state)
            match evidence.state:
                case ClassificationOutcome.PRESENT:
                    state = self._advance_present(state, target.target_id, evidence)
                case ClassificationOutcome.ABSENT:
                    try:
                        support = self._support(handle, target, evidence, policy)
                    except RecordingSearchManifestCorruptError:
                        return _safe(
                            NarrowingStatus.CORRUPT, "authoritative_evidence_invalid", state
                        )
                    if support is None:
                        return _safe(
                            NarrowingStatus.INDETERMINATE, "absence_support_unusable", state
                        )
                    state = self._advance_absent(state, target.target_id, evidence, support)
                case ClassificationOutcome.INDETERMINATE:
                    return _safe(NarrowingStatus.INDETERMINATE, "visual_indeterminate", state)
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
        if not _valid_absence_support(support, policy.absence_confirmation_frames):
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
        ),
    )


def _safe(
    status: NarrowingStatus,
    reason: str,
    state: NarrowingState | None,
) -> NarrowingResult:
    return NarrowingResult(status=status, current_state=state, safe_reason=reason)


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
    frames = tuple(item.canonical_frame_id for item in support)
    sessions = tuple(item.decode_session_id for item in support)
    decoded_times = tuple(_required_time(item.decoded_frame_utc) for item in support)
    decoded_pts = tuple(_required_int(item.decoded_pts) for item in support)
    decoded_ordinals = tuple(_required_int(item.decoded_ordinal) for item in support)
    return (
        len(set(observations)) == expected_count
        and len(set(frames)) == expected_count
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
