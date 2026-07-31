from datetime import datetime, timezone
from pathlib import Path
from typing import final

from vigi import (
    RecordDay,
    RecordDaysResponse,
    RecordSearchProcessResponse,
    RecordSearchResultsResponse,
    VigiError,
)
from vigi import (
    RecordSegment as SdkRecordSegment,
)

from vigi_vision.recording import RecordingPlanner, ReplayRequest
from vigi_vision.reference_frame_artifacts import ReferenceFrameArtifactStore
from vigi_vision.reference_frame_candidate_models import (
    DEFAULT_CANDIDATE_OFFSETS,
    ReferenceFrameCandidateSetRequest,
)
from vigi_vision.reference_frame_candidate_service import (
    ReferenceFrameCandidateFailure,
    ReferenceFrameCandidateSetService,
    ReferenceFrameCandidateSuccess,
)
from vigi_vision.reference_frame_decoder import ReferenceFrameDecodeRequest
from vigi_vision.reference_frame_models import (
    DecodedFrameEvidence,
    ReferenceFrameOutcome,
    TimingPrecisionStatus,
    parse_reference_frame_request,
)
from vigi_vision.reference_frame_resources import ReferenceFrameResourceStore
from vigi_vision.reference_frame_service import ReferenceFrameService
from vigi_vision.replay import ReplayClip

_JPEG_BYTES = b"\xff\xd8\xff\xe0candidate\xff\xd9"
_CAPACITY_MESSAGE = "private process capacity"
_TRANSIENT_FAILURE_MESSAGE = "private transient result failure"


@final
class LimitedSearchRecords:
    free_process_limit: int
    failed_result_call: int | None
    free_process_calls: int
    results_calls: int
    observed_process_ids: list[int]

    def __init__(self, free_process_limit: int, failed_result_call: int | None = None) -> None:
        self.free_process_limit = free_process_limit
        self.failed_result_call = failed_result_call
        self.free_process_calls = 0
        self.results_calls = 0
        self.observed_process_ids = []

    def list_days(self, channel_id: int, start_month: str, end_month: str) -> RecordDaysResponse:
        assert channel_id == 1
        assert start_month == end_month == "202607"
        return RecordDaysResponse(days=(RecordDay(day="20260720"),), error_code=0)

    def get_free_process(self) -> RecordSearchProcessResponse:
        self.free_process_calls += 1
        if self.free_process_calls > self.free_process_limit:
            raise VigiError(_CAPACITY_MESSAGE)
        return RecordSearchProcessResponse(process_id=7, error_code=0)

    def list_results(
        self,
        channel_id: int,
        process_id: int,
        day: str,
        start_index: int = 0,
        end_index: int = 99,
    ) -> RecordSearchResultsResponse:
        self.results_calls += 1
        self.observed_process_ids.append(process_id)
        assert channel_id == 1
        assert day == "20260720"
        assert (start_index, end_index) == (0, 99)
        if self.results_calls == self.failed_result_call:
            raise VigiError(_TRANSIENT_FAILURE_MESSAGE)
        return RecordSearchResultsResponse(
            results=(SdkRecordSegment(start_time="1784518200", end_time="1784519400"),),
            error_code=0,
        )


@final
class FakeStream:
    def build_replay_url(
        self, host: str, channel_id: int, start_time: str, end_time: str, stream: int = 1
    ) -> str:
        return f"rtsp://{host}/replay/{channel_id}/{stream}?start={start_time}&end={end_time}"


@final
class FakeRecordingClient:
    records: LimitedSearchRecords
    stream: FakeStream

    def __init__(self, records: LimitedSearchRecords) -> None:
        self.records = records
        self.stream = FakeStream()


@final
class TrackingReplayExtractor:
    temporary_directory: Path
    paths: list[Path]

    def __init__(self, temporary_directory: Path) -> None:
        self.temporary_directory = temporary_directory
        self.paths = []

    def extract(self, request: ReplayRequest) -> ReplayClip:
        path = self.temporary_directory / f"replay-{len(self.paths)}.mp4"
        _ = path.write_bytes(b"mp4")
        self.paths.append(path)
        return ReplayClip(
            request.window.channel_id,
            request.window.start_utc,
            request.window.end_utc,
            request.replay_url,
            path,
            request.window.duration_seconds,
        )


@final
class FakeDecoder:
    def decode(self, request: ReferenceFrameDecodeRequest) -> DecodedFrameEvidence:
        _ = request.output_path.write_bytes(_JPEG_BYTES)
        return DecodedFrameEvidence(
            request.output_path,
            2.0,
            1280,
            720,
            TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
            (),
        )


def test_candidate_batch_reuses_one_search_process_for_new_and_reused_children(
    tmp_path: Path,
) -> None:
    records = LimitedSearchRecords(free_process_limit=2)
    service, extractor = _service(tmp_path, records)
    request = _candidate_request()

    first = service.execute(request)
    repeated = service.execute(request)

    assert all(isinstance(item, ReferenceFrameCandidateSuccess) for item in first.items)
    assert (
        tuple(
            item.resolution.outcome
            for item in first.items
            if isinstance(item, ReferenceFrameCandidateSuccess)
        )
        == (ReferenceFrameOutcome.CREATED,) * 5
    )
    assert all(isinstance(item, ReferenceFrameCandidateSuccess) for item in repeated.items)
    assert (
        tuple(
            item.resolution.outcome
            for item in repeated.items
            if isinstance(item, ReferenceFrameCandidateSuccess)
        )
        == (ReferenceFrameOutcome.REUSED,) * 5
    )
    assert records.free_process_calls == 1
    assert records.observed_process_ids == [7] * 10
    assert len(extractor.paths) == 5
    assert not any(path.exists() for path in extractor.paths)


def test_failed_candidate_does_not_allocate_or_poison_the_search_process(tmp_path: Path) -> None:
    records = LimitedSearchRecords(free_process_limit=1, failed_result_call=3)
    service, extractor = _service(tmp_path, records)

    result = service.execute(_candidate_request())

    assert isinstance(result.items[0], ReferenceFrameCandidateSuccess)
    assert isinstance(result.items[1], ReferenceFrameCandidateSuccess)
    assert isinstance(result.items[2], ReferenceFrameCandidateFailure)
    assert result.items[2].code == "nvr_unavailable"
    assert isinstance(result.items[3], ReferenceFrameCandidateSuccess)
    assert isinstance(result.items[4], ReferenceFrameCandidateSuccess)
    assert records.free_process_calls == 1
    assert records.observed_process_ids == [7] * 5
    assert len(extractor.paths) == 4
    assert not any(path.exists() for path in extractor.paths)


def _service(
    tmp_path: Path, records: LimitedSearchRecords
) -> tuple[ReferenceFrameCandidateSetService, TrackingReplayExtractor]:
    extractor = TrackingReplayExtractor(tmp_path)
    output_root = tmp_path / "artifacts"
    reference_service = ReferenceFrameService(
        RecordingPlanner(FakeRecordingClient(records), "nvr.example.test"),
        extractor,
        FakeDecoder(),
        ReferenceFrameArtifactStore(output_root),
        completed_resources=ReferenceFrameResourceStore(output_root),
    )
    return ReferenceFrameCandidateSetService(reference_service), extractor


def _candidate_request() -> ReferenceFrameCandidateSetRequest:
    reference_time = parse_reference_frame_request(
        channel_id=1,
        requested_time_text="2026-07-20 12:34:18",
        source_timezone="Asia/Seoul",
        now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    return ReferenceFrameCandidateSetRequest(
        reference_time,
        DEFAULT_CANDIDATE_OFFSETS,
        datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
