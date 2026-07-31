import traceback
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import final

import pytest
from typing_extensions import override

from vigi_vision.channel_selection import Channel
from vigi_vision.recording import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.reference_frame_artifacts import (
    ReferenceFrameArtifactStore,
    ReferenceFrameManifest,
)
from vigi_vision.reference_frame_decoder import ReferenceFrameDecodeRequest
from vigi_vision.reference_frame_direct_support import DirectReferenceFrameRequest
from vigi_vision.reference_frame_models import (
    DecodedFrameEvidence,
    ReferenceFrameArtifactConflictError,
    ReferenceFrameArtifactError,
    ReferenceFrameChannelNotFoundError,
    ReferenceFrameCleanupError,
    ReferenceFrameDecodeError,
    ReferenceFrameOutcome,
    ReferenceFrameRequest,
    ReferenceFrameSegmentMismatchError,
    TimingPrecisionStatus,
    parse_reference_frame_request,
)
from vigi_vision.reference_frame_resources import ReferenceFrameResourceStore
from vigi_vision.reference_frame_service import ReferenceFrameService
from vigi_vision.replay import ReplayClip

_JPEG_BYTES = b"\xff\xd8\xff\xe0reference-frame\xff\xd9"


@final
class FakePlanner:
    segment: RecordingSegment
    mismatch: bool
    find_calls: int
    plan_calls: int

    def __init__(self, segment: RecordingSegment, *, mismatch: bool = False) -> None:
        self.segment = segment
        self.mismatch = mismatch
        self.find_calls = 0
        self.plan_calls = 0

    def find_covering_segment(self, channel_id: int, instant_utc: datetime) -> RecordingSegment:
        self.find_calls += 1
        assert channel_id == self.segment.channel_id
        assert self.segment.start_utc <= instant_utc < self.segment.end_utc
        return self.segment

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        self.plan_calls += 1
        assert segment == self.segment
        planned_window = (
            RecordingWindow(
                window.channel_id,
                window.start_utc,
                window.end_utc - timedelta(seconds=1),
            )
            if self.mismatch
            else window
        )
        return ReplayRequest(planned_window, "rtsp://safe.example/replay")


@final
class FakeReplayExtractor:
    temporary_path: Path
    calls: int

    def __init__(self, temporary_path: Path) -> None:
        self.temporary_path = temporary_path
        self.calls = 0

    def extract(self, request: ReplayRequest) -> ReplayClip:
        self.calls += 1
        _ = self.temporary_path.write_bytes(b"mp4")
        return ReplayClip(
            request.window.channel_id,
            request.window.start_utc,
            request.window.end_utc,
            request.replay_url,
            self.temporary_path,
            request.window.duration_seconds,
        )


@final
class CleanupFailingReplayExtractor:
    temporary_path: Path

    def __init__(self, temporary_path: Path) -> None:
        self.temporary_path = temporary_path

    def extract(self, request: ReplayRequest) -> ReplayClip:
        _ = self.temporary_path.write_bytes(b"mp4")
        return CleanupFailingReplayClip(
            request.window.channel_id,
            request.window.start_utc,
            request.window.end_utc,
            request.replay_url,
            self.temporary_path,
            request.window.duration_seconds,
        )


@final
class CleanupFailingReplayClip(ReplayClip):
    @override
    def remove(self) -> None:
        raise PermissionError


@final
class FakeDecoder:
    fail: bool
    interrupt: bool
    write_output: bool
    requests: list[ReferenceFrameDecodeRequest]

    def __init__(
        self, *, fail: bool = False, interrupt: bool = False, write_output: bool = True
    ) -> None:
        self.fail = fail
        self.interrupt = interrupt
        self.write_output = write_output
        self.requests = []

    def decode(self, request: ReferenceFrameDecodeRequest) -> DecodedFrameEvidence:
        self.requests.append(request)
        if self.interrupt:
            raise KeyboardInterrupt
        if self.fail:
            raise ReferenceFrameDecodeError
        if self.write_output:
            _ = request.output_path.write_bytes(_JPEG_BYTES)
        return DecodedFrameEvidence(
            request.output_path,
            2.0,
            1280,
            720,
            TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
            ("Source timestamp mapping is unavailable pending real-NVR replay validation.",),
        )


@final
class FakeDirectAcquirer:
    requests: list[DirectReferenceFrameRequest]

    def __init__(self) -> None:
        self.requests = []

    def acquire(self, request: DirectReferenceFrameRequest) -> DecodedFrameEvidence:
        self.requests.append(request)
        _ = request.output_path.write_bytes(_JPEG_BYTES)
        return DecodedFrameEvidence(
            request.output_path,
            request.target_offset_seconds,
            1280,
            720,
            TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
            ("Source timestamp mapping is unavailable pending real-NVR replay validation.",),
        )


@final
class FakeInventory:
    entries: tuple[Channel, ...]

    def __init__(self, entries: tuple[Channel, ...]) -> None:
        self.entries = entries

    def channels(self) -> tuple[Channel, ...]:
        return self.entries


def test_reference_frame_service_publishes_credential_free_artifact_and_removes_replay(
    tmp_path: Path,
) -> None:
    # Given
    request = _request()
    segment = _segment(request.requested_time_utc)
    planner = FakePlanner(segment)
    replay_path = tmp_path / "temporary.mp4"
    decoder = FakeDecoder()
    service = ReferenceFrameService(
        planner,
        FakeReplayExtractor(replay_path),
        decoder,
        ReferenceFrameArtifactStore(tmp_path / "artifacts"),
    )

    # When
    result = service.execute(request)

    # Then
    package = tmp_path / "artifacts" / result.resource_id
    manifest = (package / "manifest.json").read_text(encoding="utf-8")
    assert planner.find_calls == 1
    assert planner.plan_calls == 1
    assert decoder.requests[0].target_offset_seconds == 2.0
    assert (package / "frame.jpg").read_bytes() == _JPEG_BYTES
    assert result.timing_precision_status is TimingPrecisionStatus.MEASURED_CLIP_RELATIVE
    assert result.estimated_source_time_utc is None
    assert result.offset_from_requested_seconds is None
    assert "rtsp://" not in manifest
    assert "safe.example" not in manifest
    assert not replay_path.exists()
    assert not tuple((tmp_path / "artifacts").glob(".*"))


def test_reference_frame_service_uses_direct_acquisition_without_creating_a_replay_clip(
    tmp_path: Path,
) -> None:
    request = _request()
    replay = FakeReplayExtractor(tmp_path / "temporary.mp4")
    decoder = FakeDecoder()
    direct_acquirer = FakeDirectAcquirer()
    service = ReferenceFrameService(
        FakePlanner(_segment(request.requested_time_utc)),
        replay,
        decoder,
        ReferenceFrameArtifactStore(tmp_path / "artifacts"),
        direct_acquirer=direct_acquirer,
    )

    result = service.execute(request)

    assert result.decoded_local_pts_seconds == 2.0
    assert replay.calls == 0
    assert decoder.requests == []
    assert direct_acquirer.requests[0].target_offset_seconds == 2.0
    assert (tmp_path / "artifacts" / result.resource_id / "frame.jpg").read_bytes() == _JPEG_BYTES


def test_reference_frame_service_rejects_mismatched_plan_before_replay_or_artifact(
    tmp_path: Path,
) -> None:
    # Given
    request = _request()
    replay_path = tmp_path / "temporary.mp4"
    replay_extractor = FakeReplayExtractor(replay_path)
    service = ReferenceFrameService(
        FakePlanner(_segment(request.requested_time_utc), mismatch=True),
        replay_extractor,
        FakeDecoder(),
        ReferenceFrameArtifactStore(tmp_path / "artifacts"),
    )

    # When / Then
    with pytest.raises(ReferenceFrameSegmentMismatchError):
        _ = service.execute(request)

    assert replay_extractor.calls == 0
    assert not (tmp_path / "artifacts").exists()
    assert not replay_path.exists()


def test_reference_frame_service_rejects_proven_missing_channel_before_recording_work(
    tmp_path: Path,
) -> None:
    # Given
    request = _request()
    planner = FakePlanner(_segment(request.requested_time_utc))
    replay_extractor = FakeReplayExtractor(tmp_path / "temporary.mp4")
    service = ReferenceFrameService(
        planner,
        replay_extractor,
        FakeDecoder(),
        ReferenceFrameArtifactStore(tmp_path / "artifacts"),
        FakeInventory(()),
    )

    # When / Then
    with pytest.raises(ReferenceFrameChannelNotFoundError):
        _ = service.execute(request)

    assert planner.find_calls == 0
    assert replay_extractor.calls == 0


@pytest.mark.parametrize("failure", ["decode", "interrupt"])
def test_reference_frame_service_discards_staging_and_replay_on_failure(
    tmp_path: Path, failure: str
) -> None:
    # Given
    request = _request()
    replay_path = tmp_path / "temporary.mp4"
    decoder = FakeDecoder(fail=failure == "decode", interrupt=failure == "interrupt")
    service = ReferenceFrameService(
        FakePlanner(_segment(request.requested_time_utc)),
        FakeReplayExtractor(replay_path),
        decoder,
        ReferenceFrameArtifactStore(tmp_path / "artifacts"),
    )

    # When / Then
    if failure == "decode":
        with pytest.raises(ReferenceFrameDecodeError):
            _ = service.execute(request)
    else:
        with pytest.raises(KeyboardInterrupt):
            _ = service.execute(request)

    assert not replay_path.exists()
    assert not tuple((tmp_path / "artifacts").glob("*"))


def test_reference_frame_service_rejects_missing_decoder_output(tmp_path: Path) -> None:
    # Given
    request = _request()
    replay_path = tmp_path / "temporary.mp4"
    service = ReferenceFrameService(
        FakePlanner(_segment(request.requested_time_utc)),
        FakeReplayExtractor(replay_path),
        FakeDecoder(write_output=False),
        ReferenceFrameArtifactStore(tmp_path / "artifacts"),
    )

    # When / Then
    with pytest.raises(ReferenceFrameArtifactError):
        _ = service.execute(request)

    assert not replay_path.exists()
    assert not tuple((tmp_path / "artifacts").glob("*"))


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ("cleanup", ReferenceFrameCleanupError),
        ("decode", ReferenceFrameDecodeError),
    ],
)
def test_reference_frame_service_cleanup_error_does_not_publish_or_mask_primary_failure(
    tmp_path: Path,
    failure: str,
    expected_error: type[ReferenceFrameCleanupError] | type[ReferenceFrameDecodeError],
) -> None:
    # Given
    request = _request()
    replay_path = tmp_path / "temporary.mp4"
    service = ReferenceFrameService(
        FakePlanner(_segment(request.requested_time_utc)),
        CleanupFailingReplayExtractor(replay_path),
        FakeDecoder(fail=failure == "decode"),
        ReferenceFrameArtifactStore(tmp_path / "artifacts"),
    )

    # When / Then
    with pytest.raises(expected_error):
        _ = service.execute(request)

    assert replay_path.is_file()
    assert not tuple((tmp_path / "artifacts").glob("*"))


def test_reference_frame_service_does_not_overwrite_completed_artifact(tmp_path: Path) -> None:
    # Given
    request = _request()
    replay_path = tmp_path / "temporary.mp4"
    service = ReferenceFrameService(
        FakePlanner(_segment(request.requested_time_utc)),
        FakeReplayExtractor(replay_path),
        FakeDecoder(),
        ReferenceFrameArtifactStore(tmp_path / "artifacts"),
    )
    _ = service.execute(request)

    # When / Then
    with pytest.raises(ReferenceFrameArtifactConflictError):
        _ = service.execute(request)

    assert not replay_path.exists()


def test_reference_frame_service_reuses_compatible_completed_resource(tmp_path: Path) -> None:
    # Given
    request = _request()
    output_root = tmp_path / "artifacts"
    replay_extractor = FakeReplayExtractor(tmp_path / "temporary.mp4")
    decoder = FakeDecoder()
    service = ReferenceFrameService(
        FakePlanner(_segment(request.requested_time_utc)),
        replay_extractor,
        decoder,
        ReferenceFrameArtifactStore(output_root),
        completed_resources=ReferenceFrameResourceStore(output_root),
    )

    # When
    created = service.execute_or_resolve(request)
    reused = service.execute_or_resolve(request)

    # Then
    assert created.outcome is ReferenceFrameOutcome.CREATED
    assert reused.outcome is ReferenceFrameOutcome.REUSED
    assert reused.result.resource_id == created.result.resource_id
    assert replay_extractor.calls == 1
    assert len(decoder.requests) == 1


def test_reference_frame_service_preserves_corrupt_completed_resource(tmp_path: Path) -> None:
    # Given
    request = _request()
    output_root = tmp_path / "artifacts"
    replay_extractor = FakeReplayExtractor(tmp_path / "temporary.mp4")
    service = ReferenceFrameService(
        FakePlanner(_segment(request.requested_time_utc)),
        replay_extractor,
        FakeDecoder(),
        ReferenceFrameArtifactStore(output_root),
        completed_resources=ReferenceFrameResourceStore(output_root),
    )
    created = service.execute_or_resolve(request)
    jpeg_path = output_root / created.result.resource_id / "frame.jpg"
    _ = jpeg_path.write_bytes(b"not-a-jpeg")

    # When / Then
    with pytest.raises(ReferenceFrameArtifactConflictError):
        _ = service.execute_or_resolve(request)

    assert jpeg_path.read_bytes() == b"not-a-jpeg"
    assert replay_extractor.calls == 1


@pytest.mark.parametrize("target_contents", ["empty", "nonempty"])
def test_reference_frame_artifact_promotion_refuses_late_existing_target(
    tmp_path: Path, target_contents: str
) -> None:
    # Given
    request = _request()
    segment = _segment(request.requested_time_utc)
    store = ReferenceFrameArtifactStore(tmp_path / "artifacts")
    session = store.begin(request, segment)
    evidence = DecodedFrameEvidence(
        session.jpeg_path,
        2.0,
        1280,
        720,
        TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
        (),
    )
    _ = session.jpeg_path.write_bytes(_JPEG_BYTES)
    session.final_directory.mkdir()
    existing_path = session.final_directory / "existing.txt"
    if target_contents == "nonempty":
        _ = existing_path.write_text("keep", encoding="utf-8")
    manifest = ReferenceFrameManifest(
        request,
        segment,
        RecordingWindow(
            1, request.requested_time_utc, request.requested_time_utc + timedelta(seconds=1)
        ),
        session.resource_id,
        evidence,
        None,
        None,
    )

    # When / Then
    with pytest.raises(ReferenceFrameArtifactConflictError):
        _ = session.finalize(manifest)

    if target_contents == "nonempty":
        assert existing_path.read_text(encoding="utf-8") == "keep"
    else:
        assert not tuple(session.final_directory.iterdir())
    assert not session.staging_directory.exists()


def test_reference_frame_artifact_store_reserves_identical_inflight_resource(
    tmp_path: Path,
) -> None:
    # Given
    request = _request()
    segment = _segment(request.requested_time_utc)
    store = ReferenceFrameArtifactStore(tmp_path / "artifacts")
    session = store.begin(request, segment)

    # When / Then
    with pytest.raises(ReferenceFrameArtifactConflictError):
        _ = store.begin(request, segment)

    session.discard()
    replacement = store.begin(request, segment)
    replacement.discard()


def test_reference_frame_artifact_store_releases_claim_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    request = _request()
    segment = _segment(request.requested_time_utc)
    store = ReferenceFrameArtifactStore(tmp_path / "artifacts")
    original_exists = Path.exists

    def interrupted_exists(path: Path) -> bool:
        if path.name.startswith("channel-"):
            raise KeyboardInterrupt
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", interrupted_exists)

    # When / Then
    with pytest.raises(KeyboardInterrupt):
        _ = store.begin(request, segment)

    assert not tuple((tmp_path / "artifacts").glob("*"))


def test_reference_frame_artifact_identity_separates_generation_policy_versions(
    tmp_path: Path,
) -> None:
    # Given
    request = _request()
    changed_request = replace(
        request, generation_policy_version=request.generation_policy_version + 1
    )
    segment = _segment(request.requested_time_utc)
    store = ReferenceFrameArtifactStore(tmp_path / "artifacts")

    # When
    first = store.begin(request, segment)
    first_resource_id = first.resource_id
    first.discard()
    second = store.begin(changed_request, segment)

    # Then
    assert second.resource_id != first_resource_id
    assert second.final_directory.parent == store.output_root
    second.discard()


def test_reference_frame_artifact_store_conflicts_with_incompatible_existing_schema(
    tmp_path: Path,
) -> None:
    # Given
    request = _request()
    segment = _segment(request.requested_time_utc)
    store = ReferenceFrameArtifactStore(tmp_path / "artifacts")
    session = store.begin(request, segment)
    final_directory = session.final_directory
    session.discard()
    final_directory.mkdir()
    _ = (final_directory / "manifest.json").write_text('{"schema_version":999}\n', encoding="utf-8")

    # When / Then
    with pytest.raises(ReferenceFrameArtifactConflictError):
        _ = store.begin(request, segment)


def test_reference_frame_artifact_preflight_redacts_filesystem_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    request = _request()
    replay_path = tmp_path / "private-replay.mp4"
    output_root = tmp_path / "private-artifacts"
    service = ReferenceFrameService(
        FakePlanner(_segment(request.requested_time_utc)),
        FakeReplayExtractor(replay_path),
        FakeDecoder(),
        ReferenceFrameArtifactStore(output_root),
    )
    original_exists = Path.exists
    final_checks = 0
    filesystem_marker = "opaque-filesystem-marker"

    def failing_final_preflight(path: Path) -> bool:
        nonlocal final_checks
        if path.name.startswith("channel-"):
            final_checks += 1
            if final_checks == 2:
                raise PermissionError(filesystem_marker)
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", failing_final_preflight)

    # When
    with pytest.raises(ReferenceFrameArtifactError) as exception_info:
        _ = service.execute(request)

    # Then
    rendered = "".join(
        traceback.format_exception(
            type(exception_info.value),
            exception_info.value,
            exception_info.value.__traceback__,
        )
    )
    assert filesystem_marker not in rendered
    assert not replay_path.exists()
    assert not tuple(output_root.glob("*"))


def test_reference_frame_internal_representations_redact_paths_urls_and_dependencies(
    tmp_path: Path,
) -> None:
    # Given
    request = _request()
    segment = _segment(request.requested_time_utc)
    store = ReferenceFrameArtifactStore(tmp_path / "private-artifacts")
    session = store.begin(request, segment)
    replay_path = tmp_path / "private-replay.mp4"
    service = ReferenceFrameService(
        FakePlanner(segment),
        FakeReplayExtractor(replay_path),
        FakeDecoder(),
        store,
    )
    replay = ReplayClip(
        1,
        request.requested_time_utc,
        request.requested_time_utc + timedelta(seconds=1),
        "rtsp://private.example/replay",
        replay_path,
        1,
    )
    decode_request = ReferenceFrameDecodeRequest(
        replay_path,
        0.0,
        request.frame_selection_policy,
        session.jpeg_path,
    )

    # When
    representations = " ".join(
        (repr(store), repr(session), repr(service), repr(replay), repr(decode_request))
    )

    # Then
    assert "private" not in representations
    assert "rtsp://" not in representations
    session.discard()


def _request() -> ReferenceFrameRequest:
    return parse_reference_frame_request(
        channel_id=1,
        requested_time_text="2026-07-20 12:34:18",
        source_timezone="Asia/Seoul",
        now_utc=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )


def _segment(target: datetime) -> RecordingSegment:
    return RecordingSegment(
        channel_id=1,
        recording_day=target.date(),
        start_epoch_seconds=int((target - timedelta(seconds=10)).timestamp()),
        end_epoch_seconds=int((target + timedelta(seconds=10)).timestamp()),
        start_utc=target - timedelta(seconds=10),
        end_utc=target + timedelta(seconds=10),
    )
