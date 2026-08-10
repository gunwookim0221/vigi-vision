from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest
import tests.test_investigation_confirmation as confirmation_fixture
from typing_extensions import override

import vigi_vision.recording_search_a2_repository as a2_repository
from vigi_vision.channel_selection import Channel
from vigi_vision.recording import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.recording_models import RecordingUnavailableError
from vigi_vision.recording_search_a2_models import (
    AcquisitionOperationRecord,
    BatchDecodeRequest,
    DecodedTargetResult,
    ProbeFrameRequestRecord,
    ProbeRequestStatus,
    RecordingSearchManifestV2,
    SourceTimeBase,
    canonical_frame_id_for,
    decoded_frame_utc_for,
)
from vigi_vision.recording_search_a2_repository import parse_schema2_manifest
from vigi_vision.recording_search_a2_service import validate_successful_request
from vigi_vision.recording_search_models import (
    RecordingSearchArtifactError,
    RecordingSearchBaselineError,
    RecordingSearchManifestCorruptError,
    RecordingSearchRequest,
    RecordingSearchState,
)
from vigi_vision.recording_search_repository import RecordingSearchRepository
from vigi_vision.recording_search_service import RecordingSearchRunHandle, RecordingSearchService
from vigi_vision.replay import ReplayClip

_JPEG_BYTES = cast("bytes", confirmation_fixture.__dict__["_JPEG_BYTES"])
_NOW = datetime(2026, 8, 2, 4, 5, 6, tzinfo=timezone.utc)
_ORIGIN = datetime(2026, 7, 20, 3, 34, 0, tzinfo=timezone.utc)


def test_source_time_mapping_rounds_ties_to_even_and_rejects_unreduced_base() -> None:
    base = SourceTimeBase(numerator=1, denominator=2)
    assert decoded_frame_utc_for(_ORIGIN, 1, base) == _ORIGIN + timedelta(microseconds=500_000)
    assert decoded_frame_utc_for(_ORIGIN, 3, base) == _ORIGIN + timedelta(
        seconds=1, microseconds=500_000
    )
    half_microsecond = SourceTimeBase(numerator=1, denominator=2_000_000)
    assert decoded_frame_utc_for(_ORIGIN, 1, half_microsecond) == _ORIGIN
    assert decoded_frame_utc_for(_ORIGIN, 3, half_microsecond) == _ORIGIN + timedelta(
        microseconds=2
    )
    with pytest.raises(ValueError, match="Value error"):
        _ = SourceTimeBase(numerator=2, denominator=4)


def test_canonical_frame_identity_uses_only_the_five_approved_inputs() -> None:
    decoded = _ORIGIN + timedelta(seconds=20)
    first = canonical_frame_id_for(
        "object-disappearance-v3-ch1-20260720T033418Z",
        "search-run-abcdef12",
        1,
        "segment-20260720T033400Z-20260720T034000Z",
        decoded,
    )
    second = canonical_frame_id_for(
        "object-disappearance-v3-ch1-20260720T033418Z",
        "search-run-abcdef12",
        1,
        "segment-20260720T033400Z-20260720T034000Z",
        decoded,
    )
    assert first == second


@dataclass(frozen=True, slots=True)
class _Inventory:
    def channels(self) -> tuple[Channel, ...]:
        return (Channel(1, "Counter", "Counter", online=True),)


@dataclass(frozen=True, slots=True)
class _FailingSchema2Repository(RecordingSearchRepository):
    fail_on: int = 3
    writes: list[int] = field(default_factory=lambda: [0], repr=False, compare=False)

    @override
    def write_schema2_manifest(self, manifest: RecordingSearchManifestV2, directory: Path) -> None:
        self.writes[0] += 1
        if self.writes[0] == self.fail_on:
            raise RecordingSearchArtifactError
        RecordingSearchRepository.write_schema2_manifest(self, manifest, directory)


@dataclass(frozen=True, slots=True)
class _InterruptingSchema2Repository(RecordingSearchRepository):
    interrupt_on: int = 2
    writes: list[int] = field(default_factory=lambda: [0], repr=False, compare=False)

    @override
    def write_schema2_manifest(self, manifest: RecordingSearchManifestV2, directory: Path) -> None:
        self.writes[0] += 1
        RecordingSearchRepository.write_schema2_manifest(self, manifest, directory)
        if self.writes[0] == self.interrupt_on:
            raise KeyboardInterrupt


@dataclass(frozen=True, slots=True)
class _InterruptBeforeSchema2ManifestRepository(RecordingSearchRepository):
    interrupt_on: int = 2
    writes: list[int] = field(default_factory=lambda: [0], repr=False, compare=False)

    @override
    def write_schema2_manifest(self, manifest: RecordingSearchManifestV2, directory: Path) -> None:
        self.writes[0] += 1
        if self.writes[0] == self.interrupt_on:
            raise KeyboardInterrupt
        RecordingSearchRepository.write_schema2_manifest(self, manifest, directory)


@dataclass(frozen=True, slots=True)
class _ServiceOptions:
    operation_ids: tuple[str, ...] = ("acquisition-op-test",)
    repository: RecordingSearchRepository | None = None


@dataclass(slots=True)
class _Planner:
    segment: RecordingSegment
    unavailable: bool = False

    def find_covering_segment(self, channel_id: int, instant_utc: datetime) -> RecordingSegment:
        if self.unavailable:
            raise RecordingUnavailableError
        assert channel_id == self.segment.channel_id
        assert self.segment.start_utc <= instant_utc < self.segment.end_utc
        return self.segment

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        assert segment == self.segment
        return ReplayRequest(window, "rtsp://example.invalid/replay")


@dataclass(slots=True)
class _Extractor:
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


@dataclass(slots=True)
class _Decoder:
    alias: bool = False
    source_pts_override: int | None = None
    origin: datetime = _ORIGIN
    jpeg_bytes: bytes = _JPEG_BYTES

    def decode_targets(
        self, acquisition: BatchDecodeRequest, ordered_requested_targets: tuple[datetime, ...]
    ) -> tuple[DecodedTargetResult, ...]:
        values: list[DecodedTargetResult] = []
        for target in ordered_requested_targets:
            source_pts = (
                self.source_pts_override
                if self.source_pts_override is not None
                else (
                    int((ordered_requested_targets[0] - _ORIGIN).total_seconds())
                    if self.alias
                    else int((target - _ORIGIN).total_seconds())
                )
            )
            values.append(
                DecodedTargetResult(
                    requested_time_utc=target,
                    physical_replay_origin_utc=self.origin,
                    source_pts=source_pts,
                    source_time_base=SourceTimeBase(numerator=1, denominator=1),
                    decoded_pts=source_pts,
                    replay_time_base=SourceTimeBase(numerator=1, denominator=1),
                    decoded_ordinal=len(values),
                    source_width=1280,
                    source_height=720,
                    jpeg_bytes=self.jpeg_bytes,
                    decode_session_id="decode-session-test",
                )
            )
        assert acquisition.segment.start_utc <= _ORIGIN
        return tuple(values)


def _service(
    tmp_path: Path,
    *,
    planner: _Planner,
    decoder: _Decoder,
    extractor: _Extractor,
    options: _ServiceOptions | None = None,
) -> tuple[RecordingSearchService, str]:
    resolved = _ServiceOptions() if options is None else options
    context = confirmation_fixture.build_context(tmp_path / "confirmation")
    _ = context.service.confirm(confirmation_fixture.build_request(context.resource_id))
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=(
            RecordingSearchRepository(tmp_path / "searches")
            if resolved.repository is None
            else resolved.repository
        ),
        channel_inventory=_Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
        recording_planner=planner,
        replay_extractor=extractor,
        batch_decoder=decoder,
        operation_id_factory=iter(resolved.operation_ids).__next__,
        probe_request_id_factory=iter(
            tuple(f"probe-request-{index}" for index in range(1, 17))
        ).__next__,
    )
    return service, context.investigation_id


def _successful_a2_run(
    tmp_path: Path,
) -> tuple[
    RecordingSearchService,
    str,
    RecordingSearchRunHandle,
    RecordingSearchManifestV2,
    ProbeFrameRequestRecord,
]:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(),
        options=_ServiceOptions(),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None
    request = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))[0]
    manifest = service.repository.load(investigation_id, started.manifest.search_run_id)
    assert isinstance(manifest, RecordingSearchManifestV2)
    return service, investigation_id, started.run_handle, manifest, request


def test_production_acquisition_reuses_one_frame_for_an_alias_and_removes_replay(
    tmp_path: Path,
) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    extractor = _Extractor(tmp_path)
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=extractor,
        decoder=_Decoder(alias=True),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None
    target = _ORIGIN + timedelta(seconds=20)

    requests = service.acquire_targets(
        started.run_handle,
        (target, target + timedelta(seconds=1), target + timedelta(seconds=2)),
    )

    assert [item.status.value for item in requests] == ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]
    assert len({item.canonical_frame_id for item in requests}) == 1
    assert requests[0].alias_of_probe_request_id is None
    assert requests[1].alias_of_probe_request_id == requests[0].probe_request_id
    assert requests[2].alias_of_probe_request_id == requests[0].probe_request_id
    assert extractor.calls == 1
    assert not list(tmp_path.glob("clip-*.mp4"))
    started.run_handle.release()


def test_concurrent_same_frame_acquisitions_serialize_without_lost_indexes(
    tmp_path: Path,
) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(source_pts_override=20),
        options=_ServiceOptions(
            operation_ids=("acquisition-op-one", "acquisition-op-two"),
        ),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None
    targets = (_ORIGIN + timedelta(seconds=20), _ORIGIN + timedelta(seconds=21))
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(service.acquire_targets, started.run_handle, (target,))
            for target in targets
        ]
        results = [future.result()[0] for future in futures]

    assert {result.operation_id for result in results} == {
        "acquisition-op-one",
        "acquisition-op-two",
    }
    assert len({result.probe_request_id for result in results}) == 2
    assert len({result.canonical_frame_id for result in results}) == 1
    status = service.status(investigation_id, started.manifest.search_run_id)
    assert status.state is RecordingSearchState.RUNNING
    started.run_handle.release()


def test_unindexed_final_frame_is_not_reopened_as_evidence(tmp_path: Path) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(),
        options=_ServiceOptions(),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None
    _ = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))
    run_path = service.repository.run_path(investigation_id, started.manifest.search_run_id)
    orphan = run_path / "frames" / f"frame-{'a' * 64}.json"
    _ = orphan.write_text("{}", encoding="utf-8")

    status = service.status(investigation_id, started.manifest.search_run_id)

    assert status.state is RecordingSearchState.RUNNING
    started.run_handle.release()


def test_changed_jpeg_is_rejected_on_strict_reopen(tmp_path: Path) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None
    request = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))[0]
    run_path = service.repository.run_path(investigation_id, started.manifest.search_run_id)
    frame_path = run_path / "evidence" / "frames" / f"{request.canonical_frame_id}.jpg"
    _ = frame_path.write_bytes(b"changed-jpeg")

    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = service.status(investigation_id, started.manifest.search_run_id)
    started.run_handle.release()


def test_cross_run_frame_reference_is_rejected_by_status_path(tmp_path: Path) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(),
        options=_ServiceOptions(operation_ids=("acquisition-op-one", "acquisition-op-two")),
    )
    request = RecordingSearchRequest(
        investigation_id=investigation_id,
        search_end_time_text="2026-07-20T13:00:00+09:00",
        source_timezone="Asia/Seoul",
    )
    first = service.start(request)
    assert first.run_handle is not None
    first_request = service.acquire_targets(first.run_handle, (_ORIGIN + timedelta(seconds=20),))[0]
    first_run_id = first.manifest.search_run_id
    first.run_handle.release()
    second = service.start(request)
    assert second.run_handle is not None
    second_request = service.acquire_targets(second.run_handle, (_ORIGIN + timedelta(seconds=21),))[
        0
    ]
    first_request_path = (
        service.repository.run_path(investigation_id, first_run_id)
        / "requests"
        / f"{first_request.probe_request_id}.json"
    )
    raw = first_request_path.read_text(encoding="utf-8")
    _ = first_request_path.write_text(
        raw.replace(
            first_request.canonical_frame_id or "", second_request.canonical_frame_id or ""
        ),
        encoding="utf-8",
    )

    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = service.status(investigation_id, first_run_id)
    second.run_handle.release()


def test_recording_unavailable_is_a_failed_request_without_absent_state(tmp_path: Path) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment, unavailable=True),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None

    requests = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))

    assert requests[0].status.value == "FAILED"
    assert requests[0].failure_reason == "recording_unavailable"
    assert (
        service.status(investigation_id, started.manifest.search_run_id).state
        is RecordingSearchState.RUNNING
    )
    started.run_handle.release()


def test_failed_retry_then_identical_duplicate_reuses_latest_success(tmp_path: Path) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    planner = _Planner(segment, unavailable=True)
    extractor = _Extractor(tmp_path)
    service, investigation_id = _service(
        tmp_path,
        planner=planner,
        extractor=extractor,
        decoder=_Decoder(),
        options=_ServiceOptions(operation_ids=("acquisition-op-failed", "acquisition-op-success")),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None
    target = (_ORIGIN + timedelta(seconds=20),)

    first = service.acquire_targets(started.run_handle, target)[0]
    planner.unavailable = False
    second = service.acquire_targets(started.run_handle, target)[0]
    extraction_calls = extractor.calls
    third = service.acquire_targets(started.run_handle, target)[0]

    assert first.status is ProbeRequestStatus.FAILED
    assert second.status is ProbeRequestStatus.SUCCEEDED
    assert third == second
    assert extractor.calls == extraction_calls
    manifest = service.repository.load(investigation_id, started.manifest.search_run_id)
    assert isinstance(manifest, RecordingSearchManifestV2)
    assert len(manifest.acquisition_operation_ids) == 2
    assert len(manifest.probe_request_ids) == 2
    assert len(manifest.canonical_frame_ids) == 1
    started.run_handle.release()


def test_operation_admission_failure_before_final_record_leaves_no_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(),
        options=_ServiceOptions(
            operation_ids=("acquisition-op-interrupted", "acquisition-op-retry")
        ),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None

    def interrupt_before_final(path: Path, payload: str | bytes) -> None:
        if path.parent.name == "operations":
            raise KeyboardInterrupt
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(payload.encode("utf-8") if isinstance(payload, str) else payload)

    monkeypatch.setattr(a2_repository, "_write_no_replace", interrupt_before_final)
    with pytest.raises(KeyboardInterrupt):
        _ = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))
    monkeypatch.undo()

    manifest = service.repository.load(investigation_id, started.manifest.search_run_id)
    assert isinstance(manifest, RecordingSearchManifestV2)
    assert manifest.acquisition_operation_ids == ()
    assert not list(
        service.repository.run_path(investigation_id, started.manifest.search_run_id)
        .joinpath("operations")
        .iterdir()
    )
    retried = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))[0]
    assert retried.status is ProbeRequestStatus.SUCCEEDED
    retried_manifest = service.repository.load(investigation_id, started.manifest.search_run_id)
    assert isinstance(retried_manifest, RecordingSearchManifestV2)
    assert retried_manifest.acquisition_operation_ids == ("acquisition-op-retry",)
    started.run_handle.release()


def test_unindexed_operation_after_final_write_is_recovered_as_residue(
    tmp_path: Path,
) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    repository = _InterruptBeforeSchema2ManifestRepository(tmp_path / "searches")
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(),
        options=_ServiceOptions(repository=repository),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None
    with pytest.raises(KeyboardInterrupt):
        _ = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))

    recovered = service.status(investigation_id, started.manifest.search_run_id)
    assert isinstance(recovered, RecordingSearchManifestV2)
    assert recovered.acquisition_operation_ids == ()
    run_path = service.repository.run_path(investigation_id, started.manifest.search_run_id)
    assert not list((run_path / "operations").iterdir())
    assert not list(run_path.glob(".phase7a2-admission-*"))
    started.run_handle.release()


def test_manifest_commit_then_interruption_reopens_with_admitted_operation(
    tmp_path: Path,
) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    repository = _InterruptingSchema2Repository(tmp_path / "searches")
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(),
        options=_ServiceOptions(repository=repository),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None
    with pytest.raises(KeyboardInterrupt):
        _ = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))

    recovered = service.status(investigation_id, started.manifest.search_run_id)
    assert isinstance(recovered, RecordingSearchManifestV2)
    assert len(recovered.acquisition_operation_ids) == 1
    run_path = service.repository.run_path(investigation_id, started.manifest.search_run_id)
    assert not list(run_path.glob(".phase7a2-admission-*"))
    started.run_handle.release()


def test_unindexed_operation_without_admission_marker_is_rejected(tmp_path: Path) -> None:
    service, investigation_id, handle, manifest, request = _successful_a2_run(tmp_path)
    operation_path = (
        service.repository.run_path(investigation_id, manifest.search_run_id)
        / "operations"
        / f"{request.operation_id}.json"
    )
    operation = AcquisitionOperationRecord.model_validate_json(
        operation_path.read_text(encoding="utf-8"), strict=True
    )
    extra = operation.model_copy(update={"operation_id": "acquisition-op-inserted"})
    inserted_path = operation_path.with_name("acquisition-op-inserted.json")
    _ = inserted_path.write_text(extra.model_dump_json(), encoding="utf-8")

    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = service.status(investigation_id, manifest.search_run_id)
    handle.release()


@pytest.mark.parametrize("variant", ["malformed", "foreign"])
def test_malformed_or_foreign_unindexed_operation_is_rejected(tmp_path: Path, variant: str) -> None:
    service, investigation_id, handle, manifest, request = _successful_a2_run(tmp_path)
    run_path = service.repository.run_path(investigation_id, manifest.search_run_id)
    operation_path = run_path / "operations" / f"{request.operation_id}.json"
    operation = AcquisitionOperationRecord.model_validate_json(
        operation_path.read_text(encoding="utf-8"), strict=True
    )
    operation_id = f"acquisition-op-{variant}"
    staging = run_path / f".phase7a2-admission-{operation_id}"
    _ = staging.mkdir()
    final = run_path / "operations" / f"{operation_id}.json"
    if variant == "malformed":
        payload = "{}"
    else:
        foreign = operation.model_copy(
            update={
                "operation_id": operation_id,
                "investigation_id": "object-disappearance-v3-ch2-20260720T033428Z",
            }
        )
        payload = foreign.model_dump_json()
    _ = (staging / "operation.json").write_text(payload, encoding="utf-8")
    _ = final.write_text(payload, encoding="utf-8")

    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = service.status(investigation_id, manifest.search_run_id)
    handle.release()


def test_conflicting_or_ownership_inconsistent_unindexed_operation_is_rejected(
    tmp_path: Path,
) -> None:
    service, investigation_id, handle, manifest, request = _successful_a2_run(tmp_path)
    run_path = service.repository.run_path(investigation_id, manifest.search_run_id)
    operation_path = run_path / "operations" / f"{request.operation_id}.json"
    operation = AcquisitionOperationRecord.model_validate_json(
        operation_path.read_text(encoding="utf-8"), strict=True
    )
    conflict_id = "acquisition-op-conflict"
    conflict_staging = run_path / f".phase7a2-admission-{conflict_id}"
    _ = conflict_staging.mkdir()
    staged = operation.model_copy(update={"operation_id": conflict_id})
    published = staged.model_copy(
        update={"admitted_at_utc": staged.admitted_at_utc + timedelta(microseconds=1)}
    )
    _ = (conflict_staging / "operation.json").write_text(staged.model_dump_json(), encoding="utf-8")
    _ = (run_path / "operations" / f"{conflict_id}.json").write_text(
        published.model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = service.status(investigation_id, manifest.search_run_id)
    _ = (conflict_staging / "operation.json").unlink()
    _ = (run_path / "operations" / f"{conflict_id}.json").unlink()
    _ = conflict_staging.rmdir()

    ownership_id = "acquisition-op-owned"
    ownership_staging = run_path / f".phase7a2-admission-{ownership_id}"
    _ = ownership_staging.mkdir()
    _ = (ownership_staging / "operation.json").write_text(
        staged.model_dump_json(), encoding="utf-8"
    )
    _ = (run_path / "operations" / f"{ownership_id}.json").write_text(
        staged.model_copy(update={"operation_id": ownership_id}).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = service.status(investigation_id, manifest.search_run_id)
    handle.release()


def test_indexed_operation_missing_record_is_rejected(tmp_path: Path) -> None:
    service, investigation_id, handle, manifest, request = _successful_a2_run(tmp_path)
    operation_path = (
        service.repository.run_path(investigation_id, manifest.search_run_id)
        / "operations"
        / f"{request.operation_id}.json"
    )
    _ = operation_path.unlink()

    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = service.status(investigation_id, manifest.search_run_id)
    handle.release()


def test_handoff_validator_accepts_same_frame_from_distinct_operations(tmp_path: Path) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(source_pts_override=20),
        options=_ServiceOptions(operation_ids=("acquisition-op-one", "acquisition-op-two")),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None
    first = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))[0]
    second = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=21),))[0]

    validated = validate_successful_request(
        service,
        investigation_id,
        started.manifest.search_run_id,
        second.probe_request_id,
    )

    assert first.operation_id != second.operation_id
    assert validated.request.canonical_frame_id == validated.frame.canonical_frame_id
    assert validated.request.operation_id != validated.frame.operation_id
    manifest_path = (
        service.repository.run_path(investigation_id, started.manifest.search_run_id)
        / "manifest.json"
    )
    raw = manifest_path.read_text(encoding="utf-8")
    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = parse_schema2_manifest(
            raw.replace(
                '"schema_version": 2,',
                '"schema_version": 2,\n  "schema_version": 2,',
                1,
            )
        )
    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = parse_schema2_manifest(
            raw.replace(
                '  "failure_reason": null,',
                '  "failure_reason": null,\n  "future_field": 1',
                1,
            )
        )
    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = parse_schema2_manifest(raw.replace('  "failure_reason": null,\n', "", 1))
    started.run_handle.release()


def test_inconsistent_provenance_for_one_canonical_id_fails_safely(tmp_path: Path) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    decoder = _Decoder(source_pts_override=20)
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=decoder,
        options=_ServiceOptions(operation_ids=("acquisition-op-one", "acquisition-op-two")),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None
    _ = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))
    decoder.origin = _ORIGIN + timedelta(seconds=1)
    decoder.source_pts_override = 19

    with pytest.raises(RecordingSearchArtifactError):
        _ = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=21),))
    started.run_handle.release()


def test_unconfigured_decoder_fails_safely_before_replay(tmp_path: Path) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(),
    )
    service.batch_decoder = None
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None

    requests = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))

    assert requests[0].status.value == "FAILED"
    assert requests[0].failure_reason == "missing_provenance"
    started.run_handle.release()


def test_invalid_decoder_timing_is_missing_provenance(tmp_path: Path) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(source_pts_override=-1),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None

    requests = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))

    assert requests[0].status is ProbeRequestStatus.FAILED
    assert requests[0].failure_reason == "missing_provenance"
    started.run_handle.release()


def test_invalid_jpeg_is_a_failed_request_without_publication(tmp_path: Path) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(jpeg_bytes=b"invalid-jpeg"),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None

    requests = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))

    assert requests[0].status is ProbeRequestStatus.FAILED
    assert requests[0].failure_reason == "invalid_artifact"
    assert not list(
        (
            service.repository.run_path(investigation_id, started.manifest.search_run_id) / "frames"
        ).glob("*.json")
    )
    started.run_handle.release()


def test_manifest_publication_failure_removes_owned_staging_and_children(tmp_path: Path) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    repository = _FailingSchema2Repository(tmp_path / "searches", fail_on=3)
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(),
        options=_ServiceOptions(repository=repository),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None

    with pytest.raises(RecordingSearchArtifactError):
        _ = service.acquire_targets(started.run_handle, (_ORIGIN + timedelta(seconds=20),))

    run_path = repository.run_path(investigation_id, started.manifest.search_run_id)
    assert not list(run_path.glob(".phase7a2-*"))
    assert not list((run_path / "frames").glob("*.json"))
    assert not list((run_path / "requests").glob("*.json"))


def test_acquisition_rejects_non_utc_or_out_of_policy_targets(tmp_path: Path) -> None:
    segment = RecordingSegment(
        1,
        _ORIGIN.date(),
        int(_ORIGIN.timestamp()),
        int((_ORIGIN + timedelta(minutes=1)).timestamp()),
        _ORIGIN,
        _ORIGIN + timedelta(minutes=1),
    )
    service, investigation_id = _service(
        tmp_path,
        planner=_Planner(segment),
        extractor=_Extractor(tmp_path),
        decoder=_Decoder(),
    )
    started = service.start(
        RecordingSearchRequest(
            investigation_id=investigation_id,
            search_end_time_text="2026-07-20T13:00:00+09:00",
            source_timezone="Asia/Seoul",
        )
    )
    assert started.run_handle is not None
    naive = datetime(2026, 7, 20, 3, 34, 20, tzinfo=timezone.utc).replace(tzinfo=None)

    with pytest.raises(RecordingSearchBaselineError):
        _ = service.acquire_targets(started.run_handle, (naive,))
    started.run_handle.release()
