# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportExplicitAny=false, reportImplicitOverride=false, reportIncompatibleMethodOverride=false, reportUnannotatedClassAttribute=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnusedCallResult=false
"""Browser-start lifecycle and real Phase 6 to Schema 7 production-chain tests."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from functools import cache
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

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
from vigi_vision.nvr import NvrErrorKind, NvrRequestError
from vigi_vision.recording_models import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.recording_search_7e_1c import (
    CommonSessionAcquirer,
    CommonSessionAcquisition,
    CommonSessionMediaError,
    DecodedLocalFrame,
    FfmpegLocalDecoder,
    FfprobeMediaProbe,
    MediaProbeFacts,
    Phase7E1CExecutor,
    Phase7EB4Input,
)
from vigi_vision.recording_search_7e_1d import Phase7EStatus
from vigi_vision.recording_search_7e_background import Phase7EBackgroundManager
from vigi_vision.recording_search_7e_media_diagnostics import Phase7EMediaProbeDiagnostic
from vigi_vision.recording_search_7e_models import Schema5PhaseState, StrictIdentityEnvelope
from vigi_vision.recording_search_7e_phase8 import Phase8HandoffRepository
from vigi_vision.recording_search_7e_public import (
    Phase7EFailureDiagnostic,
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

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import Response

_NOW = datetime(2026, 8, 2, 4, 5, 6, tzinfo=timezone.utc)
_REQUEST_ID = "12345678-1234-4234-8234-123456789abc"
_MEDIA_PROBE_FAILED = "media_probe_failed"
_FIXTURE_CONFIGURATION_ERROR = "invalid deterministic Uvicorn fixture configuration"
_SECRET_BEARING_ERROR = "password=uvicorn-secret-sentinel"  # noqa: S105
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


class _WatchdogService(_BlockingService):
    def __init__(self) -> None:
        super().__init__()
        self.durable = "RUNNING"
        self.durable_reason: str | None = None
        self.publish_calls = 0

    def publish_background_terminal(
        self,
        prepared: object,
        *,
        status: str,
        reason_code: str,
    ) -> Phase7EPublicStatus:
        self.publish_calls += 1
        self.durable = status
        self.durable_reason = reason_code
        request = prepared.request
        return Phase7EPublicStatus(
            Phase7EStatus(
                request.investigation_id,
                request.run_id,
                8,
                status,
                reason_code,
                "successor-terminal-v1-" + "a" * 64,
            )
        )

    def status(self, investigation_id: str, run_id: str) -> Phase7EPublicStatus:
        if self.durable == "RUNNING":
            return Phase7EPublicStatus(
                Phase7EStatus(investigation_id, run_id, 8, "RUNNING", None, None)
            )
        return Phase7EPublicStatus(
            Phase7EStatus(
                investigation_id,
                run_id,
                8,
                self.durable,
                self.durable_reason,
                "successor-terminal-v1-" + "a" * 64,
            )
        )


class _WorkerFailureService(_WatchdogService):
    def execute_prepared(self, prepared: object, *, cancellation: object) -> Phase7EPublicStatus:
        _ = (prepared, cancellation)
        self.started.set()
        raise RuntimeError


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


class _UnannotatedPublicFailingService(_FailingService):
    def execute_prepared(self, prepared: object, *, cancellation: object) -> Phase7EPublicStatus:
        _ = (prepared, cancellation)
        self.execute_failed.set()
        unsafe_detail = "private.example?token=secret"
        raise Phase7EPublicError(unsafe_detail)


class _DurableMediaFailureService(_FailingService):
    def resolve_existing(self, prepared: object) -> Phase7EPublicStatus | None:
        if not self.execute_failed.is_set():
            return None
        request = prepared.request
        return Phase7EPublicStatus(
            Phase7EStatus(
                request.investigation_id,
                request.run_id,
                5,
                "FAILED",
                _MEDIA_PROBE_FAILED,
                None,
            )
        )

    def execute_prepared(self, prepared: object, *, cancellation: object) -> Phase7EPublicStatus:
        _ = (prepared, cancellation)
        self.execute_failed.set()
        diagnostic = Phase7EFailureDiagnostic(
            "media_validation",
            _MEDIA_PROBE_FAILED,
            "CommonSessionMediaError",
            "no_failure_reported",
            media_probe=Phase7EMediaProbeDiagnostic("ffprobe_invalid_json"),
        )
        raise Phase7EPublicError(_MEDIA_PROBE_FAILED, diagnostic=diagnostic)


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


def test_background_keeps_media_diagnostic_after_durable_schema5_failure() -> None:
    service = _DurableMediaFailureService(durable_interrupted=False)
    manager = Phase7EBackgroundManager(cast("Any", service))
    receipt = manager.start("inv-01", "2026-07-20T12:00:05", _REQUEST_ID)
    assert service.execute_failed.wait(1)
    deadline = time.monotonic() + 1
    job = cast("Any", manager)._jobs[receipt.request_id]
    while job.failure_diagnostic is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.failure_diagnostic is not None
    assert job.failure_diagnostic.category == _MEDIA_PROBE_FAILED
    assert job.failure_diagnostic.media_probe is not None
    assert job.failure_diagnostic.media_probe.stage == "ffprobe_invalid_json"
    first = manager.pre_run_failure_diagnostic(receipt.investigation_id, receipt.run_id)
    second = manager.pre_run_failure_diagnostic(receipt.investigation_id, receipt.run_id)
    assert first == second == job.failure_diagnostic
    manager.close()
    restarted = Phase7EBackgroundManager(cast("Any", service))
    assert restarted.pre_run_failure_diagnostic(receipt.investigation_id, receipt.run_id) is None
    restarted.close()


def test_background_rejects_unannotated_failure_text_from_public_status() -> None:
    service = _UnannotatedPublicFailingService(durable_interrupted=False)
    manager = Phase7EBackgroundManager(cast("Any", service))
    receipt = manager.start("inv-01", "2026-07-20T12:00:05", _REQUEST_ID)
    assert service.execute_failed.wait(1)
    deadline = time.monotonic() + 1
    projected = manager.status(receipt.investigation_id, receipt.run_id)
    while projected.phase7.status != "FAILED" and time.monotonic() < deadline:
        time.sleep(0.01)
        projected = manager.status(receipt.investigation_id, receipt.run_id)
    assert projected.phase7.reason_code == "internal_error"
    assert "private.example" not in json.dumps(projected.as_dict())
    manager.close()


def test_background_shutdown_cancels_and_joins_the_only_worker() -> None:
    service = _BlockingService()
    manager = Phase7EBackgroundManager(cast("Any", service))
    _ = manager.start("inv-01", "2026-07-20T12:00:05", _REQUEST_ID)
    assert service.started.wait(1)
    manager.close()
    assert service.active == 0
    assert service.calls == 1


def test_background_watchdog_publishes_bounded_terminal() -> None:
    service = _WatchdogService()
    manager = Phase7EBackgroundManager(
        cast("Any", service),
        execution_deadline_seconds=0.05,
    )
    receipt = manager.start("inv-01", "2026-07-20T12:00:05", _REQUEST_ID)
    deadline = time.monotonic() + 1
    projected = manager.status(receipt.investigation_id, receipt.run_id)
    while projected.phase7.status == "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.01)
        projected = manager.status(receipt.investigation_id, receipt.run_id)
    assert projected.phase7.status == "INCONCLUSIVE"
    assert projected.phase7.reason_code == "execution_deadline_exhausted"
    assert service.publish_calls == 1
    manager.close()


def test_background_worker_exception_publishes_durable_terminal() -> None:
    service = _WorkerFailureService()
    manager = Phase7EBackgroundManager(cast("Any", service))
    receipt = manager.start("inv-01", "2026-07-20T12:00:05", _REQUEST_ID)
    assert service.started.wait(1)
    deadline = time.monotonic() + 1
    projected = manager.status(receipt.investigation_id, receipt.run_id)
    while projected.phase7.status == "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.01)
        projected = manager.status(receipt.investigation_id, receipt.run_id)
    assert projected.phase7.status == "FAILED"
    assert projected.phase7.reason_code == "internal_error"
    assert service.publish_calls == 1
    manager.close()


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


def test_phase7e_http_missing_run_stays_not_found_while_other_run_is_active(
    tmp_path: Path,
) -> None:
    app, _service, repository, _extractor, investigation_id = _pre_schema5_failure_app(
        tmp_path,
        _UnavailablePlanner(),
    )
    active_run_id = "search-run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    with (
        repository.invocation_ownership(investigation_id, active_run_id, timeout_seconds=0),
        TestClient(app) as client,
    ):
        response = client.get(
            f"/api/v1/recording-searches/{investigation_id}/여기에-새로운-search-run-id"
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "search_run_not_found"


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


class _NvrFailingPlanner:
    def __init__(self, kind: NvrErrorKind) -> None:
        self.windows: list[RecordingWindow] = []
        self.kind = kind

    def find_segments_for_window(self, window: RecordingWindow) -> tuple[RecordingSegment, ...]:
        self.windows.append(window)
        raise NvrRequestError(self.kind, "ConnectionError")

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        raise AssertionError((segment, window))


class _UnavailablePlanner:
    def __init__(self) -> None:
        self.windows: list[RecordingWindow] = []

    def find_segments_for_window(self, window: RecordingWindow) -> tuple[RecordingSegment, ...]:
        self.windows.append(window)
        return ()

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        raise AssertionError((segment, window))


class _UnexpectedPlanner:
    def __init__(self) -> None:
        self.windows: list[RecordingWindow] = []

    def find_segments_for_window(self, window: RecordingWindow) -> tuple[RecordingSegment, ...]:
        self.windows.append(window)
        secret_bearing_message = "rtsp://user:password@private.example/replay?token=secret"  # noqa: S105
        raise RuntimeError(secret_bearing_message)

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        raise AssertionError((segment, window))


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


class _ExistingMediaExtractor:
    """Return a copied real replay without replacing its bytes with a fixture."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    def extract(self, request: ReplayRequest) -> ReplayClip:
        self.calls += 1
        assert self.path.is_file()
        return ReplayClip(
            request.window.channel_id,
            request.window.start_utc,
            request.window.end_utc,
            request.replay_url,
            self.path,
            request.window.duration_seconds,
        )


class _Probe:
    # The probe fixture intentionally exposes each production media fact.
    def __init__(  # noqa: PLR0913
        self,
        *,
        duration_ticks: int = 5,
        time_base_num: int = 1,
        time_base_den: int = 1,
        codec: str = "h264",
        profile: str = "High",
        width: int = 8,
        height: int = 8,
        average_frame_rate_num: int = 1,
        average_frame_rate_den: int = 1,
        level: int = 41,
    ) -> None:
        self.duration_ticks = duration_ticks
        self.time_base_num = time_base_num
        self.time_base_den = time_base_den
        self.codec = codec
        self.profile = profile
        self.width = width
        self.height = height
        self.average_frame_rate_num = average_frame_rate_num
        self.average_frame_rate_den = average_frame_rate_den
        self.level = level
        self.observed_facts: list[MediaProbeFacts] = []

    def probe(self, path: Path, timeout_seconds: float) -> MediaProbeFacts:
        assert path.is_file()
        assert timeout_seconds > 0
        facts = MediaProbeFacts(
            selected_video_stream_index=0,
            video_stream_count=1,
            audio_stream_count=0,
            container_start_pts=0,
            time_base_num=self.time_base_num,
            time_base_den=self.time_base_den,
            duration_ticks=self.duration_ticks,
            codec=self.codec,
            profile=self.profile,
            pixel_format="yuv420p",
            width=self.width,
            height=self.height,
            average_frame_rate_num=self.average_frame_rate_num,
            average_frame_rate_den=self.average_frame_rate_den,
            level=self.level,
        )
        self.observed_facts.append(facts)
        return facts


class _DelayedProbe(_Probe):
    def probe(self, path: Path, timeout_seconds: float) -> MediaProbeFacts:
        time.sleep(0.15)
        return super().probe(path, timeout_seconds)


class _DelayedFailingProbe(_Probe):
    def probe(self, path: Path, timeout_seconds: float) -> MediaProbeFacts:
        assert path.is_file()
        assert timeout_seconds > 0
        time.sleep(0.15)
        try:
            raise RuntimeError(_SECRET_BEARING_ERROR)  # noqa: TRY301
        except RuntimeError as error:
            raise CommonSessionMediaError(
                probe_diagnostic=Phase7EMediaProbeDiagnostic("ffprobe_invalid_json")
            ) from error


class _TimelineDecoder:
    def __init__(
        self,
        *,
        decoded_dimensions: tuple[int, int] | None = None,
        maximum_offset: Fraction | None = None,
    ) -> None:
        self.decoded_dimensions = decoded_dimensions
        self.maximum_offset = maximum_offset
        self.selected_indices: list[int] = []

    def decode(
        self,
        session: CommonSessionAcquisition,
        targets: tuple[datetime, ...],
        timeout_seconds: float,
    ) -> tuple[DecodedLocalFrame, ...]:
        assert timeout_seconds > 0
        frames = []
        width, height = self.decoded_dimensions or (session.media.width, session.media.height)
        for target in targets:
            offset = int((target - session.request.start_utc).total_seconds())
            frame_period = Fraction(
                session.media.average_frame_rate_den,
                session.media.average_frame_rate_num,
            )
            observed_last = session.usable_duration - frame_period
            if self.maximum_offset is not None:
                observed_last = min(observed_last, self.maximum_offset)
            selected_offset = min(Fraction(offset), observed_last)
            raw_pts = (
                selected_offset * session.media.time_base_den // session.media.time_base_num
                + session.media.container_start_pts
            )
            selected = int(raw_pts - session.media.container_start_pts)
            self.selected_indices.append(selected)
            frames.append(
                DecodedLocalFrame(
                    target,
                    int(raw_pts),
                    selected,
                    width,
                    height,
                    bytes([selected % 256]) * (width * height * 3),
                    decode_session_id=session.common_session_id,
                    container_start_pts=session.media.container_start_pts,
                    time_base_num=session.media.time_base_num,
                    time_base_den=session.media.time_base_den,
                )
            )
        return tuple(frames)


class _ReconstructingClassifier:
    def __init__(
        self,
        confirmation_service: InvestigationConfirmationService,
        *,
        initial_present: bool = True,
        followup_outcome: str = "ABSENT",
    ) -> None:
        self.confirmation_service = confirmation_service
        self.initial_present = initial_present
        self.followup_outcome = followup_outcome
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
            "PRESENT"
            if self.initial_present
            and requested <= confirmed.anchor_time_utc + timedelta(seconds=1)
            else self.followup_outcome
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


def create_uvicorn_phase7e_fixture_app() -> FastAPI:
    """Build a credential-free real-Uvicorn fixture from an isolated test root."""
    root_text = os.environ.get("VIGI_PHASE7E_UVICORN_TEST_ROOT")
    scenario = os.environ.get("VIGI_PHASE7E_UVICORN_TEST_SCENARIO")
    if root_text is None or scenario not in {
        "invalid_duration",
        "ffprobe_invalid_json",
        "schema7_jitter",
        "available_found",
        "available_inconclusive",
        "available_pts_tail_missing",
        "real_hevc",
    }:
        raise RuntimeError(_FIXTURE_CONFIGURATION_ERROR)
    root = Path(root_text).resolve(strict=True)
    confirmation_service, _investigation_id, _resource_id = _confirmed_phase6(root)
    confirmed = confirmation_service.load_confirmed(_investigation_id)
    segment = RecordingSegment(
        1,
        confirmed.anchor_time_utc.date(),
        int((confirmed.anchor_time_utc - timedelta(seconds=30)).timestamp()),
        int((confirmed.anchor_time_utc + timedelta(seconds=90)).timestamp()),
        confirmed.anchor_time_utc - timedelta(seconds=30),
        confirmed.anchor_time_utc + timedelta(seconds=90),
    )

    class _SensitivePlanner(_Planner):
        def plan_for_segment(
            self,
            selected: RecordingSegment,
            window: RecordingWindow,
        ) -> ReplayRequest:
            assert selected == self.segment
            return ReplayRequest(
                window,
                "rtsp://operator:uvicorn-secret-sentinel@private.example/replay",
            )

    if scenario == "real_hevc":
        source = (
            Path(__file__).parents[1]
            / "artifacts/investigation-searches/.media/"
            / "object-disappearance-v3-ch1-20260904T051732Z/"
            / "search-run-bee8920323d442d0b05ba4cf87075aa2/"
            / (
                "rr-common-session-v1-3684126fcd6718fb6859b14bb95a53629cfafe30e075df8ab9aad6a73997a3a3"
                ".mp4"
            )
        )
        if not source.is_file():
            detail = "preserved real HEVC fixture is unavailable"
            raise RuntimeError(detail)
        _ = shutil.copyfile(source, root / "temporary-replay.mp4")
        ffmpeg = Path(shutil.which("ffmpeg") or "ffmpeg")
        ffprobe = Path(shutil.which("ffprobe") or "ffprobe")
        probe = FfprobeMediaProbe(ffprobe)
        decoder: object = FfmpegLocalDecoder(ffmpeg, ffprobe)
    elif scenario == "invalid_duration":
        probe = _DelayedProbe(duration_ticks=0)
        decoder = _TimelineDecoder()
    elif scenario == "ffprobe_invalid_json":
        probe = _DelayedFailingProbe()
        decoder = _TimelineDecoder()
    elif scenario == "schema7_jitter":
        probe = _Probe(
            duration_ticks=59_873,
            time_base_den=1_000,
            codec="hevc",
            profile="Main",
            width=2_560,
            height=1_440,
            average_frame_rate_num=25,
        )
        decoder = _TimelineDecoder()
    elif scenario in {"available_found", "available_inconclusive"}:
        probe = _Probe(
            duration_ticks=55_200,
            time_base_den=1_000,
            codec="hevc",
            profile="Main",
            width=2_560,
            height=1_440,
            average_frame_rate_num=25,
        )
        decoder = _TimelineDecoder()
    else:
        probe = _Probe(
            duration_ticks=60_000,
            time_base_den=1_000,
            codec="hevc",
            profile="Main",
            width=2_560,
            height=1_440,
            average_frame_rate_num=25,
        )
        decoder = _TimelineDecoder()
    planner = _SensitivePlanner(segment)
    extractor: object = (
        _ExistingMediaExtractor(root / "temporary-replay.mp4")
        if scenario == "real_hevc"
        else _Extractor(root / "temporary-replay.mp4")
    )
    repository = RecordingSearch7ERepository(
        root / "phase7e",
        lock_timeout_seconds=0.1,
        media_probe=probe,
    )
    executor = Phase7E1CExecutor(repository, CommonSessionAcquirer(planner, extractor, probe))
    policy, classifier_policy, object_policy = approved_phase7e_policy()
    timing_policy = (
        StrictIdentityEnvelope.from_payload(
            "policy",
            {**policy.payload, "binary_stop_seconds": 60},
        )
        if scenario
        in {
            "schema7_jitter",
            "available_found",
            "available_inconclusive",
            "available_pts_tail_missing",
            "real_hevc",
        }
        else policy
    )
    service = Phase7EPublicService(
        repository,
        executor,
        confirmation_service,
        _ReconstructingClassifier(
            confirmation_service,
            followup_outcome=("PRESENT" if scenario == "available_inconclusive" else "ABSENT"),
        ),
        decoder
        if scenario == "real_hevc"
        else _TimelineDecoder(
            decoded_dimensions=(8, 8),
            maximum_offset=(Fraction(50) if scenario == "available_pts_tail_missing" else None),
        ),
        timing_policy,
        classifier_policy,
        object_policy,
        Phase8HandoffRepository(
            root / "phase8",
            root / "phase7e" / ".media",
            probe,
            _UnusedClipGenerator(),
        ),
        lambda: _NOW,
        probe,
    )
    return create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        confirmation_service=confirmation_service,
        phase7e_service=service,
    )


def _pre_schema5_failure_app(
    tmp_path: Path,
    planner: object,
) -> tuple[Any, Phase7EPublicService, RecordingSearch7ERepository, _Extractor, str]:
    confirmation_service, investigation_id, _resource_id = _confirmed_phase6(tmp_path)
    extractor = _Extractor(tmp_path / "must-not-exist.mp4")
    probe = _Probe()
    repository = RecordingSearch7ERepository(tmp_path / "phase7e", lock_timeout_seconds=0.1)
    executor = Phase7E1CExecutor(
        repository,
        CommonSessionAcquirer(cast("Any", planner), extractor, probe),
    )
    policy, classifier_policy, object_policy = approved_phase7e_policy()
    service = Phase7EPublicService(
        repository,
        executor,
        confirmation_service,
        object(),
        object(),
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
    app = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        confirmation_service=confirmation_service,
        phase7e_service=service,
    )
    return app, service, repository, extractor, investigation_id


def _wait_for_phase7e_status(client: TestClient, status_url: str) -> Response:
    deadline = time.monotonic() + 1
    projected = client.get(status_url)
    while projected.json()["status"] in {"ACCEPTED", "RUNNING"} and time.monotonic() < deadline:
        time.sleep(0.01)
        projected = client.get(status_url)
    return projected


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


@pytest.mark.parametrize(
    ("nvr_kind", "expected_reason", "expected_exception"),
    [
        (NvrErrorKind.SDK_REQUEST, "acquisition_failed", "NvrRequestError"),
        (NvrErrorKind.TIMEOUT, "acquisition_failed", "NvrRequestError"),
        (
            None,
            "recording_unavailable",
            "CommonSessionRecordingUnavailableError",
        ),
    ],
)
def test_pre_schema5_known_failure_retains_safe_category_without_publication(
    tmp_path: Path,
    nvr_kind: NvrErrorKind | None,
    expected_reason: str,
    expected_exception: str,
) -> None:
    planner = _NvrFailingPlanner(nvr_kind) if nvr_kind is not None else _UnavailablePlanner()
    app, service, repository, extractor, investigation_id = _pre_schema5_failure_app(
        tmp_path,
        planner,
    )
    body = {
        "investigation_id": investigation_id,
        "search_end": "2026-07-20T12:34:33",
        "request_id": _REQUEST_ID,
    }
    with TestClient(app) as client:
        accepted = client.post("/api/v1/recording-searches", json=body)
        assert accepted.status_code == 202
        status_url = accepted.json()["status_url"]
        projected = _wait_for_phase7e_status(client, status_url)
        manager = cast("Phase7EBackgroundManager", app.state.phase7e_background_manager)
        diagnostic = manager.pre_run_failure_diagnostic(
            investigation_id,
            accepted.json()["run_id"],
        )
        assert diagnostic is not None
        assert diagnostic.as_dict() == {
            "boundary": "recording_discovery",
            "category": expected_reason,
            "exception_class": expected_exception,
            "cleanup_outcome": "not_required",
        }
        call_count = len(planner.windows)
        duplicate = client.post("/api/v1/recording-searches", json=body)
        assert duplicate.status_code == 202
        assert duplicate.json()["status"] == "FAILED"
        assert len(planner.windows) == call_count

        fresh_body = {
            **body,
            "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        }
        fresh = client.post("/api/v1/recording-searches", json=fresh_body)
        assert fresh.status_code == 202
        assert fresh.json()["run_id"] != accepted.json()["run_id"]
        fresh_run_id = fresh.json()["run_id"]
        fresh_projected = _wait_for_phase7e_status(client, fresh.json()["status_url"])
        assert fresh_projected.json()["reason_code"] == expected_reason

    assert projected.status_code == 200
    assert projected.json() == {
        "investigation_id": investigation_id,
        "run_id": "search-run-12345678123442348234123456789abc",
        "schema_version": 0,
        "status": "FAILED",
        "reason_code": expected_reason,
        "terminal_result_id": None,
        "phase8_status": None,
        "phase8_reason": None,
        "terminal_details": None,
    }
    assert len(planner.windows) == 2
    assert extractor.calls == 0
    assert not repository.run_path(
        investigation_id,
        "search-run-12345678123442348234123456789abc",
    ).exists()
    assert not repository.run_path(
        investigation_id,
        fresh_run_id,
    ).exists()
    with repository.invocation_ownership(
        investigation_id,
        "search-run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        timeout_seconds=0.1,
    ) as ownership:
        assert ownership.active
        assert ownership.lock.held

    restarted = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        phase7e_service=service,
    )
    with TestClient(restarted) as client:
        after_restart = client.get(status_url)
    assert after_restart.status_code == 404


def test_pre_schema5_unexpected_failure_stays_internal_and_redacted(tmp_path: Path) -> None:
    planner = _UnexpectedPlanner()
    app, _service, repository, extractor, investigation_id = _pre_schema5_failure_app(
        tmp_path,
        planner,
    )
    body = {
        "investigation_id": investigation_id,
        "search_end": "2026-07-20T12:34:33",
        "request_id": _REQUEST_ID,
    }
    with TestClient(app) as client:
        accepted = client.post("/api/v1/recording-searches", json=body)
        assert accepted.status_code == 202
        projected = _wait_for_phase7e_status(client, accepted.json()["status_url"])
        manager = cast("Phase7EBackgroundManager", app.state.phase7e_background_manager)
        diagnostic = manager.pre_run_failure_diagnostic(
            investigation_id,
            accepted.json()["run_id"],
        )

    assert projected.status_code == 200
    assert projected.json()["status"] == "FAILED"
    assert projected.json()["reason_code"] == "internal_error"
    assert set(projected.json()) == {
        "investigation_id",
        "run_id",
        "schema_version",
        "status",
        "reason_code",
        "terminal_result_id",
        "phase8_status",
        "phase8_reason",
        "terminal_details",
    }
    assert "private.example" not in projected.text
    assert "password" not in projected.text
    assert "token" not in projected.text
    assert diagnostic is not None
    assert diagnostic.as_dict() == {
        "boundary": "internal",
        "category": "internal_error",
        "exception_class": "unexpected_exception",
        "cleanup_outcome": "unknown",
    }
    with pytest.raises(ValueError, match="invalid failure diagnostic vocabulary"):
        Phase7EFailureDiagnostic(
            "internal",
            "internal_error",
            "RuntimeError: secret-bearing detail",
            "unknown",
        )
    assert extractor.calls == 0
    assert not repository.run_path(
        investigation_id,
        accepted.json()["run_id"],
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
                "2026-07-20T14:34:29",
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
        # The fully production-shaped one-second narrowing plan can require
        # more than ninety seconds on a loaded CI worker; keep the bound
        # finite while allowing the authorized chain to reach its terminal
        # Schema-7 publication.
        deadline = time.monotonic() + 180
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


def test_production_shaped_nvr_duration_jitter_reaches_schema7(
    tmp_path: Path,
) -> None:
    """Exercise the full public/background chain with the observed NVR facts."""
    confirmation_service, investigation_id, resource_id = _confirmed_phase6(tmp_path)
    confirmed = confirmation_service.load_confirmed(investigation_id)
    segment = RecordingSegment(
        1,
        confirmed.anchor_time_utc.date(),
        int((confirmed.anchor_time_utc - timedelta(seconds=30)).timestamp()),
        int((confirmed.anchor_time_utc + timedelta(seconds=90)).timestamp()),
        confirmed.anchor_time_utc - timedelta(seconds=30),
        confirmed.anchor_time_utc + timedelta(seconds=90),
    )
    planner = _Planner(segment)
    extractor = _Extractor(tmp_path / "nvr-jitter.mp4")
    probe = _Probe(
        duration_ticks=59_873,
        time_base_den=1_000,
        codec="hevc",
        profile="Main",
        width=2_560,
        height=1_440,
        average_frame_rate_num=25,
    )
    repository = RecordingSearch7ERepository(
        tmp_path / "phase7e",
        lock_timeout_seconds=0.1,
        media_probe=probe,
    )
    executor = Phase7E1CExecutor(repository, CommonSessionAcquirer(planner, extractor, probe))
    policy, classifier_policy, object_policy = approved_phase7e_policy()
    # Keep this exact-duration fixture bounded: the production policy's
    # one-second narrowing is already exercised by the adjacent full-chain
    # test; this fixture focuses on the NVR media facts reaching Schema 7.
    timing_policy = StrictIdentityEnvelope.from_payload(
        "policy",
        {**policy.payload, "binary_stop_seconds": 60},
    )
    classifier = _ReconstructingClassifier(confirmation_service)
    decoder = _TimelineDecoder(decoded_dimensions=(8, 8))
    service = Phase7EPublicService(
        repository,
        executor,
        confirmation_service,
        classifier,
        # The fake decoder boundary keeps the deterministic fixture compact;
        # the retained-media probe still carries the observed HEVC/1440p facts.
        decoder,
        timing_policy,
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
    app = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        confirmation_service=confirmation_service,
        phase7e_service=service,
    )
    body = {
        "investigation_id": investigation_id,
        "search_end": "2026-07-20T12:35:28",
        "request_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    }
    with TestClient(app) as client:
        accepted = client.post("/api/v1/recording-searches", json=body)
        assert accepted.status_code == 202, accepted.text
        deadline = time.monotonic() + 120
        projected = client.get(accepted.json()["status_url"])
        while projected.json()["status"] in {"ACCEPTED", "RUNNING"} and time.monotonic() < deadline:
            time.sleep(1.0)
            projected = client.get(accepted.json()["status_url"])
    assert projected.json()["status"] == "FOUND"
    run = repository.reopen_schema7(investigation_id, accepted.json()["run_id"])
    session = next(item for item in run.records if item.family == "common-session")
    facts = probe.observed_facts[-1]
    assert facts.video_stream_count == 1
    assert facts.audio_stream_count == 0
    assert facts.codec == "hevc"
    assert (facts.width, facts.height) == (2_560, 1_440)
    assert session.payload["duration_ticks"] == 59_873
    assert session.payload["time_base_den"] == 1_000
    assert session.payload["mp4_size_bytes"] == len(b"one-retained-session")
    assert decoder.selected_indices == sorted(decoder.selected_indices)
    assert classifier.authoritative_facts
    assert set(classifier.authoritative_facts) == {(1, "Asia/Seoul", 10, resource_id)}
