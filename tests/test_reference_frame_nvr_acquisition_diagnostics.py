"""Fake-only tests for candidate-scoped NVR acquisition diagnostics."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from urllib.error import URLError

from fastapi.testclient import TestClient
from vigi import (
    AuthenticationError,
    RecordDay,
    RecordDaysResponse,
    RecordSearchProcessResponse,
    RecordSearchResultsResponse,
    TransportError,
)
from vigi import ConnectionError as SdkConnectionError
from vigi import RecordSegment as SdkRecordSegment
from vigi import (
    TimeoutError as SdkTimeoutError,
)

from vigi_vision import reference_frame_nvr_acquisition_diagnostics as acquisition_diagnostics
from vigi_vision.nvr import SdkNvrGateway
from vigi_vision.recording import (
    RecordingPlanner,
    RecordingSegment,
    RecordingWindow,
    ReplayRequest,
)
from vigi_vision.reference_frame_api import create_reference_frame_app
from vigi_vision.reference_frame_artifacts import (
    ReferenceFrameArtifactStore,
    ReferenceFrameManifest,
)
from vigi_vision.reference_frame_decoder import ReferenceFrameDecodeRequest
from vigi_vision.reference_frame_direct_support import DirectReferenceFrameRequest
from vigi_vision.reference_frame_models import (
    DecodedFrameEvidence,
    ReferenceFrameDecodeError,
    ReferenceFrameRequest,
    TimingPrecisionStatus,
    parse_reference_frame_request,
)
from vigi_vision.reference_frame_nvr_acquisition_diagnostics import (
    MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORD_BYTES,
    MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORDS,
    NvrAcquisitionDiagnostic,
    NvrAcquisitionDiagnosticOperation,
    NvrAcquisitionDiagnosticStage,
    NvrAcquisitionDiagnosticStore,
    NvrAcquisitionErrorKind,
)
from vigi_vision.reference_frame_resources import (
    ReferenceFrameImageResource,
    ReferenceFrameResourceStore,
)
from vigi_vision.reference_frame_service import ReferenceFrameService

if TYPE_CHECKING:
    from typing import NoReturn

    import pytest
    from fastapi import FastAPI
    from vigi import VigiClient

    from vigi_vision.config import NvrConnection
    from vigi_vision.reference_frame_direct import DirectReferenceFrameAcquisitionBoundary
    from vigi_vision.reference_frame_nvr_acquisition_diagnostics import (
        NvrAcquisitionDiagnosticWriter,
    )
    from vigi_vision.reference_frame_service import (
        ChannelInventoryBoundary,
        RecordingSegmentPlanningBoundary,
    )

_REFERENCE_TIME = datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc)
_JPEG_BYTES = b"\xff\xd8\xff\xe0reference-frame\xff\xd9"


class _TestResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def text(self) -> str: ...

    def json(self) -> dict[str, object]: ...


class _FakeRecords:
    days: tuple[str, ...]
    results: tuple[SdkRecordSegment, ...]
    free_error: Exception | None
    days_error: Exception | None
    results_error: Exception | None

    def __init__(
        self,
        *,
        days: tuple[str, ...] = ("20260720",),
        results: tuple[SdkRecordSegment, ...] = (),
        free_error: Exception | None = None,
        days_error: Exception | None = None,
        results_error: Exception | None = None,
    ) -> None:
        self.days = days
        self.results = results
        self.free_error = free_error
        self.days_error = days_error
        self.results_error = results_error

    def get_free_process(self) -> RecordSearchProcessResponse:
        if self.free_error is not None:
            raise self.free_error
        return RecordSearchProcessResponse(process_id=17, error_code=0)

    def list_days(self, channel_id: int, start_month: str, end_month: str) -> RecordDaysResponse:
        _ = (channel_id, start_month, end_month)
        if self.days_error is not None:
            raise self.days_error
        return RecordDaysResponse(
            days=tuple(RecordDay(day=day) for day in self.days),
            error_code=0,
        )

    def list_results(
        self,
        channel_id: int,
        process_id: int,
        day: str,
        start_index: int = 0,
        end_index: int = 99,
    ) -> RecordSearchResultsResponse:
        _ = (channel_id, process_id, day, start_index, end_index)
        if self.results_error is not None:
            raise self.results_error
        return RecordSearchResultsResponse(results=self.results, error_code=0)


class _FakeStream:
    error: Exception | None

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def build_replay_url(
        self,
        host: str,
        channel_id: int,
        start_time: str,
        end_time: str,
        stream: int = 1,
    ) -> str:
        _ = (host, channel_id, start_time, end_time, stream)
        if self.error is not None:
            raise self.error
        return "rtsp://nvr.example.test/replay"


class _FakeRecordingClient:
    records: _FakeRecords
    stream: _FakeStream

    def __init__(self, records: _FakeRecords, stream: _FakeStream | None = None) -> None:
        self.records = records
        self.stream = _FakeStream() if stream is None else stream


class _UnusedReplayExtractor:
    def extract(self, request: ReplayRequest) -> NoReturn:
        _ = request
        raise AssertionError


class _UnusedDecoder:
    def decode(self, request: ReferenceFrameDecodeRequest) -> DecodedFrameEvidence:
        _ = request
        raise AssertionError


class _UnusedResources:
    def resolve_image(self, resource_id: str) -> ReferenceFrameImageResource:
        _ = resource_id
        raise AssertionError


class _FailingDiagnosticWriter:
    def write(self, diagnostic: NvrAcquisitionDiagnostic) -> None:
        _ = diagnostic
        raise OSError


class _LoginFailureClient:
    error: Exception

    def __init__(self, error: Exception) -> None:
        self.error = error

    def login(self) -> None:
        raise self.error


class _MismatchedPlanner:
    def find_covering_segment(self, channel_id: int, instant_utc: datetime) -> RecordingSegment:
        return _covering_segment(channel_id, instant_utc)

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        _ = segment
        mismatched = RecordingWindow(
            window.channel_id,
            window.start_utc + timedelta(seconds=1),
            window.end_utc,
        )
        return ReplayRequest(mismatched, "rtsp://nvr.example.test/replay")


class _DecodeFailureAcquirer:
    def acquire(self, request: DirectReferenceFrameRequest) -> DecodedFrameEvidence:
        _ = request
        raise ReferenceFrameDecodeError


def _covering_segment(channel_id: int, instant_utc: datetime) -> RecordingSegment:
    start = instant_utc - timedelta(minutes=1)
    end = instant_utc + timedelta(minutes=1)
    return RecordingSegment(
        channel_id=channel_id,
        recording_day=instant_utc.date(),
        start_epoch_seconds=int(start.timestamp()),
        end_epoch_seconds=int(end.timestamp()),
        start_utc=start,
        end_utc=end,
    )


def _sdk_covering_segment(instant_utc: datetime = _REFERENCE_TIME) -> SdkRecordSegment:
    return SdkRecordSegment(
        start_time=str(int((instant_utc - timedelta(minutes=1)).timestamp())),
        end_time=str(int((instant_utc + timedelta(minutes=1)).timestamp())),
    )


def _planner(
    records: _FakeRecords | None = None,
    stream: _FakeStream | None = None,
) -> RecordingPlanner:
    return RecordingPlanner(
        _FakeRecordingClient(_FakeRecords() if records is None else records, stream),
        "nvr.example.test",
    )


def _app(
    tmp_path: Path,
    planner: RecordingSegmentPlanningBoundary,
    diagnostic_store: NvrAcquisitionDiagnosticWriter | None,
    *,
    channel_inventory: ChannelInventoryBoundary | None = None,
    direct_acquirer: DirectReferenceFrameAcquisitionBoundary | None = None,
) -> FastAPI:
    service = ReferenceFrameService(
        planner=planner,
        replay_extractor=_UnusedReplayExtractor(),
        decoder=_UnusedDecoder(),
        artifacts=ReferenceFrameArtifactStore(tmp_path / "reference-frames"),
        channel_inventory=channel_inventory,
        direct_acquirer=direct_acquirer,
    )
    return create_reference_frame_app(
        service=service,
        resources=_UnusedResources(),
        nvr_acquisition_diagnostic_store=diagnostic_store,
    )


def _post_candidate(app: FastAPI, offsets: tuple[int, ...] = (0,)) -> _TestResponse:
    with TestClient(app) as client:
        return client.post(
            "/api/v1/reference-frame-candidate-sets",
            json={
                "channel_id": 1,
                "reference_time": "2026-07-20T03:34:18Z",
                "offsets_seconds": list(offsets),
            },
        )


def _candidate_failure_code(response: _TestResponse) -> str:
    candidates = response.json().get("candidates")
    assert isinstance(candidates, list)
    candidates = cast("list[object]", candidates)
    assert candidates
    candidate_value = candidates[0]
    assert isinstance(candidate_value, dict)
    candidate = cast("dict[str, object]", candidate_value)
    failure = candidate.get("failure")
    assert isinstance(failure, dict)
    failure = cast("dict[str, object]", failure)
    code = failure.get("code")
    assert isinstance(code, str)
    return code


def _api_error_code(response: _TestResponse) -> str:
    error = response.json().get("error")
    assert isinstance(error, dict)
    error = cast("dict[str, object]", error)
    code = error.get("code")
    assert isinstance(code, str)
    return code


def _documents(root: Path) -> list[dict[str, object]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*.json"))]


def _diagnostic(
    diagnostic_id: str,
    *,
    sanitized_error_kind: NvrAcquisitionErrorKind = "timeout",
    stage: NvrAcquisitionDiagnosticStage = NvrAcquisitionDiagnosticStage.RECORDING_DAYS,
    operation: NvrAcquisitionDiagnosticOperation = NvrAcquisitionDiagnosticOperation.LIST_DAYS,
) -> NvrAcquisitionDiagnostic:
    return NvrAcquisitionDiagnostic(
        diagnostic_id=diagnostic_id,
        channel_id=1,
        requested_time_utc=_REFERENCE_TIME,
        candidate_offset_seconds=0,
        candidate_time_utc=_REFERENCE_TIME,
        timestamp_utc=_REFERENCE_TIME,
        stage=stage,
        operation=operation,
        sanitized_error_kind=sanitized_error_kind,
        exception_class_name="TimeoutError",
    )


def _full_diagnostic_store(root: Path) -> NvrAcquisitionDiagnosticStore:
    store = NvrAcquisitionDiagnosticStore(root)
    for index in range(MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORDS):
        store.write(_diagnostic(f"{index:032x}"))
    oldest = root / f"{0:032x}.json"
    os.utime(oldest, ns=(1, 1))
    return store


def _diagnostic_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.glob("*.json")}


def test_channel_refresh_connection_failure_keeps_nvr_unavailable_and_persists_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = SdkConnectionError("password=channel-secret")
    error.__cause__ = URLError(ConnectionRefusedError("token=channel-token"))
    sdk_client = _LoginFailureClient(error)

    def fake_client(_gateway: SdkNvrGateway) -> VigiClient:
        return cast("VigiClient", cast("object", sdk_client))

    monkeypatch.setattr(SdkNvrGateway, "_client", fake_client)
    gateway = SdkNvrGateway(cast("NvrConnection", object()))
    diagnostic_root = tmp_path / "diagnostics"

    response = _post_candidate(
        _app(
            tmp_path,
            _planner(),
            NvrAcquisitionDiagnosticStore(diagnostic_root),
            channel_inventory=gateway,
        )
    )

    assert response.status_code == 200
    assert _candidate_failure_code(response) == "nvr_unavailable"
    [record] = _documents(diagnostic_root)
    assert record["stage"] == "channel_refresh"
    assert record["operation"] == "sdk_nvr_gateway.channels"
    assert record["sanitized_error_kind"] == "connection_refused"
    assert record["channel_id"] == 1
    assert record["requested_time_utc"] == "2026-07-20T03:34:18Z"
    assert record["candidate_offset_seconds"] == 0
    assert record["candidate_time_utc"] == "2026-07-20T03:34:18Z"


def test_recording_free_process_auth_failure_is_attributed_to_its_sdk_operation(
    tmp_path: Path,
) -> None:
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    response = _post_candidate(
        _app(
            tmp_path,
            _planner(_FakeRecords(free_error=AuthenticationError("private auth response"))),
            store,
        )
    )

    assert response.status_code == 200
    assert _candidate_failure_code(response) == "nvr_unavailable"
    [record] = _documents(store.root)
    assert record["stage"] == "recording_free_process"
    assert record["operation"] == "records.get_free_process"
    assert record["sanitized_error_kind"] == "authentication"
    assert record["exception_class_name"] == "AuthenticationError"


def test_recording_days_timeout_is_attributed_without_changing_public_failure(
    tmp_path: Path,
) -> None:
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    response = _post_candidate(
        _app(
            tmp_path,
            _planner(_FakeRecords(days_error=SdkTimeoutError("private timeout text"))),
            store,
        )
    )

    assert response.status_code == 200
    assert _candidate_failure_code(response) == "nvr_unavailable"
    [record] = _documents(store.root)
    assert record["stage"] == "recording_days"
    assert record["operation"] == "records.list_days"
    assert record["sanitized_error_kind"] == "timeout"


def test_recording_results_sdk_failure_is_attributed_as_generic_sdk_request(
    tmp_path: Path,
) -> None:
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    response = _post_candidate(
        _app(
            tmp_path,
            _planner(_FakeRecords(results_error=TransportError("private SDK response"))),
            store,
        )
    )

    assert response.status_code == 200
    assert _candidate_failure_code(response) == "nvr_unavailable"
    [record] = _documents(store.root)
    assert record["stage"] == "recording_search_results"
    assert record["operation"] == "records.list_results"
    assert record["sanitized_error_kind"] == "sdk_request"


def test_malformed_recording_response_records_parse_stage_and_keeps_replay_failure(
    tmp_path: Path,
) -> None:
    malformed = SdkRecordSegment(start_time="not-an-epoch", end_time="1784530000")
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    response = _post_candidate(_app(tmp_path, _planner(_FakeRecords(results=(malformed,))), store))

    assert response.status_code == 200
    assert _candidate_failure_code(response) == "replay_failure"
    [record] = _documents(store.root)
    assert record["stage"] == "recording_response_parse"
    assert record["operation"] == "recording_results.parse"
    assert record["sanitized_error_kind"] == "unexpected"
    assert record["exception_class_name"] == "RecordingDataError"


def test_no_recording_keeps_recording_unavailable_without_nvr_diagnostic(
    tmp_path: Path,
) -> None:
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    response = _post_candidate(_app(tmp_path, _planner(_FakeRecords(days=())), store))

    assert response.status_code == 200
    assert _candidate_failure_code(response) == "recording_unavailable"
    assert not store.root.exists()


def test_replay_url_build_failure_records_its_boundary_and_existing_public_mapping(
    tmp_path: Path,
) -> None:
    records = _FakeRecords(results=(_sdk_covering_segment(),))
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    response = _post_candidate(
        _app(
            tmp_path,
            _planner(records, _FakeStream(error=TransportError("private URL response"))),
            store,
        )
    )

    assert response.status_code == 200
    assert _candidate_failure_code(response) == "nvr_unavailable"
    [record] = _documents(store.root)
    assert record["stage"] == "replay_url_build"
    assert record["operation"] == "stream.build_replay_url"
    assert record["sanitized_error_kind"] == "sdk_request"


def test_segment_selection_unexpected_failure_is_captured_without_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def malformed_segment(
        cls: type[RecordingSegment],
        channel_id: int,
        recording_day: date,
        sdk_segment: SdkRecordSegment,
    ) -> RecordingSegment:
        _ = (cls, sdk_segment)
        return RecordingSegment(
            channel_id=channel_id,
            recording_day=recording_day,
            start_epoch_seconds=0,
            end_epoch_seconds=1,
            start_utc=cast("datetime", cast("object", None)),
            end_utc=_REFERENCE_TIME,
        )

    monkeypatch.setattr(RecordingSegment, "from_sdk", classmethod(malformed_segment))
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    response = _post_candidate(
        _app(
            tmp_path,
            _planner(_FakeRecords(results=(_sdk_covering_segment(),))),
            store,
        )
    )

    assert response.status_code == 500
    assert _api_error_code(response) == "internal_error"
    [record] = _documents(store.root)
    assert record["stage"] == "recording_segment_selection"
    assert record["operation"] == "recording_segments.select_covering"
    assert record["sanitized_error_kind"] == "unexpected"
    assert record["exception_class_name"] == "TypeError"


def test_application_segment_mismatch_keeps_replay_failure_without_nvr_diagnostic(
    tmp_path: Path,
) -> None:
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    response = _post_candidate(_app(tmp_path, _MismatchedPlanner(), store))

    assert response.status_code == 200
    assert _candidate_failure_code(response) == "replay_failure"
    assert not store.root.exists()


def test_decode_failure_keeps_decode_mapping_without_nvr_diagnostic(tmp_path: Path) -> None:
    records = _FakeRecords(results=(_sdk_covering_segment(),))
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    service = ReferenceFrameService(
        planner=_planner(records),
        replay_extractor=_UnusedReplayExtractor(),
        decoder=_UnusedDecoder(),
        artifacts=ReferenceFrameArtifactStore(tmp_path / "reference-frames"),
        direct_acquirer=_DecodeFailureAcquirer(),
    )
    app = create_reference_frame_app(
        service=service,
        resources=_UnusedResources(),
        nvr_acquisition_diagnostic_store=store,
    )

    response = _post_candidate(app)

    assert response.status_code == 200
    assert _candidate_failure_code(response) == "decode_failure"
    assert not store.root.exists()
    assert list((tmp_path / "reference-frames").glob("*.claim")) == []


def test_multiple_candidate_offsets_write_separate_linkable_records(tmp_path: Path) -> None:
    records = _FakeRecords(days_error=SdkTimeoutError("private timeout text"))
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    response = _post_candidate(
        _app(tmp_path, _planner(records), store),
        offsets=(-10, 0, 10),
    )

    assert response.status_code == 200
    payload = response.json()
    candidates = cast("list[object]", payload["candidates"])
    assert isinstance(candidates, list)
    failure_codes: list[str] = []
    for value in candidates:
        assert isinstance(value, dict)
        candidate = cast("dict[str, object]", value)
        failure = candidate["failure"]
        assert isinstance(failure, dict)
        failure = cast("dict[str, object]", failure)
        code = failure["code"]
        assert isinstance(code, str)
        failure_codes.append(code)
    assert tuple(failure_codes) == (
        "nvr_unavailable",
        "nvr_unavailable",
        "nvr_unavailable",
    )
    records_written = _documents(store.root)
    assert len(records_written) == 3
    assert len({record["diagnostic_id"] for record in records_written}) == 3
    by_offset = {record["candidate_offset_seconds"]: record for record in records_written}
    assert set(by_offset) == {-10, 0, 10}
    assert {offset: by_offset[offset]["candidate_time_utc"] for offset in by_offset} == {
        -10: "2026-07-20T03:34:08Z",
        0: "2026-07-20T03:34:18Z",
        10: "2026-07-20T03:34:28Z",
    }


def test_diagnostic_write_failure_keeps_candidate_response_and_http_status(tmp_path: Path) -> None:
    response = _post_candidate(
        _app(
            tmp_path,
            _planner(_FakeRecords(free_error=AuthenticationError("private auth response"))),
            _FailingDiagnosticWriter(),
        )
    )

    assert response.status_code == 200
    assert _candidate_failure_code(response) == "nvr_unavailable"


def test_diagnostic_construction_failure_keeps_candidate_response_and_http_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenClock:
        @staticmethod
        def now(tz: timezone) -> datetime:
            _ = tz
            raise RuntimeError

    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    monkeypatch.setattr(acquisition_diagnostics, "datetime", _BrokenClock)

    response = _post_candidate(
        _app(
            tmp_path,
            _planner(_FakeRecords(free_error=AuthenticationError("private auth response"))),
            store,
        )
    )

    assert response.status_code == 200
    assert _candidate_failure_code(response) == "nvr_unavailable"
    assert not store.root.exists()


def test_closed_diagnostic_redacts_secrets_from_exception_response_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker_values = (
        "diag-password-93",
        "diag-token-41",
        "diag-authorization-76",
        "diag-user-52",
        "diag-query-secret-28",
        "nvr-private-host-61",
    )
    secret_context = (
        f"password={marker_values[0]} token={marker_values[1]} "
        f"Authorization: Bearer {marker_values[2]} "
        f"rtsp://diag-user-52:{marker_values[0]}@nvr-private-host-61/live?token={marker_values[4]}"
    )
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")

    with caplog.at_level(logging.DEBUG):
        response = _post_candidate(
            _app(
                tmp_path,
                _planner(_FakeRecords(free_error=AuthenticationError(secret_context))),
                store,
            )
        )

    [path] = list(store.root.glob("*.json"))
    serialized = path.read_text(encoding="utf-8")
    assert response.status_code == 200
    assert _candidate_failure_code(response) == "nvr_unavailable"
    for marker in marker_values:
        assert marker not in serialized
        assert marker not in response.text
        assert marker not in caplog.text
    document = cast("dict[str, object]", json.loads(serialized))
    assert set(document) == {
        "schema_version",
        "diagnostic_kind",
        "diagnostic_id",
        "channel_id",
        "requested_time_utc",
        "candidate_offset_seconds",
        "candidate_time_utc",
        "timestamp_utc",
        "stage",
        "operation",
        "sanitized_error_kind",
        "exception_class_name",
    }


def test_diagnostic_collection_and_each_record_are_bounded(tmp_path: Path) -> None:
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    for index in range(MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORDS + 3):
        diagnostic = NvrAcquisitionDiagnostic(
            diagnostic_id=f"{index:032x}",
            channel_id=1,
            requested_time_utc=_REFERENCE_TIME,
            candidate_offset_seconds=0,
            candidate_time_utc=_REFERENCE_TIME,
            timestamp_utc=_REFERENCE_TIME,
            stage=NvrAcquisitionDiagnosticStage.RECORDING_DAYS,
            operation=NvrAcquisitionDiagnosticOperation.LIST_DAYS,
            sanitized_error_kind="timeout",
            exception_class_name="TimeoutError",
        )
        store.write(diagnostic)

    paths = tuple(store.root.glob("*.json"))
    assert len(paths) == MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORDS
    assert all(path.stat().st_size <= MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORD_BYTES for path in paths)


def test_conflicting_oldest_diagnostic_at_retention_limit_preserves_all_records(
    tmp_path: Path,
) -> None:
    store = _full_diagnostic_store(tmp_path / "diagnostics")
    before = _diagnostic_snapshot(store.root)

    store.write(_diagnostic(f"{0:032x}", sanitized_error_kind="authentication"))

    assert _diagnostic_snapshot(store.root) == before
    assert len(tuple(store.root.glob("*.json"))) == MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORDS


def test_identical_diagnostic_publication_is_idempotent_without_pruning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _full_diagnostic_store(tmp_path / "diagnostics")
    diagnostic = _diagnostic(f"{0:032x}")
    before = _diagnostic_snapshot(store.root)

    def fail_if_pruned(_: NvrAcquisitionDiagnosticStore) -> None:
        raise AssertionError

    monkeypatch.setattr(NvrAcquisitionDiagnosticStore, "_prune_older_records", fail_if_pruned)
    store.write(diagnostic)

    assert _diagnostic_snapshot(store.root) == before
    assert len(tuple(store.root.glob("*.json"))) == MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORDS


def test_conflicting_non_oldest_diagnostic_does_not_prune(tmp_path: Path) -> None:
    store = _full_diagnostic_store(tmp_path / "diagnostics")
    target_id = f"{64:032x}"
    before = _diagnostic_snapshot(store.root)

    store.write(_diagnostic(target_id, sanitized_error_kind="authentication"))

    assert _diagnostic_snapshot(store.root) == before
    assert len(tuple(store.root.glob("*.json"))) == MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORDS


def test_new_diagnostic_at_retention_limit_prunes_only_the_oldest_record(
    tmp_path: Path,
) -> None:
    store = _full_diagnostic_store(tmp_path / "diagnostics")
    before = _diagnostic_snapshot(store.root)
    new_id = "f" * 32

    store.write(_diagnostic(new_id))

    after = _diagnostic_snapshot(store.root)
    assert len(after) == MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORDS
    assert f"{0:032x}.json" not in after
    assert f"{new_id}.json" in after
    assert all(after[name] == value for name, value in before.items() if name != f"{0:032x}.json")


def test_conflicting_publication_keeps_candidate_response_and_existing_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic_id = "a" * 32
    store = NvrAcquisitionDiagnosticStore(tmp_path / "diagnostics")
    existing = _diagnostic(
        diagnostic_id,
        sanitized_error_kind="authentication",
        stage=NvrAcquisitionDiagnosticStage.REPLAY_URL_BUILD,
        operation=NvrAcquisitionDiagnosticOperation.BUILD_REPLAY_URL,
    )
    store.write(existing)
    existing_path = store.root / f"{diagnostic_id}.json"
    before = existing_path.read_bytes()

    def fixed_diagnostic_id(_: int) -> str:
        return diagnostic_id

    monkeypatch.setattr(acquisition_diagnostics, "token_hex", fixed_diagnostic_id)

    response = _post_candidate(
        _app(
            tmp_path,
            _planner(_FakeRecords(days_error=SdkTimeoutError("private timeout text"))),
            store,
        )
    )

    assert response.status_code == 200
    assert _candidate_failure_code(response) == "nvr_unavailable"
    assert existing_path.read_bytes() == before
    assert len(tuple(store.root.glob("*.json"))) == 1


def test_historical_reference_frame_resource_reads_without_diagnostic_sidecar(
    tmp_path: Path,
) -> None:
    request: ReferenceFrameRequest = parse_reference_frame_request(
        channel_id=1,
        requested_time_text="2026-07-20T03:34:18Z",
        now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    segment = _covering_segment(request.channel_id, request.requested_time_utc)
    artifact_root = tmp_path / "artifacts"
    session = ReferenceFrameArtifactStore(artifact_root).begin(request, segment)
    _ = session.jpeg_path.write_bytes(_JPEG_BYTES)
    evidence = DecodedFrameEvidence(
        jpeg_path=session.jpeg_path,
        local_pts_seconds=2.0,
        width=1280,
        height=720,
        timing_precision_status=TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
        warnings=(),
    )
    manifest = ReferenceFrameManifest(
        request=request,
        segment=segment,
        extraction_window=RecordingWindow(
            1,
            request.requested_time_utc - timedelta(seconds=2),
            request.requested_time_utc + timedelta(seconds=4),
        ),
        resource_id=session.resource_id,
        evidence=evidence,
        estimated_source_time_utc=None,
        offset_from_requested_seconds=None,
    )
    _ = session.finalize(manifest)

    image = ReferenceFrameResourceStore(artifact_root).resolve_image(session.resource_id)

    assert image.jpeg_path.read_bytes() == _JPEG_BYTES
    assert not (tmp_path / "reference-frame-nvr-acquisition-v1").exists()
