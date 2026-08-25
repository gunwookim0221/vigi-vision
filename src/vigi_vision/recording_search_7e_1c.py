# pyright: reportAny=false, reportExplicitAny=false, reportUnknownParameterType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportArgumentType=false, reportInvalidTypeForm=false, reportOptionalMemberAccess=false, reportUnnecessaryIsInstance=false, reportCallInDefaultInitializer=false, reportUnusedImport=false, reportUnusedFunction=false
# ruff: noqa: B009, C901, D105, I001, PLR0912, PLR0913, PTH105, RUF022, TC001, TC006, TRY300, UP037
"""Phase 7E-1C common-session acquisition and local evidence admission.

The 1C boundary owns one bounded replay/remux and all subsequent local reads of
that retained media.  It deliberately exposes small protocols so automated
tests can use deterministic media fakes without changing the production
RecordingPlanner, ReplayExtractor, or B4 contracts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from itertools import pairwise
from tempfile import TemporaryDirectory, mkstemp
from pathlib import Path
from time import monotonic
from typing import Any, Protocol, cast

from vigi_vision.investigation_confirmation_integrity import (
    compute_jpeg_integrity_from_bytes,
)
from vigi_vision.investigation_confirmation_models import ConfirmationArtifactError
from vigi_vision.recording import (
    RecordingSegment,
    RecordingUnavailableError,
    RecordingWindow,
    ReplayRequest,
)
from vigi_vision.recording_search_7e_models import (
    Schema5PhaseState,
    Schema6TargetState,
    StrictIdentityEnvelope,
)
from vigi_vision.recording_search_7e_repository import (
    Phase7ERun,
    RecordingSearch7ERepository,
)
from vigi_vision.recording_search_7e_validation import (
    Schema5Envelope,
    Schema6Envelope,
)
from vigi_vision.recording_search_models import RecordingSearchError
from vigi_vision.replay import (
    ReplayAuthenticationError,
    ReplayClip,
    ReplayError,
    ReplayUnavailableError,
    ReplayTimeoutError,
)

DEFAULT_SEARCH_DURATION_SECONDS = 300
MAX_SEARCH_DURATION_SECONDS = 600
REPLAY_MARGIN_SECONDS = 40
CLEANUP_RESERVE_SECONDS = 60
MAX_MP4_BYTES = 4_294_967_296
MAX_SELECTED_RGB24_FRAMES = 12
MAX_TARGETS_PER_DECODER_PASS = 32
MAX_DECODER_PASSES = 11
DECODER_TIMEOUT_SECONDS = 120
MEDIA_PROBE_TIMEOUT_SECONDS = 20


class CommonSessionError(RecordingSearchError):
    """Safe base error for the 1C boundary."""

    code = "unexpected_error"

    def __str__(self) -> str:
        return self.code


class CommonSessionValidationError(CommonSessionError):
    """The request or observed media is outside the approved contract."""

    code = "invalid_request"


class CommonSessionRecordingUnavailableError(CommonSessionError):
    """No single SDK segment covers the complete half-open window."""

    code = "recording_unavailable"


class CommonSessionReplayTimeoutError(CommonSessionError):
    """The bounded replay/remux exceeded its operation deadline."""

    code = "replay_timeout"


class CommonSessionReplayError(CommonSessionError):
    """Replay/remux failed without exposing native diagnostics."""

    code = "replay_failed"


class CommonSessionReplayAuthenticationError(CommonSessionReplayError):
    """The replay server rejected authentication."""

    code = "replay_authentication_failed"


class CommonSessionMediaError(CommonSessionError):
    """The retained MP4 failed confinement, size, or media validation."""

    code = "media_probe_failed"


class CommonSessionDecoderTimeoutError(CommonSessionError):
    """A local decoder pass exceeded its bounded deadline."""

    code = "decoder_timeout"


class CommonSessionDecoderError(CommonSessionError):
    """A local decoder pass failed safely."""

    code = "decoder_failed"


class CommonSessionDeadlineError(CommonSessionError):
    """The invocation deadline leaves no safe blocking-operation budget."""

    code = "invocation_deadline_exhausted"


class CommonSessionCapacityError(CommonSessionError):
    """A bounded target/frame/pass capacity was exceeded."""

    code = "capacity_exhausted"


class CommonSessionCleanupError(CommonSessionError):
    """Invocation-owned replay media could not be removed safely."""

    code = "cleanup_failed"


class CommonSessionCancelledError(CommonSessionError):
    """The caller cancelled the current acquisition boundary."""

    code = "interrupted"


@dataclass(frozen=True, slots=True)
class CommonSessionPolicy:
    """Validated resource/deadline ceilings for one request-relative session."""

    default_search_duration_seconds: int = DEFAULT_SEARCH_DURATION_SECONDS
    maximum_search_duration_seconds: int = MAX_SEARCH_DURATION_SECONDS
    replay_margin_seconds: int = REPLAY_MARGIN_SECONDS
    cleanup_reserve_seconds: int = CLEANUP_RESERVE_SECONDS
    invocation_deadline_seconds: int = 2_520
    maximum_mp4_bytes: int = MAX_MP4_BYTES
    maximum_process_memory_bytes: int = 2_147_483_648
    maximum_selected_rgb24_frames: int = MAX_SELECTED_RGB24_FRAMES
    maximum_targets_per_decoder_pass: int = MAX_TARGETS_PER_DECODER_PASS
    maximum_decoder_passes: int = MAX_DECODER_PASSES
    maximum_classifications: int = 32
    decoder_timeout_seconds: int = DECODER_TIMEOUT_SECONDS
    ffprobe_timeout_seconds: int = MEDIA_PROBE_TIMEOUT_SECONDS
    support_cadence_seconds: int = 1

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CommonSessionPolicy":
        """Read the approved policy fields without coercing caller values."""
        values: dict[str, int] = {}
        names = {
            "default_search_duration_seconds",
            "maximum_search_duration_seconds",
            "replay_margin_seconds",
            "cleanup_reserve_seconds",
            "invocation_deadline_seconds",
            "maximum_mp4_bytes",
            "maximum_process_memory_bytes",
            "maximum_selected_rgb24_frames",
            "maximum_targets_per_decoder_pass",
            "maximum_decoder_passes",
            "maximum_classifications",
            "decoder_timeout_seconds",
            "ffprobe_timeout_seconds",
            "support_cadence_seconds",
        }
        for name in names:
            if name in payload:
                value = payload[name]
                if type(value) is not int:
                    raise CommonSessionValidationError
                values[name] = value
        result = cls(**values)
        result.validate()
        return result

    def validate(self) -> None:
        """Reject non-positive, unbounded, or internally inconsistent limits."""
        fields = (
            self.maximum_search_duration_seconds,
            self.replay_margin_seconds,
            self.cleanup_reserve_seconds,
            self.invocation_deadline_seconds,
            self.maximum_mp4_bytes,
            self.maximum_process_memory_bytes,
            self.maximum_selected_rgb24_frames,
            self.maximum_targets_per_decoder_pass,
            self.maximum_decoder_passes,
            self.maximum_classifications,
            self.decoder_timeout_seconds,
            self.ffprobe_timeout_seconds,
            self.support_cadence_seconds,
        )
        if any(type(value) is not int or value <= 0 for value in fields):
            raise CommonSessionValidationError
        if (
            self.default_search_duration_seconds <= 0
            or self.default_search_duration_seconds > self.maximum_search_duration_seconds
        ):
            raise CommonSessionValidationError
        if self.maximum_search_duration_seconds > MAX_SEARCH_DURATION_SECONDS:
            raise CommonSessionValidationError
        if self.invocation_deadline_seconds <= self.cleanup_reserve_seconds:
            raise CommonSessionValidationError


def _utc_second(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise CommonSessionValidationError
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CommonSessionRequest:
    """One request-relative half-open common-session interval."""

    investigation_id: str
    run_id: str
    channel_id: int
    start_utc: datetime
    end_utc: datetime
    policy: CommonSessionPolicy = field(default_factory=CommonSessionPolicy)

    def __post_init__(self) -> None:
        """Validate exact whole-second interval and the hard 600-second cap."""
        self.policy.validate()
        if (
            not self.investigation_id
            or not self.run_id
            or "\0" in self.investigation_id + self.run_id
        ):
            raise CommonSessionValidationError
        if type(self.channel_id) is not int or self.channel_id <= 0:
            raise CommonSessionValidationError
        start = _utc_second(self.start_utc)
        end = _utc_second(self.end_utc)
        if start != self.start_utc or end != self.end_utc:
            raise CommonSessionValidationError
        duration = (end - start).total_seconds()
        if type(duration) is not float or not duration.is_integer() or duration <= 0:
            raise CommonSessionValidationError
        if int(duration) > self.policy.maximum_search_duration_seconds:
            raise CommonSessionValidationError

    @property
    def duration_seconds(self) -> int:
        """Return the exact client-side duration limit."""
        return int((self.end_utc - self.start_utc).total_seconds())

    @classmethod
    def from_start_and_duration(
        cls,
        investigation_id: str,
        run_id: str,
        channel_id: int,
        start_utc: datetime,
        duration_seconds: int | None = None,
        policy: CommonSessionPolicy | None = None,
    ) -> "CommonSessionRequest":
        """Build a request using the policy's five-minute default."""
        selected_policy = policy or CommonSessionPolicy()
        duration = (
            selected_policy.default_search_duration_seconds
            if duration_seconds is None
            else duration_seconds
        )
        if type(duration) is not int:
            raise CommonSessionValidationError
        return cls(
            investigation_id,
            run_id,
            channel_id,
            start_utc,
            _utc_second(start_utc) + timedelta(seconds=duration),
            selected_policy,
        )


@dataclass(frozen=True, slots=True)
class MediaProbeFacts:
    """Strict structural facts observed from one retained MP4."""

    selected_video_stream_index: int
    video_stream_count: int
    audio_stream_count: int
    container_start_pts: int
    time_base_num: int
    time_base_den: int
    duration_ticks: int
    codec: str = ""
    profile: str = ""
    pixel_format: str = ""
    width: int = 0
    height: int = 0
    average_frame_rate_num: int = 0
    average_frame_rate_den: int = 1

    def validate(self) -> None:
        """Require one video stream, no audio, positive reduced timing facts."""
        if (
            type(self.selected_video_stream_index) is not int
            or type(self.video_stream_count) is not int
            or type(self.audio_stream_count) is not int
            or self.selected_video_stream_index < 0
            or self.video_stream_count != 1
            or self.audio_stream_count != 0
            or self.container_start_pts < 0
            or self.time_base_num <= 0
            or self.time_base_den <= 0
            or math.gcd(self.time_base_num, self.time_base_den) != 1
            or self.duration_ticks <= 0
            or self.width <= 0
            or self.height <= 0
            or self.average_frame_rate_num <= 0
            or self.average_frame_rate_den <= 0
        ):
            raise CommonSessionMediaError


class MediaProbe(Protocol):
    """Probe one local retained media path within a caller-supplied timeout."""

    def probe(self, path: Path, timeout_seconds: float) -> MediaProbeFacts:
        """Return strict structural media facts."""
        ...


class RecordingPlannerBoundary(Protocol):
    """The existing planner methods consumed by the 1C adapter."""

    def find_covering_segment(self, channel_id: int, instant_utc: datetime) -> RecordingSegment:
        """Find one segment covering the requested start."""
        ...

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        """Build the bounded replay request for that segment."""
        ...


class Decoder(Protocol):
    """Decode selected targets from one retained common-session MP4."""

    def decode(
        self,
        session: "CommonSessionAcquisition",
        targets: tuple[datetime, ...],
        timeout_seconds: float,
    ) -> tuple["DecodedLocalFrame", ...]:
        """Return one result per target in request order."""
        ...


class Classifier(Protocol):
    """Adapt the existing B4 classifier without changing its semantics."""

    def classify(self, frame: "DecodedLocalFrame", target: object) -> object:
        """Return a production ClassificationOperation or a safe failure."""
        ...


@dataclass(frozen=True, slots=True)
class DecodedLocalFrame:
    """One deterministic local frame and its exact source/media bytes."""

    requested_time_utc: datetime
    raw_pts: int
    ordinal: int
    width: int
    height: int
    rgb24_bytes: bytes = field(repr=False)
    jpeg_bytes: bytes = field(repr=False)
    decoder_operation_id: str = ""
    decode_session_id: str = ""
    container_start_pts: int = 0
    time_base_num: int = 1
    time_base_den: int = 1

    @property
    def rgb24_sha256(self) -> str:
        """Return the approved digest over row-major interleaved RGB24 bytes."""
        return hashlib.sha256(self.rgb24_bytes).hexdigest()

    @property
    def decoded_offset(self) -> Fraction:
        """Return exact request-relative offset without floating-point arithmetic."""
        return Fraction(
            (self.raw_pts - self.container_start_pts) * self.time_base_num,
            self.time_base_den,
        )

    def validate(self, *, max_rgb24_frames: int = MAX_SELECTED_RGB24_FRAMES) -> None:
        """Validate dimensions, stride-free RGB24 layout, and monotonic facts."""
        if (
            type(self.raw_pts) is not int
            or self.raw_pts < 0
            or type(self.ordinal) is not int
            or self.ordinal < 0
            or type(self.width) is not int
            or type(self.height) is not int
            or self.width <= 0
            or self.height <= 0
            or type(self.time_base_num) is not int
            or type(self.time_base_den) is not int
            or self.time_base_num <= 0
            or self.time_base_den <= 0
            or math.gcd(self.time_base_num, self.time_base_den) != 1
            or len(self.rgb24_bytes) != self.width * self.height * 3
            or not self.jpeg_bytes
            or max_rgb24_frames <= 0
        ):
            raise CommonSessionDecoderError
        if self.decoded_offset < 0:
            raise CommonSessionDecoderError


@dataclass(frozen=True, slots=True)
class CommonSessionAcquisition:
    """Successful one-replay common session; the caller owns clip cleanup."""

    request: CommonSessionRequest
    segment: RecordingSegment
    replay_request: ReplayRequest
    replay_clip: ReplayClip
    media: MediaProbeFacts
    session: StrictIdentityEnvelope
    retained_mp4_path: Path | None = field(default=None, repr=False)

    @property
    def common_session_id(self) -> str:
        """Return the immutable common-session identity."""
        return self.session.identity

    def remove(self) -> None:
        """Remove the invocation-owned retained MP4."""
        try:
            self.replay_clip.remove()
        except OSError as exc:
            raise CommonSessionCleanupError from exc

    @property
    def media_path(self) -> Path:
        """Return the durable MP4 when admitted, otherwise the replay temp path."""
        return self.retained_mp4_path or self.replay_clip.temporary_mp4_path


@dataclass(frozen=True, slots=True)
class DurableCommonSessionMedia:
    """Atomically retain one validated MP4 outside the immutable run tree."""

    root: Path

    def publish(self, acquisition: CommonSessionAcquisition) -> CommonSessionAcquisition:
        """Copy, fsync, read back, and atomically publish the invocation's MP4."""
        source = acquisition.replay_clip.temporary_mp4_path
        final_directory = (
            self.root / acquisition.request.investigation_id / acquisition.request.run_id
        )
        final = final_directory / f"{acquisition.common_session_id}.mp4"
        temporary: Path | None = None
        try:
            _validate_media_root(self.root)
            final_directory.mkdir(parents=True, exist_ok=True)
            if not _is_safe_child(self.root, final_directory):
                raise CommonSessionMediaError
            if source.is_symlink() or not source.is_file():
                raise CommonSessionMediaError
            source_size = source.stat().st_size
            source_digest = _sha256_file(source)
            if final.exists():
                if final.is_symlink() or not final.is_file():
                    raise CommonSessionMediaError
                if final.stat().st_size != source_size or _sha256_file(final) != source_digest:
                    raise CommonSessionMediaError
            else:
                descriptor, temporary_name = mkstemp(
                    prefix=f".{acquisition.common_session_id}-",
                    suffix=".tmp",
                    dir=final_directory,
                )
                os.close(descriptor)
                temporary = Path(temporary_name)
                with source.open("rb") as source_stream, temporary.open("wb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                    target_stream.flush()
                    os.fsync(target_stream.fileno())
                if (
                    temporary.stat().st_size != source_size
                    or _sha256_file(temporary) != source_digest
                ):
                    raise CommonSessionMediaError
                os.replace(temporary, final)
                temporary = None
            if final.stat().st_size != source_size or _sha256_file(final) != source_digest:
                raise CommonSessionMediaError
            return CommonSessionAcquisition(
                acquisition.request,
                acquisition.segment,
                acquisition.replay_request,
                acquisition.replay_clip,
                acquisition.media,
                acquisition.session,
                final,
            )
        except (OSError, RuntimeError) as exc:
            raise CommonSessionMediaError from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    raise CommonSessionCleanupError from exc


@dataclass(frozen=True, slots=True)
class FfprobeMediaProbe:
    """Strict ffprobe JSON adapter for a retained MP4."""

    executable: Path
    runner: Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]] = field(
        default=lambda args, timeout: subprocess.run(  # noqa: S603
            args,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        ),
        repr=False,
    )

    def probe(self, path: Path, timeout_seconds: float) -> MediaProbeFacts:
        """Read only safe stream/format metadata from the local file."""
        try:
            completed = self.runner(
                (
                    str(self.executable),
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(path),
                ),
                timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommonSessionMediaError from exc
        except OSError as exc:
            raise CommonSessionMediaError from exc
        if completed.returncode != 0:
            raise CommonSessionMediaError
        try:
            document = json.loads(completed.stdout)
            streams = document["streams"]
            format_data = document.get("format", {})
            video = [item for item in streams if item.get("codec_type") == "video"]
            audio = [item for item in streams if item.get("codec_type") == "audio"]
            if len(video) != 1:
                raise CommonSessionMediaError
            stream = video[0]
            time_base_num, time_base_den = _fraction_text(stream["time_base"])
            rate_num, rate_den = _fraction_text(stream["avg_frame_rate"])
            duration_ticks = int(stream.get("duration_ts") or format_data["duration_ts"])
            start_pts = int(stream.get("start_pts") or 0)
            facts = MediaProbeFacts(
                selected_video_stream_index=int(stream.get("index", 0)),
                video_stream_count=len(video),
                audio_stream_count=len(audio),
                container_start_pts=start_pts,
                time_base_num=time_base_num,
                time_base_den=time_base_den,
                duration_ticks=duration_ticks,
                codec=str(stream.get("codec_name") or ""),
                profile=str(stream.get("profile") or ""),
                pixel_format=str(stream.get("pix_fmt") or ""),
                width=int(stream["width"]),
                height=int(stream["height"]),
                average_frame_rate_num=rate_num,
                average_frame_rate_den=rate_den,
            )
            facts.validate()
            return facts
        except (CommonSessionError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, CommonSessionError):
                raise
            raise CommonSessionMediaError from exc


@dataclass(frozen=True, slots=True)
class FfmpegLocalDecoder:
    """Decode selected frames from the retained MP4 using bounded local tools."""

    ffmpeg: Path
    ffprobe: Path
    probe_runner: Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]] = field(
        default=lambda args, timeout: subprocess.run(  # noqa: S603
            args,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        ),
        repr=False,
    )

    def decode(
        self,
        session: CommonSessionAcquisition,
        targets: tuple[datetime, ...],
        timeout_seconds: float,
    ) -> tuple[DecodedLocalFrame, ...]:
        """Probe once, select exact candidates, and decode each target locally."""
        try:
            probe = self.probe_runner(
                (
                    str(self.ffprobe),
                    "-v",
                    "error",
                    "-select_streams",
                    f"v:{session.media.selected_video_stream_index}",
                    "-show_frames",
                    "-of",
                    "json",
                    str(session.media_path),
                ),
                timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommonSessionDecoderTimeoutError from exc
        except OSError as exc:
            raise CommonSessionDecoderError from exc
        if probe.returncode != 0:
            raise CommonSessionDecoderError
        try:
            raw_frames = json.loads(probe.stdout)["frames"]
            offsets: list[Fraction] = []
            raw_pts: list[int] = []
            for value in raw_frames:
                timestamp = value.get("best_effort_timestamp_time")
                if timestamp is None:
                    timestamp = value.get("pkt_pts_time") or value.get("pkt_dts_time")
                offset = _seconds_fraction(timestamp)
                offsets.append(offset)
                tick_fraction = offset * session.media.time_base_den / session.media.time_base_num
                if tick_fraction.denominator != 1:
                    raise CommonSessionDecoderError
                raw_pts.append(session.media.container_start_pts + tick_fraction.numerator)
        except (CommonSessionError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, CommonSessionError):
                raise
            raise CommonSessionDecoderError from exc
        if not offsets or any(current < prior for prior, current in pairwise(offsets)):
            raise CommonSessionDecoderError
        with TemporaryDirectory(prefix="vigi-vision-7e1c-") as temporary:
            results: list[DecodedLocalFrame] = []
            for target in targets:
                target_offset = Fraction(
                    int((target - session.request.start_utc).total_seconds()), 1
                )
                index = select_target_index(
                    offsets,
                    target_offset,
                    Fraction(session.request.duration_seconds, 1),
                    logical_end=target == session.request.end_utc,
                    tolerance=Fraction(session.request.policy.support_cadence_seconds, 1),
                )
                jpeg_path = Path(temporary) / f"frame-{index}.jpg"
                try:
                    completed = subprocess.run(  # noqa: S603
                        (
                            str(self.ffmpeg),
                            "-nostdin",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-y",
                            "-i",
                            str(session.media_path),
                            "-map",
                            f"0:{session.media.selected_video_stream_index}",
                            "-vf",
                            f"select=eq(n\\,{index})",
                            "-frames:v",
                            "1",
                            "-q:v",
                            "5",
                            "-an",
                            str(jpeg_path),
                        ),
                        capture_output=True,
                        check=False,
                        stdin=subprocess.DEVNULL,
                        timeout=timeout_seconds,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise CommonSessionDecoderTimeoutError from exc
                except OSError as exc:
                    raise CommonSessionDecoderError from exc
                if completed.returncode != 0:
                    raise CommonSessionDecoderError
                try:
                    jpeg = jpeg_path.read_bytes()
                except OSError as exc:
                    raise CommonSessionDecoderError from exc
                rgb = self._decode_rgb(session, index, timeout_seconds)
                results.append(
                    DecodedLocalFrame(
                        requested_time_utc=target,
                        raw_pts=raw_pts[index],
                        ordinal=index,
                        width=session.media.width,
                        height=session.media.height,
                        rgb24_bytes=rgb,
                        jpeg_bytes=jpeg,
                        decode_session_id=session.common_session_id,
                        container_start_pts=session.media.container_start_pts,
                        time_base_num=session.media.time_base_num,
                        time_base_den=session.media.time_base_den,
                    )
                )
            return tuple(results)

    def _decode_rgb(self, session: CommonSessionAcquisition, index: int, timeout: float) -> bytes:
        """Decode one selected frame to row-major RGB24 bytes."""
        try:
            completed = subprocess.run(  # noqa: S603
                (
                    str(self.ffmpeg),
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(session.media_path),
                    "-map",
                    f"0:{session.media.selected_video_stream_index}",
                    "-vf",
                    f"select=eq(n\\,{index})",
                    "-frames:v",
                    "1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "pipe:1",
                ),
                capture_output=True,
                check=False,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommonSessionDecoderTimeoutError from exc
        except OSError as exc:
            raise CommonSessionDecoderError from exc
        if (
            completed.returncode != 0
            or len(completed.stdout) != session.media.width * session.media.height * 3
        ):
            raise CommonSessionDecoderError
        return bytes(completed.stdout)


@dataclass(frozen=True, slots=True)
class CommonSessionAcquirer:
    """Perform one bounded planner → replay → probe acquisition."""

    recording_planner: RecordingPlannerBoundary
    replay_extractor: object
    media_probe: MediaProbe
    monotonic_clock: Callable[[], float] = monotonic
    cancellation: Callable[[], bool] | None = None

    def locate(self, request: CommonSessionRequest) -> RecordingSegment:
        """Locate the one SDK segment without constructing or extracting replay."""
        request.policy.validate()
        self._check_cancelled()
        try:
            segment = self.recording_planner.find_covering_segment(
                request.channel_id, request.start_utc
            )
        except RecordingUnavailableError as exc:
            raise CommonSessionRecordingUnavailableError from exc
        except (OSError, ValueError, RecordingSearchError) as exc:
            raise CommonSessionRecordingUnavailableError from exc
        if (
            segment.channel_id != request.channel_id
            or segment.start_utc > request.start_utc
            or segment.end_utc < request.end_utc
        ):
            raise CommonSessionRecordingUnavailableError
        return segment

    def acquire(
        self,
        request: CommonSessionRequest,
        *,
        segment: RecordingSegment | None = None,
    ) -> CommonSessionAcquisition:
        """Acquire exactly one replay and retain its MP4 for local consumers."""
        request.policy.validate()
        deadline = self.monotonic_clock() + request.policy.invocation_deadline_seconds
        self._check_budget(deadline, request.policy)
        self._check_cancelled()
        selected_segment = segment or self.locate(request)
        if (
            selected_segment.channel_id != request.channel_id
            or selected_segment.start_utc > request.start_utc
            or selected_segment.end_utc < request.end_utc
        ):
            raise CommonSessionRecordingUnavailableError
        window = RecordingWindow(request.channel_id, request.start_utc, request.end_utc)
        try:
            replay_request = self.recording_planner.plan_for_segment(selected_segment, window)
        except RecordingUnavailableError as exc:
            raise CommonSessionRecordingUnavailableError from exc
        self._check_budget(
            deadline,
            request.policy,
            request.duration_seconds + request.policy.replay_margin_seconds,
        )
        self._check_cancelled()
        clip: ReplayClip | None = None
        try:
            clip = cast(Any, self.replay_extractor).extract(replay_request)
            self._check_cancelled()
            self._validate_retained_clip(clip, request.policy.maximum_mp4_bytes)
            probe_budget = self._remaining(deadline, request.policy)
            if probe_budget <= 0:
                raise CommonSessionDeadlineError
            media = self.media_probe.probe(
                clip.temporary_mp4_path,
                min(float(request.policy.ffprobe_timeout_seconds), probe_budget),
            )
            media.validate()
            observed_duration = Fraction(
                media.duration_ticks * media.time_base_num,
                media.time_base_den,
            )
            if observed_duration < request.duration_seconds:
                raise CommonSessionMediaError
            session_payload = {
                "investigation_id": request.investigation_id,
                "run_id": request.run_id,
                "replay_operation_id": "",  # filled by the admission adapter
                "policy_id": "",  # filled by the admission adapter
                "segment_id": _segment_id(selected_segment),
                "replay_start_requested_time_utc": _whole_text(request.start_utc),
                "replay_end_requested_time_utc": _whole_text(request.end_utc),
                "selected_video_stream_index": media.selected_video_stream_index,
                "container_start_pts": media.container_start_pts,
                "time_base_num": media.time_base_num,
                "time_base_den": media.time_base_den,
                "duration_ticks": media.duration_ticks,
                "mp4_size_bytes": clip.temporary_mp4_path.stat().st_size,
                "mp4_sha256": _sha256_file(clip.temporary_mp4_path),
                "provenance_level": "REQUEST_RELATIVE_ESTIMATE",
                "physical_time_bias": "UNKNOWN_UNBOUNDED",
            }
            # The two server-owned bindings are completed by ``bind_session``.
            return CommonSessionAcquisition(
                request,
                selected_segment,
                replay_request,
                clip,
                media,
                StrictIdentityEnvelope.from_payload("common-session", session_payload),
            )
        except (
            CommonSessionError,
            ReplayTimeoutError,
            ReplayAuthenticationError,
            ReplayUnavailableError,
            ReplayError,
        ) as exc:
            if clip is not None:
                _remove_clip_or_raise(clip)
            if isinstance(exc, CommonSessionError):
                raise
            if isinstance(exc, ReplayTimeoutError):
                raise CommonSessionReplayTimeoutError from exc
            if isinstance(exc, ReplayAuthenticationError):
                raise CommonSessionReplayAuthenticationError from exc
            if isinstance(exc, ReplayUnavailableError):
                raise CommonSessionRecordingUnavailableError from exc
            raise CommonSessionReplayError from exc
        except (OSError, ConfirmationArtifactError, ValueError, TypeError) as exc:
            if clip is not None:
                _remove_clip_or_raise(clip)
            raise CommonSessionMediaError from exc

    def _validate_retained_clip(self, clip: ReplayClip, maximum_bytes: int) -> None:
        """Reject symlinks, non-regular files, escapes, and oversized media."""
        path = clip.temporary_mp4_path
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
                raise CommonSessionMediaError
            if path.stat().st_size > maximum_bytes:
                raise CommonSessionMediaError
        except (OSError, RuntimeError) as exc:
            raise CommonSessionMediaError from exc

    def _check_cancelled(self) -> None:
        if self.cancellation is not None and self.cancellation():
            raise CommonSessionCancelledError

    def _remaining(self, deadline: float, policy: CommonSessionPolicy) -> float:
        return max(0.0, deadline - self.monotonic_clock() - policy.cleanup_reserve_seconds)

    def _check_budget(
        self,
        deadline: float,
        policy: CommonSessionPolicy,
        required_seconds: int = 1,
    ) -> None:
        if self._remaining(deadline, policy) < required_seconds:
            raise CommonSessionDeadlineError


def bind_session(
    acquisition: CommonSessionAcquisition,
    replay_operation_id: str,
    policy_id: str,
) -> CommonSessionAcquisition:
    """Bind server-owned replay/policy IDs and recompute the session identity."""
    payload = dict(acquisition.session.payload)
    payload["replay_operation_id"] = replay_operation_id
    payload["policy_id"] = policy_id
    return CommonSessionAcquisition(
        acquisition.request,
        acquisition.segment,
        acquisition.replay_request,
        acquisition.replay_clip,
        acquisition.media,
        StrictIdentityEnvelope.from_payload("common-session", payload),
        acquisition.retained_mp4_path,
    )


@dataclass(frozen=True, slots=True)
class CommonSessionAdmissionResult:
    """The schema-6 zero-evidence run and the retained local session."""

    run: Phase7ERun
    acquisition: CommonSessionAcquisition


def _schema5_state(
    phase_state: Schema5PhaseState,
    replay_operation_id: str | None,
    reason_code: str | None = None,
) -> Schema5Envelope:
    """Create one matrix-valid schema-5 state envelope."""
    if phase_state is Schema5PhaseState.PLANNED:
        return Schema5Envelope(
            run_state="RUNNING",
            phase_state=phase_state,
            active_replay_operation_id=None,
            reason_code=None,
            attempt_count=0,
        )
    if replay_operation_id is None:
        raise CommonSessionValidationError
    if phase_state is Schema5PhaseState.ACQUISITION_FAILED:
        return Schema5Envelope(
            run_state="FAILED",
            phase_state=phase_state,
            active_replay_operation_id=replay_operation_id,
            reason_code=reason_code or "acquisition_failed",
            attempt_count=1,
        )
    return Schema5Envelope(
        run_state="RUNNING",
        phase_state=phase_state,
        active_replay_operation_id=replay_operation_id,
        reason_code=None,
        attempt_count=1,
    )


@dataclass(frozen=True, slots=True)
class Phase7E1CExecutor:
    """Compose 1B persistence with one 1C acquisition and media admission."""

    repository: RecordingSearch7ERepository
    acquirer: CommonSessionAcquirer
    media_store: DurableCommonSessionMedia

    def execute(
        self,
        request: CommonSessionRequest,
        schema5_manifest: StrictIdentityEnvelope,
        base_records: Sequence[StrictIdentityEnvelope],
        classifier_policy: StrictIdentityEnvelope,
        target_requests: Sequence[StrictIdentityEnvelope],
        *,
        replay_operation: StrictIdentityEnvelope | None = None,
    ) -> CommonSessionAdmissionResult:
        """Persist schema 5, acquire once, and publish zero-evidence schema 6."""
        _validate_executor_inputs(
            request,
            schema5_manifest,
            base_records,
            classifier_policy,
            target_requests,
        )
        planned = _schema5_state(Schema5PhaseState.PLANNED, None)
        self.repository.create_schema5(
            schema5_manifest,
            planned,
            base_records,
            investigation_id=request.investigation_id,
            run_id=request.run_id,
        )
        segment = self.acquirer.locate(request)
        operation = replay_operation or make_replay_envelope(
            request,
            schema5_manifest.payload["policy_id"],
            schema5_manifest.payload["plan_id"],
            segment,
        )
        if operation.family != "replay-operation":
            raise CommonSessionValidationError
        acquiring_records = (*base_records, operation)
        self.repository.admit_schema5(
            request.investigation_id,
            request.run_id,
            schema5_manifest,
            _schema5_state(Schema5PhaseState.ACQUIRING, operation.identity),
            acquiring_records,
        )
        try:
            acquisition = self.acquirer.acquire(request, segment=segment)
            bound = bind_session(
                acquisition,
                operation.identity,
                schema5_manifest.payload["policy_id"],
            )
            retained = self.media_store.publish(bound)
            retained.remove()
            acquired_records = (*base_records, operation)
            self.repository.admit_schema5(
                request.investigation_id,
                request.run_id,
                schema5_manifest,
                _schema5_state(Schema5PhaseState.ACQUIRED, operation.identity),
                acquired_records,
            )
            target_ids = tuple(item.identity for item in target_requests)
            schema6_manifest = make_schema6_manifest(
                request,
                schema5_manifest.identity,
                schema5_manifest.payload["policy_id"],
                classifier_policy.identity,
                schema5_manifest.payload["plan_id"],
                operation.identity,
                retained.common_session_id,
                target_request_ids=target_ids,
            )
            schema6_records = (
                *acquired_records,
                classifier_policy,
                retained.session,
            )
            state = Schema6Envelope(
                run_state="RUNNING",
                target_state=Schema6TargetState.REQUESTED,
                active_target_request_id=target_ids[0] if target_ids else None,
                active_decoder_operation_id=None,
                active_frame_id=None,
                active_classification_attempt_id=None,
                active_classification_operation_id=None,
                active_observation_id=None,
                reason_code=None,
                attempt_count=0,
                predecessor_target_state=None,
            )
            result = self.repository.transition_schema5_to_schema6(
                request.investigation_id,
                request.run_id,
                schema6_manifest,
                state,
                schema6_records,
                expected_schema5_manifest_id=schema5_manifest.identity,
            )
            return CommonSessionAdmissionResult(result.run, retained)
        except CommonSessionError as exc:
            failure_reason = getattr(exc, "code", "acquisition_failed")
            self.repository.admit_schema5(
                request.investigation_id,
                request.run_id,
                schema5_manifest,
                _schema5_state(
                    Schema5PhaseState.ACQUISITION_FAILED,
                    operation.identity,
                    failure_reason,
                ),
                acquiring_records,
            )
            raise


def _validate_executor_inputs(
    request: CommonSessionRequest,
    schema5_manifest: StrictIdentityEnvelope,
    base_records: Sequence[StrictIdentityEnvelope],
    classifier_policy: StrictIdentityEnvelope,
    target_requests: Sequence[StrictIdentityEnvelope],
) -> None:
    """Validate immutable request bindings before creating a run directory."""
    if schema5_manifest.family != "schema5-manifest":
        raise CommonSessionValidationError
    if classifier_policy.family != "classifier-policy":
        raise CommonSessionValidationError
    if any(not isinstance(item, StrictIdentityEnvelope) for item in base_records):
        raise CommonSessionValidationError
    if any(item.family != "target-request" for item in target_requests):
        raise CommonSessionValidationError
    base_target_ids = {item.identity for item in base_records if item.family == "target-request"}
    payload = schema5_manifest.payload
    if (
        payload.get("investigation_id") != request.investigation_id
        or payload.get("run_id") != request.run_id
        or payload.get("policy_id")
        not in {item.identity for item in base_records if item.family == "policy"}
        or payload.get("plan_id")
        not in {item.identity for item in base_records if item.family == "coarse-plan"}
        or base_target_ids != {item.identity for item in target_requests}
        or set(payload.get("coarse_target_request_ids", ()))
        != {item.identity for item in target_requests}
    ):
        raise CommonSessionValidationError


CommonSessionExecutor = Phase7E1CExecutor
CommonSessionPersistenceAdapter = Phase7E1CExecutor


def _schema6_successor_manifest(
    current: StrictIdentityEnvelope,
    **index_additions: str,
) -> StrictIdentityEnvelope:
    """Return a successor manifest with one deterministic index addition."""
    if current.family != "schema6-manifest":
        raise CommonSessionValidationError
    payload = dict(current.payload)
    raw_indexes = payload.get("indexes")
    if not isinstance(raw_indexes, Mapping):
        raise CommonSessionValidationError
    indexes = {key: list(value) for key, value in raw_indexes.items()}
    for key, identity in index_additions.items():
        if key not in indexes or not isinstance(identity, str) or identity in indexes[key]:
            raise CommonSessionValidationError
        indexes[key].append(identity)
    payload["indexes"] = indexes
    return StrictIdentityEnvelope.from_payload("schema6-manifest", payload)


def _as_envelope(value: object, family: str) -> StrictIdentityEnvelope:
    """Coerce one classifier/model completion into a strict envelope."""
    if isinstance(value, StrictIdentityEnvelope):
        envelope = value
    elif callable(getattr(value, "model_dump", None)):
        dump = getattr(value, "model_dump")
        envelope = StrictIdentityEnvelope.from_payload(family, dump(mode="json"))
    elif isinstance(value, Mapping):
        envelope = StrictIdentityEnvelope.from_payload(family, dict(value))
    else:
        raise CommonSessionValidationError
    if envelope.family != family:
        raise CommonSessionValidationError
    return envelope


def make_observation_envelope(
    acquisition: CommonSessionAcquisition,
    classification_operation: StrictIdentityEnvelope,
) -> StrictIdentityEnvelope:
    """Derive the visual observation only from a validated operation payload."""
    if classification_operation.family != "classification-operation":
        raise CommonSessionValidationError
    payload = classification_operation.payload
    if payload.get("result_kind") != "VISUAL":
        raise CommonSessionValidationError
    observation_payload = {
        "investigation_id": acquisition.request.investigation_id,
        "run_id": acquisition.request.run_id,
        "common_session_id": acquisition.common_session_id,
        "classification_operation_id": classification_operation.identity,
        "frame_id": payload.get("frame_id"),
        "target_request_id": payload.get("target_request_id"),
        "classifier_policy_id": payload.get("classifier_policy_id"),
        "outcome": payload.get("outcome"),
        "reason_code": payload.get("reason_code"),
        "classifier_evidence": payload.get("classifier_evidence"),
    }
    return StrictIdentityEnvelope.from_payload("observation", observation_payload)


def admit_frame_then_classify(
    repository: RecordingSearch7ERepository,
    acquisition: CommonSessionAcquisition,
    target_request: StrictIdentityEnvelope,
    decoder_operation: StrictIdentityEnvelope,
    frame: DecodedLocalFrame,
    classifier: Classifier,
    target: object,
    *,
    classification_attempt_id: str,
) -> Phase7ERun:
    """Persist/reopen a frame before invoking B4, then persist its completion."""
    if target_request.family != "target-request" or decoder_operation.family != "decoder-operation":
        raise CommonSessionValidationError
    if not classification_attempt_id:
        raise CommonSessionValidationError
    current = repository.reopen_schema6(
        acquisition.request.investigation_id,
        acquisition.request.run_id,
    )
    if not isinstance(current.state, Schema6Envelope):
        raise CommonSessionValidationError
    frame_envelope = make_frame_envelope(
        acquisition,
        decoder_operation.identity,
        target_request.identity,
        frame,
    )
    if current.state.target_state is not Schema6TargetState.REQUESTED:
        raise CommonSessionValidationError
    decoding_manifest = _schema6_successor_manifest(
        current.manifest,
        decoder_operation_ids=decoder_operation.identity,
    )
    decoding_state = Schema6Envelope(
        run_state="RUNNING",
        target_state=Schema6TargetState.DECODING,
        active_target_request_id=target_request.identity,
        active_decoder_operation_id=decoder_operation.identity,
        active_frame_id=None,
        active_classification_attempt_id=None,
        active_classification_operation_id=None,
        active_observation_id=None,
        reason_code=None,
        attempt_count=1,
        predecessor_target_state=current.state.target_state,
    )
    records = (
        *(record for record in current.records if record.family != "schema5-manifest"),
        decoder_operation,
    )
    repository.admit_schema6(
        acquisition.request.investigation_id,
        acquisition.request.run_id,
        decoding_manifest,
        decoding_state,
        records,
        expected_manifest_id=current.manifest_id,
    )
    ready_manifest = _schema6_successor_manifest(
        decoding_manifest,
        frame_ids=frame_envelope.identity,
    )
    ready_state = Schema6Envelope(
        run_state="RUNNING",
        target_state=Schema6TargetState.FRAME_READY,
        active_target_request_id=target_request.identity,
        active_decoder_operation_id=decoder_operation.identity,
        active_frame_id=frame_envelope.identity,
        active_classification_attempt_id=None,
        active_classification_operation_id=None,
        active_observation_id=None,
        reason_code=None,
        attempt_count=1,
        predecessor_target_state=Schema6TargetState.DECODING,
    )
    repository.admit_schema6(
        acquisition.request.investigation_id,
        acquisition.request.run_id,
        ready_manifest,
        ready_state,
        (*records, frame_envelope),
        expected_manifest_id=decoding_manifest.identity,
        binary_records={frame_envelope.identity: frame.jpeg_bytes},
    )
    repository.reopen_schema6(acquisition.request.investigation_id, acquisition.request.run_id)
    classifying_manifest = ready_manifest
    classifying_state = Schema6Envelope(
        run_state="RUNNING",
        target_state=Schema6TargetState.CLASSIFYING,
        active_target_request_id=target_request.identity,
        active_decoder_operation_id=decoder_operation.identity,
        active_frame_id=frame_envelope.identity,
        active_classification_attempt_id=classification_attempt_id,
        active_classification_operation_id=None,
        active_observation_id=None,
        reason_code=None,
        attempt_count=1,
        predecessor_target_state=Schema6TargetState.FRAME_READY,
    )
    repository.admit_schema6(
        acquisition.request.investigation_id,
        acquisition.request.run_id,
        classifying_manifest,
        classifying_state,
        (*records, frame_envelope),
        expected_manifest_id=ready_manifest.identity,
    )
    completion = _as_envelope(
        classify_after_readback(classifier, frame, target),
        "classification-operation",
    )
    payload = completion.payload
    if (
        payload.get("investigation_id") != acquisition.request.investigation_id
        or payload.get("run_id") != acquisition.request.run_id
        or payload.get("frame_id") != frame_envelope.identity
        or payload.get("target_request_id") != target_request.identity
    ):
        raise CommonSessionValidationError
    observation: StrictIdentityEnvelope | None = None
    visual = payload.get("result_kind") == "VISUAL"
    if visual:
        observation = make_observation_envelope(acquisition, completion)
    elif payload.get("result_kind") != "OPERATIONAL":
        raise CommonSessionValidationError
    final_manifest = _schema6_successor_manifest(
        classifying_manifest,
        classification_operation_ids=completion.identity,
        **({"observation_ids": observation.identity} if observation is not None else {}),
    )
    final_state = Schema6Envelope(
        run_state="RUNNING" if visual else "FAILED",
        target_state=Schema6TargetState.OBSERVED
        if visual
        else Schema6TargetState.CLASSIFICATION_FAILED,
        active_target_request_id=target_request.identity,
        active_decoder_operation_id=decoder_operation.identity,
        active_frame_id=frame_envelope.identity,
        active_classification_attempt_id=None,
        active_classification_operation_id=completion.identity,
        active_observation_id=observation.identity if observation is not None else None,
        reason_code=None if visual else str(payload.get("operational_reason")),
        attempt_count=1,
        predecessor_target_state=Schema6TargetState.CLASSIFYING,
    )
    final_records = (*records, frame_envelope, completion)
    if observation is not None:
        final_records = (*final_records, observation)
    return repository.admit_schema6(
        acquisition.request.investigation_id,
        acquisition.request.run_id,
        final_manifest,
        final_state,
        final_records,
        expected_manifest_id=classifying_manifest.identity,
    ).run


def _segment_id(segment: RecordingSegment) -> str:
    """Use the existing credential-free segment identity convention."""
    return f"segment-{segment.start_utc:%Y%m%dT%H%M%SZ}-{segment.end_utc:%Y%m%dT%H%M%SZ}"


def _whole_text(value: datetime) -> str:
    return _utc_second(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_media_root(root: Path) -> None:
    """Reject symlinked media roots and unsafe parent components."""
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise CommonSessionMediaError
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise CommonSessionMediaError


def _is_safe_child(root: Path, child: Path) -> bool:
    """Return whether a media child is a real descendant without symlinks."""
    try:
        root_resolved = root.resolve(strict=True)
        current = child
        while current != root:
            if current.is_symlink():
                return False
            current = current.parent
        return child.resolve(strict=True).is_relative_to(root_resolved)
    except (OSError, RuntimeError):
        return False


def _remove_clip_or_raise(clip: ReplayClip) -> None:
    try:
        clip.remove()
    except OSError as exc:
        raise CommonSessionCleanupError from exc


def _fraction_text(value: object) -> tuple[int, int]:
    if not isinstance(value, str) or "/" not in value:
        raise CommonSessionMediaError
    left, right = value.split("/", 1)
    if not left.isdigit() or not right.isdigit() or int(right) == 0:
        raise CommonSessionMediaError
    numerator, denominator = int(left), int(right)
    divisor = math.gcd(numerator, denominator)
    return numerator // divisor, denominator // divisor


def _seconds_fraction(value: object) -> Fraction:
    """Parse one finite decimal ffprobe timestamp without binary rounding."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise CommonSessionDecoderError
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CommonSessionDecoderError from exc
    if not decimal.is_finite() or decimal < 0:
        raise CommonSessionDecoderError
    numerator, denominator = decimal.as_integer_ratio()
    return Fraction(numerator, denominator)


def select_target_index(
    frame_offsets: Sequence[Fraction],
    target_offset: Fraction,
    end_offset: Fraction,
    *,
    logical_end: bool = False,
    tolerance: Fraction = Fraction(1, 1),
) -> int:
    """Select an eligible frame in the half-open session interval.

    ``logical_end`` uses the greatest offset strictly before ``E``; all other
    targets use nearest distance with exact ties resolved toward the earlier
    frame.  No floating-point conversion occurs.
    """
    if not frame_offsets or target_offset < 0 or (not logical_end and target_offset >= end_offset):
        raise CommonSessionDecoderError
    if any(current < prior for prior, current in pairwise(frame_offsets)):
        raise CommonSessionDecoderError
    eligible = [
        (index, offset) for index, offset in enumerate(frame_offsets) if 0 <= offset < end_offset
    ]
    if not eligible:
        raise CommonSessionDecoderError
    if logical_end:
        candidates = [(index, offset) for index, offset in eligible if offset < end_offset]
        if not candidates:
            raise CommonSessionDecoderError
        selected = max(candidates, key=lambda item: (item[1], -item[0]))
    else:
        selected = min(eligible, key=lambda item: (abs(item[1] - target_offset), item[1], item[0]))
    if (not logical_end and abs(selected[1] - target_offset) > tolerance) or (
        logical_end and end_offset - selected[1] > tolerance
    ):
        raise CommonSessionDecoderError
    return selected[0]


def validate_decoded_order(frames: Sequence[DecodedLocalFrame]) -> None:
    """Reject PTS/ordinal resets and duplicate physical positions."""
    for prior, current in pairwise(frames):
        if current.decoded_offset <= prior.decoded_offset or current.ordinal <= prior.ordinal:
            raise CommonSessionDecoderError
    positions = {(frame.decode_session_id, frame.ordinal) for frame in frames}
    if len(positions) != len(frames):
        raise CommonSessionDecoderError


def rgb24_sha256(rgb24_bytes: bytes, width: int, height: int) -> str:
    """Hash exactly row-major interleaved RGB24 bytes after shape validation."""
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise CommonSessionDecoderError
    if type(rgb24_bytes) is not bytes or len(rgb24_bytes) != width * height * 3:
        raise CommonSessionDecoderError
    return hashlib.sha256(rgb24_bytes).hexdigest()


def validate_jpeg_and_rgb24(frame: DecodedLocalFrame) -> tuple[str, int, str]:
    """Validate encoded JPEG integrity and return JPEG/RGB24 identity facts."""
    try:
        jpeg = compute_jpeg_integrity_from_bytes(frame.jpeg_bytes, frame.width, frame.height)
    except ConfirmationArtifactError as exc:
        raise CommonSessionDecoderError from exc
    return jpeg.sha256, jpeg.size_bytes, rgb24_sha256(frame.rgb24_bytes, frame.width, frame.height)


def make_frame_envelope(
    acquisition: CommonSessionAcquisition,
    decoder_operation_id: str,
    target_request_id: str,
    frame: DecodedLocalFrame,
) -> StrictIdentityEnvelope:
    """Construct one strict identity-bound decoded-frame record."""
    frame.validate()
    jpeg_sha, jpeg_size, rgb_sha = validate_jpeg_and_rgb24(frame)
    offset = frame.decoded_offset
    estimated = acquisition.request.start_utc + timedelta(seconds=int(offset))
    payload = {
        "investigation_id": acquisition.request.investigation_id,
        "run_id": acquisition.request.run_id,
        "common_session_id": acquisition.common_session_id,
        "decoder_operation_id": decoder_operation_id,
        "selected_video_stream_index": acquisition.media.selected_video_stream_index,
        "target_request_id": target_request_id,
        "raw_pts": frame.raw_pts,
        "container_start_pts": frame.container_start_pts,
        "time_base_num": frame.time_base_num,
        "time_base_den": frame.time_base_den,
        "estimated_requested_time_utc": estimated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ordinal": frame.ordinal,
        "width": frame.width,
        "height": frame.height,
        "jpeg_size_bytes": jpeg_size,
        "jpeg_sha256": jpeg_sha,
        "rgb24_sha256": rgb_sha,
    }
    return StrictIdentityEnvelope.from_payload("frame", payload)


def make_decoder_envelope(
    acquisition: CommonSessionAcquisition,
    pass_number: int,
    target_request_ids: Sequence[str],
) -> StrictIdentityEnvelope:
    """Construct one identity-bound local decoder operation."""
    if type(pass_number) is not int or pass_number <= 0:
        raise CommonSessionValidationError
    payload = {
        "investigation_id": acquisition.request.investigation_id,
        "run_id": acquisition.request.run_id,
        "common_session_id": acquisition.common_session_id,
        "pass_number": pass_number,
        "target_request_ids": list(target_request_ids),
    }
    return StrictIdentityEnvelope.from_payload("decoder-operation", payload)


def make_replay_envelope(
    request: CommonSessionRequest,
    policy_id: str,
    plan_id: str,
    segment: RecordingSegment,
) -> StrictIdentityEnvelope:
    """Construct the one replay-operation identity for the session."""
    payload = {
        "investigation_id": request.investigation_id,
        "run_id": request.run_id,
        "policy_id": policy_id,
        "plan_id": plan_id,
        "channel_id": request.channel_id,
        "segment_id": _segment_id(segment),
        "replay_start_requested_time_utc": _whole_text(request.start_utc),
        "replay_end_requested_time_utc": _whole_text(request.end_utc),
    }
    return StrictIdentityEnvelope.from_payload("replay-operation", payload)


def make_target_envelope(
    request: CommonSessionRequest,
    plan_id: str,
    sequence: int,
    requested_time_utc: datetime,
    *,
    kind: str = "COARSE",
    selection_rule: str = "NEAREST_IN_HALF_OPEN_SESSION",
    origin_target_request_id: str | None = None,
) -> StrictIdentityEnvelope:
    """Construct one strict target-request identity."""
    if type(sequence) is not int or sequence <= 0:
        raise CommonSessionValidationError
    payload: dict[str, Any] = {
        "investigation_id": request.investigation_id,
        "run_id": request.run_id,
        "plan_id": plan_id,
        "sequence": sequence,
        "kind": kind,
        "requested_time_utc": _whole_text(requested_time_utc),
        "selection_rule": selection_rule,
    }
    if origin_target_request_id is not None:
        payload["origin_target_request_id"] = origin_target_request_id
    return StrictIdentityEnvelope.from_payload("target-request", payload)


def make_schema6_manifest(
    request: CommonSessionRequest,
    schema5_manifest_id: str,
    policy_id: str,
    classifier_policy_id: str,
    plan_id: str,
    replay_operation_id: str,
    common_session_id: str,
    *,
    target_request_ids: Sequence[str],
    decoder_operation_ids: Sequence[str] = (),
    frame_ids: Sequence[str] = (),
    classification_operation_ids: Sequence[str] = (),
    observation_ids: Sequence[str] = (),
    alias_ids: Sequence[str] = (),
    support_group_ids: Sequence[str] = (),
    c2_bracket_ids: Sequence[str] = (),
    d1_input_ids: Sequence[str] = (),
    d1_history_ids: Sequence[str] = (),
    narrowed_bracket_ids: Sequence[str] = (),
) -> StrictIdentityEnvelope:
    """Construct the exact schema-6 manifest payload/index shape."""
    payload = {
        "schema_version": 6,
        "investigation_id": request.investigation_id,
        "run_id": request.run_id,
        "schema5_predecessor_manifest_id": schema5_manifest_id,
        "policy_id": policy_id,
        "classifier_policy_id": classifier_policy_id,
        "plan_id": plan_id,
        "replay_operation_id": replay_operation_id,
        "common_session_id": common_session_id,
        "indexes": {
            "target_request_ids": list(target_request_ids),
            "decoder_operation_ids": list(decoder_operation_ids),
            "frame_ids": list(frame_ids),
            "classification_operation_ids": list(classification_operation_ids),
            "observation_ids": list(observation_ids),
            "alias_ids": list(alias_ids),
            "support_group_ids": list(support_group_ids),
            "c2_bracket_ids": list(c2_bracket_ids),
            "d1_input_ids": list(d1_input_ids),
            "d1_history_ids": list(d1_history_ids),
            "narrowed_bracket_ids": list(narrowed_bracket_ids),
        },
    }
    return StrictIdentityEnvelope.from_payload("schema6-manifest", payload)


def execute_local_targets(
    acquisition: CommonSessionAcquisition,
    decoder: Decoder,
    targets: Sequence[datetime],
    *,
    pass_number: int = 1,
    cancellation: Callable[[], bool] | None = None,
    logical_end: bool = False,
) -> tuple[DecodedLocalFrame, ...]:
    """Run bounded local decoding over the same retained MP4 only."""
    ordered = tuple(_utc_second(target) for target in targets)
    if not ordered or tuple(sorted(ordered)) != ordered or len(set(ordered)) != len(ordered):
        raise CommonSessionValidationError
    if len(ordered) > acquisition.request.policy.maximum_selected_rgb24_frames:
        raise CommonSessionCapacityError
    if len(ordered) > acquisition.request.policy.maximum_targets_per_decoder_pass:
        raise CommonSessionCapacityError
    if pass_number > acquisition.request.policy.maximum_decoder_passes:
        raise CommonSessionCapacityError
    if any(
        target < acquisition.request.start_utc
        or (
            target >= acquisition.request.end_utc
            and not (logical_end and target == acquisition.request.end_utc)
        )
        for target in ordered
    ):
        raise CommonSessionValidationError
    if cancellation is not None and cancellation():
        raise CommonSessionCancelledError
    usable_seconds = acquisition.request.policy.decoder_timeout_seconds
    try:
        frames = tuple(decoder.decode(acquisition, ordered, float(usable_seconds)))
    except CommonSessionError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise CommonSessionDecoderTimeoutError from exc
    except (OSError, ValueError, TypeError, RecordingSearchError) as exc:
        raise CommonSessionDecoderError from exc
    if len(frames) != len(ordered):
        raise CommonSessionDecoderError
    for frame, target in zip(frames, ordered, strict=True):
        if _utc_second(frame.requested_time_utc) != target:
            raise CommonSessionDecoderError
        frame.validate(max_rgb24_frames=acquisition.request.policy.maximum_selected_rgb24_frames)
    validate_decoded_order(frames)
    reject_duplicate_frame_evidence(frames)
    return frames


def collapse_target_aliases(
    targets: Sequence[datetime],
) -> tuple[tuple[datetime, ...], tuple[tuple[int, int], ...]]:
    """Return unique ordered targets and duplicate-to-origin alias positions."""
    ordered = tuple(_utc_second(target) for target in targets)
    if tuple(sorted(ordered)) != ordered:
        raise CommonSessionValidationError
    first_by_time: dict[datetime, int] = {}
    unique: list[datetime] = []
    aliases: list[tuple[int, int]] = []
    for index, target in enumerate(ordered):
        origin = first_by_time.get(target)
        if origin is None:
            first_by_time[target] = index
            unique.append(target)
        else:
            aliases.append((index, origin))
    return tuple(unique), tuple(aliases)


def reject_duplicate_frame_evidence(frames: Sequence[DecodedLocalFrame]) -> None:
    """Reject aliases or repeated media from being counted as new evidence."""
    identities = {(frame.decode_session_id, frame.ordinal) for frame in frames}
    rgb_digests = {frame.rgb24_sha256 for frame in frames}
    if len(identities) != len(frames) or len(rgb_digests) != len(frames):
        raise CommonSessionDecoderError


def make_alias_envelope(
    request: CommonSessionRequest,
    target_request_id: str,
    frame_id: str,
    alias_of_target_request_id: str,
) -> StrictIdentityEnvelope:
    """Record a request alias without treating it as another observation."""
    payload = {
        "investigation_id": request.investigation_id,
        "run_id": request.run_id,
        "target_request_id": target_request_id,
        "frame_id": frame_id,
        "alias_of_target_request_id": alias_of_target_request_id,
    }
    return StrictIdentityEnvelope.from_payload("alias", payload)


def classify_after_readback(
    classifier: Classifier,
    frame: DecodedLocalFrame,
    target: object,
) -> object:
    """Invoke B4 only after the caller has strictly persisted/reopened a frame."""
    try:
        result = classifier.classify(frame, target)
    except CommonSessionError:
        raise
    except (OSError, TimeoutError, ValueError, TypeError, RecordingSearchError) as exc:
        raise CommonSessionError from exc
    return result


__all__ = [
    "CLEANUP_RESERVE_SECONDS",
    "CommonSessionAcquirer",
    "CommonSessionAcquisition",
    "CommonSessionAdmissionResult",
    "CommonSessionExecutor",
    "CommonSessionCancelledError",
    "CommonSessionCapacityError",
    "CommonSessionCleanupError",
    "CommonSessionDecoderError",
    "CommonSessionDecoderTimeoutError",
    "CommonSessionDeadlineError",
    "CommonSessionError",
    "CommonSessionMediaError",
    "CommonSessionPolicy",
    "CommonSessionRecordingUnavailableError",
    "CommonSessionReplayError",
    "CommonSessionReplayAuthenticationError",
    "CommonSessionReplayTimeoutError",
    "CommonSessionRequest",
    "CommonSessionValidationError",
    "DecodedLocalFrame",
    "Decoder",
    "DurableCommonSessionMedia",
    "FfprobeMediaProbe",
    "MAX_DECODER_PASSES",
    "MAX_MP4_BYTES",
    "MAX_SELECTED_RGB24_FRAMES",
    "MAX_TARGETS_PER_DECODER_PASS",
    "MediaProbe",
    "MediaProbeFacts",
    "Phase7E1CExecutor",
    "CommonSessionPersistenceAdapter",
    "bind_session",
    "classify_after_readback",
    "collapse_target_aliases",
    "admit_frame_then_classify",
    "execute_local_targets",
    "make_decoder_envelope",
    "make_alias_envelope",
    "make_observation_envelope",
    "make_frame_envelope",
    "make_replay_envelope",
    "make_schema6_manifest",
    "make_target_envelope",
    "rgb24_sha256",
    "reject_duplicate_frame_evidence",
    "select_target_index",
    "validate_decoded_order",
    "validate_jpeg_and_rgb24",
]
