from pathlib import Path
from subprocess import CompletedProcess

import pytest

from vigi_vision.video import VideoError, VideoSampler, sample_timestamps


def test_sampling_uses_bounded_cadence_before_the_media_endpoint() -> None:
    # Given

    # When
    timestamps = sample_timestamps(9.0)

    # Then
    assert timestamps == (0, 2_917, 5_833, 8_750)


def test_sampling_keeps_the_final_timestamp_inside_the_decodable_interval() -> None:
    # Given
    duration_seconds = 29.009

    # When
    timestamps = sample_timestamps(duration_seconds)

    # Then
    assert timestamps == tuple(sorted(set(timestamps)))
    assert timestamps[0] == 0
    assert timestamps[-1] < round(duration_seconds * 1_000)


def test_video_sampler_probes_extracts_ordered_frames_and_cleans_up(tmp_path: Path) -> None:
    # Given
    video_path = tmp_path / "clip.mp4"
    _ = video_path.write_bytes(b"mp4")
    calls: list[tuple[str, ...]] = []

    def probe_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        return CompletedProcess(
            arguments,
            0,
            stdout='{"format":{"duration":"6.0"},"streams":[{"width":1280,"height":720}]}',
        )

    def extract_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        calls.append(arguments)
        _ = Path(arguments[-1]).write_bytes(b"jpeg")
        return CompletedProcess(arguments, 0)

    sampler = VideoSampler(
        ffmpeg=Path("ffmpeg"),
        ffprobe=Path("ffprobe"),
        probe_runner=probe_runner,
        extract_runner=extract_runner,
    )

    # When
    with sampler.sample(video_path) as sample:
        paths = tuple(frame.temporary_path for frame in sample.frames)

        # Then
        assert sample.metadata.duration_seconds == 6.0
        assert tuple(frame.timestamp_ms for frame in sample.frames) == (0, 2_875, 5_750)
        assert tuple(frame.display_label for frame in sample.frames) == (
            "Frame 1 — 00:00.0",
            "Frame 2 — 00:02.8",
            "Frame 3 — 00:05.7",
        )
        assert all(path.is_file() for path in paths)
        assert len(calls) == 3
        assert calls[0][1:17] == (
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "0.000",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            "scale=1024:1024:force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-q:v",
        )
        assert calls[0][17:20] == ("5", "-pix_fmt", "yuvj420p")
        assert calls[0][-1].endswith(".jpg")

    assert not any(path.exists() for path in paths)


def test_video_sampler_rejects_non_mp4_file(tmp_path: Path) -> None:
    # Given
    video_path = tmp_path / "clip.mov"
    _ = video_path.write_bytes(b"movie")
    sampler = VideoSampler(Path("ffmpeg"), Path("ffprobe"))

    # When / Then
    with pytest.raises(VideoError, match="MP4"), sampler.sample(video_path):
        pass


def test_video_sampler_rejects_duration_over_limit(tmp_path: Path) -> None:
    # Given
    video_path = tmp_path / "clip.mp4"
    _ = video_path.write_bytes(b"mp4")

    def probe_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        return CompletedProcess(
            arguments,
            0,
            stdout='{"format":{"duration":"31.07"},"streams":[{"width":1280,"height":720}]}',
        )

    sampler = VideoSampler(Path("ffmpeg"), Path("ffprobe"), probe_runner=probe_runner)

    # When / Then
    with pytest.raises(VideoError, match="up to 30 seconds"), sampler.sample(video_path):
        pass


def test_video_sampler_rejects_ffprobe_or_ffmpeg_failure(tmp_path: Path) -> None:
    # Given
    video_path = tmp_path / "clip.mp4"
    _ = video_path.write_bytes(b"mp4")

    def failed_probe(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        return CompletedProcess(arguments, 1)

    probe_sampler = VideoSampler(Path("ffmpeg"), Path("ffprobe"), probe_runner=failed_probe)

    # When / Then
    with pytest.raises(VideoError, match="ffprobe"), probe_sampler.sample(video_path):
        pass

    def successful_probe(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        return CompletedProcess(
            arguments,
            0,
            stdout='{"format":{"duration":"3.0"},"streams":[{"width":1280,"height":720}]}',
        )

    def empty_extract(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        return CompletedProcess(arguments, 0)

    extraction_sampler = VideoSampler(
        Path("ffmpeg"),
        Path("ffprobe"),
        probe_runner=successful_probe,
        extract_runner=empty_extract,
    )

    with pytest.raises(VideoError, match="ffmpeg"), extraction_sampler.sample(video_path):
        pass


def test_video_sampler_proceeds_with_hevc_style_metadata_and_a_safe_final_timestamp(
    tmp_path: Path,
) -> None:
    # Given
    video_path = tmp_path / "hevc.mp4"
    _ = video_path.write_bytes(b"mp4")
    calls: list[tuple[str, ...]] = []

    def probe_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        return CompletedProcess(
            arguments,
            0,
            stdout=(
                '{"streams":[{"codec_type":"video","width":2560,"height":1440}],'
                '"format":{"duration":"29.009000"}}'
            ),
        )

    def extract_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        calls.append(arguments)
        _ = Path(arguments[-1]).write_bytes(b"jpeg")
        return CompletedProcess(arguments, 0)

    sampler = VideoSampler(
        Path("ffmpeg"), Path("ffprobe"), probe_runner=probe_runner, extract_runner=extract_runner
    )

    # When
    with sampler.sample(video_path) as sample:
        timestamps = tuple(frame.timestamp_ms for frame in sample.frames)

    # Then
    assert sample.metadata == VideoSampler.probe(sampler, video_path)
    assert len(calls) == len(timestamps)
    assert timestamps[-1] < 29_009


def test_video_sampler_uses_partial_samples_when_two_frames_extract(tmp_path: Path) -> None:
    # Given
    video_path = tmp_path / "partial.mp4"
    _ = video_path.write_bytes(b"mp4")
    output_paths: list[Path] = []

    def probe_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        return CompletedProcess(
            arguments,
            0,
            stdout='{"format":{"duration":"6.0"},"streams":[{"width":1280,"height":720}]}',
        )

    def extract_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        output_path = Path(arguments[-1])
        output_paths.append(output_path)
        if len(output_paths) < 3:
            _ = output_path.write_bytes(b"jpeg")
            return CompletedProcess(arguments, 0)
        return CompletedProcess(arguments, 1, stderr="decoder failed")

    sampler = VideoSampler(
        Path("ffmpeg"), Path("ffprobe"), probe_runner=probe_runner, extract_runner=extract_runner
    )

    # When
    with sampler.sample(video_path) as sample:
        frames = sample.frames

    # Then
    assert tuple(frame.index for frame in frames) == (1, 2)
    assert not any(path.parent.exists() for path in output_paths)


def test_video_sampler_reports_ffmpeg_diagnostics_and_cleans_up_after_total_failure(
    tmp_path: Path,
) -> None:
    # Given
    video_path = tmp_path / "broken.mp4"
    _ = video_path.write_bytes(b"mp4")
    output_paths: list[Path] = []

    def probe_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        return CompletedProcess(
            arguments,
            0,
            stdout='{"format":{"duration":"3.0"},"streams":[{"width":1280,"height":720}]}',
        )

    def extract_runner(arguments: tuple[str, ...]) -> CompletedProcess[str]:
        output_paths.append(Path(arguments[-1]))
        return CompletedProcess(arguments, 1, stderr="decoder failed")

    sampler = VideoSampler(
        Path("ffmpeg"), Path("ffprobe"), probe_runner=probe_runner, extract_runner=extract_runner
    )

    # When / Then
    with pytest.raises(VideoError, match="ffmpeg") as exception_info, sampler.sample(video_path):
        pass

    assert exception_info.value.diagnostic == "decoder failed"
    assert not any(path.parent.exists() for path in output_paths)
