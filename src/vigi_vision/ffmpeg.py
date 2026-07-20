"""One-frame RTSP extraction through an external ffmpeg executable."""

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import final
from urllib.parse import quote, urlsplit, urlunsplit

from typing_extensions import override

_EXTRACTION_TIMEOUT_SECONDS = 15.0


@final
@dataclass(frozen=True, slots=True)
class FfmpegUnavailableError(RuntimeError):
    """Raised when no configured or PATH-resolved ffmpeg executable is available."""

    @override
    def __str__(self) -> str:
        """Return concise installation guidance."""
        return "ffmpeg is required. Install it or set FFMPEG_PATH to its executable."


@dataclass(frozen=True, slots=True)
class FfmpegExtractionError(RuntimeError):
    """Raised for an ffmpeg failure without leaking RTSP connection details."""

    @override
    def __str__(self) -> str:
        """Return a redacted extraction error."""
        return "ffmpeg could not extract a frame from the selected camera."


@final
@dataclass(frozen=True, slots=True)
class FfmpegTimeoutError(FfmpegExtractionError):
    """Raised when ffmpeg exceeds the bounded frame-capture timeout."""

    @override
    def __str__(self) -> str:
        return "ffmpeg timed out while extracting a frame from the selected camera."


FfmpegRunner = Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]]


def _run_ffmpeg(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # Fixed executable and tuple arguments; never a shell command.
        arguments,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=_EXTRACTION_TIMEOUT_SECONDS,
    )


def resolve_ffmpeg(configured_path: Path | None) -> Path:
    """Return a configured executable or the safe PATH-resolved ffmpeg command."""
    if configured_path is not None:
        if configured_path.is_file():
            return configured_path
        raise FfmpegUnavailableError
    discovered_path = shutil.which("ffmpeg")
    if discovered_path is None:
        raise FfmpegUnavailableError
    return Path(discovered_path)


@dataclass(frozen=True, slots=True)
class FfmpegExtractor:
    """Extract one JPEG with RTSP-over-TCP and ffmpeg URL Digest credentials."""

    executable: Path
    runner: FfmpegRunner = _run_ffmpeg

    def extract(self, live_url: str, *, username: str, password: str, output_path: Path) -> Path:
        """Run ffmpeg once and return a frame only when it was written successfully."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        authenticated_url = _with_rtsp_credentials(live_url, username, password)
        arguments = (
            str(self.executable),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-rtsp_transport",
            "tcp",
            "-i",
            authenticated_url,
            "-frames:v",
            "1",
            "-an",
            "-y",
            str(output_path),
        )
        try:
            completed = self.runner(arguments)
        except subprocess.TimeoutExpired as error:
            raise FfmpegTimeoutError from error
        except OSError as error:
            raise FfmpegExtractionError from error
        if completed.returncode != 0 or not output_path.is_file():
            raise FfmpegExtractionError
        return output_path


def _with_rtsp_credentials(live_url: str, username: str, password: str) -> str:
    parsed = urlsplit(live_url)
    if parsed.scheme != "rtsp" or not parsed.hostname or parsed.username or parsed.password:
        raise FfmpegExtractionError
    credentialed_netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{parsed.netloc}"
    return urlunsplit((parsed.scheme, credentialed_netloc, parsed.path, parsed.query, ""))
