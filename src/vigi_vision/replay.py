"""Temporary MP4 extraction for credential-free NVR replay requests."""

import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import final
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import SecretStr
from typing_extensions import override

from vigi_vision.recording import ReplayRequest
from vigi_vision.replay_progress import (
    ReplayProgressDiagnostics,
    ReplayProgressRunner,
    log_progress_timeout,
    run_ffmpeg_with_progress,
)

_STARTUP_ALLOWANCE_SECONDS = 30.0
_FINALIZATION_MARGIN_SECONDS = 10.0
_LOGGER = logging.getLogger(__name__)

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
    timeout_diagnostic_directory: Path | None = field(default=None, repr=False)
    progress_diagnostics: bool = field(default=False, repr=False)
    runner: ReplayRunner = field(default=_run_ffmpeg, repr=False)
    progress_runner: ReplayProgressRunner = field(default=run_ffmpeg_with_progress, repr=False)

    def extract(self, request: ReplayRequest) -> ReplayClip:
        """Extract one bounded MP4 from a credential-free replay request."""
        try:
            output_path = self._temporary_path()
        except OSError:
            raise ReplayExtractionError from None
        started_at = perf_counter()
        diagnostics: ReplayProgressDiagnostics | None = (
            ReplayProgressDiagnostics(request.window.duration_seconds)
            if self.progress_diagnostics
            else None
        )
        try:
            arguments = self._arguments(request, output_path)
            timeout_seconds = (
                request.window.duration_seconds
                + _STARTUP_ALLOWANCE_SECONDS
                + _FINALIZATION_MARGIN_SECONDS
            )
            completed = self._run(arguments, timeout_seconds, diagnostics)
        except subprocess.TimeoutExpired:
            try:
                partial_output_bytes = output_path.stat().st_size
            except OSError:
                partial_output_bytes = 0
            elapsed_ms = round((perf_counter() - started_at) * 1_000)
            _LOGGER.warning(
                "replay.timeout channel_id=%d window_start_utc=%s window_end_utc=%s duration_seconds=%d elapsed_ms=%d partial_output_bytes=%d",  # noqa: E501
                request.window.channel_id,
                request.window.start_utc.isoformat(),
                request.window.end_utc.isoformat(),
                request.window.duration_seconds,
                elapsed_ms,
                partial_output_bytes,
            )
            if diagnostics is not None:
                log_progress_timeout(
                    request.window.channel_id,
                    request.window.duration_seconds,
                    elapsed_ms,
                    diagnostics.summary(now=perf_counter()),
                )
            self._preserve_timeout_partial(request, output_path)
            raise ReplayTimeoutError from None
        except OSError:
            _remove_partial(output_path)
            raise ReplayExtractionError from None
        except ReplayExtractionError:
            _remove_partial(output_path)
            raise
        except KeyboardInterrupt:
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

    def _run(
        self,
        arguments: tuple[str, ...],
        timeout_seconds: float,
        diagnostics: ReplayProgressDiagnostics | None,
    ) -> subprocess.CompletedProcess[str]:
        if diagnostics is None:
            return self.runner(arguments, timeout_seconds)
        return self.progress_runner(arguments, timeout_seconds, diagnostics)

    def _preserve_timeout_partial(self, request: ReplayRequest, output_path: Path) -> None:
        if self.timeout_diagnostic_directory is None or not _is_nonempty_file(output_path):
            _remove_partial(output_path)
            return
        diagnostic_path = self.timeout_diagnostic_directory / (
            f"channel-{request.window.channel_id}-"
            f"{request.window.start_utc:%Y%m%dT%H%M%SZ}-timeout.mp4"
        )
        created_diagnostic_file = False
        try:
            self.timeout_diagnostic_directory.mkdir(parents=True, exist_ok=True)
            if (
                self.timeout_diagnostic_directory.is_symlink()
                or not self.timeout_diagnostic_directory.is_dir()
            ):
                _remove_partial(output_path)
                return
            descriptor = os.open(
                diagnostic_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.close(descriptor)
            created_diagnostic_file = True
            _ = shutil.copyfile(output_path, diagnostic_path)
        except OSError:
            if created_diagnostic_file:
                _remove_partial(diagnostic_path)
        finally:
            _remove_partial(output_path)

    def _arguments(self, request: ReplayRequest, output_path: Path) -> tuple[str, ...]:
        authenticated_url = authenticated_replay_url(
            request.replay_url,
            self.username,
            self.password.get_secret_value(),
        )
        progress_arguments = (
            ("-progress", "pipe:1", "-nostats", "-stats_period", "0.5")
            if self.progress_diagnostics
            else ()
        )
        return (
            str(self.executable),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            *progress_arguments,
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


def authenticated_replay_url(replay_url: str, username: str, password: str) -> str:
    """Embed replay credentials only in an in-memory RTSP URL for ffmpeg."""
    parsed = urlsplit(replay_url)
    if parsed.scheme != "rtsp" or not parsed.hostname or parsed.username or parsed.password:
        raise ReplayExtractionError
    credentialed_netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{parsed.netloc}"
    return urlunsplit((parsed.scheme, credentialed_netloc, parsed.path, parsed.query, ""))
