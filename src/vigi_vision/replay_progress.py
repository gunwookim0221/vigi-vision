"""Bounded FFmpeg progress collection for replay-timeout diagnosis."""

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock, Thread
from time import perf_counter
from typing import TextIO, final

_MAX_PROGRESS_LINE_LENGTH = 256
_MAX_STDERR_CHARACTERS = 4_096
_MAX_PROGRESS_VALUE = 86_400_000_000
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReplayProgressSummary:
    """Allowlisted aggregate facts from one bounded FFmpeg progress stream."""

    highest_frame: int | None
    highest_media_time_us: int | None
    highest_total_size: int | None
    last_progress_age_ms: int | None
    media_time_stalled_ms: int | None
    size_stalled_ms: int | None
    reached_requested_duration: bool
    progress_end_seen: bool


@final
class ReplayProgressDiagnostics:
    """Mutable bounded accumulator for untrusted FFmpeg progress records."""

    def __init__(self, requested_duration_seconds: int) -> None:
        """Initialize bounded aggregates for one requested output duration."""
        self._current_media_time_us: int | None = None
        self._highest_frame: int | None = None
        self._highest_media_time_us: int | None = None
        self._highest_total_size: int | None = None
        self._last_media_time_change_at: float | None = None
        self._last_progress_at: float | None = None
        self._last_size_change_at: float | None = None
        self._lock = Lock()
        self._progress_end_seen = False
        self._requested_duration_us = requested_duration_seconds * 1_000_000

    def observe_line(self, line: str, *, now: float) -> None:
        """Parse one bounded progress record without retaining raw input."""
        if len(line) > _MAX_PROGRESS_LINE_LENGTH:
            return
        key, separator, value = line.strip().partition("=")
        if not separator:
            return
        with self._lock:
            self._record_field(key, value, now)

    def _record_field(self, key: str, value: str, now: float) -> None:
        match key:  # noqa: MATCH_OK - protocol keys are untrusted open input.
            case "frame":
                self._record_frame(_parse_nonnegative(value))
            case "total_size":
                self._record_size(_parse_nonnegative(value), now)
            case "out_time_us":
                self._current_media_time_us = _parse_nonnegative(value)
            case "out_time_ms":
                if self._current_media_time_us is None:
                    self._current_media_time_us = _parse_nonnegative(value)
            case "out_time":
                if self._current_media_time_us is None:
                    self._current_media_time_us = _parse_timestamp(value)
            case "progress":
                self._record_progress(value, now)
            case _:
                return

    def summary(self, *, now: float) -> ReplayProgressSummary:
        """Return only safe aggregates needed to classify a timeout."""
        with self._lock:
            return ReplayProgressSummary(
                highest_frame=self._highest_frame,
                highest_media_time_us=self._highest_media_time_us,
                highest_total_size=self._highest_total_size,
                last_progress_age_ms=_age_ms(self._last_progress_at, now),
                media_time_stalled_ms=_age_ms(self._last_media_time_change_at, now),
                size_stalled_ms=_age_ms(self._last_size_change_at, now),
                reached_requested_duration=(
                    self._highest_media_time_us is not None
                    and self._highest_media_time_us >= self._requested_duration_us
                ),
                progress_end_seen=self._progress_end_seen,
            )

    def _record_frame(self, frame: int | None) -> None:
        if frame is not None and (self._highest_frame is None or frame > self._highest_frame):
            self._highest_frame = frame

    def _record_size(self, total_size: int | None, now: float) -> None:
        if total_size is not None and (
            self._highest_total_size is None or total_size > self._highest_total_size
        ):
            self._highest_total_size = total_size
            self._last_size_change_at = now

    def _record_progress(self, value: str, now: float) -> None:
        if value not in {"continue", "end"}:
            return
        self._last_progress_at = now
        if value == "end":
            self._progress_end_seen = True
        media_time_us = self._current_media_time_us
        self._current_media_time_us = None
        if media_time_us is not None and (
            self._highest_media_time_us is None or media_time_us > self._highest_media_time_us
        ):
            self._highest_media_time_us = media_time_us
            self._last_media_time_change_at = now


ReplayProgressRunner = Callable[
    [tuple[str, ...], float, ReplayProgressDiagnostics], subprocess.CompletedProcess[str]
]


def run_ffmpeg_with_progress(
    arguments: tuple[str, ...], timeout_seconds: float, diagnostics: ReplayProgressDiagnostics
) -> subprocess.CompletedProcess[str]:
    """Run FFmpeg while concurrently draining bounded progress and stderr streams."""
    process = subprocess.Popen(  # noqa: S603  # Fixed executable and tuple arguments; never a shell command.
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.stdout is None or process.stderr is None:
        raise OSError
    stderr_chunks: list[str] = []
    progress_reader = Thread(
        target=_drain_progress,
        args=(process.stdout, diagnostics),
        name="vigi-replay-progress",
    )
    stderr_reader = Thread(
        target=_drain_stderr,
        args=(process.stderr, stderr_chunks),
        name="vigi-replay-stderr",
    )
    progress_reader.start()
    stderr_reader.start()
    try:
        returncode = process.wait(timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        _ = process.wait()
        raise
    except KeyboardInterrupt:
        process.kill()
        _ = process.wait()
        raise
    finally:
        progress_reader.join()
        stderr_reader.join()
    return subprocess.CompletedProcess(arguments, returncode, "", "".join(stderr_chunks))


def log_progress_timeout(
    channel_id: int, duration_seconds: int, elapsed_ms: int, summary: ReplayProgressSummary
) -> None:
    """Log only safe aggregate progress facts for a replay timeout."""
    _LOGGER.warning(
        "replay.progress_timeout channel_id=%d duration_seconds=%d elapsed_ms=%d frame=%s out_time_us=%s total_size=%s last_progress_age_ms=%s media_time_stalled_ms=%s size_stalled_ms=%s reached_requested_duration=%s progress_end_seen=%s",  # noqa: E501
        channel_id,
        duration_seconds,
        elapsed_ms,
        _safe_value(summary.highest_frame),
        _safe_value(summary.highest_media_time_us),
        _safe_value(summary.highest_total_size),
        _safe_value(summary.last_progress_age_ms),
        _safe_value(summary.media_time_stalled_ms),
        _safe_value(summary.size_stalled_ms),
        summary.reached_requested_duration,
        summary.progress_end_seen,
    )


def _drain_progress(stream: TextIO, diagnostics: ReplayProgressDiagnostics) -> None:
    for line in stream:
        diagnostics.observe_line(line, now=perf_counter())


def _drain_stderr(stream: TextIO, chunks: list[str]) -> None:
    remaining = _MAX_STDERR_CHARACTERS
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return
        chunks.append(chunk)
        remaining -= len(chunk)
    while stream.read(1):
        continue


def _parse_nonnegative(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    if 0 <= parsed <= _MAX_PROGRESS_VALUE:
        return parsed
    return None


def _parse_timestamp(value: str) -> int | None:
    try:
        hours, minutes, seconds = value.split(":")
        parsed = ((int(hours) * 60 + int(minutes)) * 60 + float(seconds)) * 1_000_000
    except ValueError:
        return None
    return _parse_nonnegative(str(round(parsed)))


def _age_ms(changed_at: float | None, now: float) -> int | None:
    if changed_at is None:
        return None
    return max(0, round((now - changed_at) * 1_000))


def _safe_value(value: int | None) -> int | str:
    return "none" if value is None else value
