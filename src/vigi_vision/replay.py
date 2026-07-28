"""Temporary MP4 extraction for credential-free NVR replay requests."""

import os
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import final
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import SecretStr
from typing_extensions import override

from vigi_vision.recording import ReplayRequest

_STARTUP_ALLOWANCE_SECONDS = 30.0
_FINALIZATION_MARGIN_SECONDS = 10.0

ReplayRunner = Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]]


class ReplayError(RuntimeError):
    """Base class for safe replay retrieval errors."""


@final
@dataclass(frozen=True, slots=True)
class ReplayAuthenticationError(ReplayError):
    """Raised when the NVR rejects RTSP credentials."""

    @override
    def __str__(self) -> str:
        return "The NVR rejected the RTSP credentials."


@final
@dataclass(frozen=True, slots=True)
class ReplayUnavailableError(ReplayError):
    """Raised when an RTSP replay request has no available recording."""

    @override
    def __str__(self) -> str:
        return "The NVR has no replay available for the requested time window."


@final
@dataclass(frozen=True, slots=True)
class ReplayTimeoutError(ReplayError):
    """Raised when ffmpeg exceeds the bounded replay extraction timeout."""

    @override
    def __str__(self) -> str:
        return "ffmpeg timed out while extracting the requested replay clip."


@final
@dataclass(frozen=True, slots=True)
class ReplayExtractionError(ReplayError):
    """Raised for non-authentication ffmpeg extraction failures."""

    @override
    def __str__(self) -> str:
        return "ffmpeg could not extract the requested replay clip."


@dataclass(frozen=True, slots=True)
class ReplayClip:
    """A removable temporary MP4 extracted from one credential-free replay request."""

    channel_id: int
    requested_start_utc: datetime
    requested_end_utc: datetime
    replay_url: str = field(repr=False)
    temporary_mp4_path: Path = field(repr=False)
    duration_seconds: int

    def remove(self) -> None:
        """Remove the consumer-owned temporary MP4."""
        self.temporary_mp4_path.unlink(missing_ok=True)


def _run_ffmpeg(
    arguments: tuple[str, ...], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # Fixed executable and tuple arguments; never a shell command.
        arguments,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=timeout_seconds,
    )


@dataclass(frozen=True, slots=True)
class ReplayExtractor:
    """Extract one temporary video-only MP4 with RTSP/TCP and client-side duration."""

    executable: Path = field(repr=False)
    username: str = field(repr=False)
    password: SecretStr = field(repr=False)
    temporary_directory: Path | None = field(default=None, repr=False)
    runner: ReplayRunner = field(default=_run_ffmpeg, repr=False)

    def extract(self, request: ReplayRequest) -> ReplayClip:
        """Extract one bounded MP4 from a credential-free replay request."""
        try:
            output_path = self._temporary_path()
        except OSError:
            raise ReplayExtractionError from None
        try:
            arguments = self._arguments(request, output_path)
            timeout_seconds = (
                request.window.duration_seconds
                + _STARTUP_ALLOWANCE_SECONDS
                + _FINALIZATION_MARGIN_SECONDS
            )
            completed = self.runner(arguments, timeout_seconds)
        except subprocess.TimeoutExpired:
            _remove_partial(output_path)
            raise ReplayTimeoutError from None
        except OSError:
            _remove_partial(output_path)
            raise ReplayExtractionError from None
        except ReplayExtractionError:
            _remove_partial(output_path)
            raise
        except BaseException:
            _remove_partial(output_path)
            raise
        if completed.returncode != 0:
            _remove_partial(output_path)
            raise _process_error(completed.stderr)
        if not _is_nonempty_file(output_path):
            _remove_partial(output_path)
            raise ReplayExtractionError
        return ReplayClip(
            channel_id=request.window.channel_id,
            requested_start_utc=request.window.start_utc,
            requested_end_utc=request.window.end_utc,
            replay_url=request.replay_url,
            temporary_mp4_path=output_path,
            duration_seconds=request.window.duration_seconds,
        )

    def _temporary_path(self) -> Path:
        if self.temporary_directory is not None:
            self.temporary_directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix="vigi-vision-replay-",
            suffix=".mp4",
            dir=self.temporary_directory,
        )
        os.close(descriptor)
        return Path(temporary_path)

    def _arguments(self, request: ReplayRequest, output_path: Path) -> tuple[str, ...]:
        authenticated_url = _with_rtsp_credentials(
            request.replay_url,
            self.username,
            self.password.get_secret_value(),
        )
        return (
            str(self.executable),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-rtsp_transport",
            "tcp",
            "-i",
            authenticated_url,
            "-map",
            "0:v:0",
            "-t",
            str(request.window.duration_seconds),
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            str(output_path),
        )


def _remove_partial(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


def _is_nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _process_error(stderr: str) -> ReplayError:
    if "401" in stderr:
        return ReplayAuthenticationError()
    if "454" in stderr:
        return ReplayUnavailableError()
    return ReplayExtractionError()


def _with_rtsp_credentials(replay_url: str, username: str, password: str) -> str:
    parsed = urlsplit(replay_url)
    if parsed.scheme != "rtsp" or not parsed.hostname or parsed.username or parsed.password:
        raise ReplayExtractionError
    credentialed_netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{parsed.netloc}"
    return urlunsplit((parsed.scheme, credentialed_netloc, parsed.path, parsed.query, ""))
