# pyright: reportAny=false, reportExplicitAny=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportOptionalMemberAccess=false, reportUnannotatedClassAttribute=false, reportUnusedCallResult=false, reportUnusedParameter=false, reportUnusedFunction=false
# ruff: noqa: ANN401, I001
"""Focused Phase 7E-1C common-session tests."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from vigi_vision.recording import RecordingSegment, ReplayRequest
from vigi_vision.recording_search_7e_1c import (
    CommonSessionAcquirer,
    CommonSessionPolicy,
    CommonSessionRequest,
    CommonSessionReplayTimeoutError,
    DecodedLocalFrame,
    DurableCommonSessionMedia,
    MediaProbeFacts,
    Phase7E1CExecutor,
    admit_frame_then_classify,
    collapse_target_aliases,
    make_decoder_envelope,
    make_frame_envelope,
    rgb24_sha256,
    select_target_index,
)
from vigi_vision.recording_search_7e_models import StrictIdentityEnvelope
from vigi_vision.recording_search_7e_models import (
    ClassificationOperation,
    ClassifierEvidence,
    TargetRequest,
)
from vigi_vision.recording_search_7e_repository import RecordingSearch7ERepository
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


def _jpeg_bytes() -> bytes:
    image = Image.new("RGB", (8, 8), (120, 80, 40))
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=90)
    return stream.getvalue()


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
    acquisition = acquirer.acquire(_request())
    bound = acquisition
    durable = DurableCommonSessionMedia(tmp_path / ".media").publish(bound)
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
    executor = Phase7E1CExecutor(
        repository,
        acquirer,
        DurableCommonSessionMedia(tmp_path / ".media"),
    )
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
    assert getattr(result.run.state, "target_state", None).value == "REQUESTED"


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
    result = Phase7E1CExecutor(
        repository,
        acquirer,
        DurableCommonSessionMedia(tmp_path / ".media"),
    ).execute(
        _request(CommonSessionPolicy.from_payload(policy_envelope.payload)),
        schema5,
        (policy_envelope, plan_envelope, *target_envelopes),
        classifier_policy,
        target_envelopes,
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
        jpeg_bytes=_jpeg_bytes(),
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
    operation = ClassificationOperation(
        investigation_id="inv-01",
        run_id="run-01",
        frame_id=frame_envelope.identity,
        target_request_id=target_envelope.identity,
        baseline_identity="baseline-v3-01",
        classifier_policy_id=classifier_policy.identity,
        attempt=1,
        result_kind="VISUAL",
        outcome="PRESENT",
        reason_code=None,
        classifier_evidence=ClassifierEvidence.model_validate(evidence),
        operational_reason=None,
    )

    class Classifier:
        def classify(self, frame: DecodedLocalFrame, target: object) -> ClassificationOperation:
            return operation

    final = admit_frame_then_classify(
        repository,
        result.acquisition,
        target_envelope,
        decoder_operation,
        frame,
        Classifier(),
        TargetRequest(payload=target_envelope.payload),
        classification_attempt_id="attempt-1",
    )
    assert final.is_schema6
    assert any(record.family == "observation" for record in final.records)
    assert (final.root / "frames" / f"{frame_envelope.identity}.jpg").is_file()
