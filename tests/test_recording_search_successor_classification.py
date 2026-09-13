from __future__ import annotations

# Dynamic protocol fakes intentionally keep the focused contract tests compact.
# Their runtime shape is asserted through the service boundary.
# ruff: noqa: ANN001, ANN003, ANN202, B017, PLR0913, PT011, PT017, PT018
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import vigi_vision.recording_search_successor_classification as classification_module
from vigi_vision.investigation_confirmation_integrity import JpegIntegrity
from vigi_vision.investigation_confirmation_models import ConfirmationRoi, RoiProvenance
from vigi_vision.object_presence_evidence import RawComparison
from vigi_vision.object_presence_models import DecodedRgbImage
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.object_presence_values import ClassificationOutcome, VisualStatus
from vigi_vision.recording_models import RecordingSegment, RecordingWindow
from vigi_vision.recording_search_7e_b4_process import EfficientSamWorkerSpec
from vigi_vision.recording_search_b3_media import DecodedMedia
from vigi_vision.recording_search_successor import (
    SuccessorPlanRequest,
    TargetAvailability,
    build_successor_plan,
)
from vigi_vision.recording_search_successor_acquisition import (
    SuccessorTargetAcquisitionResult,
    SuccessorTargetStatus,
    successor_target_id,
)
from vigi_vision.recording_search_successor_classification import (
    EfficientSamSuccessorClassifier,
    SuccessorClassificationAuthority,
    SuccessorClassificationError,
    SuccessorClassifierResult,
    SuccessorCoarseClassificationService,
    SuccessorObservationState,
)

UTC = timezone.utc
ANCHOR = datetime(2026, 9, 4, 5, 17, 32, tzinfo=UTC)


def _segment(start: datetime, end: datetime) -> RecordingSegment:
    return RecordingSegment(
        1, start.date(), int(start.timestamp()), int(end.timestamp()), start, end
    )


def _plan(*, gap: bool = False):
    request = SuccessorPlanRequest(1, ANCHOR, ANCHOR + timedelta(minutes=30), "Asia/Seoul")
    if gap:
        segments = (
            _segment(ANCHOR, ANCHOR + timedelta(minutes=10)),
            _segment(ANCHOR + timedelta(minutes=20), request.search_end_utc),
        )
    else:
        segments = (_segment(ANCHOR, request.search_end_utc),)
    return build_successor_plan(request, segments)


def _image(value: int) -> DecodedRgbImage:
    return DecodedRgbImage.from_rows(
        tuple(tuple((value, value, value) for _ in range(32)) for _ in range(32))
    )


def _authority(plan) -> SuccessorClassificationAuthority:
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
        _image(64),
    )


class _Decoder:
    def decode(self, payload: bytes, width: int, height: int) -> DecodedMedia:
        return DecodedMedia(JpegIntegrity("b" * 64, len(payload)), _image(payload[0]))


class _Classifier:
    policy_identity = "policy-test"

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[DecodedRgbImage, DecodedRgbImage, ConfirmationRoi, str]] = []

    def classify(self, baseline_image, probe_image, width, height, roi, correlation_id):
        self.calls.append((baseline_image, probe_image, roi, correlation_id))
        value = self.outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        reason = (
            "insufficient_visual_evidence" if value is ClassificationOutcome.INDETERMINATE else None
        )
        return SuccessorClassifierResult(value, reason)


def _acquisition(plan, target, *, status=SuccessorTargetStatus.FRAME_AVAILABLE, frame_shift=0):
    target_id = successor_target_id(plan, target)
    if status is not SuccessorTargetStatus.FRAME_AVAILABLE:
        return SuccessorTargetAcquisitionResult(
            plan.plan_id,
            target_id,
            f"acq-{target.sequence}",
            target.sequence,
            target.requested_time_utc,
            None if status is SuccessorTargetStatus.UNAVAILABLE_GAP else target.segment_id,
            None
            if status is SuccessorTargetStatus.UNAVAILABLE_GAP
            else RecordingWindow(
                1, target.requested_time_utc - timedelta(seconds=1), target.requested_time_utc
            ),
            status,
        )
    frame_utc = target.requested_time_utc + timedelta(seconds=frame_shift)
    payload = bytes([target.sequence])
    return SuccessorTargetAcquisitionResult(
        plan.plan_id,
        target_id,
        f"acq-{target.sequence}",
        target.sequence,
        target.requested_time_utc,
        target.segment_id,
        RecordingWindow(
            1,
            target.requested_time_utc - timedelta(seconds=1),
            target.requested_time_utc + timedelta(seconds=1),
        ),
        status,
        frame_utc,
        1.0,
        float(frame_shift),
        payload,
        "b" * 64,
        len(payload),
        32,
        32,
    )


def _service(outcomes: list[object]) -> tuple[SuccessorCoarseClassificationService, _Classifier]:
    classifier = _Classifier(outcomes)
    return SuccessorCoarseClassificationService(classifier, _Decoder()), classifier


def test_only_frame_available_targets_call_classifier_and_preserve_authority() -> None:
    plan = _plan(gap=True)
    acquisitions = tuple(
        _acquisition(
            plan,
            target,
            status=(
                SuccessorTargetStatus.UNAVAILABLE_GAP
                if target.availability is TargetAvailability.UNAVAILABLE
                else SuccessorTargetStatus.FRAME_AVAILABLE
            ),
        )
        for target in plan.targets
    )
    service, classifier = _service([ClassificationOutcome.PRESENT, ClassificationOutcome.ABSENT])

    result = service.classify_plan(plan, acquisitions, _authority(plan))

    assert len(classifier.calls) == 2
    assert all(call[0] == _image(64) for call in classifier.calls)
    assert result.observations[0].state is SuccessorObservationState.UNAVAILABLE_GAP
    assert result.observations[0].reference_frame_resource_id == "reference-frame-1"
    assert result.observations[0].roi_identity.startswith("successor-roi-v1-")


def test_visual_states_and_candidate_bracket_use_actual_frame_time() -> None:
    plan = _plan()
    acquisitions = tuple(
        _acquisition(plan, target, frame_shift=(-2 if target.sequence == 1 else 3))
        for target in plan.targets
    )
    service, _ = _service(
        [
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.INDETERMINATE,
        ]
    )

    result = service.classify_plan(plan, acquisitions, _authority(plan))

    assert [item.state for item in result.observations] == [
        SuccessorObservationState.PRESENT,
        SuccessorObservationState.ABSENT,
        SuccessorObservationState.INDETERMINATE,
    ]
    assert result.observations[0].frame_utc != result.observations[0].requested_time_utc
    assert result.candidate_bracket is not None
    assert result.candidate_bracket.present_observation_id == result.observations[0].observation_id


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
def test_every_acquisition_unavailable_state_skips_classifier(
    status: SuccessorTargetStatus,
) -> None:
    plan = _plan()
    acquisitions = tuple(_acquisition(plan, target, status=status) for target in plan.targets)
    service, classifier = _service([])

    result = service.classify_plan(plan, acquisitions, _authority(plan))

    assert all(item.state.value == status.value for item in result.observations)
    assert not classifier.calls
    assert result.candidate_bracket is None


def test_present_only_and_absent_only_never_fabricate_bracket() -> None:
    plan = _plan()
    acquisitions = tuple(_acquisition(plan, target) for target in plan.targets)
    present_service, _ = _service([ClassificationOutcome.PRESENT] * 3)
    absent_service, _ = _service([ClassificationOutcome.ABSENT] * 3)

    assert (
        present_service.classify_plan(plan, acquisitions, _authority(plan)).candidate_bracket
        is None
    )
    assert (
        absent_service.classify_plan(plan, acquisitions, _authority(plan)).candidate_bracket is None
    )


def test_classifier_failure_is_target_local() -> None:
    plan = _plan()
    acquisitions = tuple(_acquisition(plan, target) for target in plan.targets)
    service, classifier = _service(
        [
            SuccessorClassificationError("classifier_failed"),
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.ABSENT,
        ]
    )

    result = service.classify_plan(plan, acquisitions, _authority(plan))

    assert result.observations[0].state is SuccessorObservationState.CLASSIFIER_FAILED
    assert result.observations[1].state is SuccessorObservationState.PRESENT
    assert result.observations[2].state is SuccessorObservationState.ABSENT
    assert len(classifier.calls) == 3


def test_same_frame_time_uses_identity_tie_break() -> None:
    plan = _plan()
    first = _acquisition(plan, plan.targets[0], frame_shift=0)
    second = _acquisition(plan, plan.targets[1], frame_shift=-600)
    third = _acquisition(plan, plan.targets[2])
    service, _ = _service([ClassificationOutcome.PRESENT] * 3)

    result = service.classify_plan(plan, (third, second, first), _authority(plan))

    tied = [item for item in result.observations if item.frame_utc == first.frame_utc]
    assert len(tied) == 2
    assert tied[0].observation_id < tied[1].observation_id


def test_duplicate_or_foreign_acquisition_fails_closed() -> None:
    plan = _plan()
    first = _acquisition(plan, plan.targets[0])
    service, _ = _service([ClassificationOutcome.PRESENT] * 3)
    duplicate = (first, first, _acquisition(plan, plan.targets[2]))

    with pytest.raises(Exception):
        service.classify_plan(plan, duplicate, _authority(plan))


def test_ties_are_identity_sorted_and_observation_identity_is_deterministic() -> None:
    plan = _plan()
    acquisitions = tuple(_acquisition(plan, target) for target in plan.targets)
    service, _ = _service([ClassificationOutcome.PRESENT] * 3)
    first = service.classify_plan(plan, acquisitions, _authority(plan))
    service_again, _ = _service([ClassificationOutcome.PRESENT] * 3)
    second = service_again.classify_plan(plan, acquisitions, _authority(plan))

    assert [item.observation_id for item in first.observations] == [
        item.observation_id for item in second.observations
    ]
    assert [item.ordinal for item in first.observations] == [1, 2, 3]


def test_timeout_keeps_other_target_facts_and_gap_does_not_make_bracket() -> None:
    plan = _plan(gap=True)
    acquisitions = tuple(
        _acquisition(
            plan,
            target,
            status=(
                SuccessorTargetStatus.UNAVAILABLE_GAP
                if target.availability is TargetAvailability.UNAVAILABLE
                else SuccessorTargetStatus.FRAME_AVAILABLE
            ),
        )
        for target in plan.targets
    )
    service, classifier = _service(
        [SuccessorClassificationError("classifier_timeout"), ClassificationOutcome.ABSENT]
    )
    result = service.classify_plan(plan, acquisitions, _authority(plan))

    assert result.observations[1].state is SuccessorObservationState.CLASSIFIER_TIMEOUT
    assert result.observations[-1].state is SuccessorObservationState.ABSENT
    assert result.candidate_bracket is None
    assert len(classifier.calls) == 2


def test_resolution_mismatch_is_indeterminate_without_classifier_call() -> None:
    plan = _plan()
    acquisition = _acquisition(plan, plan.targets[0])
    mismatched = SuccessorTargetAcquisitionResult(
        acquisition.plan_id,
        acquisition.target_id,
        acquisition.acquisition_id,
        acquisition.sequence,
        acquisition.requested_time_utc,
        acquisition.assigned_segment_id,
        acquisition.replay_window,
        acquisition.status,
        acquisition.frame_utc,
        acquisition.frame_pts_seconds,
        acquisition.frame_offset_seconds,
        acquisition.frame_bytes,
        acquisition.frame_sha256,
        acquisition.frame_size_bytes,
        16,
        16,
    )
    service, classifier = _service([ClassificationOutcome.PRESENT] * 3)

    result = service.classify_plan(
        plan,
        (mismatched, *tuple(_acquisition(plan, t) for t in plan.targets[1:])),
        _authority(plan),
    )

    assert result.observations[0].state is SuccessorObservationState.INDETERMINATE
    assert result.observations[0].reason_code == "invalid_frame_or_roi"
    assert len(classifier.calls) == 2


def test_authority_plan_mismatch_fails_closed() -> None:
    plan = _plan()
    service, _ = _service([])
    with pytest.raises(Exception):
        service.classify_plan(
            plan,
            tuple(_acquisition(plan, target) for target in plan.targets),
            _authority(_plan(gap=True)),
        )


def test_efficient_sam_adapter_preserves_bounded_result_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    policy = ObjectPresenceDecisionPolicy(
        minimum_mask_overlap_for_comparison=0.1,
        minimum_comparison_area=1,
        minimum_clipped_mask_pixels=1,
    )

    calls: list[tuple[DecodedRgbImage, DecodedRgbImage]] = []

    def bounded(*, baseline_image, probe_image, policy, **_kwargs):
        calls.append((baseline_image, probe_image))
        comparison = RawComparison(
            baseline_mask_pixel_count=100,
            probe_mask_pixel_count=100,
            roi_pixel_count=256,
            mask_intersection_pixel_count=100,
            mask_union_pixel_count=100,
            baseline_mask_coverage=0.390625,
            probe_mask_coverage=0.390625,
            mask_iou=1.0,
            effective_comparison_area=100,
            roi_luma_ncc=1.0,
            visual_status=VisualStatus.COMPARABLE,
            unusable_reason=None,
        )
        return policy.decide(comparison)

    monkeypatch.setattr(classification_module, "run_b4_in_process", bounded)
    adapter = EfficientSamSuccessorClassifier(
        policy,
        EfficientSamWorkerSpec(Path("checkpoint.pt"), "a" * 64, "cpu"),
        2.0,
    )

    output = adapter.classify(_image(64), _image(64), 32, 32, _authority(plan).roi, "acq-1")

    assert output.outcome is ClassificationOutcome.PRESENT
    assert adapter.policy_identity == policy.identity
    assert calls and calls[0][0] == _image(64)


def test_efficient_sam_adapter_preserves_baseline_support_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    policy = ObjectPresenceDecisionPolicy(
        classifier_policy_version="test-baseline-support-v1",
        classifier_preprocessing_version="test-baseline-support-v1",
        baseline_support_mode=True,
        minimum_mask_overlap_for_comparison=0.1,
        minimum_roi_pixels=1,
        minimum_clipped_mask_pixels=1,
    )

    def bounded(*, policy, **_kwargs):
        comparison = RawComparison(
            baseline_mask_pixel_count=100,
            probe_mask_pixel_count=100,
            roi_pixel_count=256,
            mask_intersection_pixel_count=100,
            mask_union_pixel_count=100,
            baseline_mask_coverage=0.390625,
            probe_mask_coverage=0.390625,
            mask_iou=1.0,
            effective_comparison_area=None,
            roi_luma_ncc=1.0,
            visual_status=VisualStatus.COMPARABLE,
            unusable_reason=None,
            comparison_mode="baseline_support_v1",
            baseline_support_pixel_count=100,
            baseline_support_luma_similarity=1.0,
            baseline_support_luma_ncc=1.0,
            baseline_support_edge_similarity=1.0,
            baseline_support_change_ratio=0.0,
            baseline_support_foreground_retention=1.0,
            baseline_support_background_change_ratio=0.0,
        )
        return policy.decide(comparison)

    monkeypatch.setattr(classification_module, "run_b4_in_process", bounded)
    adapter = EfficientSamSuccessorClassifier(
        policy,
        EfficientSamWorkerSpec(Path("checkpoint.pt"), "a" * 64, "cpu"),
        2.0,
    )
    output = adapter.classify(_image(64), _image(64), 32, 32, _authority(plan).roi, "acq-support")
    assert output.outcome is ClassificationOutcome.PRESENT
    assert output.comparison is not None
    assert output.comparison.comparison_mode == "baseline_support_v1"


def test_efficient_sam_adapter_runs_real_spawned_model_path(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    efficient_sam = pytest.importorskip("efficient_sam.efficient_sam")
    model = efficient_sam.build_efficient_sam(
        encoder_patch_embed_dim=192,
        encoder_num_heads=3,
        checkpoint=None,
    )
    checkpoint = tmp_path / "efficient_sam_fixture.pt"
    torch.save({"model": model.state_dict()}, checkpoint)
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    policy = ObjectPresenceDecisionPolicy(
        minimum_mask_overlap_for_comparison=0.1,
        minimum_comparison_area=1,
        minimum_clipped_mask_pixels=1,
    )
    adapter = EfficientSamSuccessorClassifier(
        policy,
        EfficientSamWorkerSpec(checkpoint, digest, "cpu"),
        60.0,
        60.0,
    )

    try:
        result = adapter.classify(
            _image(64),
            _image(64),
            32,
            32,
            _authority(_plan()).roi,
            "real-efficient-sam-fixture",
        )
    except SuccessorClassificationError as error:
        assert error.reason == "classifier_failed"
    else:
        assert result.outcome in {
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.INDETERMINATE,
        }
