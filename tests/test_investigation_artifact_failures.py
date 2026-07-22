import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import final

import pytest

from vigi_vision.investigation import AnchorTime, CameraRole, InvestigationItem, InvestigationPlan
from vigi_vision.investigation_artifacts import InvestigationArtifactBuilder
from vigi_vision.investigation_collection import CollectionItemResult, CollectionResult
from vigi_vision.recording import RecordingWindow
from vigi_vision.replay import ReplayClip


@final
class StubAnchorSnapshotExtractor:
    def extract(self, video_path: Path, anchor_offset_seconds: int, output_path: Path) -> Path:
        _ = (video_path, anchor_offset_seconds)
        _ = output_path.write_bytes(b"jpeg")
        return output_path


def _item() -> InvestigationItem:
    anchor_utc = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
    return InvestigationItem(
        "restaurant-checkout-channel-1-counter",
        1,
        CameraRole("counter"),
        "counter",
        RecordingWindow(
            1,
            anchor_utc - timedelta(seconds=60),
            anchor_utc + timedelta(seconds=60),
        ),
    )


def _collection(item: InvestigationItem, source_path: Path) -> CollectionResult:
    window = item.recording_window
    _ = source_path.write_bytes(b"mp4")
    clip = ReplayClip(
        item.channel_id,
        window.start_utc,
        window.end_utc,
        "rtsp://operator:secret@nvr.example.test/replay",
        source_path,
        window.duration_seconds,
    )
    plan = InvestigationPlan(
        "restaurant-checkout",
        AnchorTime(datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc), "Asia/Seoul"),
        (item,),
    )
    return CollectionResult(plan, (CollectionItemResult.success(item, clip),))


def test_builder_transfers_replay_mp4_across_windows_volumes() -> None:
    # Given
    source_directory = Path(tempfile.gettempdir())
    artifact_root = Path(tempfile.mkdtemp(prefix="vigi-artifacts-", dir=Path.cwd()))
    if source_directory.drive == artifact_root.drive:
        shutil.rmtree(artifact_root, ignore_errors=True)
        pytest.skip("cross-volume transfer is not available on this test host")
    descriptor, source_name = tempfile.mkstemp(prefix="vigi-vision-test-", suffix=".mp4")
    os.close(descriptor)
    source_path = Path(source_name)

    # When
    try:
        result = InvestigationArtifactBuilder(artifact_root, StubAnchorSnapshotExtractor()).build(
            _collection(_item(), source_path)
        )
        video_bytes = (result.artifact_directory / "counter-channel-1.mp4").read_bytes()
    finally:
        source_path.unlink(missing_ok=True)
        shutil.rmtree(artifact_root, ignore_errors=True)

    # Then
    assert video_bytes == b"mp4"
    assert not source_path.exists()


def test_builder_keeps_preexisting_directory_on_conflict_and_removes_replay_temp(
    tmp_path: Path,
) -> None:
    # Given
    artifact_root = tmp_path / "artifacts"
    artifact_directory = artifact_root / "restaurant-checkout-20260720T030000Z"
    artifact_directory.mkdir(parents=True)
    source_path = tmp_path / "temporary-counter.mp4"
    collection = _collection(_item(), source_path)

    # When / Then
    with pytest.raises(FileExistsError):
        _ = InvestigationArtifactBuilder(artifact_root, StubAnchorSnapshotExtractor()).build(
            collection
        )
    assert artifact_directory.is_dir()
    assert not source_path.exists()
