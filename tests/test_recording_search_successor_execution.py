from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest
from anyio import CapacityLimiter
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vigi_vision.investigation_confirmation_api import install_investigation_confirmation_routes
from vigi_vision.investigation_confirmation_models import (
    ConfirmationManifest,
    ConfirmationRecord,
    ConfirmationReferenceFrame,
    ConfirmationRoi,
    ConfirmationTiming,
    ConfirmedInvestigationInput,
    RoiProvenance,
    artifact_relative_path,
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
from vigi_vision.recording_search_successor_evidence import SuccessorEvidenceRepository
from vigi_vision.recording_search_successor_execution import (
    SuccessorExecutionError,
    SuccessorExecutionService,
    SuccessorTerminalRepository,
)
from vigi_vision.recording_search_successor_narrowing import SuccessorBinaryNarrowingService
from vigi_vision.reference_frame_decoder import ReferenceFrameDecodeRequest
from vigi_vision.reference_frame_models import (
    DecodedFrameEvidence,
    FrameSelectionPolicy,
    TimingPrecisionStatus,
)
from vigi_vision.reference_frame_web_ui import install_reference_frame_web_ui
from vigi_vision.replay import ReplayClip, ReplayTimeoutError

UTC = timezone.utc
ANCHOR = datetime(2026, 9, 4, 5, 17, 32, tzinfo=UTC)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _http_json(
    base_url: str, path: str, body: dict[str, object] | None = None
) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(  # noqa: S310 - loopback URL is fixed by the test.
        f"{base_url}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urlopen(request, timeout=3) as response:  # noqa: S310 - loopback test URL.
        return response.status, json.load(response)


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
        self.requests: list[ReplayRequest] = []

    def extract(self, request: ReplayRequest) -> ReplayClip:
        self.calls += 1
        self.requests.append(request)
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


class _LateTargetTimeoutExtractor(_Extractor):
    """Fail only targets well after the first coarse bracket."""

    def extract(self, request: ReplayRequest) -> ReplayClip:
        if request.window.start_utc >= ANCHOR + timedelta(minutes=15):
            self.calls += 1
            self.requests.append(request)
            raise ReplayTimeoutError
        return super().extract(request)


class _FirstCoarseTimeoutExtractor(_Extractor):
    """Fail the first post-anchor target before any bracket can be formed."""

    def extract(self, request: ReplayRequest) -> ReplayClip:
        if request.window.start_utc > ANCHOR + timedelta(seconds=1):
            self.calls += 1
            self.requests.append(request)
            raise ReplayTimeoutError
        return super().extract(request)


class _ClassificationProxy:
    """Record coarse classification calls while delegating production behavior."""

    def __init__(self, delegate: SuccessorCoarseClassificationService) -> None:
        self.delegate = delegate
        self.coarse_sequences: list[int] = []

    def classify_anchor_target(self, *args: object, **kwargs: object) -> object:
        return self.delegate.classify_anchor_target(*args, **kwargs)  # type: ignore[arg-type]

    def classify_coarse_target(
        self, plan: object, target: object, *args: object, **kwargs: object
    ) -> object:
        self.coarse_sequences.append(target.sequence)  # type: ignore[union-attr]
        return self.delegate.classify_coarse_target(plan, target, *args, **kwargs)  # type: ignore[arg-type]


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


class _IndeterminateClassifier(_Classifier):
    def classify(
        self,
        _baseline: object,
        _probe: DecodedRgbImage,
        _width: int,
        _height: int,
        _roi: object,
        _correlation_id: str,
    ) -> SuccessorClassifierResult:
        return SuccessorClassifierResult(
            ClassificationOutcome.INDETERMINATE,
            "insufficient_visual_evidence",
        )


class _AnchorIndeterminateThenPresentClassifier(_Classifier):
    """Model an uncertain anchor followed by a usable final target."""

    def __init__(self) -> None:
        self.calls = 0

    def classify(
        self,
        _baseline: object,
        _probe: DecodedRgbImage,
        _width: int,
        _height: int,
        _roi: object,
        _correlation_id: str,
    ) -> SuccessorClassifierResult:
        self.calls += 1
        if self.calls == 1:
            return SuccessorClassifierResult(
                ClassificationOutcome.INDETERMINATE,
                "insufficient_visual_evidence",
            )
        return SuccessorClassifierResult(ClassificationOutcome.PRESENT)


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


def create_successor_browser_uvicorn_app() -> FastAPI:
    """Build a real-Uvicorn successor app with only deterministic local doubles."""
    root = Path(os.environ["VIGI_SUCCESSOR_BROWSER_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    confirmed = _confirmed(root)
    execution = _service(root, ANCHOR + timedelta(minutes=15))

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            if investigation_id != confirmed.investigation_id:
                raise AssertionError
            return confirmed

        def load_confirmation_manifest(self, investigation_id: str) -> ConfirmationManifest:
            if investigation_id != confirmed.investigation_id:
                raise AssertionError
            return ConfirmationManifest(
                schema_version=3,
                investigation_id=confirmed.investigation_id,
                investigation_kind="object_disappearance",
                scenario_id="object-disappearance",
                status="confirmed",
                anchor_time_utc=confirmed.anchor_time_utc,
                source_timezone=confirmed.source_timezone,
                confirmed_at_utc=confirmed.anchor_time_utc,
                artifact_directory_relative=artifact_relative_path(confirmed.investigation_id),
                confirmation=ConfirmationRecord(
                    channel_id=confirmed.channel_id,
                    candidate_offset_seconds=confirmed.candidate_offset_seconds,
                    reference_frame=ConfirmationReferenceFrame(
                        resource_id=confirmed.reference_frame_resource_id,
                        schema_version=1,
                        generation_policy_version=confirmed.generation_policy_version,
                        requested_time=confirmed.requested_time_text,
                        requested_time_utc=confirmed.requested_time_utc,
                        source_timezone=confirmed.source_timezone,
                        frame_selection_policy=FrameSelectionPolicy(
                            confirmed.frame_selection_policy
                        ),
                        width=confirmed.source_width,
                        height=confirmed.source_height,
                        jpeg_sha256=confirmed.jpeg_sha256,
                        jpeg_size_bytes=confirmed.jpeg_size_bytes,
                    ),
                    timing=ConfirmationTiming(
                        decoded_local_pts_seconds=confirmed.decoded_local_pts_seconds,
                        estimated_source_time_utc=confirmed.estimated_source_time_utc,
                        offset_from_requested_seconds=None,
                        timing_precision_status=TimingPrecisionStatus(
                            confirmed.timing_precision_status.lower()
                        ),
                        warnings=confirmed.warnings,
                    ),
                    roi=confirmed.roi,
                ),
            )

    policy, classifier_policy, object_policy = approved_phase7e_policy()
    service = Phase7EPublicService(
        RecordingSearch7ERepository(root / "legacy"),
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
    install_reference_frame_web_ui(app)
    limiter = CapacityLimiter(2)
    install_investigation_confirmation_routes(app, _Confirmation(), limiter)
    install_recording_search_routes(app, None, limiter, phase7e_service=service)
    return app


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


def test_historical_baseline_and_actual_anchor_are_durable_and_narrowable(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(seconds=1))
    confirmed = replace(
        _confirmed(tmp_path),
        requested_time_utc=ANCHOR - timedelta(seconds=60),
        requested_time_text="2026-09-04T14:16:32",
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:27:32",
        run_id="search-run-hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "FOUND"
    records = result.coarse_observations
    assert records[0]["state"] == "PRESENT"
    assert records[0]["requested_time_utc"] == "2026-09-04T05:16:32Z"
    assert records[0]["frame_utc"] == "2026-09-04T05:16:32Z"
    assert records[0]["frame_sha256"] == confirmed.jpeg_sha256
    assert records[1]["requested_time_utc"] == "2026-09-04T05:17:32Z"
    assert records[1]["state"] == "ABSENT"
    assert records[0]["observation_id"] != records[1]["observation_id"]
    assert result.last_present_time_utc is not None
    assert result.first_absent_time_utc is not None
    assert result.last_present_time_utc < result.first_absent_time_utc


def test_first_coarse_bracket_stops_later_replay_and_classification(tmp_path: Path) -> None:
    segment = _segment(ANCHOR + timedelta(minutes=30, seconds=1))
    planner = _Planner(segment)
    extractor = _LateTargetTimeoutExtractor(tmp_path, ANCHOR + timedelta(minutes=10))
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        extractor,
        _FrameDecoder(),
        temporary_directory=tmp_path / "temporary",
    )
    classification = SuccessorCoarseClassificationService(_Classifier(), _MediaDecoder())
    proxy = _ClassificationProxy(classification)
    service = SuccessorExecutionService(
        SuccessorPlanService(planner),
        acquisition,
        proxy,  # type: ignore[arg-type]
        SuccessorBinaryNarrowingService(acquisition, classification),
        _MediaDecoder(),
        SuccessorTerminalRepository(tmp_path / "successor"),
    )
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-stopafterbracket00000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "FOUND"
    assert proxy.coarse_sequences == [1]
    assert all(
        call.window.start_utc < ANCHOR + timedelta(minutes=15) for call in extractor.requests
    )


def test_coarse_timeout_before_bracket_publishes_safe_inconclusive(
    tmp_path: Path,
) -> None:
    segment = _segment(ANCHOR + timedelta(minutes=30, seconds=1))
    planner = _Planner(segment)
    extractor = _FirstCoarseTimeoutExtractor(tmp_path, ANCHOR + timedelta(hours=2))
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        extractor,
        _FrameDecoder(),
        temporary_directory=tmp_path / "temporary",
    )
    classification = SuccessorCoarseClassificationService(_Classifier(), _MediaDecoder())
    service = SuccessorExecutionService(
        SuccessorPlanService(planner),
        acquisition,
        classification,
        SuccessorBinaryNarrowingService(acquisition, classification),
        _MediaDecoder(),
        SuccessorTerminalRepository(tmp_path / "successor"),
    )
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:27:32",
        run_id="search-run-timeoutbeforebracket000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "INCONCLUSIVE"
    assert result.reason_code == "target_replay_timeout"
    assert len(extractor.requests) == 2


def test_future_baseline_moves_successor_effective_start(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    confirmed = replace(
        _confirmed(tmp_path),
        requested_time_utc=ANCHOR + timedelta(seconds=60),
        requested_time_text="2026-09-04T14:18:32",
    )

    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-jjjjjjjjjjjjjjjjjjjjjjjjjjjjjjjj",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    assert prepared.plan.anchor_time_utc == ANCHOR + timedelta(seconds=60)
    assert prepared.request.anchor_time_utc == ANCHOR + timedelta(seconds=60)
    assert prepared.request.duration_seconds == 1_740


def test_visual_indeterminate_reason_is_not_collapsed_in_terminal(tmp_path: Path) -> None:
    segment = _segment(ANCHOR + timedelta(minutes=30, seconds=1))
    planner = _Planner(segment)
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        _Extractor(tmp_path, ANCHOR + timedelta(hours=2)),
        _FrameDecoder(),
        temporary_directory=tmp_path / "temporary",
    )
    classification = SuccessorCoarseClassificationService(
        _IndeterminateClassifier(), _MediaDecoder()
    )
    service = SuccessorExecutionService(
        SuccessorPlanService(planner),
        acquisition,
        classification,
        SuccessorBinaryNarrowingService(acquisition, classification),
        _MediaDecoder(),
        SuccessorTerminalRepository(tmp_path / "successor"),
    )
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:27:32",
        run_id="search-run-iiiiiiiiiiiiiiiiiiiiiiiiiiiiiiii",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "INCONCLUSIVE"
    assert result.reason_code == "insufficient_visual_evidence"
    assert result.coarse_observations
    assert any(
        item["reason_code"] == "insufficient_visual_evidence" for item in result.coarse_observations
    )


def test_anchor_indeterminate_still_acquires_and_publishes_search_end_evidence(
    tmp_path: Path,
) -> None:
    segment = _segment(ANCHOR + timedelta(minutes=30, seconds=1))
    planner = _Planner(segment)
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        _Extractor(tmp_path, ANCHOR + timedelta(hours=2)),
        _FrameDecoder(),
        temporary_directory=tmp_path / "temporary",
    )
    classifier = _AnchorIndeterminateThenPresentClassifier()
    classification = SuccessorCoarseClassificationService(classifier, _MediaDecoder())
    service = SuccessorExecutionService(
        SuccessorPlanService(planner),
        acquisition,
        classification,
        SuccessorBinaryNarrowingService(acquisition, classification),
        _MediaDecoder(),
        SuccessorTerminalRepository(tmp_path / "successor"),
        SuccessorEvidenceRepository(tmp_path / "successor"),
    )
    confirmed = replace(
        _confirmed(tmp_path),
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:27:32",
        run_id="search-run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "INCONCLUSIVE"
    assert result.reason_code == "insufficient_visual_evidence"
    assert classifier.calls == 2
    assert tuple(item["requested_time_utc"] for item in result.coarse_observations) == (
        "2026-09-04T05:17:32Z",
        "2026-09-04T05:17:32Z",
        "2026-09-04T05:27:32Z",
    )
    evidence = service.evidence_repository.read(confirmed.investigation_id, prepared.request.run_id)
    assert evidence is not None
    observed = [item for item in evidence["entries"] if item["role"] == "observation"]
    assert len(observed) == 1
    assert observed[0]["requested_time_utc"] == "2026-09-04T05:27:32.000000Z"


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


def test_schema8_reopen_rejects_coercion_and_malformed_lists(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-strictterminal0000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    _ = service.execute(prepared)
    path = (
        tmp_path
        / "successor"
        / prepared.request.investigation_id
        / prepared.request.run_id
        / "terminal.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["coverage_complete"] = "false"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SuccessorExecutionError, match="successor_publication_corrupt"):
        service.publisher.read(prepared.request.investigation_id, prepared.request.run_id)

    payload["coverage_complete"] = False
    payload["coarse_target_ids"] = [1]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SuccessorExecutionError, match="successor_publication_corrupt"):
        service.publisher.read(prepared.request.investigation_id, prepared.request.run_id)


def test_schema8_reopen_rejects_timestamp_identity_and_phase8_corruption(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-stricttime0000000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    _ = service.execute(prepared)
    path = (
        tmp_path
        / "successor"
        / prepared.request.investigation_id
        / prepared.request.run_id
        / "terminal.json"
    )
    baseline = json.loads(path.read_text(encoding="utf-8"))
    mutations = (
        {"observed_start_time_utc": "not-a-timestamp"},
        {"investigation_id": "foreign-investigation"},
        {"phase8_status": "READY", "phase8_reason": None},
        {"reason_code": 7},
    )
    for mutation in mutations:
        candidate = {**baseline, **mutation}
        path.write_text(json.dumps(candidate), encoding="utf-8")
        with pytest.raises(SuccessorExecutionError, match="successor_publication_corrupt"):
            service.publisher.read(prepared.request.investigation_id, prepared.request.run_id)
    reduced = {
        key: value
        for key, value in baseline.items()
        if key
        not in {
            "phase8_status",
            "phase8_reason",
            "policy_version",
            "requested_end_time_utc",
            "narrowing_id",
            "coverage",
            "gaps",
            "coarse_observation_ids",
            "coarse_target_ids",
            "target_statuses",
            "coarse_observations",
        }
    }
    path.write_text(json.dumps(reduced), encoding="utf-8")
    reopened = service.publisher.read(prepared.request.investigation_id, prepared.request.run_id)
    assert reopened is not None
    assert reopened["status"] == "FOUND"


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
        lambda: ANCHOR + timedelta(hours=3, microseconds=123456),
        None,
        execution,
    )
    for index, search_end in enumerate(
        (
            "2026-09-04T14:27:32",
            "2026-09-04T14:47:32",
            "2026-09-04T15:17:32",
            "2026-09-04T16:17:32",
        ),
        start=1,
    ):
        prepared = service.prepare_http(
            confirmed.investigation_id,
            search_end,
            f"{index:08x}-0000-4000-8000-000000000000",
        )
        assert prepared.successor is not None
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
        ten_minute = client.post(
            "/api/v1/recording-searches",
            json={
                "investigation_id": confirmed.investigation_id,
                "search_end": "2026-09-04T14:27:32",
                "request_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            },
        )
        assert ten_minute.status_code == 202
        ten_status = client.get(ten_minute.json()["status_url"])
        for _ in range(100):
            if ten_status.json()["status"] not in {"ACCEPTED", "RUNNING"}:
                break
            ten_status = client.get(ten_minute.json()["status_url"])
        assert ten_status.json()["status"] == "NOT_FOUND"
        assert ten_status.json()["schema_version"] == 8
    restarted = FastAPI()
    install_recording_search_routes(restarted, None, CapacityLimiter(2), phase7e_service=service)
    with TestClient(restarted) as client:
        restored = client.get(status_url)
    assert restored.status_code == 200
    assert restored.json()["status"] == "FOUND"


def test_successor_browser_surface_runs_through_uvicorn_http_and_reload(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """Exercise static UI delivery and Schema 8 background polling over real Uvicorn."""
    port = _free_port()
    environment = os.environ.copy()
    environment["VIGI_SUCCESSOR_BROWSER_ROOT"] = os.fspath(tmp_path)
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "test_recording_search_successor_execution:create_successor_browser_uvicorn_app",
        "--factory",
        "--app-dir",
        "tests",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    process = subprocess.Popen(  # noqa: S603 - fixed local test command.
        command,
        cwd=Path(__file__).parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError
            try:
                response = urlopen(f"{base_url}/", timeout=3)  # noqa: S310 - loopback test URL.
                if response.status == 200:
                    response.close()
                    break
            except Exception:  # noqa: BLE001 - bounded readiness probe.
                time.sleep(0.05)
        else:
            raise AssertionError
        page = urlopen(f"{base_url}/", timeout=3).read().decode("utf-8")  # noqa: S310
        assert "기본 30분, 최대 2시간" in page
        assert 'id="recording-search-quick-ranges"' in page
        script = urlopen(f"{base_url}/static/recording-search.js", timeout=3).read().decode("utf-8")  # noqa: S310
        assert "MAX_SEARCH_DURATION_SECONDS" in script
        confirmation_code, confirmation = _http_json(
            base_url,
            "/api/v1/investigation-confirmations/object-disappearance-v3-ch1-20260904T051732Z",
        )
        assert confirmation_code == 200
        assert confirmation["status"] == "confirmed"
        assert confirmation["confirmation"]["source_timezone"] == "Asia/Seoul"
        request_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        code, accepted = _http_json(
            base_url,
            "/api/v1/recording-searches",
            {
                "investigation_id": "object-disappearance-v3-ch1-20260904T051732Z",
                "search_end": "2026-09-04T14:47:32",
                "request_id": request_id,
            },
        )
        assert code == 202
        status_payload: dict[str, object] = {}
        for _ in range(100):
            _, status_payload = _http_json(base_url, str(accepted["status_url"]))
            if status_payload["status"] not in {"ACCEPTED", "RUNNING"}:
                break
            time.sleep(0.03)
        assert status_payload["status"] == "FOUND"
        assert status_payload["schema_version"] == 8
        _, restored = _http_json(base_url, str(accepted["status_url"]))
        assert restored == status_payload
    finally:
        if process.poll() is None:
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        _stdout, _stderr = process.communicate(timeout=1)
        assert process.returncode in ({0, 3} if os.name == "nt" else {0})
