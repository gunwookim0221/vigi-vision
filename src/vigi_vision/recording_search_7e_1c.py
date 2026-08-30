# pyright: reportAny=false, reportExplicitAny=false, reportUnknownParameterType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportArgumentType=false, reportInvalidTypeForm=false, reportOptionalMemberAccess=false, reportUnnecessaryIsInstance=false, reportCallInDefaultInitializer=false, reportUnusedImport=false, reportUnusedFunction=false
# ruff: noqa: B009, B904, C901, D105, FBT001, I001, PLR0912, PLR0913, PLR0915, PTH105, RUF022, TC006, TRY300, UP037
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
import subprocess
import tempfile
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from itertools import pairwise
from io import BytesIO
from pathlib import Path
from time import monotonic
from typing import Any, Protocol, cast

from PIL import Image, UnidentifiedImageError

from vigi_vision.durable_io import is_safe_contained_path, is_safe_path

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
    Phase7ECorruptError,
    Phase7EReadbackError,
    Phase7ERepositoryError,
    Phase7EInvocationOwnership,
    Phase7ERun,
    RecordingSearch7ERepository,
)
from vigi_vision.recording_search_7e_validation import (
    Phase7EValidationError,
    Schema5Envelope,
    Schema6Envelope,
)
from vigi_vision.recording_search_b2_records import RecordingProbeObservationRecord
from vigi_vision.recording_search_b3_models import ClassifyRecordingProbeRequest
from vigi_vision.recording_search_b4_models import (
    ClassificationOperationalError,
    ClassificationOperationalReason,
    PublishedClassificationResult,
)
from vigi_vision.recording_search_b4_service import (
    AuthoritativeClassificationHandle,
    ObservationClassificationService,
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

    def __init__(self) -> None:
        """Create one safe error with an optional secondary cleanup result."""
        super().__init__(self.code)
        self.cleanup_failure_code: str | None = None
        self.cleanup_failure: CommonSessionCleanupError | None = None
        self.failed_replay_clip: ReplayClip | None = None

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


class CommonSessionMediaProbeTimeoutError(CommonSessionMediaError):
    """The bounded retained-media probe exhausted its operation budget."""

    code = "media_probe_timeout"


class CommonSessionMissingPtsError(CommonSessionError):
    """A decoded frame did not expose an exact integer timestamp."""

    code = "missing_pts"


class CommonSessionInvalidTimeBaseError(CommonSessionError):
    """The selected stream exposed no valid positive reduced time base."""

    code = "invalid_time_base"


class CommonSessionNonmonotonicPtsError(CommonSessionError):
    """Decoded frame positions were duplicate or moved backward."""

    code = "nonmonotonic_pts"


class CommonSessionTimestampResetError(CommonSessionError):
    """A decoded frame timestamp preceded the selected stream start PTS."""

    code = "timestamp_reset"


class CommonSessionRecordingGapError(CommonSessionError):
    """Repeated decoding produced incompatible timing or content facts."""

    code = "recording_gap"


class CommonSessionSegmentBoundaryError(CommonSessionError):
    """A decoded frame fell outside the admitted half-open common session."""

    code = "segment_boundary"


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


class CommonSessionPublicationError(CommonSessionError):
    """A lifecycle or evidence publication failed safely."""

    code = "publication_failed"


class CommonSessionReadbackError(CommonSessionError):
    """A committed lifecycle state could not be strictly reopened."""

    code = "readback_failed"


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
    classifier_timeout_seconds: int = 10
    classifier_total_budget_seconds: int = 320
    support_cadence_seconds: int = 1
    terminal_interpretation_seconds: int = 10
    publication_seconds: int = 10
    strict_readback_seconds: int = 20

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
            "classifier_timeout_seconds",
            "classifier_total_budget_seconds",
            "support_cadence_seconds",
            "terminal_interpretation_seconds",
            "publication_seconds",
            "strict_readback_seconds",
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
            self.classifier_timeout_seconds,
            self.classifier_total_budget_seconds,
            self.support_cadence_seconds,
            self.terminal_interpretation_seconds,
            self.publication_seconds,
            self.strict_readback_seconds,
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


@dataclass(slots=True)
class InvocationBudget:
    """One monotonic deadline and cancellation authority for a 1C invocation."""

    policy: CommonSessionPolicy
    monotonic_clock: Callable[[], float] = field(repr=False)
    cancellation: Callable[[], bool] | None = field(default=None, repr=False)
    started_at: float = field(init=False)
    deadline: float = field(init=False)
    replay_attempts: int = 0
    decoder_passes: int = 0
    selected_rgb24_frames: int = 0
    classifications: int = 0
    classifier_seconds: float = 0.0

    def __post_init__(self) -> None:
        self.policy.validate()
        self.started_at = self.monotonic_clock()
        self.deadline = self.started_at + self.policy.invocation_deadline_seconds

    def check(self) -> None:
        """Apply cancellation before deadline according to fixed precedence."""
        if self.cancellation is not None and self.cancellation():
            raise CommonSessionCancelledError
        if self.monotonic_clock() >= self.deadline:
            raise CommonSessionDeadlineError

    def usable_remaining(self) -> float:
        """Return ordinary-work time without consuming the cleanup reserve."""
        self.check()
        return max(
            0.0,
            self.deadline - self.monotonic_clock() - self.policy.cleanup_reserve_seconds,
        )

    def operation_timeout(
        self,
        ceiling_seconds: float,
        *,
        minimum_start_seconds: float = 0.0,
        downstream_reserve_seconds: float = 0.0,
    ) -> float:
        """Return the operation ceiling bounded by the one cumulative deadline."""
        if not math.isfinite(ceiling_seconds) or ceiling_seconds <= 0:
            raise CommonSessionValidationError
        remaining = self.usable_remaining() - downstream_reserve_seconds
        if remaining <= 0 or remaining < minimum_start_seconds:
            raise CommonSessionDeadlineError
        return min(ceiling_seconds, remaining)

    def cleanup_remaining(self) -> float:
        """Return the bounded interval available to failure cleanup/finalization."""
        return max(0.0, self.deadline - self.monotonic_clock())

    def admit_replay(self) -> None:
        """Consume the invocation's sole replay attempt."""
        self.check()
        if self.replay_attempts >= 1:
            raise CommonSessionCapacityError
        self.replay_attempts += 1

    def admit_decoder_pass(self, target_count: int) -> None:
        """Consume one decoder pass and its selected-frame capacity."""
        self.check()
        if (
            type(target_count) is not int
            or target_count <= 0
            or target_count > self.policy.maximum_targets_per_decoder_pass
            or self.decoder_passes >= self.policy.maximum_decoder_passes
            or self.selected_rgb24_frames + target_count > self.policy.maximum_selected_rgb24_frames
        ):
            raise CommonSessionCapacityError
        self.decoder_passes += 1
        self.selected_rgb24_frames += target_count

    def admit_classification(self) -> float:
        """Consume one classification and return its remaining bounded timeout."""
        self.check()
        if self.classifications >= self.policy.maximum_classifications:
            raise CommonSessionCapacityError
        timeout = self.operation_timeout(
            self.policy.classifier_timeout_seconds,
            minimum_start_seconds=1.0,
            downstream_reserve_seconds=40.0,
        )
        if self.classifier_seconds + timeout > self.policy.classifier_total_budget_seconds:
            timeout = self.policy.classifier_total_budget_seconds - self.classifier_seconds
        if timeout <= 0:
            raise CommonSessionCapacityError
        self.classifications += 1
        self.classifier_seconds += timeout
        return timeout


@dataclass(frozen=True, slots=True)
class Phase7EInvocation:
    """Validated repository ownership plus the invocation's one budget."""

    request: "CommonSessionRequest"
    ownership: Phase7EInvocationOwnership = field(repr=False)
    budget: InvocationBudget = field(repr=False)

    def validate(self, repository: RecordingSearch7ERepository) -> None:
        """Revalidate both ownership and cumulative invocation authority."""
        self.ownership.validate(
            repository,
            self.request.investigation_id,
            self.request.run_id,
        )
        self.budget.check()


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
            or type(self.container_start_pts) is not int
            or type(self.time_base_num) is not int
            or type(self.time_base_den) is not int
            or type(self.duration_ticks) is not int
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

    def find_segments_for_window(self, window: RecordingWindow) -> tuple[RecordingSegment, ...]:
        """Return the complete SDK segment inventory intersecting the window."""
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


@dataclass(frozen=True, slots=True)
class Phase7EB4Input:
    """Strictly reopened Phase 7E authority passed to the production B4 adapter."""

    run: Phase7ERun
    frame_record: StrictIdentityEnvelope
    frame_jpeg_bytes: bytes = field(repr=False)
    frame: "DecodedLocalFrame" = field(repr=False)
    target_request: StrictIdentityEnvelope
    classification_attempt_id: str
    budget: InvocationBudget = field(repr=False)


class B4Bridge(Protocol):
    """The sole Phase 7E seam around production B4 authority."""

    def classify(self, authoritative: Phase7EB4Input) -> object:
        """Return a Phase 7E operation derived from production B4 output."""
        ...


@dataclass(frozen=True, slots=True)
class ProductionB4Context:
    """Real B4 handle/request plus strict observation readback."""

    handle: AuthoritativeClassificationHandle
    request: ClassifyRecordingProbeRequest
    baseline_identity: str
    read_observation: Callable[[PublishedClassificationResult], RecordingProbeObservationRecord]


class ProductionB4ContextFactory(Protocol):
    """Resolve legacy B4 authority only from strictly reopened Phase 7E input."""

    def __call__(self, authoritative: Phase7EB4Input) -> ProductionB4Context:
        """Build the real B4 handle and request after Phase 7E strict readback."""
        ...


@dataclass(frozen=True, slots=True)
class ProductionB4Adapter:
    """Invoke the existing B4 service and map its strictly read-back typed record."""

    service: ObservationClassificationService
    context_factory: ProductionB4ContextFactory

    def classify(self, authoritative: Phase7EB4Input) -> StrictIdentityEnvelope:
        """Call production B4 without accepting caller-owned media or fake result shapes."""
        timeout = authoritative.budget.admit_classification()
        context = self.context_factory(authoritative)
        if not isinstance(context.request, ClassifyRecordingProbeRequest):
            raise CommonSessionValidationError
        try:
            bounded_service = (
                replace(self.service, timeout_seconds=timeout)
                if isinstance(self.service, ObservationClassificationService)
                else self.service
            )
            result = bounded_service.classify(context.handle, context.request)
        except ClassificationOperationalError as exc:
            reason = _phase7e_operational_reason(exc.reason)
            return _operational_completion(authoritative, context.baseline_identity, reason)
        authoritative.budget.check()
        observation = context.read_observation(result)
        if (
            not isinstance(result, PublishedClassificationResult)
            or not isinstance(observation, RecordingProbeObservationRecord)
            or observation.observation_id != result.observation_id
            or observation.canonical_frame_id != result.canonical_frame_id
            or observation.state != result.state
            or observation.reason_code != result.reason_code
            or observation.baseline_observation_id != context.baseline_identity
        ):
            raise CommonSessionValidationError
        classifier_policy_id = authoritative.run.manifest.payload["classifier_policy_id"]
        return StrictIdentityEnvelope.from_payload(
            "classification-operation",
            {
                "investigation_id": authoritative.run.investigation_id,
                "run_id": authoritative.run.run_id,
                "frame_id": authoritative.frame_record.identity,
                "target_request_id": authoritative.target_request.identity,
                "baseline_identity": observation.baseline_observation_id,
                "classifier_policy_id": classifier_policy_id,
                "attempt": 1,
                "result_kind": "VISUAL",
                "outcome": observation.state.value,
                "reason_code": (
                    observation.reason_code.value if observation.reason_code is not None else None
                ),
                "classifier_evidence": _phase7e_evidence(observation),
                "operational_reason": None,
            },
        )


def _phase7e_evidence(observation: RecordingProbeObservationRecord) -> dict[str, object]:
    raw = observation.classifier_evidence.model_dump(mode="python")
    result: dict[str, object] = {}
    decimal_fields = {
        "baseline_mask_coverage",
        "probe_mask_coverage",
        "mask_iou",
        "roi_luma_ncc",
    }
    for key, value in raw.items():
        if value is None:
            result[key] = None
        elif key in decimal_fields:
            result[key] = f"{value:.6f}"
        elif hasattr(value, "value"):
            result[key] = value.value
        else:
            result[key] = value
    return result


def _phase7e_operational_reason(reason: ClassificationOperationalReason) -> str:
    if reason is ClassificationOperationalReason.CLASSIFIER_TIMEOUT:
        return "classifier_timeout"
    if reason is ClassificationOperationalReason.INVALID_CLASSIFIER_OUTPUT:
        return "invalid_classifier_result"
    return "classification_failed"


def _operational_completion(
    authoritative: Phase7EB4Input,
    baseline_identity: str,
    operational_reason: str,
) -> StrictIdentityEnvelope:
    return StrictIdentityEnvelope.from_payload(
        "classification-operation",
        {
            "investigation_id": authoritative.run.investigation_id,
            "run_id": authoritative.run.run_id,
            "frame_id": authoritative.frame_record.identity,
            "target_request_id": authoritative.target_request.identity,
            "baseline_identity": baseline_identity,
            "classifier_policy_id": authoritative.run.manifest.payload["classifier_policy_id"],
            "attempt": 1,
            "result_kind": "OPERATIONAL",
            "outcome": None,
            "reason_code": None,
            "classifier_evidence": None,
            "operational_reason": operational_reason,
        },
    )


@dataclass(frozen=True, slots=True)
class DecodedLocalFrame:
    """One deterministic local frame represented only by authoritative RGB24 pixels."""

    requested_time_utc: datetime
    raw_pts: int
    ordinal: int
    width: int
    height: int
    rgb24_bytes: bytes = field(repr=False)
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
            or type(self.container_start_pts) is not int
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
            or type(self.rgb24_bytes) is not bytes
            or len(self.rgb24_bytes) != self.width * self.height * 3
            or max_rgb24_frames <= 0
        ):
            raise CommonSessionDecoderError
        if self.raw_pts < self.container_start_pts:
            raise CommonSessionTimestampResetError
        if self.decoded_offset < 0:
            raise CommonSessionTimestampResetError


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

    repository: RecordingSearch7ERepository = field(repr=False)

    @property
    def root(self) -> Path:
        """Return the only approved retained-media root."""
        return self.repository.root / ".media"

    def publish(
        self,
        acquisition: CommonSessionAcquisition,
        invocation: Phase7EInvocation,
    ) -> CommonSessionAcquisition:
        """Copy, fsync, read back, and atomically publish the invocation's MP4."""
        invocation.validate(self.repository)
        source = acquisition.replay_clip.temporary_mp4_path
        final_directory = (
            self.root / acquisition.request.investigation_id / acquisition.request.run_id
        )
        final = final_directory / f"{acquisition.common_session_id}.mp4"
        staging: Path | None = None
        published_by_invocation = False
        source_size = -1
        source_digest = ""
        try:
            _validate_media_root(self.repository.root, self.root)
            _create_safe_media_directory(
                self.root,
                acquisition.request.investigation_id,
                acquisition.request.run_id,
            )
            if (
                source.is_symlink()
                or not source.is_file()
                or source.stat().st_nlink != 1
                or not is_safe_path(source, require_target=True)
            ):
                raise CommonSessionMediaError
            source_size = source.stat().st_size
            source_digest = _sha256_file(source)
            if final.exists() or final.is_symlink():
                raise CommonSessionMediaError
            staging_root = self.repository.root / ".staging"
            _validate_media_root(self.repository.root, staging_root)
            staging = Path(
                tempfile.mkdtemp(
                    prefix=(
                        f"{acquisition.request.investigation_id}-"
                        f"{acquisition.request.run_id}-media-"
                    ),
                    dir=staging_root,
                )
            )
            if not is_safe_contained_path(self.repository.root, staging, require_target=True):
                raise CommonSessionMediaError
            temporary = staging / "session.mp4"
            with source.open("rb") as source_stream, temporary.open("xb") as target_stream:
                while True:
                    invocation.budget.check()
                    chunk = source_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    target_stream.write(chunk)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            if (
                temporary.stat().st_nlink != 1
                or temporary.stat().st_size != source_size
                or _sha256_file(temporary) != source_digest
            ):
                raise CommonSessionMediaError
            invocation.validate(self.repository)
            if final.exists() or final.is_symlink():
                raise CommonSessionMediaError
            os.replace(temporary, final)
            published_by_invocation = True
            _fsync_directory(final_directory)
            if (
                not _is_safe_child(self.root, final)
                or final.stat().st_nlink != 1
                or final.stat().st_size != source_size
                or _sha256_file(final) != source_digest
            ):
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
        except CommonSessionError:
            if published_by_invocation:
                with suppress(CommonSessionCleanupError):
                    _remove_exact_media_file(final, source_size, source_digest)
            raise
        except (OSError, RuntimeError) as exc:
            if published_by_invocation:
                with suppress(CommonSessionCleanupError):
                    _remove_exact_media_file(final, source_size, source_digest)
            raise CommonSessionMediaError from exc
        finally:
            if staging is not None:
                _remove_owned_media_directory(self.repository.root, staging)


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
            raise CommonSessionMediaProbeTimeoutError from exc
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
            try:
                time_base_num, time_base_den = _fraction_text(stream["time_base"])
            except CommonSessionMediaError as exc:
                raise CommonSessionInvalidTimeBaseError from exc
            rate_num, rate_den = _fraction_text(stream["avg_frame_rate"])
            duration_value = stream.get("duration_ts")
            if duration_value is None:
                duration_value = format_data.get("duration_ts")
            start_value = stream.get("start_pts")
            if duration_value is None or start_value is None:
                raise CommonSessionMediaError
            duration_ticks = _strict_integer_text(duration_value, nonnegative=False)
            start_pts = _strict_integer_text(start_value, nonnegative=True)
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
    monotonic_clock: Callable[[], float] = field(default=monotonic, repr=False)

    def decode(
        self,
        session: CommonSessionAcquisition,
        targets: tuple[datetime, ...],
        timeout_seconds: float,
    ) -> tuple[DecodedLocalFrame, ...]:
        """Probe once, select exact candidates, and decode each target locally."""
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise CommonSessionDecoderTimeoutError
        deadline = self.monotonic_clock() + timeout_seconds
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
                max(0.001, deadline - self.monotonic_clock()),
            )
        except subprocess.TimeoutExpired as exc:
            raise CommonSessionDecoderTimeoutError from exc
        except OSError as exc:
            raise CommonSessionDecoderError from exc
        if probe.returncode != 0:
            raise CommonSessionDecoderError
        try:
            raw_frames = json.loads(probe.stdout)["frames"]
            raw_pts: list[int] = []
            for value in raw_frames:
                timestamp = value.get("best_effort_timestamp")
                if timestamp is None:
                    timestamp = value.get("pkt_pts")
                if timestamp is None:
                    timestamp = value.get("pkt_dts")
                if timestamp is None:
                    raise CommonSessionMissingPtsError
                raw_pts.append(_strict_integer_text(timestamp, nonnegative=True))
        except (CommonSessionError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, CommonSessionError):
                raise
            raise CommonSessionDecoderError from exc
        if not raw_pts:
            raise CommonSessionMissingPtsError
        if any(current <= prior for prior, current in pairwise(raw_pts)):
            raise CommonSessionNonmonotonicPtsError
        offsets = [
            Fraction(
                (raw_pts_value - session.media.container_start_pts) * session.media.time_base_num,
                session.media.time_base_den,
            )
            for raw_pts_value in raw_pts
        ]
        if any(offset < 0 for offset in offsets):
            raise CommonSessionTimestampResetError
        session_end = Fraction(session.request.duration_seconds, 1)
        if any(offset >= session_end for offset in offsets):
            raise CommonSessionSegmentBoundaryError
        results: list[DecodedLocalFrame] = []
        for target in targets:
            target_offset = Fraction(int((target - session.request.start_utc).total_seconds()), 1)
            index = select_target_index(
                offsets,
                target_offset,
                Fraction(session.request.duration_seconds, 1),
                logical_end=target == session.request.end_utc,
                tolerance=Fraction(session.request.policy.support_cadence_seconds, 1),
            )
            remaining = deadline - self.monotonic_clock()
            if remaining <= 0:
                raise CommonSessionDecoderTimeoutError
            rgb = self._decode_rgb(session, index, remaining)
            results.append(
                DecodedLocalFrame(
                    requested_time_utc=target,
                    raw_pts=raw_pts[index],
                    ordinal=index,
                    width=session.media.width,
                    height=session.media.height,
                    rgb24_bytes=rgb,
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

    def locate(
        self,
        request: CommonSessionRequest,
        budget: InvocationBudget | None = None,
    ) -> RecordingSegment:
        """Prove exactly one SDK segment covers the complete half-open window."""
        request.policy.validate()
        active_budget = budget or InvocationBudget(
            request.policy, self.monotonic_clock, self.cancellation
        )
        active_budget.check()
        window = RecordingWindow(request.channel_id, request.start_utc, request.end_utc)
        try:
            segments = tuple(self.recording_planner.find_segments_for_window(window))
        except RecordingUnavailableError as exc:
            raise CommonSessionRecordingUnavailableError from exc
        except (OSError, ValueError, RecordingSearchError) as exc:
            raise CommonSessionRecordingUnavailableError from exc
        active_budget.check()
        if len(segments) != 1:
            raise CommonSessionRecordingUnavailableError
        segment = segments[0]
        if (
            segment.channel_id != request.channel_id
            or segment.start_utc > request.start_utc
            or request.start_utc >= request.end_utc
            or segment.end_utc < request.end_utc
            or segment.start_utc >= segment.end_utc
        ):
            raise CommonSessionRecordingUnavailableError
        return segment

    def acquire(
        self,
        request: CommonSessionRequest,
        *,
        segment: RecordingSegment | None = None,
        budget: InvocationBudget | None = None,
    ) -> CommonSessionAcquisition:
        """Acquire exactly one replay and retain its MP4 for local consumers."""
        request.policy.validate()
        active_budget = budget or InvocationBudget(
            request.policy, self.monotonic_clock, self.cancellation
        )
        active_budget.check()
        selected_segment = segment or self.locate(request, active_budget)
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
        active_budget.check()
        active_budget.admit_replay()
        replay_timeout = active_budget.operation_timeout(
            request.duration_seconds + request.policy.replay_margin_seconds,
            minimum_start_seconds=1.0,
        )
        clip: ReplayClip | None = None
        try:
            bounded_extract = getattr(self.replay_extractor, "extract_with_timeout", None)
            if callable(bounded_extract):
                clip = cast("ReplayClip", bounded_extract(replay_request, replay_timeout))
            else:
                if replay_timeout < (
                    request.duration_seconds + request.policy.replay_margin_seconds
                ):
                    raise CommonSessionDeadlineError
                clip = cast(Any, self.replay_extractor).extract(replay_request)
            active_budget.check()
            self._validate_retained_clip(clip, request.policy.maximum_mp4_bytes)
            probe_budget = active_budget.operation_timeout(
                request.policy.ffprobe_timeout_seconds,
                minimum_start_seconds=1.0,
            )
            media = self.media_probe.probe(
                clip.temporary_mp4_path,
                probe_budget,
            )
            active_budget.check()
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
                "replay_operation_id": "pending-replay-operation",  # replaced before admission
                "policy_id": "pending-policy",  # replaced before admission
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
            if isinstance(exc, CommonSessionError):
                primary = exc
            elif isinstance(exc, ReplayTimeoutError):
                primary = CommonSessionReplayTimeoutError()
            elif isinstance(exc, ReplayAuthenticationError):
                primary = CommonSessionReplayAuthenticationError()
            elif isinstance(exc, ReplayUnavailableError):
                primary = CommonSessionRecordingUnavailableError()
            else:
                primary = CommonSessionReplayError()
            if clip is not None:
                _remove_clip_preserving_primary(clip, primary)
            if primary is exc:
                raise
            raise primary from exc
        except (OSError, ConfirmationArtifactError, ValueError, TypeError) as exc:
            primary = CommonSessionMediaError()
            if clip is not None:
                _remove_clip_preserving_primary(clip, primary)
            raise primary from exc

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
    if phase_state is Schema5PhaseState.INTERRUPTED:
        return Schema5Envelope(
            run_state="INTERRUPTED",
            phase_state=phase_state,
            active_replay_operation_id=replay_operation_id,
            reason_code="interrupted",
            attempt_count=1 if replay_operation_id is not None else 0,
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

    @contextmanager
    def invocation(self, request: CommonSessionRequest) -> Generator[Phase7EInvocation]:
        """Hold one OS owner and one cumulative budget across caller-composed 1C work."""
        budget = InvocationBudget(
            request.policy,
            self.acquirer.monotonic_clock,
            self.acquirer.cancellation,
        )
        budget.check()
        lock_timeout = budget.operation_timeout(
            max(0.001, self.repository.lock_timeout_seconds),
            minimum_start_seconds=0.001,
        )
        with self.repository.invocation_ownership(
            request.investigation_id,
            request.run_id,
            timeout_seconds=lock_timeout,
        ) as ownership:
            active = Phase7EInvocation(request, ownership, budget)
            active.validate(self.repository)
            yield active

    def execute(
        self,
        request: CommonSessionRequest,
        schema5_manifest: StrictIdentityEnvelope,
        base_records: Sequence[StrictIdentityEnvelope],
        classifier_policy: StrictIdentityEnvelope,
        target_requests: Sequence[StrictIdentityEnvelope],
        *,
        replay_operation: StrictIdentityEnvelope | None = None,
        invocation: Phase7EInvocation | None = None,
    ) -> CommonSessionAdmissionResult:
        """Persist schema 5, acquire once, and publish zero-evidence schema 6."""
        if invocation is None:
            with self.invocation(request) as active:
                return self.execute(
                    request,
                    schema5_manifest,
                    base_records,
                    classifier_policy,
                    target_requests,
                    replay_operation=replay_operation,
                    invocation=active,
                )
        invocation.validate(self.repository)
        media_store = DurableCommonSessionMedia(self.repository)
        self.repository.media_root = media_store.root
        self.repository.media_probe = self.acquirer.media_probe
        _validate_executor_inputs(
            request,
            schema5_manifest,
            base_records,
            classifier_policy,
            target_requests,
        )
        segment = self.acquirer.locate(request, invocation.budget)
        planned = _schema5_state(Schema5PhaseState.PLANNED, None)
        self.repository.create_schema5(
            schema5_manifest,
            planned,
            base_records,
            investigation_id=request.investigation_id,
            run_id=request.run_id,
            ownership=invocation.ownership,
        )
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
            ownership=invocation.ownership,
        )
        acquisition: CommonSessionAcquisition | None = None
        retained: CommonSessionAcquisition | None = None
        schema6_committed = False
        try:
            acquisition = self.acquirer.acquire(
                request,
                segment=segment,
                budget=invocation.budget,
            )
            bound = bind_session(
                acquisition,
                operation.identity,
                schema5_manifest.payload["policy_id"],
            )
            retained = media_store.publish(bound, invocation)
            invocation.validate(self.repository)
            retained.remove()
            acquired_records = (*base_records, operation)
            self.repository.admit_schema5(
                request.investigation_id,
                request.run_id,
                schema5_manifest,
                _schema5_state(Schema5PhaseState.ACQUIRED, operation.identity),
                acquired_records,
                ownership=invocation.ownership,
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
                ownership=invocation.ownership,
            )
            schema6_committed = True
            invocation.validate(self.repository)
            return CommonSessionAdmissionResult(result.run, retained)
        except (KeyboardInterrupt, SystemExit) as exc:
            primary = CommonSessionCancelledError()
            self._finalize_failure(
                invocation,
                schema5_manifest,
                operation,
                acquiring_records,
                primary,
                retained or acquisition,
                schema6_committed,
            )
            raise primary from exc
        except (
            CommonSessionError,
            Phase7ERepositoryError,
            Phase7EValidationError,
        ) as exc:
            primary = _translate_repository_failure(exc)
            self._finalize_failure(
                invocation,
                schema5_manifest,
                operation,
                acquiring_records,
                primary,
                retained or acquisition,
                schema6_committed,
                failed_clip=primary.failed_replay_clip,
            )
            raise primary

    def _finalize_failure(
        self,
        invocation: Phase7EInvocation,
        schema5_manifest: StrictIdentityEnvelope,
        operation: StrictIdentityEnvelope,
        acquiring_records: Sequence[StrictIdentityEnvelope],
        primary: CommonSessionError,
        retained: CommonSessionAcquisition | None,
        schema6_committed: bool,
        failed_clip: ReplayClip | None = None,
    ) -> None:
        """Preserve the primary cause while publishing/cleaning only current ownership."""
        if not invocation.ownership.active or not invocation.ownership.lock.held:
            return
        if invocation.budget.cleanup_remaining() <= 0:
            primary.cleanup_failure_code = "cleanup_failed"
            return
        with suppress(Phase7ERepositoryError, Phase7EValidationError, CommonSessionError):
            current = self.repository.reopen_schema5(
                invocation.request.investigation_id,
                invocation.request.run_id,
                ownership=invocation.ownership,
            )
            if (
                isinstance(current.state, Schema5Envelope)
                and current.state.phase_state is Schema5PhaseState.ACQUIRING
            ):
                interrupted = isinstance(primary, CommonSessionCancelledError)
                self.repository.admit_schema5(
                    invocation.request.investigation_id,
                    invocation.request.run_id,
                    schema5_manifest,
                    _schema5_state(
                        Schema5PhaseState.INTERRUPTED
                        if interrupted
                        else Schema5PhaseState.ACQUISITION_FAILED,
                        operation.identity,
                        primary.code,
                    ),
                    acquiring_records,
                    expected_manifest_id=current.manifest_id,
                    ownership=invocation.ownership,
                )
        if retained is not None and not schema6_committed:
            try:
                if not isinstance(primary, CommonSessionReadbackError):
                    _remove_owned_retained_media(self.repository.root, retained)
                retained.remove()
            except CommonSessionCleanupError:
                primary.cleanup_failure_code = "cleanup_failed"
        # A failed acquisition owns only its temporary replay clip.  The
        # acquisition boundary has already attempted cleanup exactly once;
        # retain the context without retrying or claiming that it was removed.
        if failed_clip is not None and primary.failed_replay_clip is None:
            primary.failed_replay_clip = failed_clip


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


append_schema6_indexes = _schema6_successor_manifest


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
    classifier: B4Bridge,
    *,
    classification_attempt_id: str,
    invocation: Phase7EInvocation,
) -> Phase7ERun:
    """Persist/reopen a frame before invoking B4, then persist its completion."""
    if target_request.family != "target-request" or decoder_operation.family != "decoder-operation":
        raise CommonSessionValidationError
    if not classification_attempt_id:
        raise CommonSessionValidationError
    invocation.validate(repository)
    if invocation.request != acquisition.request:
        raise CommonSessionValidationError
    current = repository.reopen_schema6(
        acquisition.request.investigation_id,
        acquisition.request.run_id,
        ownership=invocation.ownership,
    )
    if not isinstance(current.state, Schema6Envelope):
        raise CommonSessionValidationError
    canonical_frame, canonical_jpeg = canonicalize_frame(frame)
    frame_envelope = _make_frame_envelope_from_canonical(
        acquisition,
        decoder_operation.identity,
        target_request.identity,
        canonical_frame,
        canonical_jpeg,
    )
    if current.state.target_state is not Schema6TargetState.REQUESTED:
        raise CommonSessionValidationError
    decoder_indexed = (
        decoder_operation.identity in current.manifest.payload["indexes"]["decoder_operation_ids"]
    )
    decoding_manifest = (
        current.manifest
        if decoder_indexed
        else _schema6_successor_manifest(
            current.manifest,
            decoder_operation_ids=decoder_operation.identity,
        )
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
    records = tuple(record for record in current.records if record.family != "schema5-manifest")
    if not decoder_indexed:
        records = (*records, decoder_operation)
    repository.admit_schema6(
        acquisition.request.investigation_id,
        acquisition.request.run_id,
        decoding_manifest,
        decoding_state,
        records,
        expected_manifest_id=current.manifest_id,
        ownership=invocation.ownership,
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
        binary_records={frame_envelope.identity: canonical_jpeg},
        ownership=invocation.ownership,
    )
    invocation.validate(repository)
    ready_run = repository.reopen_schema6(
        acquisition.request.investigation_id,
        acquisition.request.run_id,
        ownership=invocation.ownership,
    )
    authoritative_frame = reopened_frame_from_run(
        ready_run,
        frame_envelope.identity,
        canonical_frame.requested_time_utc,
    )
    authoritative_record = next(
        (
            item
            for item in ready_run.records
            if item.family == "frame" and item.identity == frame_envelope.identity
        ),
        None,
    )
    authoritative_jpeg = ready_run.frame_bytes.get(frame_envelope.identity)
    if authoritative_record is None or type(authoritative_jpeg) is not bytes:
        raise CommonSessionValidationError
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
        ownership=invocation.ownership,
    )
    completion = _as_envelope(
        classify_after_readback(
            classifier,
            Phase7EB4Input(
                ready_run,
                authoritative_record,
                authoritative_jpeg,
                authoritative_frame,
                target_request,
                classification_attempt_id,
                invocation.budget,
            ),
        ),
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
    published = repository.admit_schema6(
        acquisition.request.investigation_id,
        acquisition.request.run_id,
        final_manifest,
        final_state,
        final_records,
        expected_manifest_id=classifying_manifest.identity,
        ownership=invocation.ownership,
    ).run
    invocation.validate(repository)
    return repository.reopen_schema6(
        published.investigation_id,
        published.run_id,
        ownership=invocation.ownership,
    )


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


def _validate_media_root(repository_root: Path, root: Path) -> None:
    """Create only a direct safe child of the configured repository root."""
    try:
        if root.parent != repository_root or not is_safe_path(repository_root, require_target=True):
            raise CommonSessionMediaError
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise CommonSessionMediaError
        root.mkdir(parents=False, exist_ok=True)
        if (
            root.is_symlink()
            or not root.is_dir()
            or not is_safe_contained_path(repository_root, root, require_target=True)
        ):
            raise CommonSessionMediaError
    except OSError as exc:
        raise CommonSessionMediaError from exc


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


def _create_safe_media_directory(root: Path, investigation_id: str, run_id: str) -> Path:
    """Create each deterministic media component without traversing linked parents."""
    if any(
        not value or value in {".", ".."} or "/" in value or "\\" in value or "\0" in value
        for value in (investigation_id, run_id)
    ):
        raise CommonSessionMediaError
    current = root
    try:
        for component in (investigation_id, run_id):
            candidate = current / component
            if candidate.exists() or candidate.is_symlink():
                if candidate.is_symlink() or not candidate.is_dir():
                    raise CommonSessionMediaError
            else:
                candidate.mkdir(parents=False)
            if not _is_safe_child(root, candidate):
                raise CommonSessionMediaError
            current = candidate
    except OSError as exc:
        raise CommonSessionMediaError from exc
    return current


def _remove_owned_media_directory(repository_root: Path, path: Path) -> None:
    """Remove only one confined invocation staging directory without following links."""
    try:
        if (
            not path.exists()
            or path.is_symlink()
            or not is_safe_contained_path(repository_root, path, require_target=True)
        ):
            return
        for item in path.iterdir():
            if (
                item.is_symlink()
                or not item.is_file()
                or not is_safe_contained_path(repository_root, item, require_target=True)
            ):
                raise CommonSessionCleanupError
            item.unlink()
        path.rmdir()
    except OSError as exc:
        raise CommonSessionCleanupError from exc


def _remove_exact_media_file(path: Path, size_bytes: int, sha256: str) -> None:
    """Remove only the just-published regular file whose bytes remain unchanged."""
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_nlink != 1
            or path.stat().st_size != size_bytes
            or _sha256_file(path) != sha256
        ):
            raise CommonSessionCleanupError
        path.unlink()
    except OSError as exc:
        raise CommonSessionCleanupError from exc


def _remove_owned_retained_media(
    repository_root: Path,
    acquisition: CommonSessionAcquisition,
) -> None:
    """Delete only the exact uncommitted media identity owned by this invocation."""
    path = acquisition.retained_mp4_path
    if path is None:
        return
    expected_root = repository_root / ".media"
    expected = (
        expected_root
        / acquisition.request.investigation_id
        / acquisition.request.run_id
        / f"{acquisition.common_session_id}.mp4"
    )
    try:
        if path != expected or not _is_safe_child(expected_root, path):
            raise CommonSessionCleanupError
        payload = acquisition.session.payload
        if (
            path.stat().st_nlink != 1
            or path.stat().st_size != payload.get("mp4_size_bytes")
            or _sha256_file(path) != payload.get("mp4_sha256")
        ):
            raise CommonSessionCleanupError
        path.unlink()
        for parent in (path.parent, path.parent.parent):
            if _is_safe_child(expected_root, parent) and not any(parent.iterdir()):
                parent.rmdir()
    except OSError as exc:
        raise CommonSessionCleanupError from exc


def _fsync_directory(directory: Path) -> None:
    with suppress(OSError):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _strict_integer_text(value: object, *, nonnegative: bool) -> int:
    """Parse ffprobe integer JSON/string values without float coercion."""
    if type(value) is int:
        result = value
    elif (
        isinstance(value, str)
        and value
        and (value.isdigit() or (value.startswith("-") and value[1:].isdigit()))
    ):
        result = int(value)
    else:
        raise CommonSessionMissingPtsError
    if nonnegative and result < 0:
        raise CommonSessionTimestampResetError
    return result


def _translate_repository_failure(exc: BaseException) -> CommonSessionError:
    if isinstance(exc, CommonSessionError):
        return exc
    if isinstance(exc, (Phase7EReadbackError, Phase7ECorruptError)):
        return CommonSessionReadbackError()
    return CommonSessionPublicationError()


def _remove_clip_or_raise(clip: ReplayClip) -> None:
    try:
        clip.remove()
    except OSError as exc:
        raise CommonSessionCleanupError from exc


def _remove_clip_preserving_primary(
    clip: ReplayClip,
    primary: CommonSessionError,
) -> None:
    """Attempt owned temp cleanup without replacing an existing primary error."""
    try:
        clip.remove()
    except OSError:
        primary.cleanup_failure_code = CommonSessionCleanupError.code
        primary.cleanup_failure = CommonSessionCleanupError()
        primary.failed_replay_clip = clip


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


def validate_decoded_order(
    frames: Sequence[DecodedLocalFrame], *, allow_aliases: bool = False
) -> None:
    """Reject resets while optionally retaining exact repeated-frame aliases."""
    if not frames:
        raise CommonSessionDecoderError
    first = frames[0]
    first.validate()
    for prior, current in pairwise(frames):
        current.validate()
        if (
            current.decode_session_id != first.decode_session_id
            or current.container_start_pts != first.container_start_pts
            or current.time_base_num != first.time_base_num
            or current.time_base_den != first.time_base_den
        ):
            raise CommonSessionRecordingGapError
        if current.ordinal < prior.ordinal:
            raise CommonSessionDecoderError
        if current.raw_pts < prior.raw_pts or current.decoded_offset < prior.decoded_offset:
            raise CommonSessionNonmonotonicPtsError
        same_position = (
            current.ordinal == prior.ordinal
            and current.raw_pts == prior.raw_pts
            and current.decoded_offset == prior.decoded_offset
        )
        if (
            current.ordinal == prior.ordinal
            or current.raw_pts == prior.raw_pts
            or current.decoded_offset == prior.decoded_offset
        ) and not same_position:
            raise CommonSessionNonmonotonicPtsError
        if same_position and (not allow_aliases or current.rgb24_sha256 != prior.rgb24_sha256):
            raise CommonSessionDecoderError
    positions = {(frame.decode_session_id, frame.ordinal) for frame in frames}
    if not allow_aliases and len(positions) != len(frames):
        raise CommonSessionDecoderError


def validate_repeated_decode(
    authoritative: Sequence[DecodedLocalFrame],
    repeated: Sequence[DecodedLocalFrame],
) -> None:
    """Require a repeated pass to reproduce exact timing and RGB24 identities."""
    if len(authoritative) != len(repeated) or not authoritative:
        raise CommonSessionRecordingGapError
    validate_decoded_order(authoritative)
    validate_decoded_order(repeated)
    for prior, current in zip(authoritative, repeated, strict=True):
        if (
            prior.requested_time_utc != current.requested_time_utc
            or prior.raw_pts != current.raw_pts
            or prior.ordinal != current.ordinal
            or prior.container_start_pts != current.container_start_pts
            or prior.time_base_num != current.time_base_num
            or prior.time_base_den != current.time_base_den
            or prior.width != current.width
            or prior.height != current.height
            or prior.rgb24_sha256 != current.rgb24_sha256
        ):
            raise CommonSessionRecordingGapError


def rgb24_sha256(rgb24_bytes: bytes, width: int, height: int) -> str:
    """Hash exactly row-major interleaved RGB24 bytes after shape validation."""
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise CommonSessionDecoderError
    if type(rgb24_bytes) is not bytes or len(rgb24_bytes) != width * height * 3:
        raise CommonSessionDecoderError
    return hashlib.sha256(rgb24_bytes).hexdigest()


def canonicalize_frame(frame: DecodedLocalFrame) -> tuple[DecodedLocalFrame, bytes]:
    """Encode RGB24 once, then make its strictly decoded JPEG rendition authoritative."""
    frame.validate()
    try:
        image = Image.frombytes("RGB", (frame.width, frame.height), frame.rgb24_bytes)
        stream = BytesIO()
        image.save(
            stream,
            format="JPEG",
            quality=95,
            subsampling=0,
            optimize=False,
            progressive=False,
        )
        jpeg_bytes = stream.getvalue()
        with Image.open(BytesIO(jpeg_bytes)) as reopened:
            reopened.load()
            if (
                reopened.format != "JPEG"
                or reopened.mode != "RGB"
                or reopened.size
                != (
                    frame.width,
                    frame.height,
                )
            ):
                raise CommonSessionDecoderError
            canonical_rgb = reopened.tobytes()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise CommonSessionDecoderError from exc
    canonical = DecodedLocalFrame(
        requested_time_utc=frame.requested_time_utc,
        raw_pts=frame.raw_pts,
        ordinal=frame.ordinal,
        width=frame.width,
        height=frame.height,
        rgb24_bytes=canonical_rgb,
        decoder_operation_id=frame.decoder_operation_id,
        decode_session_id=frame.decode_session_id,
        container_start_pts=frame.container_start_pts,
        time_base_num=frame.time_base_num,
        time_base_den=frame.time_base_den,
    )
    return canonical, jpeg_bytes


def validate_jpeg_and_rgb24(frame: DecodedLocalFrame) -> tuple[str, int, str]:
    """Return canonical internally generated JPEG and reopened RGB24 identity facts."""
    canonical, jpeg_bytes = canonicalize_frame(frame)
    try:
        jpeg = compute_jpeg_integrity_from_bytes(jpeg_bytes, frame.width, frame.height)
    except ConfirmationArtifactError as exc:
        raise CommonSessionDecoderError from exc
    return jpeg.sha256, jpeg.size_bytes, canonical.rgb24_sha256


def make_frame_envelope(
    acquisition: CommonSessionAcquisition,
    decoder_operation_id: str,
    target_request_id: str,
    frame: DecodedLocalFrame,
) -> StrictIdentityEnvelope:
    """Construct one strict identity-bound decoded-frame record."""
    canonical, jpeg_bytes = canonicalize_frame(frame)
    return _make_frame_envelope_from_canonical(
        acquisition, decoder_operation_id, target_request_id, canonical, jpeg_bytes
    )


def _make_frame_envelope_from_canonical(
    acquisition: CommonSessionAcquisition,
    decoder_operation_id: str,
    target_request_id: str,
    frame: DecodedLocalFrame,
    jpeg_bytes: bytes,
) -> StrictIdentityEnvelope:
    """Bind a frame record to one internally generated, strictly reopened JPEG."""
    try:
        jpeg = compute_jpeg_integrity_from_bytes(jpeg_bytes, frame.width, frame.height)
    except ConfirmationArtifactError as exc:
        raise CommonSessionDecoderError from exc
    rgb_sha = rgb24_sha256(frame.rgb24_bytes, frame.width, frame.height)
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
        "jpeg_size_bytes": jpeg.size_bytes,
        "jpeg_sha256": jpeg.sha256,
        "rgb24_sha256": rgb_sha,
    }
    return StrictIdentityEnvelope.from_payload("frame", payload)


def reopened_frame_from_run(
    run: Phase7ERun,
    frame_id: str,
    requested_time_utc: datetime,
) -> DecodedLocalFrame:
    """Construct classifier input solely from a strictly reopened frame record and JPEG."""
    record = next(
        (item for item in run.records if item.family == "frame" and item.identity == frame_id),
        None,
    )
    raw = run.frame_bytes.get(frame_id)
    if record is None or type(raw) is not bytes:
        raise CommonSessionValidationError
    payload = record.payload
    try:
        with Image.open(BytesIO(raw)) as image:
            image.load()
            if image.format != "JPEG" or image.mode != "RGB":
                raise CommonSessionValidationError
            rgb24 = image.tobytes()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise CommonSessionValidationError from exc
    frame = DecodedLocalFrame(
        requested_time_utc=requested_time_utc,
        raw_pts=payload["raw_pts"],
        ordinal=payload["ordinal"],
        width=payload["width"],
        height=payload["height"],
        rgb24_bytes=rgb24,
        decoder_operation_id=payload["decoder_operation_id"],
        decode_session_id=payload["common_session_id"],
        container_start_pts=payload["container_start_pts"],
        time_base_num=payload["time_base_num"],
        time_base_den=payload["time_base_den"],
    )
    frame.validate()
    if frame.rgb24_sha256 != payload["rgb24_sha256"]:
        raise CommonSessionValidationError
    return frame


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
    if type(sequence) is not int or sequence < 0:
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
    allow_aliases: bool = False,
    budget: InvocationBudget | None = None,
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
    active_budget = budget or InvocationBudget(
        acquisition.request.policy,
        monotonic,
        cancellation,
    )
    if cancellation is not None and cancellation():
        raise CommonSessionCancelledError
    active_budget.admit_decoder_pass(len(ordered))
    usable_seconds = active_budget.operation_timeout(
        acquisition.request.policy.decoder_timeout_seconds,
        minimum_start_seconds=1.0,
        downstream_reserve_seconds=40.0,
    )
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
        active_budget.check()
        if _utc_second(frame.requested_time_utc) != target:
            raise CommonSessionDecoderError
        frame.validate(max_rgb24_frames=acquisition.request.policy.maximum_selected_rgb24_frames)
    validate_decoded_order(frames, allow_aliases=allow_aliases)
    if not allow_aliases:
        reject_duplicate_frame_evidence(frames)
    active_budget.check()
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
    classifier: B4Bridge,
    authoritative: Phase7EB4Input,
) -> object:
    """Invoke B4 only after the caller has strictly persisted/reopened a frame."""
    authoritative.budget.check()
    try:
        result = classifier.classify(authoritative)
    except CommonSessionError:
        raise
    except (OSError, TimeoutError, ValueError, TypeError, RecordingSearchError) as exc:
        raise CommonSessionError from exc
    authoritative.budget.check()
    return result


__all__ = [
    "B4Bridge",
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
    "CommonSessionMediaProbeTimeoutError",
    "CommonSessionMissingPtsError",
    "CommonSessionInvalidTimeBaseError",
    "CommonSessionNonmonotonicPtsError",
    "CommonSessionTimestampResetError",
    "CommonSessionRecordingGapError",
    "CommonSessionSegmentBoundaryError",
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
    "InvocationBudget",
    "Phase7EInvocation",
    "Phase7E1CExecutor",
    "Phase7EB4Input",
    "ProductionB4Adapter",
    "ProductionB4Context",
    "ProductionB4ContextFactory",
    "CommonSessionPersistenceAdapter",
    "bind_session",
    "canonicalize_frame",
    "classify_after_readback",
    "collapse_target_aliases",
    "admit_frame_then_classify",
    "append_schema6_indexes",
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
    "reopened_frame_from_run",
    "select_target_index",
    "validate_decoded_order",
    "validate_repeated_decode",
    "validate_jpeg_and_rgb24",
]
