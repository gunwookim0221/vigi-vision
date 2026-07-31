"""Reference-frame-only direct JPEG acquisition from a bounded NVR replay."""
# ruff: noqa: D102

import queue
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import Final, Protocol, final

from pydantic import SecretStr

from vigi_vision.reference_frame_decoder import ReferenceFrameDecodeTimeoutError
from vigi_vision.reference_frame_direct_support import (
    DirectProcess,
    DirectProcessFactory,
    DirectReferenceFrameRequest,
    FrameTiming,
    ProbeRunner,
    candidate_path,
    close_pipes,
    drain_discard,
    drain_timing,
    join_readers,
    publish_candidate,
    remove_partial,
    require_candidate_file,
    run_probe,
    select_adjacent,
    selection_warnings,
    spawn_process,
    stop_process,
    validate_jpeg,
)
from vigi_vision.reference_frame_models import (
    DecodedFrameEvidence,
    ReferenceFrameDecodeError,
    ReferenceFrameNoCandidateError,
    TimingPrecisionStatus,
)
from vigi_vision.replay import authenticated_replay_url

_STARTUP_ALLOWANCE_SECONDS: Final = 30.0
_SHUTDOWN_GRACE_SECONDS: Final = 2.0
_POLL_SECONDS: Final = 0.05
_TIMING_QUEUE_SIZE: Final = 8
_SOURCE_MAPPING_WARNING: Final = (
    "Source timestamp mapping is unavailable pending real-NVR replay validation."
)


class DirectReferenceFrameAcquisitionBoundary(Protocol):
    """Acquire one directly decoded reference frame into service-owned staging."""

    def acquire(self, request: DirectReferenceFrameRequest) -> DecodedFrameEvidence: ...


@final
@dataclass(frozen=True, slots=True)
class FfmpegDirectReferenceFrameAcquirer:
    """Acquire one validated, deterministically selected JPEG before later media stalls."""

    ffmpeg: Path = field(repr=False)
    ffprobe: Path = field(repr=False)
    username: str = field(repr=False)
    password: SecretStr = field(repr=False)
    process_factory: DirectProcessFactory = field(default=spawn_process, repr=False)
    probe_runner: ProbeRunner = field(default=run_probe, repr=False)

    def acquire(self, request: DirectReferenceFrameRequest) -> DecodedFrameEvidence:
        try:
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ReferenceFrameDecodeError from None
        with tempfile.TemporaryDirectory(
            prefix="vigi-reference-direct-", dir=request.output_path.parent
        ) as raw:
            return self._acquire(request, Path(raw))

    def _acquire(
        self, request: DirectReferenceFrameRequest, candidate_directory: Path
    ) -> DecodedFrameEvidence:
        try:
            process = self.process_factory(self._arguments(request), candidate_directory)
        except OSError:
            raise ReferenceFrameDecodeError from None
        if process.stdout is None or process.stderr is None:
            stop_process(process, _SHUTDOWN_GRACE_SECONDS)
            close_pipes(process)
            raise ReferenceFrameDecodeError
        records: queue.Queue[FrameTiming] = queue.Queue(maxsize=_TIMING_QUEUE_SIZE)
        reader_fault = Event()
        timing_reader = Thread(
            target=drain_timing,
            args=(process.stdout, records, reader_fault),
            name="vigi-reference-timing",
        )
        stderr_reader = Thread(
            target=drain_discard,
            args=(process.stderr,),
            name="vigi-reference-stderr",
        )
        timing_reader.start()
        stderr_reader.start()
        completed = False
        try:
            state = _SelectionState(process, records, reader_fault, timing_reader)
            selected, previous = self._select(request, candidate_directory, state)
            selected_path = candidate_path(candidate_directory, selected.ordinal)
            publish_candidate(selected_path, request.output_path)
            width, height = validate_jpeg(
                self.probe_runner, self.ffprobe, request.output_path, _SHUTDOWN_GRACE_SECONDS
            )
            stop_process(process, _SHUTDOWN_GRACE_SECONDS)
            completed = True
            return DecodedFrameEvidence(
                request.output_path,
                float(selected.local_pts_seconds),
                width,
                height,
                TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
                (
                    _SOURCE_MAPPING_WARNING,
                    *selection_warnings(previous, selected, request.target_offset_seconds),
                ),
            )
        finally:
            stop_process(process, _SHUTDOWN_GRACE_SECONDS)
            join_readers(timing_reader, stderr_reader)
            close_pipes(process)
            if not completed:
                remove_partial(request.output_path)

    def _select(
        self,
        request: DirectReferenceFrameRequest,
        candidate_directory: Path,
        state: "_SelectionState",
    ) -> tuple[FrameTiming, FrameTiming | None]:
        previous: FrameTiming | None = None
        deadline = (
            monotonic()
            + request.replay_request.window.duration_seconds
            + _STARTUP_ALLOWANCE_SECONDS
        )
        while True:
            if state.reader_fault.is_set():
                raise ReferenceFrameDecodeError
            if monotonic() >= deadline:
                raise ReferenceFrameDecodeTimeoutError
            try:
                current = state.records.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                if _process_running(state.process) or state.timing_reader.is_alive():
                    continue
                if previous is None:
                    raise ReferenceFrameNoCandidateError from None
                return previous, previous
            require_candidate_file(candidate_directory, current.ordinal)
            selected = select_adjacent(
                previous, current, request.target_offset_seconds, request.policy
            )
            if selected is not None:
                return selected, previous
            previous = current

    def _arguments(self, request: DirectReferenceFrameRequest) -> tuple[str, ...]:
        replay_request = request.replay_request
        authenticated_url = authenticated_replay_url(
            replay_request.replay_url, self.username, self.password.get_secret_value()
        )
        return (
            str(self.ffmpeg),
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
            str(replay_request.window.duration_seconds),
            "-vf",
            "setpts=PTS-STARTPTS",
            "-vsync",
            "0",
            "-enc_time_base",
            "-1",
            "-c:v",
            "mjpeg",
            "-q:v",
            "5",
            "-an",
            "-f",
            "tee",
            "[f=image2:atomic_writing=1:start_number=0]candidate-%08d.jpg|[f=framemd5]pipe\\:1",
        )


def _process_running(process: DirectProcess) -> bool:
    return process.poll() is None


@dataclass(frozen=True, slots=True)
class _SelectionState:
    process: DirectProcess
    records: queue.Queue[FrameTiming]
    reader_fault: Event
    timing_reader: Thread
