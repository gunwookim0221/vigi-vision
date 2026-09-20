"""Focused Phase S2-1 presence-first successor tests."""

# Deterministic protocol fakes intentionally keep these focused tests compact.
# ruff: noqa: ANN001, ANN202, E501, PLR0913

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from vigi_vision.investigation_confirmation_integrity import JpegIntegrity
from vigi_vision.investigation_confirmation_models import ConfirmationRoi, RoiProvenance
from vigi_vision.object_presence_comparator import (
    FastPresenceReference,
    fast_present_comparison,
    prepare_fast_presence_reference,
)
from vigi_vision.object_presence_models import BinaryMask, DecodedRgbImage
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_models import RecordingSegment, RecordingWindow
from vigi_vision.recording_search_7e_b4_process import StaticMaskWorkerSpec, run_b4_in_process
from vigi_vision.recording_search_b3_media import DecodedMedia
from vigi_vision.recording_search_successor import SuccessorPlanRequest, build_successor_plan
from vigi_vision.recording_search_successor_acquisition import (
    SuccessorTargetAcquisitionResult,
    SuccessorTargetStatus,
    successor_target_id,
)
from vigi_vision.recording_search_successor_classification import (
    SuccessorClassificationAuthority,
    SuccessorClassifierResult,
    SuccessorCoarseClassificationService,
    SuccessorObservationState,
)

UTC = timezone.utc
ANCHOR = datetime(2026, 9, 4, 5, 17, 32, tzinfo=UTC)
WIDTH = 32
HEIGHT = 32
ROI = ConfirmationRoi(
    x=4,
    y=4,
    width=24,
    height=24,
    coordinate_space="source_pixels",
    provenance=RoiProvenance.MANUAL,
)


def _image(
    *,
    dx: int = 0,
    dy: int = 0,
    occluded: bool = False,
    replacement: bool = False,
    scene_shift: int = 0,
    scene_patch: bool = False,
):
    rows = []
    for y in range(HEIGHT):
        row = []
        for x in range(WIDTH):
            source_x, source_y = x - dx, y - dy
            background = 170 + ((x * 3 + y * 5) % 9) + scene_shift
            if scene_patch and x < 16 and y < 16:
                background += 70
            if 12 <= source_x < 20 and 12 <= source_y < 20:
                value = 35 + ((source_x * 11 + source_y * 7) % 35) + scene_shift
                if occluded and source_y < 16:
                    value = background
                elif replacement:
                    value = 225 - ((source_x * 5 + source_y * 3) % 20)
            else:
                value = background
            row.append((max(0, min(255, value)),) * 3)
        rows.append(tuple(row))
    return DecodedRgbImage.from_rows(tuple(rows))


def _mask() -> BinaryMask:
    return BinaryMask.from_rows(
        tuple(tuple(12 <= x < 20 and 12 <= y < 20 for x in range(WIDTH)) for y in range(HEIGHT))
    )


def _policy() -> ObjectPresenceDecisionPolicy:
    return ObjectPresenceDecisionPolicy(
        classifier_policy_version="test-s2-1",
        classifier_preprocessing_version="test-s2-1",
        baseline_support_mode=True,
        baseline_support_alignment_mode=True,
        minimum_mask_overlap_for_comparison=0.1,
        minimum_roi_pixels=1,
        minimum_clipped_mask_pixels=1,
    )


def _plan():
    end = ANCHOR + timedelta(minutes=10)
    request = SuccessorPlanRequest(1, ANCHOR, end, "Asia/Seoul")
    segment = RecordingSegment(1, ANCHOR.date(), int(ANCHOR.timestamp()), int(end.timestamp()), ANCHOR, end)
    return build_successor_plan(request, (segment,))


def _authority(plan) -> SuccessorClassificationAuthority:
    return SuccessorClassificationAuthority(
        "object-disappearance-v3-ch1-20260904T051732Z",
        plan.plan_id,
        "reference-frame-1",
        "a" * 64,
        128,
        WIDTH,
        HEIGHT,
        ROI,
        _image(),
    )


def _acquisition(plan, target, payload: bytes = b"same"):
    return SuccessorTargetAcquisitionResult(
        plan.plan_id,
        successor_target_id(plan, target),
        f"acq-{target.sequence}",
        target.sequence,
        target.requested_time_utc,
        target.segment_id,
        RecordingWindow(1, target.requested_time_utc - timedelta(seconds=1), target.requested_time_utc + timedelta(seconds=1)),
        SuccessorTargetStatus.FRAME_AVAILABLE,
        target.requested_time_utc,
        1.0,
        0.0,
        payload,
        "b" * 64,
        len(payload),
        WIDTH,
        HEIGHT,
    )


class _Decoder:
    def __init__(self, images: dict[bytes, DecodedRgbImage]) -> None:
        self.images = images

    def decode(self, payload: bytes, width: int, height: int) -> DecodedMedia:
        return DecodedMedia(JpegIntegrity("b" * 64, len(payload)), self.images[payload])


class _Classifier:
    policy_identity = "policy-s2-1"

    def __init__(self, outcome: ClassificationOutcome = ClassificationOutcome.INDETERMINATE) -> None:
        self.outcome = outcome
        self.prepare_calls = 0
        self.classify_calls = 0

    def prepare_reference(self, *_args: object) -> BinaryMask:
        self.prepare_calls += 1
        return _mask()

    def classify(self, *_args: object) -> SuccessorClassifierResult:
        self.classify_calls += 1
        reason = "insufficient_visual_evidence" if self.outcome is ClassificationOutcome.INDETERMINATE else None
        return SuccessorClassifierResult(self.outcome, reason)


def _service(classifier: _Classifier, images: dict[bytes, DecodedRgbImage]):
    return SuccessorCoarseClassificationService(
        classifier,
        _Decoder(images),
        fast_present_policy=_policy(),
    )


def test_unchanged_candidate_is_fast_present_without_slow_classifier_call() -> None:
    plan = _plan()
    classifier = _Classifier()
    service = _service(classifier, {b"same": _image()})
    authority = service.prepare_reference(_authority(plan))

    observation = service.classify_coarse_target(
        plan, plan.targets[0], _acquisition(plan, plan.targets[0]), authority
    )

    assert observation.state is SuccessorObservationState.PRESENT
    assert classifier.classify_calls == 0
    assert service.metrics.fast_path_evaluations == 1
    assert service.metrics.fast_present_hits == 1
    assert observation.comparison is not None
    assert observation.comparison["baseline_support_decision_path"] == "present"


def test_reference_preparation_is_reused_for_multiple_observations() -> None:
    plan = _plan()
    classifier = _Classifier()
    service = _service(classifier, {b"same": _image()})
    authority = service.prepare_reference(_authority(plan))
    authority_again = service.prepare_reference(authority)

    assert authority_again is authority
    assert classifier.prepare_calls == 1
    for target in (plan.targets[0], plan.targets[0]):
        observation = service.classify_coarse_target(
            plan, target, replace(_acquisition(plan, target), sequence=target.sequence), authority
        )
        assert observation.state is SuccessorObservationState.PRESENT
    assert service.metrics.fast_present_hits == 2
    assert classifier.classify_calls == 0


def test_moved_candidate_delegates_to_existing_classifier() -> None:
    plan = _plan()
    classifier = _Classifier(ClassificationOutcome.PRESENT)
    service = _service(classifier, {b"moved": _image(dx=2, dy=1)})
    authority = service.prepare_reference(_authority(plan))

    observation = service.classify_coarse_target(
        plan, plan.targets[0], _acquisition(plan, plan.targets[0], b"moved"), authority
    )

    assert observation.state is SuccessorObservationState.PRESENT
    assert classifier.classify_calls == 1
    assert service.metrics.fast_present_hits == 0


def test_occlusion_replacement_and_scene_change_delegate() -> None:
    plan = _plan()
    classifier = _Classifier()
    images = {
        b"occluded": _image(occluded=True),
        b"replacement": _image(replacement=True),
        b"scene": _image(scene_patch=True),
    }
    service = _service(classifier, images)
    authority = service.prepare_reference(_authority(plan))
    for payload in images:
        observation = service.classify_coarse_target(
            plan, plan.targets[0], _acquisition(plan, plan.targets[0], payload), authority
        )
        assert observation.state is SuccessorObservationState.INDETERMINATE
    assert classifier.classify_calls == 3
    assert service.metrics.fast_present_hits == 0


def test_missing_reference_prerequisite_delegates_without_fast_state() -> None:
    plan = _plan()
    classifier = _Classifier(ClassificationOutcome.ABSENT)
    service = _service(classifier, {b"same": _image()})

    observation = service.classify_coarse_target(
        plan, plan.targets[0], _acquisition(plan, plan.targets[0]), _authority(plan)
    )

    assert observation.state is SuccessorObservationState.ABSENT
    assert classifier.classify_calls == 1
    assert service.metrics.fast_path_evaluations == 0
    assert service.metrics.fast_present_hits == 0


def test_s2_1_has_no_fast_absent_path() -> None:
    plan = _plan()
    classifier = _Classifier(ClassificationOutcome.ABSENT)
    service = _service(classifier, {b"removed": _image(occluded=True)})
    authority = service.prepare_reference(_authority(plan))

    observation = service.classify_coarse_target(
        plan, plan.targets[0], _acquisition(plan, plan.targets[0], b"removed"), authority
    )

    assert observation.state is SuccessorObservationState.ABSENT
    assert classifier.classify_calls == 1
    assert service.metrics.fast_present_hits == 0


def test_reference_mask_preparation_uses_existing_b4_process_boundary() -> None:
    mask = _mask()
    result = run_b4_in_process(
        baseline_image=_image(),
        probe_image=_image(),
        source_width=WIDTH,
        source_height=HEIGHT,
        roi=ROI,
        policy=_policy(),
        worker_spec=StaticMaskWorkerSpec(mask, mask),
        correlation_id="s2-reference",
        timeout_seconds=5.0,
        reference_only=True,
    )

    assert isinstance(result, BinaryMask)
    assert result.rows == mask.rows


def test_fast_present_delegates_when_wider_scene_guard_is_below_present_evidence() -> None:
    policy = _policy()
    baseline = _image()
    reference = prepare_fast_presence_reference(baseline, _mask(), ROI, policy)
    assert reference is not None
    with (
        patch(
            "vigi_vision.object_presence_comparator._support_luma_metrics",
            side_effect=[
                (0.90, 0.01, 0.80, 0.01, (40.0,) * len(reference.support_indices)),
                (0.90, 0.01, 0.69, 0.01, (40.0,) * len(reference.support_indices)),
            ],
        ),
        patch(
            "vigi_vision.object_presence_comparator._support_edge_similarity",
            return_value=0.90,
        ),
    ):
        assert fast_present_comparison(reference, baseline, ROI, policy) is None


def test_fast_present_delegates_when_scene_guard_background_is_too_sparse() -> None:
    policy = _policy()
    width = 16
    height = 16
    roi = ConfirmationRoi(
        x=0,
        y=0,
        width=width,
        height=height,
        coordinate_space="source_pixels",
        provenance=RoiProvenance.MANUAL,
    )
    support = tuple(range(64))
    mask = tuple(tuple(index < 64 for index in range(width)) for _ in range(height))
    reference = FastPresenceReference(
        baseline_luma=(100.0,) * (width * height),
        baseline_mask=mask,
        support_indices=support,
        baseline_background=(100.0,) * 192,
        background_indices=tuple(range(64, 256)),
        scene_guard_background=(100.0,) * 4,
        scene_guard_background_indices=(64, 65, 66, 67),
        roi_width=width,
        roi_height=height,
        roi_pixel_count=width * height,
        baseline_support_pixel_count=len(support),
        baseline_support_coverage=0.25,
    )
    image = DecodedRgbImage.from_rows(tuple(tuple((100, 100, 100) for _ in range(width)) for _ in range(height)))
    assert fast_present_comparison(reference, image, roi, policy) is None
