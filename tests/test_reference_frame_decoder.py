import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest

from vigi_vision.reference_frame_decoder import (
    DecodedFrameCandidate,
    FfmpegReferenceFrameDecoder,
    ReferenceFrameDecodeRequest,
    ReferenceFrameDecodeTimeoutError,
    select_nearest_candidate,
)
from vigi_vision.reference_frame_models import (
    FrameSelectionPolicy,
    ReferenceFrameDecodeError,
    ReferenceFrameNoCandidateError,
    TimingPrecisionStatus,
)

_JPEG_BYTES = b"\xff\xd8\xff\xe0reference-frame\xff\xd9"


def _jpeg_probe(
    arguments: tuple[str, ...], width: int = 1280, height: int = 720
) -> CompletedProcess[str]:
    return CompletedProcess(
        arguments,
        0,
        stdout=(f'{{"streams":[{{"codec_name":"mjpeg","width":{width},"height":{height}}}]}}'),
    )


def test_select_nearest_candidate_prefers_earlier_frame_on_equal_distance() -> None:
    # Given
    candidates = (
        DecodedFrameCandidate(Decimal("1.0"), 1),
        DecodedFrameCandidate(Decimal("3.0"), 2),
    )

    # When
    selected = select_nearest_candidate(candidates, 2.0, FrameSelectionPolicy.NEAREST_DECODED_FRAME)

    # Then
    assert selected.local_pts_seconds == 1.0


def test_select_nearest_candidate_preserves_decimal_tie_break() -> None:
    # Given
    candidates = (
        DecodedFrameCandidate(Decimal("1.966667"), 47),
        DecodedFrameCandidate(Decimal("2.033333"), 48),
    )

    # When
    selected = select_nearest_candidate(candidates, 2.0, FrameSelectionPolicy.NEAREST_DECODED_FRAME)

    # Then
    assert selected.local_pts_seconds == Decimal("1.966667")


def test_select_nearest_candidate_resolves_duplicate_pts_by_probe_order() -> None:
    # Given
    candidates = (
        DecodedFrameCandidate(Decimal("2.0"), 10),
        DecodedFrameCandidate(Decimal("2.0"), 11),
    )

    # When
    selected = select_nearest_candidate(candidates, 2.0, FrameSelectionPolicy.NEAREST_DECODED_FRAME)

    # Then
    assert selected.index == 10


def test_ffmpeg_decoder_probes_pts_dimensions_and_writes_selected_jpeg(tmp_path: Path) -> None:
    # Given
    clip_path = tmp_path / "clip.mp4"
    output_path = tmp_path / "frame.jpg"
    _ = clip_path.write_bytes(b"mp4")
    extract_arguments: tuple[str, ...] = ()

    def probe_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        if str(arguments[-1]).endswith(".jpg"):
            return _jpeg_probe(arguments)
        return CompletedProcess(
            arguments,
            0,
            stdout=(
                '{"streams":[{"width":1280,"height":720}],"frames":['
                '{"best_effort_timestamp_time":"0.0"},'
                '{"best_effort_timestamp_time":"1.0"},'
                '{"best_effort_timestamp_time":"3.0"}]}'
            ),
        )

    def extract_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        nonlocal extract_arguments
        extract_arguments = arguments
        _ = Path(arguments[-1]).write_bytes(_JPEG_BYTES)
        return CompletedProcess(arguments, 0)

    decoder = FfmpegReferenceFrameDecoder(
        Path("ffmpeg"), Path("ffprobe"), probe_runner, extract_runner
    )

    # When
    evidence = decoder.decode(
        ReferenceFrameDecodeRequest(
            clip_path,
            2.0,
            FrameSelectionPolicy.NEAREST_DECODED_FRAME,
            output_path,
        )
    )

    # Then
    assert evidence.local_pts_seconds == 1.0
    assert (evidence.width, evidence.height) == (1280, 720)
    assert evidence.timing_precision_status is TimingPrecisionStatus.MEASURED_CLIP_RELATIVE
    assert extract_arguments[10:12] == ("-vf", "select=eq(n\\,1)")
    assert output_path.read_bytes() == _JPEG_BYTES


@pytest.mark.parametrize(
    ("timestamps", "warning_fragment"),
    [
        (("0.0", "1.0"), "before the requested"),
        (("3.0", "4.0"), "after the requested"),
    ],
)
def test_ffmpeg_decoder_warns_when_candidates_exist_on_only_one_side(
    tmp_path: Path, timestamps: tuple[str, ...], warning_fragment: str
) -> None:
    # Given
    clip_path = tmp_path / "clip.mp4"
    output_path = tmp_path / "frame.jpg"
    _ = clip_path.write_bytes(b"mp4")
    frames = ",".join(f'{{"best_effort_timestamp_time":"{timestamp}"}}' for timestamp in timestamps)

    def probe_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        if str(arguments[-1]).endswith(".jpg"):
            return _jpeg_probe(arguments)
        return CompletedProcess(
            arguments,
            0,
            stdout=f'{{"streams":[{{"width":1280,"height":720}}],"frames":[{frames}]}}',
        )

    def extract_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        _ = Path(arguments[-1]).write_bytes(_JPEG_BYTES)
        return CompletedProcess(arguments, 0)

    decoder = FfmpegReferenceFrameDecoder(
        Path("ffmpeg"), Path("ffprobe"), probe_runner, extract_runner
    )

    # When
    evidence = decoder.decode(
        ReferenceFrameDecodeRequest(
            clip_path,
            2.0,
            FrameSelectionPolicy.NEAREST_DECODED_FRAME,
            output_path,
        )
    )

    # Then
    assert any(warning_fragment in warning for warning in evidence.warnings)


def test_ffmpeg_decoder_rejects_invalid_jpeg_and_removes_output(tmp_path: Path) -> None:
    # Given
    clip_path = tmp_path / "clip.mp4"
    output_path = tmp_path / "frame.jpg"
    _ = clip_path.write_bytes(b"mp4")

    def probe_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        if str(arguments[-1]).endswith(".jpg"):
            return CompletedProcess(arguments, 1, stdout="")
        return CompletedProcess(
            arguments,
            0,
            stdout=(
                '{"streams":[{"width":1280,"height":720}],'
                '"frames":[{"best_effort_timestamp_time":"1.0"}]}'
            ),
        )

    def extract_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        _ = Path(arguments[-1]).write_bytes(b"not-a-jpeg")
        return CompletedProcess(arguments, 0)

    decoder = FfmpegReferenceFrameDecoder(
        Path("ffmpeg"), Path("ffprobe"), probe_runner, extract_runner
    )

    # When / Then
    with pytest.raises(ReferenceFrameDecodeError):
        _ = decoder.decode(
            ReferenceFrameDecodeRequest(
                clip_path,
                1.0,
                FrameSelectionPolicy.NEAREST_DECODED_FRAME,
                output_path,
            )
        )

    assert not output_path.exists()


@pytest.mark.parametrize(
    "probe_stdout",
    [
        '{"streams":[{"width":1280,"height":720}],"frames":[]}',
        '{"streams":[{"width":1280,"height":720}],"frames":[{}]}',
    ],
)
def test_ffmpeg_decoder_rejects_no_timestamped_candidate(tmp_path: Path, probe_stdout: str) -> None:
    # Given
    clip_path = tmp_path / "clip.mp4"
    _ = clip_path.write_bytes(b"mp4")

    def probe_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        return CompletedProcess(arguments, 0, stdout=probe_stdout)

    decoder = FfmpegReferenceFrameDecoder(Path("ffmpeg"), Path("ffprobe"), probe_runner)

    # When / Then
    with pytest.raises(ReferenceFrameNoCandidateError):
        _ = decoder.decode(
            ReferenceFrameDecodeRequest(
                clip_path,
                1.0,
                FrameSelectionPolicy.NEAREST_DECODED_FRAME,
                tmp_path / "frame.jpg",
            )
        )


def test_ffmpeg_decoder_removes_partial_output_after_timeout(tmp_path: Path) -> None:
    # Given
    clip_path = tmp_path / "clip.mp4"
    output_path = tmp_path / "frame.jpg"
    _ = clip_path.write_bytes(b"mp4")

    def probe_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        return CompletedProcess(
            arguments,
            0,
            stdout=(
                '{"streams":[{"width":1280,"height":720}],'
                '"frames":[{"best_effort_timestamp_time":"1.0"}]}'
            ),
        )

    def extract_runner(arguments: tuple[str, ...], timeout_seconds: float) -> CompletedProcess[str]:
        _ = Path(arguments[-1]).write_bytes(b"partial")
        raise TimeoutExpired(arguments, timeout_seconds)

    decoder = FfmpegReferenceFrameDecoder(
        Path("ffmpeg"), Path("ffprobe"), probe_runner, extract_runner
    )

    # When / Then
    with pytest.raises(ReferenceFrameDecodeTimeoutError):
        _ = decoder.decode(
            ReferenceFrameDecodeRequest(
                clip_path,
                1.0,
                FrameSelectionPolicy.NEAREST_DECODED_FRAME,
                output_path,
            )
        )

    assert not output_path.exists()


def test_ffmpeg_decoder_rejects_non_monotonic_pts(tmp_path: Path) -> None:
    # Given
    clip_path = tmp_path / "clip.mp4"
    _ = clip_path.write_bytes(b"mp4")

    def probe_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        return CompletedProcess(
            arguments,
            0,
            stdout=(
                '{"streams":[{"width":1280,"height":720}],"frames":['
                '{"best_effort_timestamp_time":"2.0"},'
                '{"best_effort_timestamp_time":"1.0"}]}'
            ),
        )

    decoder = FfmpegReferenceFrameDecoder(Path("ffmpeg"), Path("ffprobe"), probe_runner)

    # When / Then
    with pytest.raises(ReferenceFrameDecodeError):
        _ = decoder.decode(
            ReferenceFrameDecodeRequest(
                clip_path,
                1.0,
                FrameSelectionPolicy.NEAREST_DECODED_FRAME,
                tmp_path / "frame.jpg",
            )
        )


def test_ffmpeg_decoder_rejects_malformed_pts(tmp_path: Path) -> None:
    # Given
    clip_path = tmp_path / "clip.mp4"
    _ = clip_path.write_bytes(b"mp4")

    def probe_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        return CompletedProcess(
            arguments,
            0,
            stdout=(
                '{"streams":[{"width":1280,"height":720}],'
                '"frames":[{"best_effort_timestamp_time":"malformed"}]}'
            ),
        )

    decoder = FfmpegReferenceFrameDecoder(Path("ffmpeg"), Path("ffprobe"), probe_runner)

    # When / Then
    with pytest.raises(ReferenceFrameDecodeError):
        _ = decoder.decode(
            ReferenceFrameDecodeRequest(
                clip_path,
                1.0,
                FrameSelectionPolicy.NEAREST_DECODED_FRAME,
                tmp_path / "frame.jpg",
            )
        )


@pytest.mark.parametrize(
    ("probe_failure", "error_type"),
    [
        ("timeout", ReferenceFrameDecodeTimeoutError),
        ("unavailable", ReferenceFrameDecodeError),
    ],
)
def test_ffmpeg_decoder_translates_probe_timeout_or_unavailable_executable(
    tmp_path: Path,
    probe_failure: str,
    error_type: type[ReferenceFrameDecodeTimeoutError] | type[ReferenceFrameDecodeError],
) -> None:
    # Given
    clip_path = tmp_path / "clip.mp4"
    _ = clip_path.write_bytes(b"mp4")

    def probe_runner(arguments: tuple[str, ...], timeout: float) -> CompletedProcess[str]:
        if probe_failure == "timeout":
            raise TimeoutExpired(arguments, timeout)
        raise FileNotFoundError

    decoder = FfmpegReferenceFrameDecoder(Path("ffmpeg"), Path("ffprobe"), probe_runner)

    # When / Then
    with pytest.raises(error_type):
        _ = decoder.decode(
            ReferenceFrameDecodeRequest(
                clip_path,
                1.0,
                FrameSelectionPolicy.NEAREST_DECODED_FRAME,
                tmp_path / "frame.jpg",
            )
        )


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required for the local decoder component test",
)
def test_ffmpeg_decoder_runs_against_generated_local_media(tmp_path: Path) -> None:
    # Given
    ffmpeg = Path(shutil.which("ffmpeg") or "ffmpeg")
    ffprobe = Path(shutil.which("ffprobe") or "ffprobe")
    clip_path = tmp_path / "fixture.mp4"
    expected_path = tmp_path / "expected.jpg"
    output_path = tmp_path / "frame.jpg"
    generated = subprocess.run(  # noqa: S603  # Fixed executable and test-local argument tuple.
        (
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48:rate=24",
            "-t",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(clip_path),
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert generated.returncode == 0
    expected = subprocess.run(  # noqa: S603  # Fixed executable and test-local argument tuple.
        (
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(clip_path),
            "-vf",
            "select=eq(n\\,1)",
            "-frames:v",
            "1",
            "-q:v",
            "5",
            "-an",
            "-y",
            str(expected_path),
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert expected.returncode == 0
    decoder = FfmpegReferenceFrameDecoder(ffmpeg, ffprobe)

    # When
    evidence = decoder.decode(
        ReferenceFrameDecodeRequest(
            clip_path,
            1 / 24,
            FrameSelectionPolicy.NEAREST_DECODED_FRAME,
            output_path,
        )
    )

    # Then
    assert evidence.width == 64
    assert evidence.height == 48
    assert evidence.local_pts_seconds == pytest.approx(1 / 24, abs=0.000001)
    assert evidence.jpeg_path.read_bytes() == expected_path.read_bytes()
