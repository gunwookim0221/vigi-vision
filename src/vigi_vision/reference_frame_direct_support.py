"""Bounded timing, process, and candidate helpers for direct reference frames."""
# ruff: noqa: D101, D102, D103

import json
import queue
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Event, Thread
from typing import ClassVar, Protocol, TextIO, cast, final

from pydantic import BaseModel, ConfigDict, ValidationError

from vigi_vision.recording import ReplayRequest
from vigi_vision.reference_frame_models import (
    FrameSelectionPolicy,
    ReferenceFrameDecodeError,
)

_ONLY_BEFORE_WARNING = "Only decoded frames before the requested clip position were available."
_ONLY_AFTER_WARNING = "Only decoded frames after the requested clip position were available."
_FRAME_MD5_MIN_FIELDS = 6


class DirectProcess(Protocol):
    """The direct-acquisition child process contract, including both drained pipes."""

    stdout: TextIO | None
    stderr: TextIO | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


DirectProcessFactory = Callable[[tuple[str, ...], Path], DirectProcess]
ProbeRunner = Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]]


@final
@dataclass(frozen=True, slots=True)
class DirectReferenceFrameRequest:
    """Internal input for one direct reference-frame acquisition."""

    replay_request: ReplayRequest
    target_offset_seconds: float
    policy: FrameSelectionPolicy
    output_path: Path = field(repr=False)


@final
@dataclass(frozen=True, slots=True)
class FrameTiming:
    ordinal: int
    local_pts_seconds: Decimal


class JpegStream(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    codec_name: str
    width: int
    height: int


class JpegProbe(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    streams: tuple[JpegStream, ...]


def spawn_process(arguments: tuple[str, ...], cwd: Path) -> DirectProcess:
    """Start one FFmpeg child with both output pipes owned by the caller."""
    process = subprocess.Popen(  # noqa: S603  # Fixed executable and tuple arguments; no shell interpolation.
        arguments,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return cast("DirectProcess", cast("object", process))


def run_probe(arguments: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run bounded ffprobe while discarding diagnostics that cannot leave this boundary."""
    return subprocess.run(  # noqa: S603  # Fixed executable and tuple arguments; no shell interpolation.
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
    )


def validate_jpeg(
    runner: ProbeRunner, ffprobe: Path, path: Path, timeout: float
) -> tuple[int, int]:
    """Validate one selected JPEG before it can be promoted to a durable artifact."""
    try:
        completed = runner(
            (
                str(ffprobe),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height",
                "-of",
                "json",
                str(path),
            ),
            timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReferenceFrameDecodeError from None
    if completed.returncode != 0:
        raise ReferenceFrameDecodeError
    try:
        stream = JpegProbe.model_validate_json(completed.stdout).streams[0]
    except (IndexError, ValidationError, ValueError, json.JSONDecodeError):
        raise ReferenceFrameDecodeError from None
    if stream.codec_name != "mjpeg" or stream.width <= 0 or stream.height <= 0:
        raise ReferenceFrameDecodeError
    return stream.width, stream.height


def drain_timing(stream: TextIO, records: queue.Queue[FrameTiming], reader_fault: Event) -> None:
    """Parse only bounded framemd5 PTS records and discard every other line."""
    time_base: tuple[Decimal, Decimal] | None = None
    ordinal = 0
    for line in stream:
        if line.startswith("#tb 0:"):
            time_base = parse_time_base(line)
            if time_base is None:
                reader_fault.set()
            continue
        if line.startswith("#") or not line.strip():
            continue
        if time_base is None:
            reader_fault.set()
            continue
        timing = parse_timing(line, ordinal, time_base)
        ordinal += 1
        if timing is None:
            reader_fault.set()
            continue
        try:
            records.put_nowait(timing)
        except queue.Full:
            reader_fault.set()


def drain_discard(stream: TextIO) -> None:
    """Drain FFmpeg stderr without parsing or retaining human-oriented output."""
    for _ in stream:
        continue


def parse_time_base(line: str) -> tuple[Decimal, Decimal] | None:
    try:
        numerator, denominator = line.removeprefix("#tb 0:").strip().split("/", maxsplit=1)
        parsed = Decimal(numerator), Decimal(denominator)
    except (InvalidOperation, ValueError):
        return None
    if not parsed[0].is_finite() or not parsed[1].is_finite() or parsed[0] <= 0 or parsed[1] <= 0:
        return None
    return parsed


def parse_timing(line: str, ordinal: int, time_base: tuple[Decimal, Decimal]) -> FrameTiming | None:
    fields = line.split(",", maxsplit=5)
    if len(fields) < _FRAME_MD5_MIN_FIELDS or fields[0].strip() != "0":
        return None
    try:
        pts = Decimal(fields[2].strip())
    except InvalidOperation:
        return None
    local_pts = pts * time_base[0] / time_base[1]
    if not local_pts.is_finite() or local_pts < 0:
        return None
    return FrameTiming(ordinal, local_pts)


def select_adjacent(
    previous: FrameTiming | None,
    current: FrameTiming,
    target: float,
    _policy: FrameSelectionPolicy,
) -> FrameTiming | None:
    """Select the first determinable nearest frame, preserving earlier ties."""
    target_decimal = Decimal(str(target))
    if current.local_pts_seconds < target_decimal:
        if previous is not None and current.local_pts_seconds < previous.local_pts_seconds:
            raise ReferenceFrameDecodeError
        return None
    if previous is None:
        return current
    if current.local_pts_seconds < previous.local_pts_seconds:
        raise ReferenceFrameDecodeError
    return min(
        (previous, current),
        key=lambda candidate: (
            abs(candidate.local_pts_seconds - target_decimal),
            candidate.local_pts_seconds,
        ),
    )


def selection_warnings(
    previous: FrameTiming | None, selected: FrameTiming, target: float
) -> tuple[str, ...]:
    target_decimal = Decimal(str(target))
    if previous is None and selected.local_pts_seconds >= target_decimal:
        return (_ONLY_AFTER_WARNING,)
    if selected.local_pts_seconds < target_decimal:
        return (_ONLY_BEFORE_WARNING,)
    return ()


def candidate_path(directory: Path, ordinal: int) -> Path:
    return directory / f"candidate-{ordinal:08d}.jpg"


def require_candidate_file(directory: Path, ordinal: int) -> None:
    path = candidate_path(directory, ordinal)
    if not path.is_file() or path.stat().st_size == 0:
        raise ReferenceFrameDecodeError


def publish_candidate(source: Path, destination: Path) -> None:
    try:
        _ = source.replace(destination)
    except OSError:
        remove_partial(destination)
        raise ReferenceFrameDecodeError from None


def stop_process(process: DirectProcess, shutdown_grace: float) -> None:
    """Terminate one owned process, then kill only after its bounded grace period."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        _ = process.wait(shutdown_grace)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            _ = process.wait(shutdown_grace)
        except (OSError, subprocess.TimeoutExpired):
            return
    except OSError:
        return


def join_readers(*readers: Thread) -> None:
    for reader in readers:
        reader.join()


def close_pipes(process: DirectProcess) -> None:
    """Close both process pipes after their readers have exited."""
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def remove_partial(path: Path) -> None:
    """Remove only an invocation-owned incomplete direct JPEG."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return
