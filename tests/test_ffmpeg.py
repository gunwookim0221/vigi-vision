from pathlib import Path
from secrets import token_urlsafe
from subprocess import CompletedProcess

import pytest

from vigi_vision.ffmpeg import FfmpegExtractor, FfmpegUnavailableError, resolve_ffmpeg

_TEST_PASSWORD = token_urlsafe()


def _missing_ffmpeg(_: str) -> None:
    return None


def test_resolve_ffmpeg_rejects_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr("vigi_vision.ffmpeg.shutil.which", _missing_ffmpeg)

    # When / Then
    with pytest.raises(FfmpegUnavailableError, match="ffmpeg"):
        _ = resolve_ffmpeg(None)


def test_extract_frame_uses_rtsp_tcp_and_redacts_failure(tmp_path: Path) -> None:
    # Given
    output_path = tmp_path / "frame.jpg"
    captured_arguments: tuple[str, ...] = ()

    def failing_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        nonlocal captured_arguments
        captured_arguments = arguments
        return CompletedProcess(
            arguments, 1, stderr="rtsp://operator:secret@nvr.local/live/1/1/avm"
        )

    extractor = FfmpegExtractor(Path("ffmpeg.exe"), runner=failing_runner)

    # When / Then
    with pytest.raises(RuntimeError) as exception_info:
        _ = extractor.extract(
            "rtsp://nvr.local/live/1/1/avm",
            username="operator",
            password=_TEST_PASSWORD,
            output_path=output_path,
        )

    assert "secret" not in str(exception_info.value)
    assert "rtsp://" not in str(exception_info.value)
    assert "-rtsp_transport" in captured_arguments
    assert "tcp" in captured_arguments


def test_extract_frame_writes_one_frame_to_requested_artifact_path(tmp_path: Path) -> None:
    # Given
    output_path = tmp_path / "artifacts" / "snapshots" / "frame.jpg"

    def successful_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        _ = Path(arguments[-1]).parent.mkdir(parents=True, exist_ok=True)
        _ = Path(arguments[-1]).write_bytes(b"jpeg")
        return CompletedProcess(arguments, 0, stderr="")

    extractor = FfmpegExtractor(Path("ffmpeg.exe"), runner=successful_runner)

    # When
    extracted_path = extractor.extract(
        "rtsp://nvr.local/live/1/1/avm",
        username="operator",
        password=_TEST_PASSWORD,
        output_path=output_path,
    )

    # Then
    assert extracted_path == output_path
    assert output_path.read_bytes() == b"jpeg"
