from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_a2_models import ProbeFrameRequestRecord, ProbeRequestStatus
from vigi_vision.recording_search_b3_models import ClassifyRecordingProbeRequest
from vigi_vision.recording_search_b4_models import (
    ClassificationPublicationOutcome,
    PublishedClassificationResult,
)
from vigi_vision.recording_search_c1_models import CoarseSampleStatus
from vigi_vision.recording_search_c1_planner import CoarseSamplingIdentity
from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
from vigi_vision.recording_search_d1_identity import source_bracket_identity
from vigi_vision.recording_search_d1_models import (
    NarrowingBoundEvidence,
    NarrowingProbeEvidence,
    NarrowingState,
    NarrowingStatus,
)
from vigi_vision.recording_search_d1_service import (
    BinaryNarrowingService,
    execute_binary_narrowing,
)
from vigi_vision.recording_search_models import (
    RecordingSearchArtifactError,
    RecordingSearchPolicy,
    default_policy,
)

UTC = timezone.utc
START = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
INVESTIGATION = "object-disappearance-v3-ch1-20260720T030000Z"
RUN = "search-run-abcdef12"
IDENTITY = CoarseSamplingIdentity(INVESTIGATION, RUN, INVESTIGATION, "baseline-test")


class FakeHandle:
    investigation_id: str = INVESTIGATION
    search_run_id: str = RUN
    phase6_confirmation_id: str = INVESTIGATION
    baseline_identity: str = "baseline-test"
    closed: bool = False


class FakeHost:
    def __init__(self, states: dict[datetime, ClassificationOutcome]) -> None:
        self.states: dict[datetime, ClassificationOutcome] = states
        self.acquired: list[tuple[datetime, ...]] = []
        self.classified: list[str] = []
        self.fail_at: datetime | None = None

    @contextmanager
    def a2_mutation(self, handle: FakeHandle) -> Generator[None, None, None]:
        del handle
        yield

    def acquire_targets(
        self, handle: FakeHandle, requested_times: tuple[datetime, ...]
    ) -> tuple[ProbeFrameRequestRecord, ...]:
        del handle
        self.acquired.append(requested_times)
        if self.fail_at in requested_times:
            raise RecordingSearchArtifactError
        return tuple(_request(value) for value in requested_times)

    def classify(
        self, handle: FakeHandle, request: ClassifyRecordingProbeRequest
    ) -> PublishedClassificationResult:
        del handle
        probe_request_id = request.probe_request_id
        requested_time_utc = _REQUEST_TIMES[probe_request_id]
        self.classified.append(probe_request_id)
        return PublishedClassificationResult(
            outcome=ClassificationPublicationOutcome.CREATED,
            observation_id=f"observation-{requested_time_utc.timestamp():.0f}",
            alias_id=None,
            probe_request_id=probe_request_id,
            canonical_frame_id=f"frame-{requested_time_utc.timestamp():.0f}",
            state=self.states[requested_time_utc],
            reason_code=None,
        )


class FakeStore:
    def __init__(self, states: dict[datetime, ClassificationOutcome]) -> None:
        self.states: dict[datetime, ClassificationOutcome] = states
        self.existing: dict[tuple[datetime, str], NarrowingProbeEvidence] = {}
        self.reuse_times: set[datetime] = set()
        self.duplicate_frame_times: set[datetime] = set()
        self.digest: str = "a" * 64

    def validate_bracket(
        self,
        handle: FakeHandle,
        bracket: CoarseCandidateBracket,
        policy: RecordingSearchPolicy,
    ) -> None:
        del handle, bracket, policy

    def load_state(self, handle: FakeHandle, bracket: CoarseCandidateBracket) -> NarrowingState:
        del handle
        lower = NarrowingBoundEvidence(
            target_id="source-baseline",
            requested_time_utc=START,
            state=ClassificationOutcome.PRESENT,
            observation_id="baseline-observation",
            probe_request_id=None,
            canonical_frame_id=None,
            operation_id=None,
            decode_session_id=None,
            decoded_frame_utc=None,
            decoded_pts=None,
            decoded_ordinal=None,
            is_baseline=True,
        )
        upper = tuple(_bound(START + timedelta(seconds=8 + index)) for index in range(3))
        return NarrowingState(
            investigation_id=INVESTIGATION,
            search_run_id=RUN,
            phase6_confirmation_id=INVESTIGATION,
            baseline_identity="baseline-test",
            source_bracket_id=(
                source_bracket_identity(bracket)
                if bracket.support_group_id is not None
                else "coarse-source"
            ),
            policy_version="recording-search-mvp-v1",
            lower_bound_utc=START,
            upper_bound_utc=START + timedelta(seconds=8),
            lower_evidence=lower,
            upper_support_evidence=upper,
            target_ids=(),
            evidence=(),
            iteration=0,
            manifest_digest=self.digest,
        )

    def find_existing(
        self, handle: FakeHandle, requested_time_utc: datetime, target_id: str
    ) -> NarrowingProbeEvidence | None:
        del handle
        existing = self.existing.get((requested_time_utc, target_id))
        if existing is not None or requested_time_utc in self.reuse_times:
            return existing or _evidence(
                target_id, requested_time_utc, self.states[requested_time_utc]
            )
        return None

    def resolve_request(
        self, handle: FakeHandle, request: ProbeFrameRequestRecord, target_id: str
    ) -> NarrowingProbeEvidence:
        del handle
        evidence = _evidence(
            target_id, request.requested_time_utc, self.states[request.requested_time_utc]
        )
        if request.requested_time_utc in self.duplicate_frame_times:
            evidence = replace(evidence, canonical_frame_id="frame-duplicate")
        return evidence

    def current_manifest_digest(self, handle: FakeHandle) -> str:
        del handle
        return self.digest


_REQUEST_TIMES: dict[str, datetime] = {}


def _request(requested_time_utc: datetime) -> ProbeFrameRequestRecord:
    probe_request_id = f"probe-request-{requested_time_utc.timestamp():.0f}"
    _REQUEST_TIMES[probe_request_id] = requested_time_utc
    return ProbeFrameRequestRecord.model_construct(
        record_type="probe_frame_request",
        probe_request_id=probe_request_id,
        investigation_id=INVESTIGATION,
        search_run_id=RUN,
        operation_id="acquisition-op-test",
        channel_id=1,
        requested_time_utc=requested_time_utc,
        status=ProbeRequestStatus.SUCCEEDED,
        canonical_frame_id=f"frame-{requested_time_utc.timestamp():.0f}",
        alias_of_probe_request_id=None,
        failure_reason=None,
        created_at_utc=requested_time_utc,
        completed_at_utc=requested_time_utc,
    )


def _evidence(
    target_id: str, requested_time_utc: datetime, state: ClassificationOutcome
) -> NarrowingProbeEvidence:
    stamp = requested_time_utc + timedelta(microseconds=100_000)
    suffix = f"{requested_time_utc.timestamp():.0f}"
    return NarrowingProbeEvidence(
        target_id=target_id,
        requested_time_utc=requested_time_utc,
        status=CoarseSampleStatus.SUCCESS,
        state=state,
        probe_request_id=f"probe-request-{suffix}",
        observation_id=f"observation-{suffix}",
        canonical_frame_id=f"frame-{suffix}",
        operation_id="acquisition-op-test",
        classification_operation_id=f"classification-op-{suffix}",
        decode_session_id="decode-session-test",
        decoded_frame_utc=stamp,
        decoded_pts=int(requested_time_utc.timestamp()),
        decoded_ordinal=int(requested_time_utc.timestamp()),
    )


def _bound(requested_time_utc: datetime) -> NarrowingBoundEvidence:
    evidence = _evidence(
        f"support-{requested_time_utc.timestamp():.0f}",
        requested_time_utc,
        ClassificationOutcome.ABSENT,
    )
    return NarrowingBoundEvidence(
        target_id=evidence.target_id,
        requested_time_utc=evidence.requested_time_utc,
        state=ClassificationOutcome.ABSENT,
        observation_id=evidence.observation_id or "",
        probe_request_id=evidence.probe_request_id,
        canonical_frame_id=evidence.canonical_frame_id,
        operation_id=evidence.operation_id,
        decode_session_id=evidence.decode_session_id,
        decoded_frame_utc=evidence.decoded_frame_utc,
        decoded_pts=evidence.decoded_pts,
        decoded_ordinal=evidence.decoded_ordinal,
    )


def _bracket(*, with_d1: bool = False) -> CoarseCandidateBracket:
    support = tuple(START + timedelta(seconds=8 + index) for index in range(3))
    return CoarseCandidateBracket(
        investigation_id=INVESTIGATION,
        search_run_id=RUN,
        identity=IDENTITY,
        plan_id="coarse-plan-test",
        policy_version="recording-search-mvp-v1",
        baseline_observation_id="baseline-observation",
        last_present_observation_id="baseline-observation",
        last_present_probe_request_id="probe-request-present",
        last_present_canonical_frame_id="frame-present",
        last_present_requested_time_utc=START,
        first_absent_requested_time_utc=START + timedelta(seconds=8),
        support_target_times=support,
        support_probe_request_ids=tuple(f"probe-request-support-{index}" for index in range(3)),
        support_observation_ids=tuple(f"observation-support-{index}" for index in range(3)),
        support_canonical_frame_ids=tuple(f"frame-support-{index}" for index in range(3)),
        support_decode_session_id="decode-session-test",
        support_decoded_frame_times=tuple(
            value + timedelta(microseconds=100_000) for value in support
        ),
        support_decoded_pts=(1, 2, 3),
        support_decoded_ordinals=(1, 2, 3),
        manifest_digest="a" * 64,
        last_present_target_id="present-target" if with_d1 else None,
        support_group_id="coarse-confirmation-test" if with_d1 else None,
    )


def _run(
    states: dict[datetime, ClassificationOutcome],
) -> tuple[BinaryNarrowingService[FakeHandle], FakeHost, FakeStore]:
    host = FakeHost(states)
    store = FakeStore(states)
    return BinaryNarrowingService(host, store), host, store


def test_present_midpoints_narrow_sequentially_to_one_second() -> None:
    service, host, _store = _run(
        {START + timedelta(seconds=value): ClassificationOutcome.PRESENT for value in range(1, 9)}
    )

    result = service.narrow(
        FakeHandle(), _bracket(), default_policy(START, START + timedelta(seconds=20))
    )

    assert result.status is NarrowingStatus.READY
    assert result.narrowed_bracket is not None
    assert result.narrowed_bracket.achieved_precision_seconds == 1
    assert host.acquired == [
        (START + timedelta(seconds=4),),
        (START + timedelta(seconds=6),),
        (START + timedelta(seconds=7),),
    ]
    assert len(host.classified) == 3


def test_absent_midpoint_requires_three_distinct_support_frames() -> None:
    states = {
        START + timedelta(seconds=value): ClassificationOutcome.PRESENT for value in range(1, 9)
    }
    states.update(
        {START + timedelta(seconds=value): ClassificationOutcome.ABSENT for value in (4, 5, 6)}
    )
    service, host, _store = _run(states)

    result = service.narrow(
        FakeHandle(), _bracket(), default_policy(START, START + timedelta(seconds=20))
    )

    assert result.status is NarrowingStatus.READY
    assert host.acquired[0] == (START + timedelta(seconds=4),)
    assert host.acquired[1] == tuple(START + timedelta(seconds=value) for value in (4, 5, 6))
    assert result.narrowed_bracket is not None
    assert result.narrowed_bracket.upper_bound_utc == START + timedelta(seconds=4)


def test_indeterminate_midpoint_does_not_move_either_bound() -> None:
    service, _host, _store = _run(
        {START + timedelta(seconds=4): ClassificationOutcome.INDETERMINATE}
    )

    result = service.narrow(
        FakeHandle(), _bracket(), default_policy(START, START + timedelta(seconds=20))
    )

    assert result.status is NarrowingStatus.INDETERMINATE
    assert result.current_state is not None
    assert result.current_state.lower_bound_utc == START
    assert result.current_state.upper_bound_utc == START + timedelta(seconds=8)
    assert result.current_state.target_ids == ()


def test_acquisition_failure_preserves_earlier_state() -> None:
    service, host, _store = _run({START + timedelta(seconds=4): ClassificationOutcome.PRESENT})
    host.fail_at = START + timedelta(seconds=4)

    result = service.narrow(
        FakeHandle(), _bracket(), default_policy(START, START + timedelta(seconds=20))
    )

    assert result.status is NarrowingStatus.INDETERMINATE
    assert result.current_state is not None
    assert result.current_state.target_ids == ()


def test_reentry_reuses_existing_midpoint_without_reclassification() -> None:
    service, host, store = _run(
        {START + timedelta(seconds=value): ClassificationOutcome.PRESENT for value in range(1, 9)}
    )
    store.reuse_times.add(START + timedelta(seconds=4))

    result = service.narrow(
        FakeHandle(), _bracket(), default_policy(START, START + timedelta(seconds=20))
    )

    assert result.status is NarrowingStatus.READY
    assert host.acquired == [(START + timedelta(seconds=6),), (START + timedelta(seconds=7),)]


def test_absent_support_rejects_duplicate_canonical_frames() -> None:
    states = {
        START + timedelta(seconds=value): ClassificationOutcome.PRESENT for value in range(1, 9)
    }
    states.update(
        {START + timedelta(seconds=value): ClassificationOutcome.ABSENT for value in (4, 5, 6)}
    )
    service, _host, store = _run(states)
    store.duplicate_frame_times.update({START + timedelta(seconds=5), START + timedelta(seconds=6)})

    result = service.narrow(
        FakeHandle(), _bracket(), default_policy(START, START + timedelta(seconds=20))
    )

    assert result.status is NarrowingStatus.INDETERMINATE
    assert result.current_state is not None
    assert result.current_state.upper_bound_utc == START + timedelta(seconds=8)


def test_reentry_reuses_existing_support_without_reclassification() -> None:
    states = {
        START + timedelta(seconds=value): ClassificationOutcome.PRESENT for value in range(1, 9)
    }
    states.update(
        {START + timedelta(seconds=value): ClassificationOutcome.ABSENT for value in (4, 5, 6)}
    )
    service, host, store = _run(states)
    store.reuse_times.update({START + timedelta(seconds=5), START + timedelta(seconds=6)})

    result = service.narrow(
        FakeHandle(), _bracket(), default_policy(START, START + timedelta(seconds=20))
    )

    assert result.status is NarrowingStatus.READY
    assert all(
        probe_id not in host.classified
        for probe_id in (
            _request(START + timedelta(seconds=5)).probe_request_id,
            _request(START + timedelta(seconds=6)).probe_request_id,
        )
    )


def test_closed_handle_stops_before_acquisition() -> None:
    service, host, _store = _run({START + timedelta(seconds=4): ClassificationOutcome.PRESENT})
    handle = FakeHandle()
    handle.closed = True

    result = service.narrow(
        handle, _bracket(), default_policy(START, START + timedelta(seconds=20))
    )

    assert result.status is NarrowingStatus.INTERRUPTED
    assert host.acquired == []


def test_internal_composition_wrapper_returns_narrowed_bracket() -> None:
    _service, host, store = _run(
        {START + timedelta(seconds=value): ClassificationOutcome.PRESENT for value in range(1, 9)}
    )

    result = execute_binary_narrowing(
        host,
        FakeHandle(),
        _bracket(),
        default_policy(START, START + timedelta(seconds=20)),
        store,
    )

    assert result.status is NarrowingStatus.READY
    assert result.narrowed_bracket is not None


def test_d1_history_and_identities_are_attached_to_production_handoff() -> None:
    service, _host, _store = _run(
        {START + timedelta(seconds=value): ClassificationOutcome.PRESENT for value in range(1, 9)}
    )

    result = service.narrow(
        FakeHandle(),
        _bracket(with_d1=True),
        default_policy(START, START + timedelta(seconds=20)),
    )

    assert result.status is NarrowingStatus.READY
    assert result.history
    assert result.narrowed_bracket is not None
    assert result.narrowed_bracket.d1_input_bracket is not None
    assert result.narrowed_bracket.source_bracket is not None
    assert result.narrowed_bracket.history_digest is not None
    assert result.narrowed_bracket.narrowed_bracket_id is not None


def test_absent_transition_records_d1_support_identity_and_order() -> None:
    states = {
        START + timedelta(seconds=value): ClassificationOutcome.PRESENT for value in range(1, 9)
    }
    states.update(
        {START + timedelta(seconds=value): ClassificationOutcome.ABSENT for value in (4, 5, 6)}
    )
    service, _host, _store = _run(states)

    result = service.narrow(
        FakeHandle(),
        _bracket(with_d1=True),
        default_policy(START, START + timedelta(seconds=20)),
    )

    assert result.status is NarrowingStatus.READY
    absent = next(
        entry for entry in result.history if entry.entry_kind.value == "ABSENT_TRANSITION"
    )
    assert absent.support_group_id is not None
    assert absent.support_indexes == (0, 1, 2)


def test_operational_stop_is_retained_without_visual_evidence() -> None:
    service, host, _store = _run({START + timedelta(seconds=4): ClassificationOutcome.PRESENT})
    host.fail_at = START + timedelta(seconds=4)

    result = service.narrow(
        FakeHandle(),
        _bracket(with_d1=True),
        default_policy(START, START + timedelta(seconds=20)),
    )

    assert result.status is NarrowingStatus.INDETERMINATE
    assert result.history
    assert result.history[0].entry_kind.value == "OPERATIONAL_STOP"
    assert result.history[0].evidence == ()
