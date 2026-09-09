from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from anyio import CapacityLimiter
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vigi_vision.investigation_confirmation_models import (
    ConfirmationRoi,
    ConfirmedInvestigationInput,
    RoiProvenance,
)
from vigi_vision.object_presence_values import ClassificationOutcome, DecodedRgbImage
from vigi_vision.recording_models import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.recording_search_7e_public import (
    Phase7EPublicService,
    approved_phase7e_policy,
)
from vigi_vision.recording_search_7e_repository import RecordingSearch7ERepository
from vigi_vision.recording_search_api import install_recording_search_routes
from vigi_vision.recording_search_b3_media import DecodedMedia
from vigi_vision.recording_search_successor import SuccessorPlanService
from vigi_vision.recording_search_successor_acquisition import SuccessorTargetAcquisitionService
from vigi_vision.recording_search_successor_classification import (
    SuccessorClassifierResult,
    SuccessorCoarseClassificationService,
)
from vigi_vision.recording_search_successor_execution import (
    SuccessorExecutionService,
    SuccessorTerminalRepository,
)
from vigi_vision.recording_search_successor_narrowing import SuccessorBinaryNarrowingService
from vigi_vision.reference_frame_decoder import ReferenceFrameDecodeRequest
from vigi_vision.reference_frame_models import DecodedFrameEvidence, TimingPrecisionStatus
from vigi_vision.replay import ReplayClip

UTC = timezone.utc
ANCHOR = datetime(2026, 9, 4, 5, 17, 32, tzinfo=UTC)


def _segment(end: datetime) -> RecordingSegment:
    return RecordingSegment(
        1,
        date(2026, 9, 4),
        int((ANCHOR - timedelta(seconds=1)).timestamp()),
        int(end.timestamp()),
        ANCHOR - timedelta(seconds=1),
        end,
    )


class _Planner:
    def __init__(self, segment: RecordingSegment) -> None:
        self.segment = segment
        self.windows: list[RecordingWindow] = []

    def find_segments_for_window(self, window: RecordingWindow) -> tuple[RecordingSegment, ...]:
        self.windows.append(window)
        return (self.segment,)

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        assert segment.channel_id == self.segment.channel_id
        assert self.segment.start_utc <= window.start_utc < window.end_utc <= self.segment.end_utc
        assert segment.start_utc <= window.start_utc
        assert window.end_utc <= segment.end_utc
        return ReplayRequest(window, "rtsp://redacted.example/replay")


class _Extractor:
    def __init__(self, root: Path, absent_after: datetime) -> None:
        self.root = root
        self.absent_after = absent_after
        self.calls = 0

    def extract(self, request: ReplayRequest) -> ReplayClip:
        self.calls += 1
        path = self.root / f"replay-{self.calls}.mp4"
        marker = b"absent" if request.window.end_utc >= self.absent_after else b"present"
        path.write_bytes(marker)
        return ReplayClip(
            request.window.channel_id,
            request.window.start_utc,
            request.window.end_utc,
            request.replay_url,
            path,
            request.window.duration_seconds,
        )


class _FrameDecoder:
    def decode(self, request: ReferenceFrameDecodeRequest) -> DecodedFrameEvidence:
        payload = request.clip_path.read_bytes()
        request.output_path.write_bytes(payload)
        return DecodedFrameEvidence(
            request.output_path,
            request.target_offset_seconds,
            4,
            4,
            TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
            (),
        )


class _MediaDecoder:
    def decode(self, payload: bytes, width: int, height: int) -> DecodedMedia:
        value = 255 if payload == b"absent" else 0
        image = DecodedRgbImage.from_rows(
            tuple(tuple((value, value, value) for _ in range(width)) for _ in range(height))
        )
        return SimpleNamespace(image=image, integrity=None)


class _Classifier:
    policy_identity = "successor-test-classifier-v1"

    def classify(
        self,
        _baseline: object,
        probe: DecodedRgbImage,
        _width: int,
        _height: int,
        _roi: object,
        _correlation_id: str,
    ) -> SuccessorClassifierResult:
        outcome = (
            ClassificationOutcome.ABSENT if probe.pixels[0][0][0] else ClassificationOutcome.PRESENT
        )
        return SuccessorClassifierResult(outcome)


def _confirmed(tmp_path: Path) -> ConfirmedInvestigationInput:
    path = tmp_path / "baseline.jpg"
    path.write_bytes(b"baseline")
    return ConfirmedInvestigationInput(
        "object-disappearance-v3-ch1-20260904T051732Z",
        1,
        ANCHOR,
        "Asia/Seoul",
        0,
        "reference-frame-resource-v1-test",
        "2026-09-04T14:17:32",
        ANCHOR,
        3,
        "nearest_decoded_frame",
        None,
        None,
        "MEASURED_CLIP_RELATIVE",
        (),
        4,
        4,
        ConfirmationRoi(
            x=0,
            y=0,
            width=4,
            height=4,
            coordinate_space="source_pixels",
            provenance=RoiProvenance.MANUAL,
        ),
        "a" * 64,
        len(b"baseline"),
        path,
    )


def _service(tmp_path: Path, absent_after: datetime) -> SuccessorExecutionService:
    segment = _segment(ANCHOR + timedelta(minutes=30, seconds=1))
    planner = _Planner(segment)
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        _Extractor(tmp_path, absent_after),
        _FrameDecoder(),
        temporary_directory=tmp_path / "temporary",
    )
    classification = SuccessorCoarseClassificationService(_Classifier(), _MediaDecoder())
    return SuccessorExecutionService(
        SuccessorPlanService(planner),
        acquisition,
        classification,
        SuccessorBinaryNarrowingService(acquisition, classification),
        _MediaDecoder(),
        SuccessorTerminalRepository(tmp_path / "successor"),
    )


def test_successor_http_shaped_execution_publishes_found_and_reopens(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    result = service.execute(prepared)
    assert result.status == "FOUND"
    assert result.last_present_time_utc is not None
    assert result.first_absent_time_utc is not None
    assert result.last_present_time_utc < result.first_absent_time_utc
    assert result.phase8_status == "NOT_REQUESTED"
    reopened = service.publisher.read(confirmed.investigation_id, prepared.request.run_id)
    assert reopened is not None
    assert reopened["status"] == "FOUND"
    assert reopened["schema_version"] == 8
    assert reopened["policy_version"] == prepared.plan.policy_version
    assert reopened["requested_end_time_utc"] == "2026-09-04T05:47:32Z"
    assert reopened["coarse_observation_ids"]
    assert reopened["coarse_target_ids"]
    assert reopened["target_statuses"]
    assert reopened["phase8_status"] == "NOT_REQUESTED"
    assert reopened["narrowing_id"]
    assert not tuple((tmp_path / "temporary").glob("**/*"))


def test_successor_complete_present_publishes_not_found(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(hours=2))
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    result = service.execute(prepared)
    assert result.status == "NOT_FOUND"
    assert result.reason_code == "complete_present_coverage"


def test_successor_durable_running_state_recovers_as_interrupted(tmp_path: Path) -> None:
    repository = SuccessorTerminalRepository(tmp_path / "successor")
    service = _service(tmp_path, ANCHOR + timedelta(hours=2))
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-cccccccccccccccccccccccccccccccc",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    repository.publish_running(prepared)
    assert repository.recover_abandoned() == 1
    recovered = repository.read(prepared.request.investigation_id, prepared.request.run_id)
    assert recovered is not None
    assert recovered["status"] == "INTERRUPTED"


def test_successor_runs_through_http_background_and_restart_status(tmp_path: Path) -> None:
    confirmed = _confirmed(tmp_path)
    execution = _service(tmp_path, ANCHOR + timedelta(minutes=15))

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    policy, classifier_policy, object_policy = approved_phase7e_policy()
    service = Phase7EPublicService(
        RecordingSearch7ERepository(tmp_path / "legacy"),
        SimpleNamespace(),
        _Confirmation(),
        None,
        None,
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(status=lambda *_args: (None, None)),
        lambda: ANCHOR + timedelta(hours=1),
        None,
        execution,
    )
    app = FastAPI()
    install_recording_search_routes(app, None, CapacityLimiter(2), phase7e_service=service)
    body = {
        "investigation_id": confirmed.investigation_id,
        "search_end": "2026-09-04T14:47:32",
        "request_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    }
    with TestClient(app) as client:
        accepted = client.post("/api/v1/recording-searches", json=body)
        assert accepted.status_code == 202
        status_url = accepted.json()["status_url"]
        for _ in range(100):
            status = client.get(status_url)
            if status.json()["status"] not in {"ACCEPTED", "RUNNING"}:
                break
        assert status.json()["status"] == "FOUND"
        assert status.json()["schema_version"] == 8
        duplicate = client.post("/api/v1/recording-searches", json=body)
        assert duplicate.status_code == 202
        assert duplicate.json()["status"] == "FOUND"
    restarted = FastAPI()
    install_recording_search_routes(restarted, None, CapacityLimiter(2), phase7e_service=service)
    with TestClient(restarted) as client:
        restored = client.get(status_url)
    assert restored.status_code == 200
    assert restored.json()["status"] == "FOUND"
