from __future__ import annotations

# These fakes implement the existing Slice 2/3 boundaries; they do not bypass
# the narrowing service's identity, actual-time, or authority validation.
# ruff: noqa: ANN001, ANN202, PLR0913, PT018
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vigi_vision.investigation_confirmation_integrity import JpegIntegrity
from vigi_vision.investigation_confirmation_models import ConfirmationRoi, RoiProvenance
from vigi_vision.object_presence_models import DecodedRgbImage
from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_models import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.recording_search_b3_media import DecodedMedia
from vigi_vision.recording_search_successor import (
    SuccessorPlanRequest,
    build_successor_plan,
)
from vigi_vision.recording_search_successor_acquisition import (
    SuccessorTargetAcquisitionResult,
    SuccessorTargetAcquisitionService,
    SuccessorTargetStatus,
    successor_midpoint_target_id,
    successor_target_id,
)
from vigi_vision.recording_search_successor_classification import (
    SuccessorCandidateBracket,
    SuccessorClassificationAuthority,
    SuccessorClassifierResult,
    SuccessorCoarseClassificationResult,
    SuccessorCoarseClassificationService,
    SuccessorObservation,
    SuccessorObservationState,
)
from vigi_vision.recording_search_successor_narrowing import (
    SuccessorBinaryNarrowingService,
    SuccessorNarrowingCompletion,
    SuccessorNarrowingContractError,
    SuccessorNarrowingPolicy,
)
from vigi_vision.reference_frame_models import DecodedFrameEvidence, TimingPrecisionStatus
from vigi_vision.replay import ReplayClip

UTC = timezone.utc
ANCHOR = datetime(2026, 9, 4, 5, 17, 32, tzinfo=UTC)


def _segment(start: datetime, end: datetime) -> RecordingSegment:
    return RecordingSegment(
        1, start.date(), int(start.timestamp()), int(end.timestamp()), start, end
    )


def _plan(*, minutes: int = 30, split: bool = False, gap: bool = False):
    end = ANCHOR + timedelta(minutes=minutes)
    request = SuccessorPlanRequest(1, ANCHOR, end, "Asia/Seoul")
    if gap:
        segments = (
            _segment(ANCHOR, ANCHOR + timedelta(minutes=10)),
            _segment(ANCHOR + timedelta(minutes=20), end),
        )
    elif split:
        segments = (
            _segment(ANCHOR, ANCHOR + timedelta(minutes=15)),
            _segment(ANCHOR + timedelta(minutes=15), end),
        )
    else:
        segments = (_segment(ANCHOR, end),)
    return build_successor_plan(request, segments)


def _authority(plan) -> SuccessorClassificationAuthority:
    image = DecodedRgbImage.from_rows(
        tuple(tuple((64, 64, 64) for _ in range(32)) for _ in range(32))
    )
    return SuccessorClassificationAuthority(
        "object-disappearance-v3-ch1-20260904T051732Z",
        plan.plan_id,
        "reference-frame-1",
        "a" * 64,
        128,
        32,
        32,
        ConfirmationRoi(
            x=8,
            y=8,
            width=16,
            height=16,
            coordinate_space="source_pixels",
            provenance=RoiProvenance.MANUAL,
        ),
        image,
    )


def _observation(
    plan,
    authority,
    requested: datetime,
    state: SuccessorObservationState,
    *,
    frame: datetime | None = None,
    target_id: str | None = None,
    acquisition_id: str | None = None,
    status: SuccessorTargetStatus = SuccessorTargetStatus.FRAME_AVAILABLE,
    sequence: int = 1,
) -> SuccessorObservation:
    actual = requested if frame is None and state.is_visual else frame
    if (
        state
        in {
            SuccessorObservationState.PRESENT,
            SuccessorObservationState.ABSENT,
        }
        and actual is None
    ):
        raise AssertionError
    target_id = target_id or f"coarse-target-{requested.timestamp()}"
    acquisition_id = acquisition_id or f"coarse-acquisition-{requested.timestamp()}"
    payload = "|".join(
        (plan.plan_id, target_id, acquisition_id, requested.isoformat(), str(actual), state.value)
    )
    observation_id = "successor-observation-v1-" + hashlib.sha256(payload.encode()).hexdigest()
    return SuccessorObservation(
        plan.plan_id,
        target_id,
        acquisition_id,
        sequence,
        requested,
        actual,
        None if actual is None else 1.0,
        None if actual is None else (actual - requested).total_seconds(),
        authority.authority_identity,
        authority.reference_frame_resource_id,
        authority.roi_identity,
        "classifier-policy-test",
        status,
        state,
        None
        if state.is_visual and state is not SuccessorObservationState.INDETERMINATE
        else "insufficient_visual_evidence",
        1,
        observation_id,
    )


class _Acquisition:
    def __init__(self, frame_offsets=None, statuses=None) -> None:
        self.frame_offsets = frame_offsets or {}
        self.statuses = statuses or {}
        self.calls: list[object] = []

    def acquire_midpoint(self, plan, target):
        self.calls.append(target)
        status = self.statuses.get(target.requested_time_utc, SuccessorTargetStatus.FRAME_AVAILABLE)
        target_id = successor_midpoint_target_id(plan, target)
        window = RecordingWindow(
            plan.channel_id,
            max(plan.anchor_time_utc, target.requested_time_utc - timedelta(seconds=5)),
            min(plan.search_end_utc, target.requested_time_utc + timedelta(seconds=5)),
        )
        if status is not SuccessorTargetStatus.FRAME_AVAILABLE:
            return SuccessorTargetAcquisitionResult(
                plan.plan_id,
                target_id,
                f"acq-{len(self.calls)}",
                target.sequence,
                target.requested_time_utc,
                target.segment_id,
                window,
                status,
            )
        actual = target.requested_time_utc + timedelta(
            seconds=self.frame_offsets.get(target.requested_time_utc, 0)
        )
        return SuccessorTargetAcquisitionResult(
            plan.plan_id,
            target_id,
            f"acq-{len(self.calls)}",
            target.sequence,
            target.requested_time_utc,
            target.segment_id,
            window,
            status,
            actual,
            5.0,
            (actual - target.requested_time_utc).total_seconds(),
            b"frame",
            "b" * 64,
            5,
            32,
            32,
        )


class _Classification:
    policy_identity = "classifier-policy-test"

    def __init__(self, outcomes=None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[object] = []

    def validate_authority(self, plan, authority) -> None:
        if authority.successor_plan_id != plan.plan_id:
            raise SuccessorNarrowingContractError

    def classify_target(self, plan, target, acquisition, authority):
        self.calls.append(target)
        configured = self.outcomes.get(target.requested_time_utc, SuccessorObservationState.PRESENT)
        if isinstance(configured, BaseException):
            raise configured
        if acquisition.status is not SuccessorTargetStatus.FRAME_AVAILABLE:
            state = SuccessorObservationState(acquisition.status.value)
        elif isinstance(configured, SuccessorObservationState):
            state = configured
        else:
            state = SuccessorObservationState(configured)
        frame = acquisition.frame_utc
        if state not in {
            SuccessorObservationState.PRESENT,
            SuccessorObservationState.ABSENT,
            SuccessorObservationState.INDETERMINATE,
            SuccessorObservationState.CLASSIFIER_TIMEOUT,
            SuccessorObservationState.CLASSIFIER_FAILED,
        }:
            frame = None
        return _observation(
            plan,
            authority,
            target.requested_time_utc,
            state,
            frame=frame,
            target_id=acquisition.target_id,
            acquisition_id=acquisition.acquisition_id,
            status=acquisition.status,
        )


def _coarse(plan, authority, *, left: datetime, right: datetime):
    left_observation = _observation(plan, authority, left, SuccessorObservationState.PRESENT)
    right_observation = _observation(
        plan, authority, right, SuccessorObservationState.ABSENT, sequence=2
    )
    return SuccessorCoarseClassificationResult(
        plan.plan_id,
        authority.authority_identity,
        (left_observation, right_observation),
        SuccessorCandidateBracket(
            left_observation.observation_id,
            right_observation.observation_id,
            left_observation.frame_utc,
            right_observation.frame_utc,
        ),
    )


def _service(
    plan, authority, coarse, outcomes=None, *, policy=None, offsets=None, statuses=None, cancel=None
):
    acquisition = _Acquisition(offsets, statuses)
    classification = _Classification(outcomes)
    service = SuccessorBinaryNarrowingService(
        acquisition,
        classification,
        policy or SuccessorNarrowingPolicy(),
        cancel,
    )
    return service, acquisition, classification


def test_valid_bracket_narrows_using_serial_actual_midpoint_frames() -> None:
    plan = _plan()
    authority = _authority(plan)
    left, right = ANCHOR, ANCHOR + timedelta(minutes=30)
    midpoint_outcomes = {
        ANCHOR + timedelta(seconds=900): SuccessorObservationState.PRESENT,
        ANCHOR + timedelta(seconds=1350): SuccessorObservationState.ABSENT,
        ANCHOR + timedelta(seconds=1125): SuccessorObservationState.PRESENT,
        ANCHOR + timedelta(seconds=1237): SuccessorObservationState.ABSENT,
        ANCHOR + timedelta(seconds=1181): SuccessorObservationState.PRESENT,
        ANCHOR + timedelta(seconds=1209): SuccessorObservationState.ABSENT,
    }
    service, acquisition, _ = _service(
        plan, authority, _coarse(plan, authority, left=left, right=right), midpoint_outcomes
    )

    result = service.narrow(plan, _coarse(plan, authority, left=left, right=right), authority)

    assert result.completion is SuccessorNarrowingCompletion.NARROWED
    assert result.interval_width_seconds == 28
    assert result.iterations == 6
    assert len(acquisition.calls) == 6
    assert result.last_present_frame_utc < result.first_absent_frame_utc


def test_bracket_already_within_width_does_not_acquire() -> None:
    plan = _plan()
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=30))
    service, acquisition, _ = _service(plan, authority, coarse)

    result = service.narrow(plan, coarse, authority)

    assert result.completion is SuccessorNarrowingCompletion.NARROWED
    assert result.iterations == 0
    assert not acquisition.calls


def test_odd_width_midpoint_tie_breaks_to_earlier_second() -> None:
    plan = _plan(minutes=1)
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=31))
    service, acquisition, _ = _service(
        plan,
        authority,
        coarse,
        {ANCHOR + timedelta(seconds=15): SuccessorObservationState.PRESENT},
        policy=SuccessorNarrowingPolicy(target_width_seconds=10, maximum_iterations=1),
    )

    result = service.narrow(plan, coarse, authority)

    assert result.completion is SuccessorNarrowingCompletion.ITERATION_LIMIT
    assert acquisition.calls[0].requested_time_utc == ANCHOR + timedelta(seconds=15)


def test_iteration_cap_is_six_or_less() -> None:
    plan = _plan(minutes=120)
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(hours=2))
    service, acquisition, _ = _service(
        plan,
        authority,
        coarse,
        policy=SuccessorNarrowingPolicy(target_width_seconds=1, maximum_iterations=6),
    )

    result = service.narrow(plan, coarse, authority)

    assert result.completion is SuccessorNarrowingCompletion.ITERATION_LIMIT
    assert result.iterations == 6
    assert len(acquisition.calls) == 6


def test_actual_frame_pts_updates_final_boundaries() -> None:
    plan = _plan(minutes=1)
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=40))
    midpoint = ANCHOR + timedelta(seconds=20)
    service, _, _ = _service(
        plan,
        authority,
        coarse,
        {midpoint: SuccessorObservationState.PRESENT},
        policy=SuccessorNarrowingPolicy(target_width_seconds=30, maximum_iterations=1),
        offsets={midpoint: -2},
    )

    result = service.narrow(plan, coarse, authority)

    assert result.last_present_frame_utc == midpoint - timedelta(seconds=2)
    assert result.last_present.requested_time_utc == midpoint
    assert result.interval_width_seconds == 22


@pytest.mark.parametrize(
    "status",
    [
        SuccessorTargetStatus.RECORDING_UNAVAILABLE,
        SuccessorTargetStatus.REPLAY_TIMEOUT,
        SuccessorTargetStatus.REPLAY_FAILED,
        SuccessorTargetStatus.DECODE_TIMEOUT,
        SuccessorTargetStatus.DECODE_UNAVAILABLE,
    ],
)
def test_midpoint_acquisition_status_is_target_local_safe_completion(status) -> None:
    plan = _plan(minutes=1)
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=40))
    midpoint = ANCHOR + timedelta(seconds=20)
    service, _, _ = _service(
        plan,
        authority,
        coarse,
        statuses={midpoint: status},
        policy=SuccessorNarrowingPolicy(target_width_seconds=30, maximum_iterations=1),
    )

    result = service.narrow(plan, coarse, authority)

    assert result.completion is SuccessorNarrowingCompletion.ACQUISITION_UNAVAILABLE
    assert len(result.midpoint_observations) == 1


@pytest.mark.parametrize(
    ("state", "completion"),
    [
        (
            SuccessorObservationState.INDETERMINATE,
            SuccessorNarrowingCompletion.INDETERMINATE_OBSERVATION,
        ),
        (
            SuccessorObservationState.CLASSIFIER_TIMEOUT,
            SuccessorNarrowingCompletion.CLASSIFICATION_UNAVAILABLE,
        ),
        (
            SuccessorObservationState.CLASSIFIER_FAILED,
            SuccessorNarrowingCompletion.CLASSIFICATION_UNAVAILABLE,
        ),
    ],
)
def test_midpoint_classification_status_closes_safely(state, completion) -> None:
    plan = _plan(minutes=1)
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=40))
    midpoint = ANCHOR + timedelta(seconds=20)
    service, _, _ = _service(
        plan,
        authority,
        coarse,
        {midpoint: state},
        policy=SuccessorNarrowingPolicy(target_width_seconds=30, maximum_iterations=1),
    )

    result = service.narrow(plan, coarse, authority)

    assert result.completion is completion


def test_gap_in_source_bracket_is_rejected_before_replay() -> None:
    plan = _plan(gap=True)
    authority = _authority(plan)
    coarse = _coarse(
        plan,
        authority,
        left=ANCHOR + timedelta(minutes=5),
        right=ANCHOR + timedelta(minutes=25),
    )
    service, acquisition, _ = _service(plan, authority, coarse)

    with pytest.raises(SuccessorNarrowingContractError):
        service.narrow(plan, coarse, authority)
    assert not acquisition.calls


def test_missing_midpoint_segment_is_incomplete_without_replay() -> None:
    plan = _plan(minutes=1)
    plan = replace(plan, segments=(plan.segments[0],), gaps=())
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=40))
    service, acquisition, _ = _service(
        plan,
        authority,
        coarse,
        policy=SuccessorNarrowingPolicy(target_width_seconds=30, maximum_iterations=1),
    )
    broken = replace(plan, segments=())

    result = service.narrow(broken, replace(coarse, plan_id=broken.plan_id), authority)

    assert result.completion is SuccessorNarrowingCompletion.INCOMPLETE_COVERAGE
    assert not acquisition.calls


def test_actual_frame_at_boundary_returns_no_progress() -> None:
    plan = _plan(minutes=1)
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=40))
    midpoint = ANCHOR + timedelta(seconds=20)
    service, _, _ = _service(
        plan,
        authority,
        coarse,
        {midpoint: SuccessorObservationState.PRESENT},
        policy=SuccessorNarrowingPolicy(target_width_seconds=30, maximum_iterations=1),
        offsets={midpoint: -20},
    )

    result = service.narrow(plan, coarse, authority)

    assert result.completion is SuccessorNarrowingCompletion.NO_PROGRESS


def test_cancellation_is_observed_before_midpoint_request() -> None:
    plan = _plan(minutes=1)
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=40))
    service, acquisition, _ = _service(
        plan,
        authority,
        coarse,
        policy=SuccessorNarrowingPolicy(target_width_seconds=30, maximum_iterations=1),
        cancel=lambda: True,
    )

    result = service.narrow(plan, coarse, authority)

    assert result.completion is SuccessorNarrowingCompletion.CANCELLED
    assert not acquisition.calls


def test_midpoint_uses_one_assigned_segment_at_boundary() -> None:
    plan = _plan(split=True)
    authority = _authority(plan)
    left = ANCHOR + timedelta(minutes=14)
    right = ANCHOR + timedelta(minutes=16)
    coarse = _coarse(plan, authority, left=left, right=right)
    midpoint = ANCHOR + timedelta(minutes=15)
    service, acquisition, _ = _service(
        plan,
        authority,
        coarse,
        {midpoint: SuccessorObservationState.PRESENT},
        policy=SuccessorNarrowingPolicy(target_width_seconds=30, maximum_iterations=1),
    )

    service.narrow(plan, coarse, authority)

    assert acquisition.calls[0].segment_id == plan.segments[1].segment_id


def test_source_bracket_requires_present_then_absent() -> None:
    plan = _plan(minutes=1)
    authority = _authority(plan)
    left = _observation(plan, authority, ANCHOR, SuccessorObservationState.ABSENT)
    right = _observation(
        plan, authority, ANCHOR + timedelta(seconds=40), SuccessorObservationState.PRESENT
    )
    coarse = SuccessorCoarseClassificationResult(
        plan.plan_id,
        authority.authority_identity,
        (left, right),
        SuccessorCandidateBracket(
            left.observation_id, right.observation_id, left.frame_utc, right.frame_utc
        ),
    )
    service, _, _ = _service(plan, authority, coarse)

    with pytest.raises(SuccessorNarrowingContractError):
        service.narrow(plan, coarse, authority)


def test_authority_mismatch_is_rejected() -> None:
    plan = _plan(minutes=1)
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=40))
    other = replace(authority, reference_frame_resource_id="other-reference")
    service, _, _ = _service(plan, authority, coarse)

    with pytest.raises(SuccessorNarrowingContractError):
        service.narrow(plan, coarse, other)


def test_plan_mismatch_is_rejected() -> None:
    plan = _plan(minutes=1)
    other = _plan(minutes=2)
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=40))
    service, _, _ = _service(plan, authority, coarse)

    with pytest.raises(SuccessorNarrowingContractError):
        service.narrow(other, coarse, authority)


def test_identity_and_window_are_deterministic_for_same_inputs() -> None:
    plan = _plan(minutes=1)
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=40))
    midpoint = ANCHOR + timedelta(seconds=20)
    policy = SuccessorNarrowingPolicy(target_width_seconds=30, maximum_iterations=1)
    first, _, _ = _service(
        plan, authority, coarse, {midpoint: SuccessorObservationState.PRESENT}, policy=policy
    )
    second, _, _ = _service(
        plan, authority, coarse, {midpoint: SuccessorObservationState.PRESENT}, policy=policy
    )

    first_result = first.narrow(plan, coarse, authority)
    second_result = second.narrow(plan, coarse, authority)

    assert first_result.narrowing_id == second_result.narrowing_id
    assert first_result.source_bracket_id == second_result.source_bracket_id


def test_midpoint_target_identity_is_not_coarse_target_identity() -> None:
    plan = _plan()
    target = plan.targets[0]
    midpoint = replace(target, requested_time_utc=target.requested_time_utc + timedelta(seconds=1))

    assert successor_midpoint_target_id(plan, midpoint) != successor_target_id(plan, target)


def test_observations_preserve_coarse_and_midpoint_evidence() -> None:
    plan = _plan(minutes=1)
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=40))
    midpoint = ANCHOR + timedelta(seconds=20)
    service, _, _ = _service(
        plan,
        authority,
        coarse,
        {midpoint: SuccessorObservationState.ABSENT},
        policy=SuccessorNarrowingPolicy(target_width_seconds=30, maximum_iterations=1),
    )

    result = service.narrow(plan, coarse, authority)

    assert result.coarse_observations == coarse.observations
    assert result.midpoint_observations[0].requested_time_utc == midpoint
    assert result.midpoint_observations[0].frame_utc == midpoint


class _ReplayPlanner:
    def __init__(self) -> None:
        self.windows: list[RecordingWindow] = []

    def plan_for_segment(self, segment, window: RecordingWindow) -> ReplayRequest:
        self.windows.append(window)
        return ReplayRequest(window, "rtsp://example.invalid/bounded")


class _ReplayExtractor:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.paths: list[Path] = []

    def extract(self, request: ReplayRequest) -> ReplayClip:
        path = self.root / f"midpoint-{len(self.paths)}.mp4"
        path.write_bytes(b"bounded-replay")
        self.paths.append(path)
        return ReplayClip(
            request.window.channel_id,
            request.window.start_utc,
            request.window.end_utc,
            request.replay_url,
            path,
            request.window.duration_seconds,
        )


class _ReplayDecoder:
    def decode(self, request) -> DecodedFrameEvidence:
        request.output_path.write_bytes(b"midpoint-jpeg")
        return DecodedFrameEvidence(
            request.output_path,
            1.5,
            32,
            32,
            TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
            (),
        )


class _MediaDecoder:
    def decode(self, payload: bytes, width: int, height: int) -> DecodedMedia:
        image = DecodedRgbImage.from_rows(
            tuple(tuple((80, 80, 80) for _ in range(width)) for _ in range(height))
        )
        return DecodedMedia(JpegIntegrity("c" * 64, len(payload)), image)


class _ProductionShapedClassifier:
    policy_identity = "classifier-policy-production-shaped"

    def classify(self, baseline_image, probe_image, width, height, roi, correlation_id):
        return SuccessorClassifierResult(ClassificationOutcome.ABSENT)


def test_production_shaped_slice2_slice3_midpoint_reaches_narrowed_and_cleans_media(
    tmp_path: Path,
) -> None:
    plan = _plan(minutes=1)
    authority = _authority(plan)
    coarse = _coarse(plan, authority, left=ANCHOR, right=ANCHOR + timedelta(seconds=40))
    planner = _ReplayPlanner()
    extractor = _ReplayExtractor(tmp_path)
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        extractor,
        _ReplayDecoder(),
        temporary_directory=tmp_path,
    )
    classification = SuccessorCoarseClassificationService(
        _ProductionShapedClassifier(),
        _MediaDecoder(),
    )
    service = SuccessorBinaryNarrowingService(
        acquisition,
        classification,
        SuccessorNarrowingPolicy(target_width_seconds=30, maximum_iterations=1),
    )

    result = service.narrow(plan, coarse, authority)

    assert result.completion is SuccessorNarrowingCompletion.NARROWED
    assert result.midpoint_observations[0].state is SuccessorObservationState.ABSENT
    assert result.midpoint_observations[0].frame_utc == planner.windows[0].start_utc + timedelta(
        seconds=1.5
    )
    assert planner.windows[0].duration_seconds == 10
    assert extractor.paths and not extractor.paths[0].exists()
    assert not list(tmp_path.glob("vigi-vision-successor-*"))
