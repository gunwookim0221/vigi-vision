# pyright: reportAny=false, reportExplicitAny=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportOptionalMemberAccess=false, reportUnannotatedClassAttribute=false, reportUnusedCallResult=false, reportUnusedParameter=false, reportUnusedFunction=false, reportImplicitOverride=false, reportUnknownLambdaType=false
# ruff: noqa: ANN401, I001, PLR0915
"""Focused Phase 7E-1C common-session tests."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import pytest

from vigi_vision.recording import RecordingSegment, ReplayRequest
from vigi_vision.recording_search_7e_1c import (
    CommonSessionAcquirer,
    CommonSessionPolicy,
    CommonSessionRequest,
    CommonSessionReplayTimeoutError,
    CommonSessionRecordingUnavailableError,
    CommonSessionDeadlineError,
    CommonSessionNonmonotonicPtsError,
    CommonSessionRecordingGapError,
    DecodedLocalFrame,
    DurableCommonSessionMedia,
    FfmpegLocalDecoder,
    InvocationBudget,
    MediaProbeFacts,
    Phase7E1CExecutor,
    ProductionB4Adapter,
    ProductionB4Context,
    admit_frame_then_classify,
    collapse_target_aliases,
    execute_local_targets,
    make_decoder_envelope,
    make_frame_envelope,
    rgb24_sha256,
    select_target_index,
    validate_decoded_order,
    validate_repeated_decode,
)
from vigi_vision.object_presence_evidence import RawComparison
from vigi_vision.object_presence_values import ClassificationOutcome, VisualStatus
from vigi_vision.recording_search_b2_identity import observation_id_for
from vigi_vision.recording_search_b2_records import RecordingProbeObservationRecord
from vigi_vision.recording_search_b3_models import ClassifyRecordingProbeRequest
from vigi_vision.recording_search_b4_models import (
    ClassificationPublicationOutcome,
    PublishedClassificationResult,
)
from vigi_vision.recording_search_7e_models import StrictIdentityEnvelope
from vigi_vision.recording_search_7e_repository import (
    Phase7ECorruptError,
    Phase7EInProgressError,
    RecordingSearch7ERepository,
)
from vigi_vision.recording_search_7e_validation import Schema5Envelope
from vigi_vision.recording_search_7e_models import Schema5PhaseState
from vigi_vision.replay import ReplayClip


_DOC = Path(__file__).parents[1] / "docs" / "design" / "object-disappearance-recording-search.md"


def _vectors() -> list[dict[str, Any]]:
    text = _DOC.read_text(encoding="utf-8")
    result: list[dict[str, Any]] = []
    for match in re.finditer(r"```json", text):
        end = text.find("```", match.end())
        if end < 0:
            continue
        try:
            value = json.loads(text[match.end() : end])
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, dict) for item in value)
            and all({"family", "expected_id", "payload"} <= set(item) for item in value)
        ):
            result.extend(value)
    return result


def _env(vector: dict[str, Any]) -> StrictIdentityEnvelope:
    return StrictIdentityEnvelope(
        family=vector["family"],
        identity=vector["expected_id"],
        payload=vector["payload"],
    )


class _Planner:
    def __init__(self, segment: RecordingSegment) -> None:
        self.segment = segment
        self.find_calls = 0
        self.plan_calls = 0

    def find_covering_segment(self, channel_id: int, instant_utc: datetime) -> RecordingSegment:
        self.find_calls += 1
        return self.segment

    def find_segments_for_window(self, window: Any) -> tuple[RecordingSegment, ...]:
        self.find_calls += 1
        return (self.segment,)

    def plan_for_segment(self, segment: RecordingSegment, window: Any) -> ReplayRequest:
        self.plan_calls += 1
        return ReplayRequest(window, "rtsp://redacted.example/replay")


class _Extractor:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    def extract(self, request: ReplayRequest) -> ReplayClip:
        self.calls += 1
        self.path.write_bytes(b"one-retained-session")
        return ReplayClip(
            request.window.channel_id,
            request.window.start_utc,
            request.window.end_utc,
            request.replay_url,
            self.path,
            request.window.duration_seconds,
        )


class _Probe:
    def probe(self, path: Path, timeout_seconds: float) -> MediaProbeFacts:
        return MediaProbeFacts(
            selected_video_stream_index=0,
            video_stream_count=1,
            audio_stream_count=0,
            container_start_pts=0,
            time_base_num=1,
            time_base_den=1,
            duration_ticks=4,
            codec="h264",
            profile="High",
            pixel_format="yuv420p",
            width=8,
            height=8,
            average_frame_rate_num=1,
            average_frame_rate_den=1,
        )


def _request(policy: CommonSessionPolicy | None = None) -> CommonSessionRequest:
    start = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
    return CommonSessionRequest.from_start_and_duration("inv-01", "run-01", 1, start, 4, policy)


def _acquirer(tmp_path: Path) -> tuple[CommonSessionAcquirer, _Extractor, _Planner]:
    start = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
    segment = RecordingSegment(
        1,
        date(2026, 7, 20),
        int(start.timestamp()),
        int((start + timedelta(seconds=30)).timestamp()),
        start,
        start + timedelta(seconds=30),
    )
    planner = _Planner(segment)
    extractor = _Extractor(tmp_path / "replay.mp4")
    return CommonSessionAcquirer(cast("Any", planner), extractor, _Probe()), extractor, planner


def test_one_replay_is_planned_and_cleanup_removes_only_temp_clip(tmp_path: Path) -> None:
    acquirer, extractor, planner = _acquirer(tmp_path)
    acquisition = acquirer.acquire(_request())
    assert extractor.calls == 1
    assert planner.find_calls == 1
    assert planner.plan_calls == 1
    assert acquisition.replay_clip.temporary_mp4_path.is_file()
    acquisition.remove()
    assert not acquisition.replay_clip.temporary_mp4_path.exists()


def test_replay_timeout_is_safe_and_partial_path_is_removed(tmp_path: Path) -> None:
    start = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
    segment = RecordingSegment(
        1,
        date(2026, 7, 20),
        int(start.timestamp()),
        int((start + timedelta(seconds=30)).timestamp()),
        start,
        start + timedelta(seconds=30),
    )
    path = tmp_path / "partial.mp4"

    class TimeoutExtractor:
        def extract(self, request: ReplayRequest) -> ReplayClip:
            path.write_bytes(b"partial")
            path.unlink()
            raise CommonSessionReplayTimeoutError

    acquirer = CommonSessionAcquirer(_Planner(segment), TimeoutExtractor(), _Probe())
    with pytest.raises(CommonSessionReplayTimeoutError):
        acquirer.acquire(_request())
    assert not path.exists()


def test_durable_media_is_reused_and_read_back(tmp_path: Path) -> None:
    acquirer, _, _ = _acquirer(tmp_path)
    request = _request()
    acquisition = acquirer.acquire(request)
    bound = acquisition
    repository = RecordingSearch7ERepository(tmp_path / "runs")
    executor = Phase7E1CExecutor(repository, acquirer)
    with executor.invocation(request) as invocation:
        durable = DurableCommonSessionMedia(repository).publish(bound, invocation)
    assert durable.media_path.is_file()
    assert durable.media_path.read_bytes() == b"one-retained-session"
    acquisition.remove()
    assert durable.media_path.is_file()


def test_logical_end_is_strictly_before_and_ties_are_deterministic() -> None:
    offsets = (0, 1, 2, 3, 4)
    fractions = tuple(Fraction(value, 1) for value in offsets)
    assert select_target_index(fractions, Fraction(2), Fraction(4)) == 2
    assert select_target_index(fractions, Fraction(4), Fraction(4), logical_end=True) == 3
    assert select_target_index((Fraction(0), Fraction(2)), Fraction(1), Fraction(4)) == 0


def test_duplicate_requested_time_is_explicit_alias_not_new_evidence() -> None:
    start = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
    unique, aliases = collapse_target_aliases((start, start, start + timedelta(seconds=1)))
    assert unique == (start, start + timedelta(seconds=1))
    assert aliases == ((1, 0),)


def test_rgb24_identity_uses_exact_interleaved_bytes() -> None:
    raw = bytes(range(3 * 2 * 2))
    assert rgb24_sha256(raw, 2, 2) == hashlib.sha256(raw).hexdigest()


def test_executor_admits_schema6_after_one_common_session(tmp_path: Path) -> None:
    vectors = _vectors()
    by_family = {item["family"]: item for item in vectors}
    targets = [
        item
        for item in vectors
        if item["family"] == "target-request"
        and item["expected_id"]
        in by_family["schema5-manifest"]["payload"]["coarse_target_request_ids"]
    ]
    policy_envelope = _env(by_family["policy"])
    plan_envelope = _env(by_family["coarse-plan"])
    target_envelopes = tuple(_env(item) for item in targets)
    base = (policy_envelope, plan_envelope, *target_envelopes)
    schema5 = _env(by_family["schema5-manifest"])
    classifier_policy = _env(by_family["classifier-policy"])
    policy = CommonSessionPolicy.from_payload(policy_envelope.payload)
    acquirer, extractor, _ = _acquirer(tmp_path)
    repository = RecordingSearch7ERepository(tmp_path / "runs")
    executor = Phase7E1CExecutor(repository, acquirer)
    result = executor.execute(
        _request(policy),
        schema5,
        base,
        classifier_policy,
        target_envelopes,
    )
    assert result.run.is_schema6
    assert extractor.calls == 1
    assert result.acquisition.media_path.is_file()
    assert result.acquisition.media_path.is_relative_to(repository.root / ".media")
    assert getattr(result.run.state, "target_state", None).value == "REQUESTED"


def test_executor_publishes_failed_state_and_releases_owner_on_replay_failure(
    tmp_path: Path,
) -> None:
    vectors = _vectors()
    by_family = {item["family"]: item for item in vectors}
    schema5 = _env(by_family["schema5-manifest"])
    target_ids = set(schema5.payload["coarse_target_request_ids"])
    targets = tuple(
        _env(item)
        for item in vectors
        if item["family"] == "target-request" and item["expected_id"] in target_ids
    )
    policy = _env(by_family["policy"])
    base = (policy, _env(by_family["coarse-plan"]), *targets)
    _unused_acquirer, _, planner = _acquirer(tmp_path)

    class FailingExtractor:
        def extract(self, request: ReplayRequest) -> ReplayClip:
            raise CommonSessionReplayTimeoutError

    repository = RecordingSearch7ERepository(tmp_path / "runs", lock_timeout_seconds=0)
    executor = Phase7E1CExecutor(
        repository,
        CommonSessionAcquirer(planner, FailingExtractor(), _Probe()),
    )
    with pytest.raises(CommonSessionReplayTimeoutError):
        executor.execute(
            _request(CommonSessionPolicy.from_payload(policy.payload)),
            schema5,
            base,
            _env(by_family["classifier-policy"]),
            targets,
        )
    failed = repository.reopen_schema5("inv-01", "run-01")
    assert isinstance(failed.state, Schema5Envelope)
    assert failed.state.run_state == "FAILED"
    assert failed.state.phase_state is Schema5PhaseState.ACQUISITION_FAILED
    assert failed.state.reason_code == "replay_timeout"
    with repository.invocation_ownership("inv-01", "run-01", timeout_seconds=0):
        pass
    media_root = repository.root / ".media"
    assert not media_root.exists() or not tuple(media_root.rglob("*.mp4"))


def test_frame_is_reopened_before_b4_and_observation_is_indexed(tmp_path: Path) -> None:
    vectors = _vectors()
    by_family = {item["family"]: item for item in vectors}
    targets = [
        item
        for item in vectors
        if item["family"] == "target-request"
        and item["expected_id"]
        in by_family["schema5-manifest"]["payload"]["coarse_target_request_ids"]
    ]
    policy_envelope = _env(by_family["policy"])
    classifier_policy = _env(by_family["classifier-policy"])
    plan_envelope = _env(by_family["coarse-plan"])
    target_envelopes = tuple(_env(item) for item in targets)
    schema5 = _env(by_family["schema5-manifest"])
    acquirer, _, _ = _acquirer(tmp_path)
    repository = RecordingSearch7ERepository(tmp_path / "runs")
    request_model = _request(CommonSessionPolicy.from_payload(policy_envelope.payload))
    executor = Phase7E1CExecutor(repository, acquirer)
    invocation_context = executor.invocation(request_model)
    invocation = invocation_context.__enter__()
    result = executor.execute(
        request_model,
        schema5,
        (policy_envelope, plan_envelope, *target_envelopes),
        classifier_policy,
        target_envelopes,
        invocation=invocation,
    )
    target_envelope = target_envelopes[0]
    decoder_operation = make_decoder_envelope(result.acquisition, 1, [target_envelope.identity])
    frame = DecodedLocalFrame(
        requested_time_utc=result.acquisition.request.start_utc,
        raw_pts=1,
        ordinal=1,
        width=8,
        height=8,
        rgb24_bytes=bytes(range(8 * 8 * 3)),
        decode_session_id=result.acquisition.common_session_id,
        container_start_pts=0,
        time_base_num=1,
        time_base_den=1,
    )
    frame_envelope = make_frame_envelope(
        result.acquisition,
        decoder_operation.identity,
        target_envelope.identity,
        frame,
    )
    evidence = {
        "baseline_mask_pixel_count": 64,
        "probe_mask_pixel_count": 64,
        "roi_pixel_count": 128,
        "mask_intersection_pixel_count": 32,
        "mask_union_pixel_count": 96,
        "baseline_mask_coverage": "0.500000",
        "probe_mask_coverage": "0.500000",
        "mask_iou": "0.333333",
        "effective_comparison_area": 32,
        "roi_luma_ncc": "0.700000",
        "visual_status": "comparable",
        "unusable_reason": None,
    }
    comparison = RawComparison.model_validate(
        {
            **evidence,
            "baseline_mask_coverage": 0.5,
            "probe_mask_coverage": 0.5,
            "mask_iou": 0.333333,
            "roi_luma_ncc": 0.7,
            "visual_status": VisualStatus.COMPARABLE,
        }
    )
    baseline_id = "baseline-" + "a" * 64
    canonical_frame_id = "canonical-frame-01"
    policy_version = "phase7e-policy-v1"
    observation_id = observation_id_for(
        investigation_id="inv-01",
        search_run_id="run-01",
        channel_id=1,
        baseline_observation_id=baseline_id,
        canonical_frame_id=canonical_frame_id,
        classifier_policy_version=policy_version,
    )
    published_at = result.acquisition.request.start_utc.replace(microsecond=1)
    observation = RecordingProbeObservationRecord(
        record_type="recording_probe",
        observation_id=observation_id,
        investigation_id="inv-01",
        search_run_id="run-01",
        channel_id=1,
        classification_operation_id="classification-op-phase7e",
        baseline_observation_id=baseline_id,
        canonical_frame_id=canonical_frame_id,
        primary_probe_request_id="probe-phase7e",
        primary_requested_time_utc=result.acquisition.request.start_utc,
        classifier_policy_version=policy_version,
        state=ClassificationOutcome.PRESENT,
        reason_code=None,
        classifier_evidence=comparison,
        published_at_utc=published_at,
    )
    published = PublishedClassificationResult(
        ClassificationPublicationOutcome.CREATED,
        observation_id,
        None,
        "probe-phase7e",
        canonical_frame_id,
        ClassificationOutcome.PRESENT,
        None,
    )
    request = ClassifyRecordingProbeRequest("inv-01", "run-01", "probe-phase7e")
    handle = object()
    calls: list[str] = []

    class Service:
        def classify(self, received_handle: object, received_request: object) -> object:
            assert received_handle is handle
            assert received_request == request
            calls.append("production-b4")
            return published

    def context(authoritative: Any) -> ProductionB4Context:
        assert authoritative.frame is not frame
        assert authoritative.frame.rgb24_bytes != frame.rgb24_bytes
        assert authoritative.frame_jpeg_bytes == final_jpeg.read_bytes()
        calls.append("strict-readback")
        return ProductionB4Context(
            cast("Any", handle), request, baseline_id, lambda _result: observation
        )

    adapter = ProductionB4Adapter(cast("Any", Service()), cast("Any", context))

    class Classifier:
        def classify(self, authoritative: Any) -> object:
            return adapter.classify(authoritative)

    final_jpeg = result.run.root / "frames" / f"{frame_envelope.identity}.jpg"

    final = admit_frame_then_classify(
        repository,
        result.acquisition,
        target_envelope,
        decoder_operation,
        frame,
        Classifier(),
        classification_attempt_id="attempt-1",
        invocation=invocation,
    )
    invocation_context.__exit__(None, None, None)
    assert final.is_schema6
    assert calls == ["strict-readback", "production-b4"]
    assert any(record.family == "observation" for record in final.records)
    persisted = final.root / "frames" / f"{frame_envelope.identity}.jpg"
    assert persisted.is_file()
    before_manifest = (final.root / "manifest.json").read_bytes()
    persisted.write_bytes(persisted.read_bytes()[:-1])
    with pytest.raises(Phase7ECorruptError):
        repository.reopen_schema6("inv-01", "run-01")
    assert (final.root / "manifest.json").read_bytes() == before_manifest


def test_strict_reopen_rehashes_and_reprobes_retained_mp4(tmp_path: Path) -> None:
    vectors = _vectors()
    by_family = {item["family"]: item for item in vectors}
    targets = tuple(
        _env(item)
        for item in vectors
        if item["family"] == "target-request"
        and item["expected_id"]
        in by_family["schema5-manifest"]["payload"]["coarse_target_request_ids"]
    )
    policy = _env(by_family["policy"])
    acquirer, _, _ = _acquirer(tmp_path)
    repository = RecordingSearch7ERepository(tmp_path / "runs")
    result = Phase7E1CExecutor(repository, acquirer).execute(
        _request(CommonSessionPolicy.from_payload(policy.payload)),
        _env(by_family["schema5-manifest"]),
        (policy, _env(by_family["coarse-plan"]), *targets),
        _env(by_family["classifier-policy"]),
        targets,
    )
    before = (result.run.root / "manifest.json").read_bytes()
    archive = next((result.run.root / "manifests").glob("*.json"))
    archived_bytes = archive.read_bytes()
    archived_document = json.loads(archived_bytes)
    archived_document["state"].update(
        {
            "run_state": "RUNNING",
            "phase_state": "PLANNED",
            "active_replay_operation_id": None,
            "reason_code": None,
            "attempt_count": 0,
        }
    )
    archive.write_text(json.dumps(archived_document), encoding="utf-8")
    changed_archive = archive.read_bytes()
    with pytest.raises(Phase7ECorruptError):
        repository.reopen_schema6("inv-01", "run-01")
    assert archive.read_bytes() == changed_archive
    archive.write_bytes(archived_bytes)
    result.acquisition.media_path.write_bytes(b"changed-retained-media")
    with pytest.raises(Phase7ECorruptError):
        repository.reopen_schema6("inv-01", "run-01")
    assert (result.run.root / "manifest.json").read_bytes() == before


def test_invocation_owner_blocks_recovery_and_unrelated_investigation_can_run(
    tmp_path: Path,
) -> None:
    vectors = _vectors()
    by_family = {item["family"]: item for item in vectors}
    manifest = _env(by_family["schema5-manifest"])
    target_ids = set(manifest.payload["coarse_target_request_ids"])
    records = tuple(
        _env(item)
        for item in vectors
        if item["family"] in {"policy", "coarse-plan"}
        or (item["family"] == "target-request" and item["expected_id"] in target_ids)
    )
    repository = RecordingSearch7ERepository(tmp_path / "runs", lock_timeout_seconds=0)
    repository.create_schema5(
        manifest,
        Schema5Envelope(
            run_state="RUNNING",
            phase_state=Schema5PhaseState.PLANNED,
            active_replay_operation_id=None,
            reason_code=None,
            attempt_count=0,
        ),
        records,
    )
    with repository.invocation_ownership("inv-01", "run-01", timeout_seconds=0):
        with pytest.raises(Phase7EInProgressError):
            repository.recover_active("inv-01", "run-01")
        with repository.invocation_ownership("inv-02", "run-02", timeout_seconds=0):
            pass
    recovered = repository.recover_active("inv-01", "run-01")
    assert recovered.state.run_state == "INTERRUPTED"


def test_released_or_foreign_invocation_owner_is_rejected(tmp_path: Path) -> None:
    first = RecordingSearch7ERepository(tmp_path / "first")
    second = RecordingSearch7ERepository(tmp_path / "second")
    with (
        first.invocation_ownership("inv-01", "run-01", timeout_seconds=0) as owner,
        pytest.raises(Phase7EInProgressError),
    ):
        owner.validate(second, "inv-01", "run-01")
    with pytest.raises(Phase7EInProgressError):
        owner.validate(first, "inv-01", "run-01")


def test_one_budget_shrinks_timeouts_and_preserves_cleanup_reserve() -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    policy = CommonSessionPolicy(invocation_deadline_seconds=100, cleanup_reserve_seconds=60)
    budget = InvocationBudget(policy, clock)
    assert budget.operation_timeout(120) == 40
    clock.value = 15
    assert budget.operation_timeout(120) == 25
    clock.value = 40
    with pytest.raises(CommonSessionDeadlineError):
        budget.operation_timeout(1, minimum_start_seconds=0.001)
    assert budget.cleanup_remaining() == 60


def test_repeated_decoder_passes_share_the_same_deadline(tmp_path: Path) -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    policy = CommonSessionPolicy(invocation_deadline_seconds=200, cleanup_reserve_seconds=60)
    request = _request(policy)
    acquirer, _, _ = _acquirer(tmp_path)
    acquisition = acquirer.acquire(request)
    received: list[float] = []

    class DecoderFake:
        def decode(
            self,
            session: Any,
            targets: tuple[datetime, ...],
            timeout_seconds: float,
        ) -> tuple[DecodedLocalFrame, ...]:
            received.append(timeout_seconds)
            clock.value += 30
            ordinal = len(received) - 1
            value = len(received)
            return (
                DecodedLocalFrame(
                    targets[0],
                    ordinal,
                    ordinal,
                    1,
                    1,
                    bytes((value, value, value)),
                    decode_session_id=session.common_session_id,
                ),
            )

    budget = InvocationBudget(policy, clock)
    execute_local_targets(
        acquisition,
        DecoderFake(),
        (request.start_utc,),
        budget=budget,
    )
    execute_local_targets(
        acquisition,
        DecoderFake(),
        (request.start_utc + timedelta(seconds=1),),
        pass_number=2,
        budget=budget,
    )
    assert received == [100, 70]


def test_unique_full_window_segment_is_required(tmp_path: Path) -> None:
    acquirer, _, planner = _acquirer(tmp_path)
    request = _request()
    assert acquirer.locate(request) == planner.segment

    class AmbiguousPlanner(_Planner):
        def find_segments_for_window(self, window: Any) -> tuple[RecordingSegment, ...]:
            return (self.segment, self.segment)

    ambiguous = CommonSessionAcquirer(
        AmbiguousPlanner(planner.segment), _Extractor(tmp_path / "x"), _Probe()
    )
    with pytest.raises(CommonSessionRecordingUnavailableError):
        ambiguous.locate(request)
    partial = RecordingSegment(
        1,
        planner.segment.recording_day,
        planner.segment.start_epoch_seconds,
        planner.segment.end_epoch_seconds,
        request.start_utc + timedelta(seconds=1),
        request.end_utc,
    )
    with pytest.raises(CommonSessionRecordingUnavailableError):
        CommonSessionAcquirer(_Planner(partial), _Extractor(tmp_path / "y"), _Probe()).locate(
            request
        )


def test_nonzero_start_pts_is_subtracted_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    acquirer, _, _ = _acquirer(tmp_path)
    acquisition = acquirer.acquire(_request())
    media = MediaProbeFacts(
        selected_video_stream_index=0,
        video_stream_count=1,
        audio_stream_count=0,
        container_start_pts=1_000,
        time_base_num=1,
        time_base_den=100,
        duration_ticks=400,
        width=1,
        height=1,
        average_frame_rate_num=1,
        average_frame_rate_den=1,
    )
    acquisition = type(acquisition)(
        acquisition.request,
        acquisition.segment,
        acquisition.replay_request,
        acquisition.replay_clip,
        media,
        acquisition.session,
        acquisition.retained_mp4_path,
    )
    probe = subprocess.CompletedProcess(
        (),
        0,
        json.dumps(
            {
                "frames": [
                    {"best_effort_timestamp": str(value)} for value in (1000, 1100, 1200, 1300)
                ]
            }
        ),
        "",
    )
    monkeypatch.setattr(
        "vigi_vision.recording_search_7e_1c.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess((), 0, b"\x01\x02\x03", b""),
    )
    decoder = FfmpegLocalDecoder(Path("ffmpeg"), Path("ffprobe"), lambda _args, _timeout: probe)
    frame = decoder.decode(
        acquisition,
        (acquisition.request.start_utc + timedelta(seconds=1),),
        10,
    )[0]
    assert frame.raw_pts == 1100
    assert frame.decoded_offset == Fraction(1, 1)


def test_timing_rejects_backward_pts_and_cross_pass_redefinition() -> None:
    start = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)

    def frame(raw_pts: int, ordinal: int, value: int = 1) -> DecodedLocalFrame:
        return DecodedLocalFrame(
            start + timedelta(seconds=ordinal),
            raw_pts,
            ordinal,
            1,
            1,
            bytes((value, value, value)),
            decode_session_id="session",
            container_start_pts=100,
            time_base_num=1,
            time_base_den=10,
        )

    valid = (frame(100, 0), frame(110, 1, 2))
    validate_decoded_order(valid)
    with pytest.raises(CommonSessionNonmonotonicPtsError):
        validate_decoded_order((frame(110, 0), frame(109, 1, 2)))
    with pytest.raises(CommonSessionRecordingGapError):
        validate_repeated_decode(valid, (frame(100, 0), frame(110, 1, 3)))
