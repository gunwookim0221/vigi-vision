from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from typing import cast, final

import pytest
from pydantic import SecretStr

import vigi_vision.reference_frame_direct as direct
from vigi_vision.recording import RecordingWindow, ReplayRequest
from vigi_vision.reference_frame_decoder import ReferenceFrameDecodeTimeoutError
from vigi_vision.reference_frame_direct import FfmpegDirectReferenceFrameAcquirer
from vigi_vision.reference_frame_direct_support import (
    DirectProcess,
    DirectReferenceFrameRequest,
    FrameTiming,
    select_adjacent,
)
from vigi_vision.reference_frame_models import (
    FrameSelectionPolicy,
    ReferenceFrameDecodeError,
    ReferenceFrameNoCandidateError,
)


@final
class FakeProcess:
    stdout: StringIO
    stderr: StringIO
    terminate_calls: int
    kill_calls: int

    def __init__(self, timing: str, *, running: bool) -> None:
        self.stdout = StringIO(timing)
        self.stderr = StringIO("")
        self.returncode: int | None = None if running else 0
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        if self.returncode is None:
            command = "ffmpeg"
            timeout_value: float = timeout if timeout is not None else 0.0
            raise TimeoutExpired(command, timeout_value)
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


def _request(tmp_path: Path) -> DirectReferenceFrameRequest:
    return DirectReferenceFrameRequest(
        replay_request=ReplayRequest(
            RecordingWindow(
                1,
                datetime(2026, 7, 20, 3, 34, 16, tzinfo=timezone.utc),
                datetime(2026, 7, 20, 3, 34, 22, tzinfo=timezone.utc),
            ),
            "rtsp://nvr.example.test/replay",
        ),
        target_offset_seconds=2.0,
        policy=FrameSelectionPolicy.NEAREST_DECODED_FRAME,
        output_path=tmp_path / "frame.jpg",
    )


def _acquirer(
    process: FakeProcess, *, valid_jpeg: bool = True
) -> FfmpegDirectReferenceFrameAcquirer:
    def factory(_args: tuple[str, ...], _directory: Path) -> DirectProcess:
        for ordinal in range(4):
            _ = (_directory / f"candidate-{ordinal:08d}.jpg").write_bytes(b"jpeg")
        return cast("DirectProcess", cast("object", process))

    def probe(_args: tuple[str, ...], _timeout: float) -> CompletedProcess[str]:
        document = '{"streams":[{"codec_name":"mjpeg","width":640,"height":480}]}'
        return CompletedProcess(("ffprobe",), 0 if valid_jpeg else 1, stdout=document)

    return FfmpegDirectReferenceFrameAcquirer(
        ffmpeg=Path("ffmpeg"),
        ffprobe=Path("ffprobe"),
        username="operator",
        password=SecretStr("password"),
        process_factory=factory,
        probe_runner=probe,
    )


def test_direct_acquirer_releases_nearest_frame_before_later_stream_stall(tmp_path: Path) -> None:
    # Given
    process = FakeProcess(
        "#tb 0: 1/10\n0, 19, 19, 1, 1, hash\n0, 21, 21, 1, 1, hash\n", running=True
    )

    # When
    evidence = _acquirer(process).acquire(_request(tmp_path))

    # Then
    assert evidence.local_pts_seconds == 1.9
    assert evidence.width == 640
    assert evidence.height == 480
    assert process.terminate_calls == 1
    assert process.stdout.closed
    assert process.stderr.closed
    assert (tmp_path / "frame.jpg").read_bytes() == b"jpeg"
    assert list(tmp_path.glob("vigi-reference-direct-*")) == []


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        (None, FrameTiming(0, Decimal("2.2")), Decimal("2.2")),
        (FrameTiming(0, Decimal("1.8")), FrameTiming(1, Decimal("2.2")), Decimal("1.8")),
        (FrameTiming(0, Decimal("1.9")), FrameTiming(1, Decimal("2.1")), Decimal("1.9")),
    ],
)
def test_adjacent_selection_uses_nearest_frame_and_earlier_tie(
    previous: FrameTiming | None, current: FrameTiming, expected: Decimal
) -> None:
    selected = select_adjacent(previous, current, 2.0, FrameSelectionPolicy.NEAREST_DECODED_FRAME)

    assert selected is not None
    assert selected.local_pts_seconds == expected


def test_direct_acquirer_uses_only_before_frame_at_natural_end(tmp_path: Path) -> None:
    process = FakeProcess(
        "#tb 0: 1/10\n0, 15, 15, 1, 1, hash\n0, 18, 18, 1, 1, hash\n", running=False
    )

    evidence = _acquirer(process).acquire(_request(tmp_path))

    assert evidence.local_pts_seconds == 1.8
    assert "Only decoded frames before" in evidence.warnings[1]


@pytest.mark.parametrize(
    ("timing", "error"),
    [
        ("", ReferenceFrameNoCandidateError),
        ("#tb 0: 1/10\ninvalid\n", ReferenceFrameDecodeError),
        (
            "#tb 0: 1/10\n0, 18, 18, 1, 1, hash\n0, 15, 15, 1, 1, hash\n",
            ReferenceFrameDecodeError,
        ),
    ],
)
def test_direct_acquirer_rejects_missing_or_unusable_timing(
    tmp_path: Path, timing: str, error: type[Exception]
) -> None:
    process = FakeProcess(timing, running=False)

    with pytest.raises(error):
        _ = _acquirer(process).acquire(_request(tmp_path))

    assert not (tmp_path / "frame.jpg").exists()
    assert process.stdout.closed
    assert process.stderr.closed


def test_direct_acquirer_times_out_before_target_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcess("", running=True)
    monkeypatch.setattr(direct, "_STARTUP_ALLOWANCE_SECONDS", -6.0)

    with pytest.raises(ReferenceFrameDecodeTimeoutError):
        _ = _acquirer(process).acquire(_request(tmp_path))

    assert process.terminate_calls == 1
    assert not (tmp_path / "frame.jpg").exists()


def test_direct_acquirer_rejects_invalid_jpeg_and_removes_partial_output(tmp_path: Path) -> None:
    process = FakeProcess("#tb 0: 1/10\n0, 20, 20, 1, 1, hash\n", running=True)

    with pytest.raises(ReferenceFrameDecodeError):
        _ = _acquirer(process, valid_jpeg=False).acquire(_request(tmp_path))

    assert process.terminate_calls == 1
    assert not (tmp_path / "frame.jpg").exists()


def test_direct_acquirer_cleans_up_the_child_on_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcess("", running=True)

    def cancel(*_: object) -> tuple[FrameTiming, FrameTiming | None]:
        raise KeyboardInterrupt

    monkeypatch.setattr(FfmpegDirectReferenceFrameAcquirer, "_select", cancel)

    with pytest.raises(KeyboardInterrupt):
        _ = _acquirer(process).acquire(_request(tmp_path))

    assert process.terminate_calls == 1
    assert process.stdout.closed
    assert process.stderr.closed
