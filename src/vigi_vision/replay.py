"""Temporary MP4 extraction for credential-free NVR replay requests."""

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from time import perf_counter
from typing import final
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import SecretStr
from typing_extensions import override

from vigi_vision.recording import ReplayRequest
from vigi_vision.replay_progress import (
    ReplayProgressDiagnostics,
    ReplayProgressRunner,
    TargetReplayProgressRunner,
    log_progress_timeout,
    run_ffmpeg_until_target,
    run_ffmpeg_with_progress,
)

_STARTUP_ALLOWANCE_SECONDS = 30.0
_FINALIZATION_MARGIN_SECONDS = 10.0
_TARGET_SENTINEL_CONTEXT_SECONDS = 1.0
_REPLAY_TIMEOUT_MULTIPLIER = 3.0
_PROGRESS_POLL_INTERVAL_SECONDS = 0.5
_PROGRESS_SAMPLE_INTERVAL_SECONDS = 10.0
_MAX_OUTPUT_PROGRESS_EVENTS = 16
_OUTPUT_GROWTH_THRESHOLD_BYTES = 4 * 1024
_RTSP_METHODS = r"(?:OPTIONS|DESCRIBE|SETUP|PLAY|PAUSE|TEARDOWN|ANNOUNCE|RECORD)"
_RTSP_STATUS_CONTEXT_PREFIX = (
    rf"(?i)(?:\brtsp\b|\b{_RTSP_METHODS}\b|\bserver\s+returned\b)[^\r\n]{{0,96}}"
)
_RTSP_STATUS_CONTEXT = re.compile(rf"{_RTSP_STATUS_CONTEXT_PREFIX}\b(?P<status>401|454)\b")
_RTSP_STATUS_LINE = re.compile(r"(?im)^\s*(?:RTSP|HTTP)/\d(?:\.\d)?\s+(?P<status>401|454)\b")
_LOGGER = logging.getLogger(__name__)
_PROGRESS_LOGGER = logging.getLogger("uvicorn.error.vigi_vision.phase7e")

_REPLAY_PROGRESS_STAGES = frozenset(
    {
        "started",
        "first_output",
        "output_progress",
        "process_exited",
        "timeout_started",
        "termination_requested",
        "termination_completed",
        "cleanup_completed",
    }
)
_REPLAY_TERMINATION_STAGES = frozenset({"none", "requested", "completed", "cleanup"})
_REPLAY_CLEANUP_OUTCOMES = frozenset({"not_required", "completed", "failed", "unknown"})

ReplayRunner = Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]]


def effective_replay_timeout_seconds(
    duration_seconds: int,
    minimum_timeout_seconds: float,
) -> float:
    """Return a bounded duration-aware replay ceiling."""
    if type(duration_seconds) is not int or duration_seconds <= 0:
        raise ValueError
    if not math.isfinite(minimum_timeout_seconds) or minimum_timeout_seconds <= 0:
        raise ValueError
    return max(float(minimum_timeout_seconds), duration_seconds * _REPLAY_TIMEOUT_MULTIPLIER)


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
    _cleanup_callback: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def remove(self) -> None:
        """Remove the consumer-owned temporary MP4."""
        try:
            self.temporary_mp4_path.unlink(missing_ok=True)
        finally:
            if self._cleanup_callback is not None:
                self._cleanup_callback()


@dataclass(slots=True)
class _ReplayLifecycle:
    """Process-local safe replay lifecycle and output-growth observation."""

    request: ReplayRequest
    output_path: Path
    deadline_seconds: float
    diagnostics: ReplayProgressDiagnostics | None
    started_at: float = field(default_factory=perf_counter)
    output_created: bool = False
    last_size_bytes: int = 0
    total_growth_bytes: int = 0
    last_growth_at: float | None = None
    last_progress_log_at: float | None = None
    output_progress_events: int = 0
    emitted_stages: set[str] = field(default_factory=set)
    process_running: bool = False
    stop_event: Event = field(default_factory=Event, repr=False)
    monitor_thread: Thread | None = field(default=None, repr=False)

    def start(self) -> None:
        """Emit start facts and begin bounded output polling."""
        self.process_running = True
        self._emit("started", termination_stage="none")
        try:
            self.monitor_thread = Thread(
                target=self._monitor_output,
                name="vigi-replay-output-progress",
                daemon=True,
            )
            self.monitor_thread.start()
        except Exception:  # noqa: BLE001  # Instrumentation must never alter replay behavior.
            self.monitor_thread = None

    def process_exited(self, exit_code: int | None) -> None:
        """Record process completion without retaining native output."""
        self.stop()
        self._observe_output(force_progress=True)
        self.process_running = False
        self._emit("process_exited", exit_code=exit_code, termination_stage="none")

    def timeout_started(self) -> None:
        """Record that the bounded deadline expired."""
        self.stop()
        self._observe_output(force_progress=True)
        self._emit("timeout_started", termination_stage="none")

    def termination_requested(self) -> None:
        """Record the bounded child-termination request."""
        self._emit("termination_requested", termination_stage="requested")

    def termination_completed(self) -> None:
        """Record that child termination/reaping completed."""
        self.process_running = False
        self._emit("termination_completed", termination_stage="completed")

    def cleanup_completed(self, outcome: str) -> None:
        """Record invocation-owned output cleanup."""
        safe_outcome = outcome if outcome in _REPLAY_CLEANUP_OUTCOMES else "unknown"
        self._observe_output(force_progress=True)
        self._emit(
            "cleanup_completed",
            termination_stage="cleanup",
            cleanup_outcome=safe_outcome,
        )

    def stop(self) -> None:
        """Stop the polling thread without affecting replay state."""
        try:
            self.stop_event.set()
            thread = self.monitor_thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
        except Exception:  # noqa: BLE001  # Instrumentation must never alter replay behavior.
            return

    def _monitor_output(self) -> None:
        while not self.stop_event.is_set():
            self._observe_output(force_progress=False)
            _ = self.stop_event.wait(_PROGRESS_POLL_INTERVAL_SECONDS)

    def _observe_output(self, *, force_progress: bool) -> None:
        now = perf_counter()
        try:
            size_bytes = self.output_path.stat().st_size
            is_file = self.output_path.is_file()
        except OSError:
            size_bytes = 0
            is_file = False
        if not is_file:
            return
        if size_bytes <= 0:
            return
        growth_bytes = max(0, size_bytes - self.last_size_bytes)
        if growth_bytes:
            self.total_growth_bytes += growth_bytes
            self.last_growth_at = now
        self.last_size_bytes = max(self.last_size_bytes, size_bytes)
        if not self.output_created:
            self.output_created = True
            self._emit("first_output", termination_stage="none")
        if (
            not growth_bytes
            and not force_progress
            and self.last_progress_log_at is not None
            and now - self.last_progress_log_at < _PROGRESS_SAMPLE_INTERVAL_SECONDS
        ):
            return
        should_log_growth = bool(growth_bytes) and (
            self.output_progress_events == 0 or growth_bytes >= _OUTPUT_GROWTH_THRESHOLD_BYTES
        )
        should_log_sample = (
            force_progress
            or self.last_progress_log_at is None
            or now - self.last_progress_log_at >= _PROGRESS_SAMPLE_INTERVAL_SECONDS
        )
        if not (should_log_growth or should_log_sample):
            return
        if self.output_progress_events >= _MAX_OUTPUT_PROGRESS_EVENTS:
            return
        self.output_progress_events += 1
        self.last_progress_log_at = now
        self._emit(
            "output_progress",
            output_growth_bytes=growth_bytes,
            termination_stage="none",
        )

    def _emit(
        self,
        stage: str,
        *,
        exit_code: int | None = None,
        output_growth_bytes: int = 0,
        termination_stage: str = "none",
        cleanup_outcome: str = "not_required",
    ) -> None:
        if stage not in _REPLAY_PROGRESS_STAGES:
            return
        if termination_stage not in _REPLAY_TERMINATION_STAGES:
            termination_stage = "none"
        if cleanup_outcome not in _REPLAY_CLEANUP_OUTCOMES:
            cleanup_outcome = "unknown"
        if stage != "output_progress" and stage in self.emitted_stages:
            return
        self.emitted_stages.add(stage)
        elapsed_ms = round((perf_counter() - self.started_at) * 1_000)
        last_growth_elapsed_ms = (
            None
            if self.last_growth_at is None
            else max(0, round((self.last_growth_at - self.started_at) * 1_000))
        )
        progress_ms = _progress_milliseconds(self.diagnostics)
        payload = {
            "event": "phase7e.replay_progress",
            "stage": stage,
            "channel_id": self.request.window.channel_id,
            "window_start_utc": self.request.window.start_utc.isoformat(),
            "window_end_utc": self.request.window.end_utc.isoformat(),
            "requested_duration_seconds": self.request.window.duration_seconds,
            "deadline_ms": round(self.deadline_seconds * 1_000),
            "elapsed_ms": elapsed_ms,
            "output_created": self.output_created,
            "output_size_bytes": self.last_size_bytes,
            "output_growth_bytes": output_growth_bytes,
            "last_output_growth_elapsed_ms": last_growth_elapsed_ms,
            "ffmpeg_out_time_ms": progress_ms,
            "process_running": self.process_running,
            "exit_code": exit_code,
            "termination_stage": termination_stage,
            "cleanup_outcome": cleanup_outcome,
        }
        _safe_replay_log(payload)


def _progress_milliseconds(diagnostics: ReplayProgressDiagnostics | None) -> int | None:
    if diagnostics is None:
        return None
    try:
        summary = diagnostics.summary(now=perf_counter())
    except Exception:  # noqa: BLE001  # Logging must never alter replay behavior.
        return None
    if summary.highest_media_time_us is None:
        return None
    return summary.highest_media_time_us // 1_000


def _safe_replay_log(payload: Mapping[str, object]) -> None:
    """Emit only the closed replay-progress vocabulary; never fail replay."""
    try:
        _PROGRESS_LOGGER.info(
            "phase7e.replay_progress %s",
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    except Exception:  # noqa: BLE001  # A logging sink is outside replay authority.
        return


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
    target_progress_runner: TargetReplayProgressRunner = field(
        default=run_ffmpeg_until_target, repr=False
    )

    def extract(self, request: ReplayRequest) -> ReplayClip:
        """Extract one bounded MP4 from a credential-free replay request."""
        return self.extract_with_timeout(request, None)

    def extract_for_target(
        self, request: ReplayRequest, target_offset_seconds: float
    ) -> ReplayClip:
        """Extract successor media and finalize after post-target context arrives."""
        if (
            not math.isfinite(target_offset_seconds)
            or target_offset_seconds < 0
            or target_offset_seconds >= request.window.duration_seconds
        ):
            raise ReplayExtractionError
        return self._extract(
            request,
            None,
            target_coverage_seconds=min(
                float(request.window.duration_seconds),
                target_offset_seconds + _TARGET_SENTINEL_CONTEXT_SECONDS,
            ),
        )

    def extract_with_timeout(
        self,
        request: ReplayRequest,
        timeout_seconds: float | None,
    ) -> ReplayClip:
        """Extract with an optional stricter invocation-owned timeout ceiling."""
        return self._extract(request, timeout_seconds, target_coverage_seconds=None)

    def _extract(  # noqa: C901, PLR0912, PLR0915
        self,
        request: ReplayRequest,
        timeout_seconds: float | None,
        *,
        target_coverage_seconds: float | None,
    ) -> ReplayClip:
        if timeout_seconds is not None and (
            not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            raise ReplayTimeoutError
        try:
            output_path = self._temporary_path()
        except OSError:
            raise ReplayExtractionError from None
        started_at = perf_counter()
        diagnostics: ReplayProgressDiagnostics | None = (
            ReplayProgressDiagnostics(request.window.duration_seconds)
            if self.progress_diagnostics or target_coverage_seconds is not None
            else None
        )
        lifecycle: _ReplayLifecycle | None = None
        try:
            arguments = self._arguments(
                request, output_path, target_aware=target_coverage_seconds is not None
            )
            normal_timeout = effective_replay_timeout_seconds(
                request.window.duration_seconds,
                request.window.duration_seconds
                + _STARTUP_ALLOWANCE_SECONDS
                + _FINALIZATION_MARGIN_SECONDS,
            )
            effective_timeout = (
                normal_timeout
                if timeout_seconds is None
                else min(float(normal_timeout), timeout_seconds)
            )
            lifecycle = _ReplayLifecycle(request, output_path, effective_timeout, diagnostics)
            lifecycle.start()
            completed = self._run(
                arguments,
                effective_timeout,
                diagnostics,
                target_coverage_seconds=target_coverage_seconds,
            )
        except subprocess.TimeoutExpired:
            if lifecycle is not None:
                lifecycle.timeout_started()
                lifecycle.termination_requested()
                lifecycle.termination_completed()
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
            if lifecycle is not None:
                lifecycle.cleanup_completed("completed" if not output_path.exists() else "failed")
            raise ReplayTimeoutError from None
        except OSError:
            _remove_partial(output_path)
            if lifecycle is not None:
                lifecycle.process_exited(None)
                lifecycle.cleanup_completed("completed" if not output_path.exists() else "failed")
            raise ReplayExtractionError from None
        except ReplayExtractionError:
            _remove_partial(output_path)
            if lifecycle is not None:
                lifecycle.process_exited(None)
                lifecycle.cleanup_completed("completed" if not output_path.exists() else "failed")
            raise
        except KeyboardInterrupt:
            _remove_partial(output_path)
            if lifecycle is not None:
                lifecycle.termination_requested()
                lifecycle.termination_completed()
                lifecycle.cleanup_completed("completed" if not output_path.exists() else "failed")
            raise
        lifecycle.process_exited(completed.returncode)
        if completed.returncode != 0:
            _remove_partial(output_path)
            lifecycle.cleanup_completed("completed" if not output_path.exists() else "failed")
            raise _process_error(completed.stderr)
        if not _is_nonempty_file(output_path):
            _remove_partial(output_path)
            lifecycle.cleanup_completed("completed" if not output_path.exists() else "failed")
            raise ReplayExtractionError
        return ReplayClip(
            channel_id=request.window.channel_id,
            requested_start_utc=request.window.start_utc,
            requested_end_utc=request.window.end_utc,
            replay_url=request.replay_url,
            temporary_mp4_path=output_path,
            duration_seconds=request.window.duration_seconds,
            _cleanup_callback=(
                lambda: lifecycle.cleanup_completed(
                    "completed" if not output_path.exists() else "failed"
                )
            ),
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
        *,
        target_coverage_seconds: float | None,
    ) -> subprocess.CompletedProcess[str]:
        if target_coverage_seconds is not None:
            if diagnostics is None:
                raise ReplayExtractionError
            return self.target_progress_runner(
                arguments, timeout_seconds, diagnostics, target_coverage_seconds
            )
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

    def _arguments(
        self, request: ReplayRequest, output_path: Path, *, target_aware: bool = False
    ) -> tuple[str, ...]:
        authenticated_url = authenticated_replay_url(
            request.replay_url,
            self.username,
            self.password.get_secret_value(),
        )
        progress_arguments = (
            ("-progress", "pipe:1", "-nostats", "-stats_period", "0.5")
            if self.progress_diagnostics or target_aware
            else ()
        )
        return (
            str(self.executable),
            "-hide_banner",
            "-loglevel",
            "error",
            *(("-nostdin",) if not target_aware else ()),
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
    status = _status_from_error_context(stderr)
    if status == "401":
        return ReplayAuthenticationError()
    if status == "454":
        return ReplayUnavailableError()
    return ReplayExtractionError()


def _status_from_error_context(stderr: str) -> str | None:
    """Read RTSP/HTTP status only when a protocol context is present."""
    match = _RTSP_STATUS_LINE.search(stderr) or _RTSP_STATUS_CONTEXT.search(stderr)
    return None if match is None else match.group("status")


def authenticated_replay_url(replay_url: str, username: str, password: str) -> str:
    """Embed replay credentials only in an in-memory RTSP URL for ffmpeg."""
    parsed = urlsplit(replay_url)
    if parsed.scheme != "rtsp" or not parsed.hostname or parsed.username or parsed.password:
        raise ReplayExtractionError
    credentialed_netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{parsed.netloc}"
    return urlunsplit((parsed.scheme, credentialed_netloc, parsed.path, parsed.query, ""))
