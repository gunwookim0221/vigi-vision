from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from typing import final

import pytest

from vigi_vision.investigation import AnchorTime, CameraRole, InvestigationItem, InvestigationPlan
from vigi_vision.investigation_artifacts import InvestigationArtifactBuilder
from vigi_vision.investigation_collection import (
    CollectionItemResult,
    CollectionResult,
    CollectionStatus,
)
from vigi_vision.investigation_snapshot import AnchorSnapshotError, FfmpegAnchorSnapshotExtractor
from vigi_vision.recording import RecordingWindow
from vigi_vision.replay import ReplayClip


@final
class StubAnchorSnapshotExtractor:
    calls: list[tuple[Path, int, Path]]

    def __init__(self) -> None:
        self.calls = []

    def extract(self, video_path: Path, anchor_offset_seconds: int, output_path: Path) -> Path:
        self.calls.append((video_path, anchor_offset_seconds, output_path))
        _ = output_path.write_bytes(b"jpeg")
        return output_path


def _item(channel_id: int, role: str, start_offset_seconds: int) -> InvestigationItem:
    anchor_utc = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
    return InvestigationItem(
        item_id=f"restaurant-checkout-channel-{channel_id}-{role}",
        channel_id=channel_id,
        role=CameraRole(role),
        profile_id=role,
        recording_window=RecordingWindow(
            channel_id,
            anchor_utc + timedelta(seconds=start_offset_seconds),
            anchor_utc + timedelta(seconds=start_offset_seconds + 120),
        ),
    )


def _plan(*items: InvestigationItem) -> InvestigationPlan:
    return InvestigationPlan(
        "restaurant-checkout",
        AnchorTime(datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc), "Asia/Seoul"),
        items,
    )


def _clip(item: InvestigationItem, temporary_path: Path) -> ReplayClip:
    window = item.recording_window
    _ = temporary_path.write_bytes(b"mp4")
    return ReplayClip(
        channel_id=item.channel_id,
        requested_start_utc=window.start_utc,
        requested_end_utc=window.end_utc,
        replay_url=f"rtsp://operator:secret@nvr.example.test/replay/{item.channel_id}",
        temporary_mp4_path=temporary_path,
        duration_seconds=window.duration_seconds,
    )


def _collection_result(
    plan: InvestigationPlan, outcomes: Mapping[int, ReplayClip | CollectionItemResult]
) -> CollectionResult:
    results = tuple(
        outcome
        if isinstance(outcome := outcomes[item.channel_id], CollectionItemResult)
        else CollectionItemResult.success(item, outcome)
        for item in plan.items
    )
    return CollectionResult(plan, results)


def test_builder_preserves_replays_generates_anchor_snapshots_and_writes_safe_manifest(
    tmp_path: Path,
) -> None:
    # Given
    item = _item(1, "counter", -60)
    plan = _plan(item)
    clip = _clip(item, tmp_path / "temporary-counter.mp4")
    snapshots = StubAnchorSnapshotExtractor()
    collection = _collection_result(plan, {1: clip})

    # When
    result = InvestigationArtifactBuilder(tmp_path / "artifacts", snapshots).build(collection)

    # Then
    assert result.artifact_directory == (
        tmp_path / "artifacts" / "restaurant-checkout-20260720T030000Z"
    )
    assert (result.artifact_directory / "counter-channel-1.mp4").read_bytes() == b"mp4"
    assert (result.artifact_directory / "counter-channel-1-anchor.jpg").read_bytes() == b"jpeg"
    assert not clip.temporary_mp4_path.exists()
    assert snapshots.calls == [
        (
            result.artifact_directory / "counter-channel-1.mp4",
            60,
            result.artifact_directory / "counter-channel-1-anchor.jpg",
        )
    ]
    assert result.manifest.investigation_id == "restaurant-checkout-20260720T030000Z"
    assert result.manifest.scenario_id == "restaurant-checkout"
    assert result.manifest.anchor_time_utc == datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
    assert result.manifest.source_timezone == "Asia/Seoul"
    assert result.manifest.items[0].item_id == item.item_id
    assert result.manifest.items[0].recording_window.start_utc == datetime(
        2026, 7, 20, 2, 59, tzinfo=timezone.utc
    )
    assert result.manifest.items[0].video_filename == "counter-channel-1.mp4"
    assert result.manifest.items[0].anchor_snapshot_filename == "counter-channel-1-anchor.jpg"
    manifest_text = (result.artifact_directory / "manifest.json").read_text()
    assert "secret" not in manifest_text
    assert "rtsp://" not in manifest_text
    assert "nvr.example.test" not in manifest_text


def test_builder_omits_failed_collection_items_from_artifacts_and_preserves_order(
    tmp_path: Path,
) -> None:
    # Given
    failed_item = _item(1, "counter", -60)
    successful_item = _item(2, "entrance", -30)
    plan = _plan(failed_item, successful_item)
    failed = CollectionItemResult.failure(
        failed_item,
        CollectionStatus.EXTRACTION_FAILED,
        "ffmpeg failed for rtsp://operator:secret@nvr.example.test/replay/1",
    )
    clip = _clip(successful_item, tmp_path / "temporary-entrance.mp4")
    collection = _collection_result(plan, {1: failed, 2: clip})

    # When
    result = InvestigationArtifactBuilder(
        tmp_path / "artifacts", StubAnchorSnapshotExtractor()
    ).build(collection)

    # Then
    assert tuple(item.item_id for item in result.manifest.items) == (
        failed_item.item_id,
        successful_item.item_id,
    )
    assert result.manifest.items[0].video_filename is None
    assert result.manifest.items[0].anchor_snapshot_filename is None
    assert result.manifest.items[0].failure_reason == "Replay extraction failed."
    assert not (result.artifact_directory / "counter-channel-1.mp4").exists()
    assert (result.artifact_directory / "entrance-channel-2.mp4").is_file()
    assert not clip.temporary_mp4_path.exists()


def test_builder_uses_a_deterministic_safe_directory_and_filenames(tmp_path: Path) -> None:
    # Given
    item = _item(7, "dining", -10)
    plan = _plan(item)
    first_clip = _clip(item, tmp_path / "first.mp4")
    second_clip = _clip(item, tmp_path / "second.mp4")

    # When
    first_result = InvestigationArtifactBuilder(
        tmp_path / "first-artifacts", StubAnchorSnapshotExtractor()
    ).build(_collection_result(plan, {7: first_clip}))
    second_result = InvestigationArtifactBuilder(
        tmp_path / "second-artifacts", StubAnchorSnapshotExtractor()
    ).build(_collection_result(plan, {7: second_clip}))

    # Then
    assert first_result.artifact_directory.name == second_result.artifact_directory.name
    assert tuple(item.video_filename for item in first_result.manifest.items) == (
        "dining-channel-7.mp4",
    )
    assert tuple(item.anchor_snapshot_filename for item in second_result.manifest.items) == (
        "dining-channel-7-anchor.jpg",
    )


def test_ffmpeg_anchor_snapshot_extractor_reads_the_preserved_local_mp4(tmp_path: Path) -> None:
    # Given
    video_path = tmp_path / "counter-channel-1.mp4"
    output_path = tmp_path / "counter-channel-1-anchor.jpg"
    _ = video_path.write_bytes(b"mp4")
    calls: list[tuple[str, ...]] = []

    def successful_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        calls.append(arguments)
        _ = Path(arguments[-1]).write_bytes(b"jpeg")
        return CompletedProcess(arguments, 0)

    # When
    result = FfmpegAnchorSnapshotExtractor(Path("ffmpeg"), successful_runner).extract(
        video_path, 60, output_path
    )

    # Then
    assert result == output_path
    assert calls == [
        (
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "60.000",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-q:v",
            "5",
            "-an",
            str(output_path),
        )
    ]


def test_ffmpeg_anchor_snapshot_extractor_removes_a_partial_jpeg_on_failure(tmp_path: Path) -> None:
    # Given
    video_path = tmp_path / "counter-channel-1.mp4"
    output_path = tmp_path / "counter-channel-1-anchor.jpg"
    _ = video_path.write_bytes(b"mp4")

    def failing_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        _ = Path(arguments[-1]).write_bytes(b"partial jpeg")
        return CompletedProcess(arguments, 1)

    # When / Then
    with pytest.raises(AnchorSnapshotError):
        _ = FfmpegAnchorSnapshotExtractor(Path("ffmpeg"), failing_runner).extract(
            video_path, 60, output_path
        )
    assert not output_path.exists()
