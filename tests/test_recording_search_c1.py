from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from typing import cast

import pytest
import tests.test_investigation_confirmation as confirmation_fixture
from tests.test_recording_search_a2 import successful_a2_run

import vigi_vision.recording_search_service as recording_search_service_module
from vigi_vision.assisted_roi_geometry import ImageSize, Point
from vigi_vision.channel_selection import Channel
from vigi_vision.investigation_confirmation_integrity import compute_jpeg_integrity_from_bytes
from vigi_vision.object_presence_models import BinaryMask, DecodedRgbImage
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording import RecordingSegment, ReplayRequest
from vigi_vision.recording_models import RecordingWindow
from vigi_vision.recording_search_a2_models import (
    BatchDecodeRequest,
    DecodedTargetResult,
    ProbeFrameRequestRecord,
    ProbeRequestStatus,
    SourceTimeBase,
)
from vigi_vision.recording_search_b3_media import DecodedMedia
from vigi_vision.recording_search_b3_models import ClassifyRecordingProbeRequest
from vigi_vision.recording_search_b3_service import RecordingSearchClassificationService
from vigi_vision.recording_search_b4_executor import ThreadedSnapshotClassificationExecutor
from vigi_vision.recording_search_b4_models import (
    ClassificationOperationalError,
    ClassificationOperationalReason,
    ClassificationPublicationOutcome,
    PublishedClassificationResult,
)
from vigi_vision.recording_search_b4_service import ObservationClassificationService
from vigi_vision.recording_search_c1_models import CoarseSampleStatus, CoarseSupportResult
from vigi_vision.recording_search_c1_planner import (
    CoarseSamplingIdentity,
    CoarseSamplingPlan,
    build_coarse_sampling_plan,
    confirmation_run_id_for,
)
from vigi_vision.recording_search_c1_service import CoarseSamplingExecutor
from vigi_vision.recording_search_models import (
    RecordingSearchPolicy,
    RecordingSearchRequest,
    RecordingSearchTerminalReopenCategory,
    RecordingSearchTerminalReopenError,
    default_policy,
)
from vigi_vision.recording_search_repository import RecordingSearchRepository
from vigi_vision.recording_search_service import RecordingSearchRunHandle, RecordingSearchService
from vigi_vision.replay import ReplayClip

_UTC = timezone.utc
_START = datetime(2026, 7, 20, 3, 0, tzinfo=_UTC)


def test_coarse_plan_is_chronological_and_includes_end_boundary() -> None:
    policy = default_policy(_START, _START + timedelta(seconds=750))

    plan = build_coarse_sampling_plan(policy)

    assert plan.target_times == (
        _START + timedelta(seconds=300),
        _START + timedelta(seconds=600),
        _START + timedelta(seconds=750),
    )
    assert plan.target_times == tuple(sorted(plan.target_times))


def test_exact_interval_boundary_does_not_duplicate_end() -> None:
    policy = default_policy(_START, _START + timedelta(seconds=600))

    plan = build_coarse_sampling_plan(policy)

    assert plan.target_times == (
        _START + timedelta(seconds=300),
        _START + timedelta(seconds=600),
    )
    assert len(set(plan.target_times)) == len(plan.target_times)


def test_short_window_has_one_end_target() -> None:
    policy = default_policy(_START, _START + timedelta(seconds=30))

    plan = build_coarse_sampling_plan(policy)

    assert plan.target_times == (_START + timedelta(seconds=30),)


def test_plan_identity_is_stable_for_same_policy() -> None:
    policy = default_policy(_START, _START + timedelta(seconds=750))

    first = build_coarse_sampling_plan(policy)
    second = build_coarse_sampling_plan(policy)

    assert first.plan_id == second.plan_id
    assert first.plan_id.startswith("coarse-plan-")


def test_confirmation_identity_binds_every_execution_owner() -> None:
    plan = build_coarse_sampling_plan(default_policy(_START, _START + timedelta(seconds=750)))
    target = plan.target_times[0]
    identity = CoarseSamplingIdentity("investigation", "search-run", "confirmation", "baseline")
    original = confirmation_run_id_for(plan, target, identity)

    assert (
        confirmation_run_id_for(
            plan, target, replace(identity, investigation_id="other-investigation")
        )
        != original
    )
    assert (
        confirmation_run_id_for(plan, target, replace(identity, search_run_id="other-run"))
        != original
    )
    assert (
        confirmation_run_id_for(
            plan, target, replace(identity, phase6_confirmation_id="other-confirmation")
        )
        != original
    )
    assert (
        confirmation_run_id_for(plan, target, replace(identity, baseline_identity="other-baseline"))
        != original
    )
    changed_plan = build_coarse_sampling_plan(
        default_policy(_START, _START + timedelta(seconds=751))
    )
    assert confirmation_run_id_for(changed_plan, changed_plan.target_times[0], identity) != original


def test_invalid_policy_window_is_rejected() -> None:
    policy = default_policy(_START, _START + timedelta(seconds=30)).model_copy(
        update={"search_start_utc": _START + timedelta(seconds=30)}
    )

    with pytest.raises(ValueError, match=r"^$"):
        _ = build_coarse_sampling_plan(policy)


def test_support_count_cannot_exceed_finite_whole_second_window() -> None:
    policy = default_policy(_START, _START + timedelta(seconds=4)).model_copy(
        update={"absence_confirmation_frames": 6}
    )

    with pytest.raises(ValueError, match=r"^$"):
        _ = build_coarse_sampling_plan(policy)


@dataclass(slots=True)
class _Handle:
    investigation_id: str = "object-disappearance-ch1-20260720T030000Z"
    search_run_id: str = "search-run-abcdef12"
    phase6_confirmation_id: str = "object-disappearance-ch1-20260720T030000Z"
    baseline_identity: str = "baseline-test"
    closed: bool = False


@dataclass(slots=True)
class _Host:
    requests: dict[datetime, ProbeFrameRequestRecord]
    results: dict[str, PublishedClassificationResult | Exception] = field(default_factory=dict)
    acquired: list[datetime] = field(default_factory=list)
    classified: list[str] = field(default_factory=list)

    def acquire_targets(
        self, handle: _Handle, requested_times: tuple[datetime, ...]
    ) -> tuple[ProbeFrameRequestRecord, ...]:
        _ = handle
        self.acquired.extend(requested_times)
        return tuple(
            self.requests.setdefault(
                value,
                _probe_request(value, f"probe-request-extra-{len(self.requests):02d}"),
            )
            for value in requested_times
        )

    def classify(
        self, handle: _Handle, request: ClassifyRecordingProbeRequest
    ) -> PublishedClassificationResult:
        _ = handle
        self.classified.append(request.probe_request_id)
        outcome = self.results[request.probe_request_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _probe_request(
    target: datetime,
    request_id: str,
    status: ProbeRequestStatus = ProbeRequestStatus.SUCCEEDED,
    failure_reason: str | None = None,
) -> ProbeFrameRequestRecord:
    return ProbeFrameRequestRecord(
        record_type="probe_frame_request",
        probe_request_id=request_id,
        investigation_id="object-disappearance-ch1-20260720T030000Z",
        search_run_id="search-run-abcdef12",
        operation_id="acquisition-op-test",
        channel_id=1,
        requested_time_utc=target,
        status=status,
        canonical_frame_id=None if status is ProbeRequestStatus.FAILED else "frame-" + "a" * 64,
        alias_of_probe_request_id=None,
        failure_reason=failure_reason,
        created_at_utc=target,
        completed_at_utc=target,
    )


def _published(
    request_id: str, state: ClassificationOutcome = ClassificationOutcome.PRESENT
) -> PublishedClassificationResult:
    return PublishedClassificationResult(
        ClassificationPublicationOutcome.CREATED,
        "observation-" + "b" * 64,
        None,
        request_id,
        "frame-" + "a" * 64,
        state,
        None,
    )


def test_absent_target_derives_bounded_support_from_one_acquisition_batch() -> None:
    plan = build_coarse_sampling_plan(default_policy(_START, _START + timedelta(seconds=610)))
    first, second, third = plan.target_times
    requests = {
        target: _probe_request(target, f"probe-request-{index:02d}")
        for index, target in enumerate(
            (first, first + timedelta(seconds=1), first + timedelta(seconds=2), second, third),
            start=1,
        )
    }
    host = _Host(
        requests,
        {
            "probe-request-01": _published("probe-request-01", ClassificationOutcome.ABSENT),
            "probe-request-02": _published("probe-request-02", ClassificationOutcome.ABSENT),
            "probe-request-03": _published("probe-request-03", ClassificationOutcome.ABSENT),
            "probe-request-04": _published("probe-request-04"),
            "probe-request-05": _published("probe-request-05"),
        },
    )

    result = CoarseSamplingExecutor(host).execute(_Handle(), plan)

    assert result.complete is True
    assert host.acquired[:3] == [first, first + timedelta(seconds=1), first + timedelta(seconds=2)]
    assert host.classified[:3] == ["probe-request-01", "probe-request-02", "probe-request-03"]
    assert len(result.support_results) == 1
    support = result.support_results[0]
    assert isinstance(support, CoarseSupportResult)
    assert support.origin_target_utc == first
    assert tuple(sample.requested_time_utc for sample in support.samples) == (
        first,
        first + timedelta(seconds=1),
        first + timedelta(seconds=2),
    )
    assert len({sample.probe_request_id for sample in support.samples}) == 3


def test_end_boundary_does_not_request_out_of_window_support() -> None:
    plan = build_coarse_sampling_plan(
        default_policy(_START, _START + timedelta(seconds=302)).model_copy(
            update={"coarse_interval_seconds": 1000}
        )
    )
    target = plan.target_times[-1]
    request = _probe_request(target, "probe-request-end")
    host = _Host(
        {target: request},
        {
            request.probe_request_id: _published(
                request.probe_request_id, ClassificationOutcome.ABSENT
            )
        },
    )

    result = CoarseSamplingExecutor(host).execute(_Handle(), plan)

    assert result.complete is True
    assert host.acquired == [target]
    assert result.support_results == ()


@pytest.mark.parametrize("support_count", [1, 2, 4])
def test_executor_accepts_each_positive_support_count(support_count: int) -> None:
    policy = default_policy(_START, _START + timedelta(seconds=1000)).model_copy(
        update={
            "coarse_interval_seconds": 300,
            "absence_confirmation_frames": support_count,
        }
    )
    plan = build_coarse_sampling_plan(policy)
    origin = plan.target_times[0]
    support_times = tuple(origin + timedelta(seconds=index) for index in range(support_count))
    requested = tuple(sorted(set(plan.target_times) | set(support_times)))
    requests = {
        target: _probe_request(target, f"probe-request-variable-{index:02d}")
        for index, target in enumerate(requested, start=1)
    }
    host = _Host(
        requests,
        {
            request.probe_request_id: _published(
                request.probe_request_id,
                ClassificationOutcome.ABSENT
                if request.requested_time_utc in support_times
                else ClassificationOutcome.PRESENT,
            )
            for request in requests.values()
        },
    )

    result = CoarseSamplingExecutor(host).execute(_Handle(), plan)

    assert result.complete is True
    support = result.support_results[0]
    assert len(support.samples) == support_count
    assert support.support_indices == tuple(range(support_count))
    assert tuple(sample.requested_time_utc for sample in support.samples) == support_times


def test_production_path_acquires_publishes_reopens_and_reuses_support_evidence(  # noqa: C901, PLR0915 - one production-path acceptance scenario.
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = datetime(2026, 7, 20, 3, 34, tzinfo=_UTC)
    jpeg_bytes = cast("bytes", confirmation_fixture.__dict__["_JPEG_BYTES"])
    segment = RecordingSegment(
        1,
        origin.date(),
        int(origin.timestamp()),
        int((origin + timedelta(minutes=1)).timestamp()),
        origin,
        origin + timedelta(minutes=1),
    )

    class BatchDecoder:
        def __init__(self) -> None:
            self.ordinal: int = 0
            self.requested_batches: list[tuple[datetime, ...]] = []

        def decode_targets(
            self,
            acquisition: BatchDecodeRequest,
            ordered_requested_targets: tuple[datetime, ...],
        ) -> tuple[DecodedTargetResult, ...]:
            _ = acquisition
            self.requested_batches.append(tuple(ordered_requested_targets))
            values: list[DecodedTargetResult] = []
            for target in ordered_requested_targets:
                values.append(
                    DecodedTargetResult(
                        requested_time_utc=target,
                        physical_replay_origin_utc=origin,
                        source_pts=int((target - origin).total_seconds()),
                        source_time_base=SourceTimeBase(numerator=1, denominator=1),
                        decoded_pts=int((target - origin).total_seconds()),
                        replay_time_base=SourceTimeBase(numerator=1, denominator=1),
                        decoded_ordinal=self.ordinal,
                        source_width=1280,
                        source_height=720,
                        jpeg_bytes=jpeg_bytes,
                        decode_session_id="decode-session-test",
                    )
                )
                self.ordinal += 1
            return tuple(values)

    @dataclass(frozen=True, slots=True)
    class IntegrationInventory:
        def channels(self) -> tuple[Channel, ...]:
            return (Channel(1, "Counter", "Counter", online=True),)

    @dataclass(slots=True)
    class IntegrationPlanner:
        segment: RecordingSegment

        def find_covering_segment(self, channel_id: int, instant_utc: datetime) -> RecordingSegment:
            assert channel_id == self.segment.channel_id
            assert self.segment.start_utc <= instant_utc < self.segment.end_utc
            return self.segment

        def plan_for_segment(
            self, segment: RecordingSegment, window: RecordingWindow
        ) -> ReplayRequest:
            assert segment == self.segment
            return ReplayRequest(window, "rtsp://example.invalid/replay")

    @dataclass(slots=True)
    class IntegrationExtractor:
        root: Path
        calls: int = 0

        def extract(self, request: ReplayRequest) -> ReplayClip:
            self.calls += 1
            path = self.root / f"clip-{self.calls}.mp4"
            _ = path.write_bytes(b"temporary")
            return ReplayClip(
                channel_id=request.window.channel_id,
                requested_start_utc=request.window.start_utc,
                requested_end_utc=request.window.end_utc,
                replay_url=request.replay_url,
                temporary_mp4_path=path,
                duration_seconds=request.window.duration_seconds,
            )

    decoder = BatchDecoder()
    context = confirmation_fixture.build_context(tmp_path / "confirmation")
    _ = context.service.confirm(confirmation_fixture.build_request(context.resource_id))
    repository = RecordingSearchRepository(
        tmp_path / "searches", confirmation_loader=context.service
    )
    operation_ids = iter(f"acquisition-op-{index}" for index in range(1, 17))
    probe_ids = iter(f"probe-request-{index}" for index in range(1, 17))
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=repository,
        channel_inventory=IntegrationInventory(),
        artifact_root=tmp_path,
        now_utc=lambda: datetime(2026, 8, 2, 4, 5, 6, tzinfo=_UTC),
        recording_planner=IntegrationPlanner(segment),
        replay_extractor=IntegrationExtractor(tmp_path),
        batch_decoder=decoder,
        operation_id_factory=operation_ids.__next__,
        probe_request_id_factory=probe_ids.__next__,
    )
    investigation_id = context.investigation_id
    base_policy = default_policy

    def policy_with_interval(start: datetime, end: datetime) -> RecordingSearchPolicy:
        return base_policy(start, end).model_copy(update={"coarse_interval_seconds": 1})

    monkeypatch.setattr(
        recording_search_service_module,
        "default_policy",
        policy_with_interval,
    )

    class MediaDecoder:
        def __init__(self) -> None:
            self.calls: int = 0
            self.payloads: list[bytes] = []
            width, height = 1280, 720
            baseline_row = tuple((x % 200, x % 200, x % 200) for x in range(width))
            probe_row = tuple((255 - r, 255 - g, 255 - b) for r, g, b in baseline_row)
            self.baseline: DecodedRgbImage = DecodedRgbImage.from_rows((baseline_row,) * height)
            self.probe: DecodedRgbImage = DecodedRgbImage.from_rows((probe_row,) * height)

        def decode(self, payload: bytes, width: int, height: int) -> DecodedMedia:
            self.calls += 1
            self.payloads.append(payload)
            image = self.baseline if self.calls % 2 else self.probe
            return DecodedMedia(compute_jpeg_integrity_from_bytes(payload, width, height), image)

    class MaskPredictor:
        def __init__(self) -> None:
            self.baseline_mask: BinaryMask = BinaryMask.from_rows(
                (tuple(10 <= x < 70 for x in range(1280)),) * 720
            )
            self.probe_mask: BinaryMask = BinaryMask.from_rows(
                (tuple(69 <= x < 130 for x in range(1280)),) * 720
            )

        def predict_from_rgb(
            self, image: DecodedRgbImage, point: Point, size: ImageSize
        ) -> BinaryMask:
            _ = (point, size)
            return self.baseline_mask if image.pixels[0][0][0] < 128 else self.probe_mask

    media_decoder = MediaDecoder()
    preparer = RecordingSearchClassificationService(
        host=service,
        media_decoder=media_decoder,
        mask_predictor=MaskPredictor(),
        policy=ObjectPresenceDecisionPolicy(
            minimum_mask_overlap_for_comparison=0.0,
            minimum_comparison_area=1,
            minimum_roi_pixels=64,
            minimum_clipped_mask_pixels=64,
        ),
    )
    attempt_ids = count(1)
    operation_ids = count(1)
    service.classification_service = ObservationClassificationService(
        host=service,
        preparer=preparer,
        executor=ThreadedSnapshotClassificationExecutor(preparer.classify_snapshot),
        timeout_seconds=5.0,
        now_utc=service.repository.now_utc,
        attempt_id_factory=lambda: f"classification-attempt-integration-{next(attempt_ids)}",
        operation_id_factory=lambda: f"classification-op-integration-{next(operation_ids)}",
    )
    classified_request_ids: list[str] = []
    original_classify = ObservationClassificationService.classify

    def classify_spy(
        classification_service: ObservationClassificationService,
        active_handle: RecordingSearchRunHandle,
        request: ClassifyRecordingProbeRequest,
    ) -> PublishedClassificationResult:
        classified_request_ids.append(request.probe_request_id)
        return original_classify(classification_service, active_handle, request)

    monkeypatch.setattr(ObservationClassificationService, "classify", classify_spy)
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T12:34:24+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None
    handle = started.run_handle
    assert handle.baseline_bytes == jpeg_bytes

    execution = service.execute_coarse_sampling(handle)
    assert execution.complete is True
    assert all(sample.status is CoarseSampleStatus.SUCCESS for sample in execution.samples), [
        (sample.status, sample.safe_reason, sample.classification) for sample in execution.samples
    ]
    assert execution.support_results
    assert classified_request_ids == [
        f"probe-request-{index}" for index in range(1, len(classified_request_ids) + 1)
    ]
    assert decoder.requested_batches
    assert all(batch == tuple(sorted(batch)) for batch in decoder.requested_batches)
    assert decoder.requested_batches[0] == (
        execution.plan.target_times[0],
        execution.plan.target_times[0] + timedelta(seconds=1),
        execution.plan.target_times[0] + timedelta(seconds=2),
    )
    result = service.interpret_coarse_sampling(handle, execution)
    assert result.status.value == "BRACKET_READY", result.safe_reason
    assert result.bracket is not None
    assert len(set(result.bracket.support_probe_request_ids)) == 3
    assert len(set(result.bracket.support_observation_ids)) == 3
    assert len(set(result.bracket.support_canonical_frame_ids)) == 3
    assert result.bracket.support_decode_session_id == "decode-session-test"
    assert media_decoder.payloads
    assert all(payload == handle.baseline_bytes for payload in media_decoder.payloads[::2])
    manifest = service.repository.load(investigation_id, handle.search_run_id)
    assert manifest.schema_version == 3
    assert manifest.state == "RUNNING"
    assert len(manifest.canonical_observation_ids) >= 3
    assert not (
        service.repository.run_path(investigation_id, handle.search_run_id) / "phase8-request.json"
    ).exists()

    run_path = service.repository.run_path(investigation_id, handle.search_run_id)
    before = {
        path.relative_to(run_path): path.read_bytes()
        for path in run_path.rglob("*")
        if path.is_file()
    }
    repeated = service.execute_coarse_sampling(handle)
    repeated_result = service.interpret_coarse_sampling(handle, repeated)
    after = {
        path.relative_to(run_path): path.read_bytes()
        for path in run_path.rglob("*")
        if path.is_file()
    }
    assert repeated_result == result
    assert after == before
    terminalized = service.terminalize(handle, coarse_execution=repeated)
    assert terminalized.publication is not None, terminalized
    assert terminalized.publication.outcome.value == "created"
    published = service.repository.load(investigation_id, handle.search_run_id)
    assert published.schema_version == 4
    assert published.state == terminalized.publication.result.result_kind.value
    assert published.d1_reconstruction is not None

    manifest_path = run_path / "manifest.json"
    original_manifest = manifest_path.read_bytes()
    tampered = cast("dict[str, object]", json.loads(original_manifest))
    reconstruction = cast("dict[str, object]", tampered["d1_reconstruction"])
    narrowed = cast("dict[str, object]", reconstruction["narrowed_bracket"])
    narrowed["manifest_digest"] = "b" * 64
    _ = manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
    try:
        with pytest.raises(RecordingSearchTerminalReopenError) as raised:
            _ = service.reopen_terminal(investigation_id, handle.search_run_id)
        assert raised.value.category is RecordingSearchTerminalReopenCategory.IDENTITY_MISMATCH
    finally:
        _ = manifest_path.write_bytes(original_manifest)
    _ = service.reopen_terminal(investigation_id, handle.search_run_id)
    handle.release()


def test_executor_is_sequential_and_reuses_a2_then_b4() -> None:
    plan = build_coarse_sampling_plan(default_policy(_START, _START + timedelta(seconds=610)))
    requests = {
        target: _probe_request(target, f"probe-request-{index:02d}")
        for index, target in enumerate(plan.target_times, start=1)
    }
    host = _Host(
        requests,
        {
            request.probe_request_id: _published(request.probe_request_id)
            for request in requests.values()
        },
    )

    result = CoarseSamplingExecutor(host).execute(_Handle(), plan)

    assert result.complete is True
    assert host.acquired == [
        plan.target_times[0],
        plan.target_times[0] + timedelta(seconds=1),
        plan.target_times[0] + timedelta(seconds=2),
        plan.target_times[1],
        plan.target_times[1] + timedelta(seconds=1),
        plan.target_times[1] + timedelta(seconds=2),
        plan.target_times[2],
    ]
    assert host.classified == [requests[target].probe_request_id for target in plan.target_times]
    assert [sample.status for sample in result.samples] == [CoarseSampleStatus.SUCCESS] * 3
    assert not hasattr(result, "candidate_interval")


def test_target_failure_does_not_stop_remaining_targets() -> None:
    plan = build_coarse_sampling_plan(default_policy(_START, _START + timedelta(seconds=610)))
    first, second, third = plan.target_times
    requests = {
        first: _probe_request(first, "probe-request-01"),
        second: _probe_request(
            second,
            "probe-request-02",
            ProbeRequestStatus.FAILED,
            "recording_unavailable",
        ),
        third: _probe_request(third, "probe-request-03"),
    }
    host = _Host(
        requests,
        {
            "probe-request-01": _published("probe-request-01"),
            "probe-request-03": _published("probe-request-03"),
        },
    )

    result = CoarseSamplingExecutor(host).execute(_Handle(), plan)

    assert result.complete is True
    assert [sample.status for sample in result.samples] == [
        CoarseSampleStatus.SUCCESS,
        CoarseSampleStatus.RECORDING_UNAVAILABLE,
        CoarseSampleStatus.SUCCESS,
    ]
    assert host.classified == ["probe-request-01", "probe-request-03"]


def test_classifier_timeout_is_safe_and_later_target_runs() -> None:
    plan = build_coarse_sampling_plan(default_policy(_START, _START + timedelta(seconds=610)))
    requests = {
        target: _probe_request(target, f"probe-request-{index:02d}")
        for index, target in enumerate(plan.target_times, start=1)
    }
    values = list(requests.values())
    host = _Host(
        requests,
        {
            values[0].probe_request_id: ClassificationOperationalError(
                ClassificationOperationalReason.CLASSIFIER_TIMEOUT
            ),
            values[1].probe_request_id: _published(values[1].probe_request_id),
            values[2].probe_request_id: _published(values[2].probe_request_id),
        },
    )

    result = CoarseSamplingExecutor(host).execute(_Handle(), plan)

    assert result.complete is True
    assert result.samples[0].status is CoarseSampleStatus.TIMEOUT
    assert result.samples[1].status is CoarseSampleStatus.SUCCESS
    assert len(host.classified) == 3


def test_recording_search_service_builds_plan_from_active_manifest(tmp_path: Path) -> None:
    service, _investigation_id, handle, _manifest, _request = successful_a2_run(tmp_path)

    plan = service.build_coarse_plan(handle)

    assert plan.target_times
    assert plan.target_times[-1] == plan.search_end_utc
    handle.release()


def test_service_execution_path_delegates_each_target_to_a2_then_b4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _investigation_id, handle, _manifest, _request = successful_a2_run(tmp_path)
    plan = build_coarse_sampling_plan(default_policy(_START, _START + timedelta(seconds=610)))
    requests = {
        target: _probe_request(target, f"probe-request-{index:02d}")
        for index, target in enumerate(plan.target_times, start=1)
    }
    acquired: list[datetime] = []
    classified: list[str] = []

    def acquire(
        _service: RecordingSearchService,
        _handle: RecordingSearchRunHandle,
        requested_times: tuple[datetime, ...],
    ) -> tuple[ProbeFrameRequestRecord, ...]:
        acquired.extend(requested_times)
        return tuple(
            requests.get(value, _probe_request(value, f"probe-request-extra-{len(requests):02d}"))
            for value in requested_times
        )

    def classify(
        _service: RecordingSearchService,
        _handle: RecordingSearchRunHandle,
        request: ClassifyRecordingProbeRequest,
    ) -> PublishedClassificationResult:
        classified.append(request.probe_request_id)
        return _published(request.probe_request_id)

    def build_plan(
        _service: RecordingSearchService, _handle: RecordingSearchRunHandle
    ) -> CoarseSamplingPlan:
        return plan

    monkeypatch.setattr(RecordingSearchService, "build_coarse_plan", build_plan)
    monkeypatch.setattr(RecordingSearchService, "acquire_targets", acquire)
    monkeypatch.setattr(RecordingSearchService, "classify", classify)

    result = service.execute_coarse_sampling(handle)

    assert result.complete is True
    assert acquired == [
        plan.target_times[0],
        plan.target_times[0] + timedelta(seconds=1),
        plan.target_times[0] + timedelta(seconds=2),
        plan.target_times[1],
        plan.target_times[1] + timedelta(seconds=1),
        plan.target_times[1] + timedelta(seconds=2),
        plan.target_times[2],
    ]
    assert classified == [request.probe_request_id for request in requests.values()]
    handle.release()
