# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportExplicitAny=false, reportImplicitOverride=false, reportIncompatibleMethodOverride=false, reportUnannotatedClassAttribute=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnusedCallResult=false
"""Browser-start lifecycle and real Phase 6 to Schema 7 production-chain tests."""

from __future__ import annotations

import base64
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from vigi_vision.investigation_confirmation_models import (
    ConfirmationRequest,
    ConfirmationRoi,
    RoiProvenance,
)
from vigi_vision.investigation_confirmation_repository import (
    InvestigationConfirmationRepository,
)
from vigi_vision.investigation_confirmation_service import InvestigationConfirmationService
from vigi_vision.recording_models import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.recording_search_7e_1c import (
    CommonSessionAcquirer,
    CommonSessionAcquisition,
    DecodedLocalFrame,
    MediaProbeFacts,
    Phase7E1CExecutor,
    Phase7EB4Input,
)
from vigi_vision.recording_search_7e_1d import Phase7EStatus
from vigi_vision.recording_search_7e_background import Phase7EBackgroundManager
from vigi_vision.recording_search_7e_models import Schema5PhaseState, StrictIdentityEnvelope
from vigi_vision.recording_search_7e_phase8 import Phase8HandoffRepository
from vigi_vision.recording_search_7e_public import (
    Phase7EPublicError,
    Phase7EPublicService,
    Phase7EPublicStatus,
    approved_phase7e_policy,
)
from vigi_vision.recording_search_7e_repository import RecordingSearch7ERepository
from vigi_vision.recording_search_7e_validation import Schema5Envelope
from vigi_vision.reference_frame_api import create_reference_frame_app
from vigi_vision.reference_frame_artifacts import (
    ReferenceFrameArtifactStore,
    ReferenceFrameManifest,
)
from vigi_vision.reference_frame_models import (
    DecodedFrameEvidence,
    TimingPrecisionStatus,
    parse_reference_frame_request,
)
from vigi_vision.reference_frame_resources import ReferenceFrameResourceStore
from vigi_vision.replay import ReplayClip

_NOW = datetime(2026, 8, 2, 4, 5, 6, tzinfo=timezone.utc)
_REQUEST_ID = "12345678-1234-4234-8234-123456789abc"
_JPEG_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjI4LjEwMgD/2wBDAAgEBAQEBAUFBQUFBQYGBgYGBgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABLAAEBAAAAAAAAAAAAAAAAAAAACAEBAAAAAAAAAAAAAAAAAAAAABABAAAAAAAAAAAAAAAAAAAAABEBAAAAAAAAAAAAAAAAAAAAAP/AABEIAtAFAAMBIgACEQADEQD/2gAMAwEAAhEDEQA/AJ/AB//Z"
)


class _UnusedReferenceFrameService:
    def execute_or_resolve(self, request: object) -> object:
        raise AssertionError(request)


class _UnusedResources:
    def resolve_image(self, resource_id: str) -> object:
        raise AssertionError(resource_id)


class _BlockingService:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.calls = 0
        self.active = 0
        self.maximum_active = 0
        self.lock = Lock()
        self.status_calls = 0

    def recover_abandoned(self) -> int:
        return 0

    def prepare_http(self, investigation_id: str, search_end: str, request_id: str) -> object:
        _ = search_end
        return SimpleNamespace(
            request=SimpleNamespace(
                investigation_id=investigation_id,
                run_id=f"search-run-{request_id.replace('-', '')}",
            )
        )

    def resolve_existing(self, prepared: object) -> Phase7EPublicStatus | None:
        _ = prepared

    def execute_prepared(self, prepared: object, *, cancellation: object) -> Phase7EPublicStatus:
        request = prepared.request
        with self.lock:
            self.calls += 1
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        self.started.set()
        while not self.release.wait(0.01):
            if cancellation():
                break
        with self.lock:
            self.active -= 1
        return Phase7EPublicStatus(
            Phase7EStatus(
                request.investigation_id,
                request.run_id,
                7,
                "NOT_FOUND",
                "search_exhausted",
                "rr-terminal-result-v1-" + "a" * 64,
            )
        )

    def status(self, investigation_id: str, run_id: str) -> Phase7EPublicStatus:
        self.status_calls += 1
        return Phase7EPublicStatus(
            Phase7EStatus(investigation_id, run_id, 0, "UNAVAILABLE", None, None)
        )


class _FailingService(_BlockingService):
    def __init__(self, *, durable_interrupted: bool) -> None:
        super().__init__()
        self.durable_interrupted = durable_interrupted
        self.execute_failed = Event()
        self.resolve_calls = 0

    def resolve_existing(self, prepared: object) -> Phase7EPublicStatus | None:
        self.resolve_calls += 1
        if not self.durable_interrupted or not self.execute_failed.is_set():
            return None
        request = prepared.request
        return Phase7EPublicStatus(
            Phase7EStatus(
                request.investigation_id,
                request.run_id,
                5,
                "INTERRUPTED",
                "interrupted",
                None,
            )
        )

    def execute_prepared(self, prepared: object, *, cancellation: object) -> Phase7EPublicStatus:
        _ = (prepared, cancellation)
        self.execute_failed.set()
        raise RuntimeError


class _DurableRetryDuringActiveService(_BlockingService):
    def resolve_existing(self, prepared: object) -> Phase7EPublicStatus | None:
        request = prepared.request
        if request.run_id == f"search-run-{_REQUEST_ID.replace('-', '')}":
            return None
        return Phase7EPublicStatus(
            Phase7EStatus(
                request.investigation_id,
                request.run_id,
                7,
                "NOT_FOUND",
                "search_exhausted",
                "rr-terminal-result-v1-" + "c" * 64,
            )
        )


def test_background_manager_deduplicates_and_never_overlaps_workers() -> None:
    service = _BlockingService()
    manager = Phase7EBackgroundManager(cast("Any", service))
    first = manager.start("inv-01", "2026-07-20T12:00:05", _REQUEST_ID)
    assert service.started.wait(1)
    duplicate = manager.start("inv-01", "2026-07-20T12:00:05", _REQUEST_ID)
    assert duplicate.run_id == first.run_id
    assert service.calls == 1

    def second_start() -> str:
        try:
            _ = manager.start(
                "inv-02",
                "2026-07-20T12:00:05",
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
        except Phase7EPublicError as error:
            return error.code
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _index: second_start(), range(2)))
    assert outcomes == ("already_running", "already_running")
    assert service.maximum_active == 1
    with pytest.raises(Phase7EPublicError, match="request_conflict"):
        _ = manager.start("inv-01", "2026-07-20T12:00:06", _REQUEST_ID)
    service.release.set()
    manager.close()


def test_background_retry_ledger_stays_bounded_while_active() -> None:
    service = _DurableRetryDuringActiveService()
    manager = Phase7EBackgroundManager(cast("Any", service))
    investigation_id = "object-disappearance-v3-ch1-20260720T033428Z"
    _ = manager.start(investigation_id, "2026-07-20T12:34:33", _REQUEST_ID)
    assert service.started.wait(1)

    for index in range(80):
        request_id = f"00000000-0000-4000-8000-{index:012x}"
        receipt = manager.start(investigation_id, "2026-07-20T12:34:33", request_id)
        assert receipt.status == "NOT_FOUND"

    assert len(cast("Any", manager)._jobs) == 64
    service.release.set()
    manager.close()


@pytest.mark.parametrize(
    ("failure_mode", "expected"),
    [("ephemeral", "FAILED"), ("durable", "INTERRUPTED")],
)
def test_background_failures_remain_observable(
    failure_mode: str,
    expected: str,
) -> None:
    service = _FailingService(durable_interrupted=failure_mode == "durable")
    manager = Phase7EBackgroundManager(cast("Any", service))
    receipt = manager.start("inv-01", "2026-07-20T12:00:05", _REQUEST_ID)
    assert service.execute_failed.wait(1)
    deadline = time.monotonic() + 1
    projected = manager.status(receipt.investigation_id, receipt.run_id)
    while projected.phase7.status not in {"FAILED", "INTERRUPTED"} and time.monotonic() < deadline:
        time.sleep(0.01)
        projected = manager.status(receipt.investigation_id, receipt.run_id)
    assert projected.phase7.status == expected
    assert service.resolve_calls >= 2
    manager.close()


def test_background_shutdown_cancels_and_joins_the_only_worker() -> None:
    service = _BlockingService()
    manager = Phase7EBackgroundManager(cast("Any", service))
    _ = manager.start("inv-01", "2026-07-20T12:00:05", _REQUEST_ID)
    assert service.started.wait(1)
    manager.close()
    assert service.active == 0
    assert service.calls == 1


def test_phase7e_http_rejects_authoritative_overrides_and_noncanonical_time() -> None:
    service = _BlockingService()
    app = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        phase7e_service=cast("Any", service),
    )
    body = {
        "investigation_id": "object-disappearance-v3-ch1-20260720T033428Z",
        "search_end": "2026-07-20T12:34:33",
        "request_id": _REQUEST_ID,
    }
    with TestClient(app) as client:
        for override in (
            {"roi": {"x": 0}},
            {"run_id": "search-run-forged"},
            {"frame_id": "forged"},
            {"media_path": "C:\\private"},
            {"source_timezone": "Asia/Seoul"},
        ):
            response = client.post("/api/v1/recording-searches", json={**body, **override})
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_recording_search_request"
            assert "private" not in response.text
        for invalid_time in (
            "2026-07-20 12:34:33",
            "2026-07-20T12:34:33+09:00",
            "2026-07-20T12:34:33.000",
        ):
            response = client.post(
                "/api/v1/recording-searches",
                json={**body, "search_end": invalid_time},
            )
            assert response.status_code == 422
        missing = client.post(
            "/api/v1/recording-searches",
            json={key: value for key, value in body.items() if key != "search_end"},
        )
        assert missing.status_code == 422
    service.release.set()


class _Planner:
    def __init__(self, segment: RecordingSegment) -> None:
        self.segment = segment
        self.windows: list[RecordingWindow] = []

    def find_segments_for_window(self, window: RecordingWindow) -> tuple[RecordingSegment, ...]:
        self.windows.append(window)
        return (self.segment,)

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        assert segment == self.segment
        return ReplayRequest(window, "rtsp://redacted.example/replay")


class _Extractor:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    def extract(self, request: ReplayRequest) -> ReplayClip:
        self.calls += 1
        _ = self.path.write_bytes(b"one-retained-session")
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
        assert path.is_file()
        assert timeout_seconds > 0
        return MediaProbeFacts(
            selected_video_stream_index=0,
            video_stream_count=1,
            audio_stream_count=0,
            container_start_pts=0,
            time_base_num=1,
            time_base_den=1,
            duration_ticks=5,
            codec="h264",
            profile="High",
            pixel_format="yuv420p",
            width=8,
            height=8,
            average_frame_rate_num=1,
            average_frame_rate_den=1,
        )


class _TimelineDecoder:
    def decode(
        self,
        session: CommonSessionAcquisition,
        targets: tuple[datetime, ...],
        timeout_seconds: float,
    ) -> tuple[DecodedLocalFrame, ...]:
        assert timeout_seconds > 0
        frames = []
        for target in targets:
            offset = int((target - session.request.start_utc).total_seconds())
            selected = min(offset, int(session.request.duration_seconds) - 1)
            frames.append(
                DecodedLocalFrame(
                    target,
                    selected,
                    selected,
                    8,
                    8,
                    bytes([selected]) * (8 * 8 * 3),
                    decode_session_id=session.common_session_id,
                )
            )
        return tuple(frames)


class _ReconstructingClassifier:
    def __init__(self, confirmation_service: InvestigationConfirmationService) -> None:
        self.confirmation_service = confirmation_service
        self.authoritative_facts: list[tuple[int, str, int, str]] = []

    def classify(self, authoritative: Phase7EB4Input) -> object:
        confirmed = self.confirmation_service.load_confirmed(authoritative.run.investigation_id)
        self.authoritative_facts.append(
            (
                confirmed.channel_id,
                confirmed.source_timezone,
                confirmed.roi.x,
                confirmed.reference_frame_resource_id,
            )
        )
        requested = datetime.fromisoformat(
            str(authoritative.target_request.payload["requested_time_utc"]).replace("Z", "+00:00")
        )
        outcome = (
            "PRESENT" if requested <= confirmed.anchor_time_utc + timedelta(seconds=1) else "ABSENT"
        )
        template = _classification_template(outcome)
        return StrictIdentityEnvelope.from_payload(
            "classification-operation",
            {
                **template,
                "investigation_id": authoritative.run.investigation_id,
                "run_id": authoritative.run.run_id,
                "frame_id": authoritative.frame_record.identity,
                "target_request_id": authoritative.target_request.identity,
                "classifier_policy_id": authoritative.run.manifest.payload["classifier_policy_id"],
            },
        )


class _UnusedClipGenerator:
    def generate(self, *args: object, **kwargs: object) -> str:
        raise AssertionError((args, kwargs))


@cache
def _classification_template(outcome: str) -> dict[str, Any]:
    document = (
        Path(__file__).parents[1] / "docs" / "design" / "object-disappearance-recording-search.md"
    ).read_text(encoding="utf-8")
    for match in re.finditer(r"```json", document):
        end = document.find("```", match.end())
        if end < 0:
            continue
        try:
            value = json.loads(document[match.end() : end])
        except json.JSONDecodeError:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            if (
                isinstance(item, dict)
                and item.get("family") == "classification-operation"
                and item.get("payload", {}).get("outcome") == outcome
            ):
                return cast("dict[str, Any]", item["payload"])
    raise AssertionError(outcome)


def _confirmed_phase6(tmp_path: Path) -> tuple[InvestigationConfirmationService, str, str]:
    resource_root = tmp_path / "reference-frames"
    frame_request = parse_reference_frame_request(
        channel_id=1,
        requested_time_text="2026-07-20T12:34:18",
        source_timezone="Asia/Seoul",
        now_utc=_NOW,
    )
    segment = RecordingSegment(
        1,
        frame_request.requested_time_utc.date(),
        int((frame_request.requested_time_utc - timedelta(minutes=1)).timestamp()),
        int((frame_request.requested_time_utc + timedelta(minutes=1)).timestamp()),
        frame_request.requested_time_utc - timedelta(minutes=1),
        frame_request.requested_time_utc + timedelta(minutes=1),
    )
    session = ReferenceFrameArtifactStore(resource_root).begin(frame_request, segment)
    _ = session.jpeg_path.write_bytes(_JPEG_BYTES)
    _ = session.finalize(
        ReferenceFrameManifest(
            frame_request,
            segment,
            RecordingWindow(
                1,
                frame_request.requested_time_utc - timedelta(seconds=2),
                frame_request.requested_time_utc + timedelta(seconds=4),
            ),
            session.resource_id,
            DecodedFrameEvidence(
                session.jpeg_path,
                2.0,
                1280,
                720,
                TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
                (),
            ),
            None,
            None,
        )
    )
    resources = ReferenceFrameResourceStore(resource_root)
    confirmation_service = InvestigationConfirmationService(
        resources,
        InvestigationConfirmationRepository(tmp_path / "investigations", resources, lambda: _NOW),
        lambda: _NOW,
    )
    result = confirmation_service.confirm(
        ConfirmationRequest(
            reference_frame_resource_id=session.resource_id,
            reference_time="2026-07-20T12:34:28",
            source_timezone="Asia/Seoul",
            candidate_offset_seconds=-10,
            source_width=1280,
            source_height=720,
            roi=ConfirmationRoi(
                x=10,
                y=20,
                width=120,
                height=80,
                coordinate_space="source_pixels",
                provenance=RoiProvenance.MANUAL,
            ),
        )
    )
    return confirmation_service, result.manifest.investigation_id, session.resource_id


def test_corrupt_phase6_is_rejected_before_background_admission(tmp_path: Path) -> None:
    confirmation_service, investigation_id, _resource_id = _confirmed_phase6(tmp_path)
    confirmed = confirmation_service.load_confirmed(investigation_id)
    _ = confirmed.jpeg_path.write_bytes(b"not-a-jpeg")
    policy, classifier_policy, object_policy = approved_phase7e_policy()
    repository = RecordingSearch7ERepository(tmp_path / "phase7e")
    service = Phase7EPublicService(
        repository,
        cast("Any", SimpleNamespace()),
        confirmation_service,
        object(),
        object(),
        policy,
        classifier_policy,
        object_policy,
        cast("Any", SimpleNamespace()),
        lambda: _NOW,
    )
    app = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        confirmation_service=confirmation_service,
        phase7e_service=service,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/recording-searches",
            json={
                "investigation_id": investigation_id,
                "search_end": "2026-07-20T12:34:33",
                "request_id": _REQUEST_ID,
            },
        )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "confirmation_corrupt"
    assert not repository.run_path(
        investigation_id,
        "search-run-12345678123442348234123456789abc",
    ).exists()


def test_actual_phase6_http_background_execution_reaches_strict_schema7(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    confirmation_service, investigation_id, resource_id = _confirmed_phase6(tmp_path)
    confirmed = confirmation_service.load_confirmed(investigation_id)
    segment = RecordingSegment(
        1,
        confirmed.anchor_time_utc.date(),
        int((confirmed.anchor_time_utc - timedelta(seconds=30)).timestamp()),
        int((confirmed.anchor_time_utc + timedelta(seconds=30)).timestamp()),
        confirmed.anchor_time_utc - timedelta(seconds=30),
        confirmed.anchor_time_utc + timedelta(seconds=30),
    )
    planner = _Planner(segment)
    extractor = _Extractor(tmp_path / "replay.mp4")
    probe = _Probe()
    repository = RecordingSearch7ERepository(tmp_path / "phase7e", lock_timeout_seconds=0.1)
    executor = Phase7E1CExecutor(repository, CommonSessionAcquirer(planner, extractor, probe))
    policy, classifier_policy, object_policy = approved_phase7e_policy()
    classifier = _ReconstructingClassifier(confirmation_service)
    service = Phase7EPublicService(
        repository,
        executor,
        confirmation_service,
        classifier,
        _TimelineDecoder(),
        policy,
        classifier_policy,
        object_policy,
        Phase8HandoffRepository(
            tmp_path / "phase8",
            tmp_path / "phase7e" / ".media",
            probe,
            _UnusedClipGenerator(),
        ),
        lambda: _NOW,
        probe,
    )
    prepared = service.prepare_http(investigation_id, "2026-07-20T12:34:33", _REQUEST_ID)
    assert service.resolve_existing(prepared) is None
    app = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        confirmation_service=confirmation_service,
        phase7e_service=service,
    )
    body = {
        "investigation_id": investigation_id,
        "search_end": "2026-07-20T12:34:33",
        "request_id": _REQUEST_ID,
    }
    with TestClient(app) as client:
        missing_confirmation = client.post(
            "/api/v1/recording-searches",
            json={
                **body,
                "investigation_id": "object-disappearance-v3-ch9-20260720T033428Z",
                "request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            },
        )
        assert missing_confirmation.status_code == 404
        assert missing_confirmation.json()["error"]["code"] == "investigation_not_found"
        for index, invalid_end in enumerate(
            (
                "2026-07-20T12:34:28",
                "2026-07-20T12:34:27",
                "2026-07-20T12:44:29",
            )
        ):
            invalid = client.post(
                "/api/v1/recording-searches",
                json={
                    **body,
                    "search_end": invalid_end,
                    "request_id": f"cccccccc-cccc-4ccc-8cc{index}-cccccccccccc",
                },
            )
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "invalid_recording_search_request"
        response = client.post("/api/v1/recording-searches", json=body)
        assert response.status_code == 202, response.text
        receipt = response.json()
        assert set(receipt) == {
            "request_id",
            "investigation_id",
            "run_id",
            "status",
            "status_url",
        }
        assert receipt["status"] == "ACCEPTED"
        duplicate = client.post("/api/v1/recording-searches", json=body)
        assert duplicate.status_code == 202
        assert duplicate.json()["run_id"] == receipt["run_id"]
        deadline = time.monotonic() + 90
        states: list[str] = []
        while time.monotonic() < deadline:
            projected = client.get(receipt["status_url"])
            assert projected.status_code == 200
            states.append(projected.json()["status"])
            if states[-1] in {"FOUND", "NOT_FOUND", "INCONCLUSIVE", "FAILED", "INTERRUPTED"}:
                break
            time.sleep(1.0)

    assert states[-1] == "FOUND"
    assert "RUNNING" in states
    reopened = repository.reopen_schema7(investigation_id, receipt["run_id"])
    assert reopened.schema_version == 7
    assert extractor.calls == 1
    assert planner.windows
    assert {window.channel_id for window in planner.windows} == {1}
    assert classifier.authoritative_facts
    assert set(classifier.authoritative_facts) == {(1, "Asia/Seoul", 10, resource_id)}
    assert "roi" not in body
    assert "source_timezone" not in body
    assert "frame_id" not in body

    abandoned = service.prepare_http(
        investigation_id,
        "2026-07-20T12:34:33",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    repository.create_schema5(
        abandoned.schema5,
        Schema5Envelope(
            run_state="RUNNING",
            phase_state=Schema5PhaseState.PLANNED,
            active_replay_operation_id=None,
            reason_code=None,
            attempt_count=0,
        ),
        abandoned.base_records,
    )
    restarted = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        confirmation_service=confirmation_service,
        phase7e_service=service,
    )
    with TestClient(restarted) as client:
        recovered = client.get(
            f"/api/v1/recording-searches/{investigation_id}/{abandoned.request.run_id}"
        )
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "INTERRUPTED"
