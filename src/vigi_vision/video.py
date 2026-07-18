"""Bounded local MP4 probing and representative-frame extraction."""

import json
import math
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from typing_extensions import override

_MAX_DURATION_SECONDS: Final = 30.0
_TARGET_INTERVAL_SECONDS: Final = 3.0
_MIN_FRAME_COUNT: Final = 2
_MAX_FRAME_COUNT: Final = 10
_TOOL_TIMEOUT_SECONDS: Final = 15.0
_MAX_FRAME_EDGE: Final = 1024
_END_SEEK_MARGIN_MS: Final = 250
_SCALE_FILTER: Final = "scale=1024:1024:force_original_aspect_ratio=decrease:force_divisible_by=2"
_FFPROBE_UNAVAILABLE: Final = (
    "ffprobe is required with ffmpeg for video analysis. Install both tools."
)
_FFPROBE_FAILURE: Final = "ffprobe could not inspect the local MP4 file."
_UNUSABLE_VIDEO: Final = "The local MP4 file has no usable video stream or duration."
_FFMPEG_FAILURE: Final = "ffmpeg could not extract representative video frames."
_DURATION_LIMIT: Final = "Video analysis supports local MP4 files up to 30 seconds."
_MP4_ONLY: Final = "Video analysis supports local MP4 files only."
_UNREADABLE_VIDEO: Final = "The local MP4 file cannot be read."

VideoRunner = Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]]


@dataclass(slots=True)
class VideoError(RuntimeError):
    """Safe error with mutable traceback state and non-rendered internal diagnostics."""

    message: str
    diagnostic: str | None = None

    @override
    def __str__(self) -> str:
        return self.message


class VideoMetadata(BaseModel):
    """Validated local facts returned by ffprobe for one video."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    duration_seconds: float = Field(gt=_END_SEEK_MARGIN_MS / 1_000)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @field_validator("duration_seconds")
    @classmethod
    def require_finite_duration(cls, value: float) -> float:
        """Reject non-finite durations before sampling arithmetic."""
        if not math.isfinite(value):
            message = "duration must be finite"
            raise ValueError(message)
        return value


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """One temporary JPEG sampled at an authoritative local timestamp."""

    index: int
    timestamp_ms: int
    display_label: str
    temporary_path: Path


@dataclass(frozen=True, slots=True)
class VideoSample:
    """Temporary sampled frames and their local metadata."""

    metadata: VideoMetadata
    frames: tuple[FrameRecord, ...]


class _ProbeFormat(BaseModel):
    """The ffprobe format payload fields required by VIGI Vision."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    duration: str


class _ProbeStream(BaseModel):
    """The ffprobe video-stream payload fields required by VIGI Vision."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    width: int
    height: int


class _ProbeDocument(BaseModel):
    """The minimal ffprobe JSON response accepted at the process boundary."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    format: _ProbeFormat
    streams: tuple[_ProbeStream, ...] = Field(min_length=1)


def _run_tool(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # Fixed executable and tuple arguments; never a shell command.
        arguments,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=_TOOL_TIMEOUT_SECONDS,
    )


def resolve_ffprobe(ffmpeg: Path) -> Path:
    """Resolve the ffprobe companion required for local-video analysis."""
    sibling = ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe")
    if sibling.is_file():
        return sibling
    discovered_path = shutil.which("ffprobe")
    if discovered_path is None:
        raise VideoError(_FFPROBE_UNAVAILABLE)
    return Path(discovered_path)


def sample_timestamps(duration_seconds: float) -> tuple[int, ...]:
    """Return unique ordered timestamps that remain safely before the media endpoint."""
    duration_ms = round(duration_seconds * 1_000)
    safe_final_ms = max(1, duration_ms - _END_SEEK_MARGIN_MS)
    frame_count = min(
        _MAX_FRAME_COUNT,
        safe_final_ms + 1,
        max(_MIN_FRAME_COUNT, math.ceil(duration_seconds / _TARGET_INTERVAL_SECONDS) + 1),
    )
    return tuple(round(safe_final_ms * index / (frame_count - 1)) for index in range(frame_count))


@dataclass(frozen=True, slots=True)
class VideoSampler:
    """Probe one local MP4 and provide its temporary ordered JPEG samples."""

    ffmpeg: Path
    ffprobe: Path
    probe_runner: VideoRunner = _run_tool
    extract_runner: VideoRunner = _run_tool

    @contextmanager
    def sample(self, video_path: Path) -> Generator[VideoSample, None, None]:
        """Yield temporary samples and clean them up on every exit path."""
        _validate_video_path(video_path)
        metadata = self.probe(video_path)
        if metadata.duration_seconds > _MAX_DURATION_SECONDS:
            raise VideoError(_DURATION_LIMIT)
        with tempfile.TemporaryDirectory(prefix="vigi-vision-video-") as directory:
            temporary_directory = Path(directory)
            temporary_directory.mkdir(parents=True, exist_ok=True)
            frames = self._extract_frames(video_path, metadata, temporary_directory)
            yield VideoSample(metadata, frames)

    def probe(self, video_path: Path) -> VideoMetadata:
        """Probe duration and video dimensions through ffprobe JSON output."""
        arguments = (
            str(self.ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=width,height",
            "-of",
            "json",
            str(video_path),
        )
        try:
            completed = self.probe_runner(arguments)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise VideoError(_FFPROBE_FAILURE) from error
        if completed.returncode != 0:
            raise VideoError(_FFPROBE_FAILURE)
        try:
            document = _ProbeDocument.model_validate_json(completed.stdout)
            duration_seconds = float(document.format.duration)
            stream = document.streams[0]
            return VideoMetadata(
                duration_seconds=duration_seconds,
                width=stream.width,
                height=stream.height,
            )
        except (ValidationError, ValueError, json.JSONDecodeError) as error:
            raise VideoError(_UNUSABLE_VIDEO) from error

    def _extract_frames(
        self, video_path: Path, metadata: VideoMetadata, temporary_directory: Path
    ) -> tuple[FrameRecord, ...]:
        records = tuple(
            _frame_record(index, timestamp_ms, temporary_directory)
            for index, timestamp_ms in enumerate(
                sample_timestamps(metadata.duration_seconds), start=1
            )
        )
        extracted_records: list[FrameRecord] = []
        extraction_errors: list[VideoError] = []
        for record in records:
            match self._extract_frame(video_path, record):
                case None:
                    extracted_records.append(record)
                case VideoError() as error:
                    extraction_errors.append(error)
        if len(extracted_records) >= _MIN_FRAME_COUNT:
            return tuple(extracted_records)
        if extraction_errors:
            raise extraction_errors[-1]
        raise VideoError(_FFMPEG_FAILURE)

    def _extract_frame(self, video_path: Path, record: FrameRecord) -> VideoError | None:
        arguments = (
            str(self.ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{record.timestamp_ms / 1_000:.3f}",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            _SCALE_FILTER,
            "-q:v",
            "5",
            "-pix_fmt",
            "yuvj420p",
            "-an",
            str(record.temporary_path),
        )
        try:
            completed = self.extract_runner(arguments)
        except (OSError, subprocess.TimeoutExpired) as error:
            return VideoError(_FFMPEG_FAILURE, type(error).__name__)
        if completed.returncode != 0 or not record.temporary_path.is_file():
            diagnostic = (completed.stderr or "").strip() or "ffmpeg wrote no JPEG output"
            return VideoError(_FFMPEG_FAILURE, diagnostic)
        return None


def _validate_video_path(video_path: Path) -> None:
    if video_path.suffix.lower() != ".mp4":
        raise VideoError(_MP4_ONLY)
    try:
        with video_path.open("rb") as video_file:
            _ = video_file.read(1)
    except OSError as error:
        raise VideoError(_UNREADABLE_VIDEO) from error


def _frame_record(index: int, timestamp_ms: int, temporary_directory: Path) -> FrameRecord:
    timestamp_text = _format_timestamp(timestamp_ms)
    return FrameRecord(
        index=index,
        timestamp_ms=timestamp_ms,
        display_label=f"Frame {index} — {timestamp_text}",
        temporary_path=temporary_directory / f"frame-{index:03d}-t{timestamp_ms:06d}ms.jpg",
    )


def _format_timestamp(timestamp_ms: int) -> str:
    total_seconds, milliseconds = divmod(timestamp_ms, 1_000)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds // 100}"
