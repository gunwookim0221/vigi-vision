"""FFmpeg and ffprobe boundary for credential-free local replay decoding."""

import json
import subprocess
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import ClassVar, Final, Protocol, final

from pydantic import BaseModel, ConfigDict, ValidationError
from typing_extensions import override

from vigi_vision.reference_frame_models import (
    DecodedFrameEvidence,
    FrameSelectionPolicy,
    ReferenceFrameDecodeError,
    ReferenceFrameNoCandidateError,
    TimingPrecisionStatus,
)

_TOOL_TIMEOUT_SECONDS: Final = 15.0
_SOURCE_MAPPING_WARNING: Final = (
    "Source timestamp mapping is unavailable pending real-NVR replay validation."
)
_ONLY_BEFORE_WARNING: Final = (
    "Only decoded frames before the requested clip position were available."
)
_ONLY_AFTER_WARNING: Final = "Only decoded frames after the requested clip position were available."
DecoderRunner = Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]]


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameDecodeTimeoutError(ReferenceFrameDecodeError):
    """Raised when bounded local frame probing or decoding exceeds its timeout."""

    @override
    def __str__(self) -> str:
        return "Timed out while decoding a reference frame from the replay clip."


@dataclass(frozen=True, slots=True)
class DecodedFrameCandidate:
    """One ffprobe-reported clip-relative video frame suitable for selection."""

    local_pts_seconds: Decimal
    index: int


@dataclass(frozen=True, slots=True)
class ReferenceFrameDecodeRequest:
    """Local replay input and output facts for a single frame-decoder invocation."""

    clip_path: Path = field(repr=False)
    target_offset_seconds: float
    policy: FrameSelectionPolicy
    output_path: Path = field(repr=False)


class ReferenceFrameDecoder(Protocol):
    """Decode and write one policy-selected JPEG from a local bounded replay clip."""

    def decode(self, request: ReferenceFrameDecodeRequest) -> DecodedFrameEvidence:
        """Write a selected JPEG and return only credential-safe frame facts."""
        ...


class _ProbeFrame(BaseModel):
    """Minimal ffprobe frame payload used for local timestamp selection."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    best_effort_timestamp_time: str | None = None
    pkt_dts_time: str | None = None
    pkt_pts_time: str | None = None


class _ProbeStream(BaseModel):
    """Minimal ffprobe video-stream dimension payload."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    width: int
    height: int
    codec_name: str | None = None


class _ProbeDocument(BaseModel):
    """Validated subset of the local ffprobe JSON document."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    frames: tuple[_ProbeFrame, ...] = ()
    streams: tuple[_ProbeStream, ...]


def _run_tool(
    arguments: tuple[str, ...], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # Fixed executable and tuple arguments; no shell interpolation.
        arguments,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=timeout_seconds,
    )


@final
@dataclass(frozen=True, slots=True)
class FfmpegReferenceFrameDecoder:
    """Select one nearest local frame using ffprobe PTS then ffmpeg extraction."""

    ffmpeg: Path = field(repr=False)
    ffprobe: Path = field(repr=False)
    probe_runner: DecoderRunner = field(default=_run_tool, repr=False)
    extract_runner: DecoderRunner = field(default=_run_tool, repr=False)

    def decode(self, request: ReferenceFrameDecodeRequest) -> DecodedFrameEvidence:
        """Write the nearest policy-compliant JPEG from one local replay clip."""
        candidates, width, height = self._probe(request.clip_path)
        selected = select_nearest_candidate(
            candidates, request.target_offset_seconds, request.policy
        )
        try:
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ReferenceFrameDecodeError from None
        try:
            completed = self.extract_runner(
                self._extract_arguments(request.clip_path, selected.index, request.output_path),
                _TOOL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            _remove_partial_output(request.output_path)
            raise ReferenceFrameDecodeTimeoutError from None
        except OSError:
            _remove_partial_output(request.output_path)
            raise ReferenceFrameDecodeError from None
        if completed.returncode != 0:
            _remove_partial_output(request.output_path)
            raise ReferenceFrameDecodeError
        try:
            self._validate_jpeg(request.output_path, width, height)
        except ReferenceFrameDecodeError:
            _remove_partial_output(request.output_path)
            raise
        return DecodedFrameEvidence(
            jpeg_path=request.output_path,
            local_pts_seconds=float(selected.local_pts_seconds),
            width=width,
            height=height,
            timing_precision_status=TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
            warnings=(
                _SOURCE_MAPPING_WARNING,
                *_candidate_warnings(candidates, request.target_offset_seconds),
            ),
        )

    def _probe(self, clip_path: Path) -> tuple[tuple[DecodedFrameCandidate, ...], int, int]:
        try:
            completed = self.probe_runner(self._probe_arguments(clip_path), _TOOL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise ReferenceFrameDecodeTimeoutError from None
        except OSError:
            raise ReferenceFrameDecodeError from None
        if completed.returncode != 0:
            raise ReferenceFrameDecodeError
        try:
            document = _ProbeDocument.model_validate_json(completed.stdout)
            stream = document.streams[0]
            candidates = _candidates(document.frames)
        except (IndexError, ValidationError, ValueError, json.JSONDecodeError):
            raise ReferenceFrameDecodeError from None
        if stream.width <= 0 or stream.height <= 0:
            raise ReferenceFrameDecodeError
        if not candidates:
            raise ReferenceFrameNoCandidateError
        if any(
            current.local_pts_seconds < prior.local_pts_seconds
            for prior, current in pairwise(candidates)
        ):
            raise ReferenceFrameDecodeError
        return candidates, stream.width, stream.height

    def _validate_jpeg(self, jpeg_path: Path, width: int, height: int) -> None:
        try:
            completed = self.probe_runner(
                self._jpeg_probe_arguments(jpeg_path), _TOOL_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            raise ReferenceFrameDecodeTimeoutError from None
        except OSError:
            raise ReferenceFrameDecodeError from None
        if completed.returncode != 0:
            raise ReferenceFrameDecodeError
        try:
            document = _ProbeDocument.model_validate_json(completed.stdout)
            stream = document.streams[0]
        except (IndexError, ValidationError, ValueError, json.JSONDecodeError):
            raise ReferenceFrameDecodeError from None
        if stream.codec_name != "mjpeg" or stream.width != width or stream.height != height:
            raise ReferenceFrameDecodeError

    def _probe_arguments(self, clip_path: Path) -> tuple[str, ...]:
        return (
            str(self.ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "stream=width,height:frame=best_effort_timestamp_time,pkt_dts_time,pkt_pts_time",
            "-of",
            "json",
            str(clip_path),
        )

    def _jpeg_probe_arguments(self, jpeg_path: Path) -> tuple[str, ...]:
        return (
            str(self.ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height",
            "-of",
            "json",
            str(jpeg_path),
        )

    def _extract_arguments(
        self, clip_path: Path, frame_index: int, output_path: Path
    ) -> tuple[str, ...]:
        return (
            str(self.ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(clip_path),
            "-map",
            "0:v:0",
            "-vf",
            f"select=eq(n\\,{frame_index})",
            "-frames:v",
            "1",
            "-q:v",
            "5",
            "-an",
            str(output_path),
        )


def select_nearest_candidate(
    candidates: tuple[DecodedFrameCandidate, ...],
    target_offset_seconds: float,
    policy: FrameSelectionPolicy,
) -> DecodedFrameCandidate:
    """Select the nearest candidate, resolving exact ties toward the earlier frame."""
    match policy:
        case FrameSelectionPolicy.NEAREST_DECODED_FRAME:
            if not candidates:
                raise ReferenceFrameNoCandidateError
            target = Decimal(str(target_offset_seconds))
            return min(
                candidates,
                key=lambda candidate: (
                    abs(candidate.local_pts_seconds - target),
                    candidate.local_pts_seconds,
                ),
            )


def _candidates(frames: tuple[_ProbeFrame, ...]) -> tuple[DecodedFrameCandidate, ...]:
    candidates: list[DecodedFrameCandidate] = []
    for index, frame in enumerate(frames):
        value = frame.best_effort_timestamp_time or frame.pkt_pts_time or frame.pkt_dts_time
        if value is None:
            continue
        try:
            timestamp = Decimal(value)
        except InvalidOperation:
            raise ReferenceFrameDecodeError from None
        if not timestamp.is_finite() or timestamp < 0:
            raise ReferenceFrameDecodeError
        candidates.append(DecodedFrameCandidate(timestamp, index))
    return tuple(candidates)


def _candidate_warnings(
    candidates: tuple[DecodedFrameCandidate, ...], target_offset_seconds: float
) -> tuple[str, ...]:
    target = Decimal(str(target_offset_seconds))
    if all(candidate.local_pts_seconds < target for candidate in candidates):
        return (_ONLY_BEFORE_WARNING,)
    if all(candidate.local_pts_seconds > target for candidate in candidates):
        return (_ONLY_AFTER_WARNING,)
    return ()


def _remove_partial_output(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)
