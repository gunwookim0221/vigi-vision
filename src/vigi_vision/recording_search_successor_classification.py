"""Phase 7E successor Slice 3: bounded coarse target classification.

This module consumes the immutable Slice 1 plan and Slice 2 acquisition facts.
It deliberately does not publish a legacy Schema 5--7 result, perform binary
narrowing, or manufacture a terminal disappearance decision.  Visual work is
delegated to the existing process-isolated B4 EfficientSAM boundary.
"""

# The classifier boundary intentionally keeps the complete input explicit;
# suppress only style rules that would obscure those contract fields.
# ruff: noqa: D102, D105, D107, EM101, PLR0913, RUF021
# pyright: reportPrivateUsage=false, reportUnnecessaryIsInstance=false, reportUnreachable=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportAttributeAccessIssue=false, reportAny=false

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from time import perf_counter
from typing import TYPE_CHECKING, Protocol

from vigi_vision.investigation_confirmation_models import (
    ConfirmationRoi,
    ConfirmedInvestigationInput,
    is_investigation_id,
)
from vigi_vision.object_presence_evidence import ClassificationResult, RawComparison
from vigi_vision.object_presence_values import ClassificationOutcome, VisualStatus
from vigi_vision.recording_search_7e_b4_process import (
    B4ProcessError,
    B4ProcessTimeout,
    EfficientSamWorkerSpec,
    run_b4_in_process,
)
from vigi_vision.recording_search_b3_models import ClassificationPreparationError
from vigi_vision.recording_search_successor import TargetAvailability
from vigi_vision.recording_search_successor_acquisition import (
    SuccessorAcquisitionContractError,
    SuccessorFrameCandidate,
    SuccessorTargetAcquisitionResult,
    SuccessorTargetStatus,
    successor_anchor_target_id,
    successor_midpoint_target_id,
    successor_target_id,
)

if TYPE_CHECKING:
    from vigi_vision.object_presence_models import DecodedRgbImage
    from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
    from vigi_vision.recording_search_b3_contracts import MediaDecoder
    from vigi_vision.recording_search_successor import (
        CoarseTargetAssignment,
        MultiSegmentCoarsePlan,
    )

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TIMING_PRECISION_CODES = frozenset(
    {
        "measured_clip_relative",
        "estimated",
        "unavailable",
        "indeterminate",
        "MEASURED_CLIP_RELATIVE",
    }
)
_AUTHORITY_VERSION = "phase7e-successor-authority-v1"
_CLASSIFICATION_VERSION = "phase7e-successor-coarse-classification-v1"
_SAFE_REASON_CODES = frozenset(
    {
        "invalid_mask",
        "background_dominant",
        "insufficient_mask_overlap",
        "insufficient_comparison_area",
        "zero_luma_variance",
        "insufficient_visual_evidence",
        "invalid_frame_or_roi",
        "frame_decode_failed",
        "frame_resolution_mismatch",
        "classifier_timeout",
        "classifier_failed",
        "target_unavailable_gap",
        "target_recording_unavailable",
        "target_replay_timeout",
        "target_replay_failed",
        "target_decode_timeout",
        "target_decode_unavailable",
        "roi_occluded",
    }
)
_FALLBACK_REASON = "ROI_OCCLUDED"
_FALLBACK_REASONS = frozenset({_FALLBACK_REASON, "DECODE_UNAVAILABLE"})
_OBSERVABILITY_STATES = frozenset({"USABLE", "OCCLUDED", "DECODE_UNAVAILABLE"})
_MAX_FALLBACK_SECONDS = 10
_MAX_CANDIDATE_TRACE = 21
_CANDIDATE_TRACE_FIELDS = 6
CandidateTraceEntry = tuple[str, str, float, bool, str | None, bool]
_OBSERVABILITY_LOGGER = logging.getLogger("uvicorn.error.vigi_vision.phase7e")


class SuccessorClassificationContractError(ValueError):
    """Raised when successor or Phase 6 authority facts do not match."""


class SuccessorClassificationError(RuntimeError):
    """Safe target-local classifier failure."""

    def __init__(self, reason: str) -> None:
        if reason not in {"classifier_timeout", "classifier_failed"}:
            raise ValueError
        super().__init__(reason)
        self.reason: str = reason


class SuccessorObservationState(str, Enum):
    """Closed coarse observation and target-fact vocabulary."""

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    INDETERMINATE = "INDETERMINATE"
    UNAVAILABLE_GAP = "UNAVAILABLE_GAP"
    RECORDING_UNAVAILABLE = "RECORDING_UNAVAILABLE"
    REPLAY_TIMEOUT = "REPLAY_TIMEOUT"
    REPLAY_FAILED = "REPLAY_FAILED"
    DECODE_TIMEOUT = "DECODE_TIMEOUT"
    DECODE_UNAVAILABLE = "DECODE_UNAVAILABLE"
    CLASSIFIER_TIMEOUT = "CLASSIFIER_TIMEOUT"
    CLASSIFIER_FAILED = "CLASSIFIER_FAILED"

    @property
    def is_visual(self) -> bool:
        return self in {
            SuccessorObservationState.PRESENT,
            SuccessorObservationState.ABSENT,
            SuccessorObservationState.INDETERMINATE,
        }


@dataclass(frozen=True, slots=True)
class SuccessorClassificationAuthority:
    """Phase 6-owned baseline and ROI authority bound to one successor plan."""

    investigation_id: str
    successor_plan_id: str
    reference_frame_resource_id: str
    reference_frame_jpeg_sha256: str
    reference_frame_jpeg_size_bytes: int
    source_width: int
    source_height: int
    roi: ConfirmationRoi
    baseline_image: DecodedRgbImage = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not is_investigation_id(self.investigation_id)
            or not self.successor_plan_id.startswith("successor-plan-v1-")
            or not self.reference_frame_resource_id
            or _SHA256.fullmatch(self.reference_frame_jpeg_sha256) is None
            or type(self.reference_frame_jpeg_size_bytes) is not int
            or self.reference_frame_jpeg_size_bytes <= 0
            or type(self.source_width) is not int
            or type(self.source_height) is not int
            or self.source_width <= 0
            or self.source_height <= 0
            or self.baseline_image.width != self.source_width
            or self.baseline_image.height != self.source_height
            or self.roi.coordinate_space != "source_pixels"
            or self.roi.x + self.roi.width > self.source_width
            or self.roi.y + self.roi.height > self.source_height
        ):
            raise SuccessorClassificationContractError

    @classmethod
    def from_confirmed_input(
        cls,
        confirmed: ConfirmedInvestigationInput,
        *,
        successor_plan_id: str,
        baseline_image: DecodedRgbImage,
    ) -> SuccessorClassificationAuthority:
        """Copy only the strictly resolved Phase 6 facts into the plan."""
        if not isinstance(confirmed, ConfirmedInvestigationInput):
            raise SuccessorClassificationContractError
        return cls(
            confirmed.investigation_id,
            successor_plan_id,
            confirmed.reference_frame_resource_id,
            confirmed.jpeg_sha256,
            confirmed.jpeg_size_bytes,
            confirmed.source_width,
            confirmed.source_height,
            confirmed.roi,
            baseline_image,
        )

    @property
    def roi_identity(self) -> str:
        return _digest_identity("successor-roi-v1-", self.roi.model_dump(mode="json"))

    @property
    def authority_identity(self) -> str:
        return _digest_identity(
            "successor-authority-v1-",
            {
                "version": _AUTHORITY_VERSION,
                "investigation_id": self.investigation_id,
                "plan_id": self.successor_plan_id,
                "resource_id": self.reference_frame_resource_id,
                "jpeg_sha256": self.reference_frame_jpeg_sha256,
                "jpeg_size_bytes": self.reference_frame_jpeg_size_bytes,
                "source_width": self.source_width,
                "source_height": self.source_height,
                "roi_identity": self.roi_identity,
            },
        )


@dataclass(frozen=True, slots=True)
class SuccessorClassifierResult:
    """Safe visual output from one existing classifier invocation."""

    outcome: ClassificationOutcome
    reason_code: str | None = None
    comparison: RawComparison | None = field(default=None, repr=False)
    stage: str = "completed"
    elapsed_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ClassificationOutcome):
            raise SuccessorClassificationContractError
        if self.reason_code is not None and self.reason_code not in _SAFE_REASON_CODES:
            raise SuccessorClassificationContractError
        if self.outcome in {ClassificationOutcome.PRESENT, ClassificationOutcome.ABSENT}:
            if self.reason_code is not None:
                raise SuccessorClassificationContractError
        elif self.reason_code is None:
            raise SuccessorClassificationContractError
        if self.stage not in {"completed", "timeout", "failed"}:
            raise SuccessorClassificationContractError
        if self.elapsed_ms is not None and (
            type(self.elapsed_ms) is not int or self.elapsed_ms < 0
        ):
            raise SuccessorClassificationContractError


class SuccessorClassifier(Protocol):
    """Existing B4-shaped classifier boundary used serially by Slice 3."""

    @property
    def policy_identity(self) -> str:
        """Return the immutable classifier policy identity."""
        ...

    def classify(
        self,
        baseline_image: DecodedRgbImage,
        probe_image: DecodedRgbImage,
        source_width: int,
        source_height: int,
        roi: ConfirmationRoi,
        correlation_id: str,
    ) -> SuccessorClassifierResult:
        """Run one bounded visual classification."""
        ...


@dataclass(frozen=True, slots=True)
class EfficientSamSuccessorClassifier:
    """Production adapter over the existing spawned EfficientSAM B4 worker."""

    policy: ObjectPresenceDecisionPolicy
    worker_spec: EfficientSamWorkerSpec
    timeout_seconds: float
    startup_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or not math.isfinite(self.startup_timeout_seconds)
            or self.startup_timeout_seconds <= 0
        ):
            raise SuccessorClassificationContractError

    @property
    def policy_identity(self) -> str:
        return self.policy.identity

    def classify(
        self,
        baseline_image: DecodedRgbImage,
        probe_image: DecodedRgbImage,
        source_width: int,
        source_height: int,
        roi: ConfirmationRoi,
        correlation_id: str,
    ) -> SuccessorClassifierResult:
        started = perf_counter()
        try:
            result = run_b4_in_process(
                baseline_image=baseline_image,
                probe_image=probe_image,
                source_width=source_width,
                source_height=source_height,
                roi=roi,
                policy=self.policy,
                worker_spec=self.worker_spec,
                correlation_id=correlation_id,
                timeout_seconds=self.timeout_seconds,
                startup_timeout_seconds=self.startup_timeout_seconds,
            )
        except B4ProcessTimeout as error:
            raise SuccessorClassificationError("classifier_timeout") from error
        except (B4ProcessError, ClassificationPreparationError) as error:
            raise SuccessorClassificationError("classifier_failed") from error
        if not isinstance(result, ClassificationResult):
            raise SuccessorClassificationContractError
        elapsed_ms = max(0, round((perf_counter() - started) * 1000))
        return SuccessorClassifierResult(
            result.outcome,
            None if result.reason_code is None else result.reason_code.value,
            result.comparison,
            "completed",
            elapsed_ms,
        )


@dataclass(frozen=True, slots=True)
class ObservableFrameFallbackPolicy:
    """Bounded temporal neighborhood used when a ROI is not observable."""

    max_seconds: int = 10
    step_seconds: int = 1
    max_candidates: int = 21

    def __post_init__(self) -> None:
        if (
            type(self.max_seconds) is not int
            or not 0 < self.max_seconds <= _MAX_FALLBACK_SECONDS
            or type(self.step_seconds) is not int
            or not 0 < self.step_seconds <= self.max_seconds
            or type(self.max_candidates) is not int
            or not 1 <= self.max_candidates <= 2 * (self.max_seconds // self.step_seconds) + 1
        ):
            raise SuccessorClassificationContractError


@dataclass(frozen=True, slots=True)
class ObservableFrameResolution:
    """Selected frame plus a bounded, credential-free candidate trace."""

    selected: SuccessorFrameCandidate | None
    fallback_used: bool
    fallback_reason: str | None
    observability: str
    candidate_trace: tuple[CandidateTraceEntry, ...]

    def __post_init__(self) -> None:
        if (
            self.fallback_reason is not None and self.fallback_reason not in _FALLBACK_REASONS
        ) or self.observability not in _OBSERVABILITY_STATES:
            raise SuccessorClassificationContractError
        if self.fallback_used and self.selected is None:
            raise SuccessorClassificationContractError


@dataclass(frozen=True, slots=True)
class SuccessorObservation:
    """One target fact, ordered by observed frame time when available."""

    plan_id: str
    target_id: str
    acquisition_id: str
    sequence: int
    requested_time_utc: datetime
    frame_utc: datetime | None
    frame_pts_seconds: float | None
    frame_offset_seconds: float | None
    authority_identity: str
    reference_frame_resource_id: str
    roi_identity: str
    classifier_policy_identity: str
    acquisition_status: SuccessorTargetStatus
    state: SuccessorObservationState
    reason_code: str | None
    ordinal: int
    observation_id: str
    timing_precision_status: str | None = None
    timing_warnings: tuple[str, ...] = ()
    frame_sha256: str | None = None
    frame_bytes: bytes | None = field(default=None, repr=False, compare=False)
    frame_width: int | None = None
    frame_height: int | None = None
    comparison: dict[str, object] | None = None
    classifier_stage: str | None = None
    classifier_elapsed_ms: int | None = None
    assigned_segment_id: str | None = None
    acquisition_mode: str = "normal"
    target_delta_ms: int | None = None
    cadence_source: str | None = None
    cadence_ms: int | None = None
    tolerance_ms: int | None = None
    raw_segment_end_utc: datetime | None = None
    media_validation_outcome: str = "not_attempted"
    fallback_used: bool = False
    fallback_reason: str | None = None
    observability: str = "USABLE"
    candidate_trace: tuple[CandidateTraceEntry, ...] = ()

    def __post_init__(self) -> None:  # noqa: C901
        if (
            not self.plan_id
            or not self.target_id
            or not self.acquisition_id
            or type(self.sequence) is not int
            or self.sequence <= 0
            or not _is_utc(self.requested_time_utc)
            or self.frame_utc is not None
            and not _is_utc(self.frame_utc)
            or self.frame_pts_seconds is not None
            and (not math.isfinite(self.frame_pts_seconds) or self.frame_pts_seconds < 0)
            or self.frame_offset_seconds is not None
            and not math.isfinite(self.frame_offset_seconds)
            or not self.authority_identity
            or not self.reference_frame_resource_id
            or not self.roi_identity
            or not self.classifier_policy_identity
            or not isinstance(self.acquisition_status, SuccessorTargetStatus)
            or not isinstance(self.state, SuccessorObservationState)
            or type(self.ordinal) is not int
            or self.ordinal <= 0
            or not self.observation_id.startswith("successor-observation-v1-")
            or self.reason_code is not None
            and self.reason_code not in _SAFE_REASON_CODES
            or self.timing_precision_status is not None
            and self.timing_precision_status not in _TIMING_PRECISION_CODES
            or self.frame_sha256 is not None
            and _SHA256.fullmatch(self.frame_sha256) is None
            or self.frame_bytes is not None
            and (not isinstance(self.frame_bytes, bytes) or not self.frame_bytes)
            or self.frame_width is not None
            and (type(self.frame_width) is not int or self.frame_width <= 0)
            or self.frame_height is not None
            and (type(self.frame_height) is not int or self.frame_height <= 0)
            or self.classifier_stage is not None
            and self.classifier_stage not in {"completed", "timeout", "failed"}
            or self.classifier_elapsed_ms is not None
            and (type(self.classifier_elapsed_ms) is not int or self.classifier_elapsed_ms < 0)
            or self.assigned_segment_id is not None
            and not self.assigned_segment_id
            or self.acquisition_mode not in {"normal", "segment_end_fallback"}
            or self.target_delta_ms is not None
            and (type(self.target_delta_ms) is not int or self.target_delta_ms < 0)
            or self.cadence_source is not None
            and self.cadence_source != "adjacent_pts"
            or self.cadence_ms is not None
            and (type(self.cadence_ms) is not int or self.cadence_ms <= 0)
            or self.tolerance_ms is not None
            and (type(self.tolerance_ms) is not int or self.tolerance_ms <= 0)
            or self.raw_segment_end_utc is not None
            and not _is_utc(self.raw_segment_end_utc)
            or self.media_validation_outcome not in {"not_attempted", "validated", "failed"}
            or type(self.fallback_used) is not bool
            or self.fallback_reason is not None
            and self.fallback_reason not in _FALLBACK_REASONS
            or self.observability not in _OBSERVABILITY_STATES
            or not isinstance(self.candidate_trace, tuple)
            or len(self.candidate_trace) > _MAX_CANDIDATE_TRACE
        ):
            raise SuccessorClassificationContractError
        for trace in self.candidate_trace:
            if (
                not isinstance(trace, tuple)
                or len(trace) != _CANDIDATE_TRACE_FIELDS
                or not isinstance(trace[0], str)
                or not isinstance(trace[1], str)
                or type(trace[2]) not in {int, float}
                or not math.isfinite(trace[2])
                or type(trace[3]) is not bool
                or (trace[4] is not None and not isinstance(trace[4], str))
                or type(trace[5]) is not bool
                or trace[5]
                and not trace[3]
            ):
                raise SuccessorClassificationContractError
        if self.fallback_used and self.fallback_reason not in _FALLBACK_REASONS:
            raise SuccessorClassificationContractError
        if (
            self.state
            in {
                SuccessorObservationState.PRESENT,
                SuccessorObservationState.ABSENT,
                SuccessorObservationState.CLASSIFIER_TIMEOUT,
                SuccessorObservationState.CLASSIFIER_FAILED,
            }
            and self.frame_utc is None
        ):
            raise SuccessorClassificationContractError
        if self.state is SuccessorObservationState.INDETERMINATE and self.reason_code is None:
            raise SuccessorClassificationContractError
        if not self.state.is_visual and self.reason_code is None:
            raise SuccessorClassificationContractError
        if (
            self.state
            in {
                SuccessorObservationState.UNAVAILABLE_GAP,
                SuccessorObservationState.RECORDING_UNAVAILABLE,
                SuccessorObservationState.REPLAY_TIMEOUT,
                SuccessorObservationState.REPLAY_FAILED,
                SuccessorObservationState.DECODE_TIMEOUT,
                SuccessorObservationState.DECODE_UNAVAILABLE,
            }
            and self.frame_utc is not None
        ):
            raise SuccessorClassificationContractError
        if self.acquisition_mode == "segment_end_fallback" and (
            self.raw_segment_end_utc is None or self.raw_segment_end_utc > self.requested_time_utc
        ):
            raise SuccessorClassificationContractError
        if self.acquisition_mode == "segment_end_fallback" and (
            self.acquisition_status is SuccessorTargetStatus.FRAME_AVAILABLE
            and (
                self.target_delta_ms is None
                or self.cadence_source != "adjacent_pts"
                or self.cadence_ms is None
                or self.tolerance_ms is None
                or self.target_delta_ms > self.tolerance_ms
            )
        ):
            raise SuccessorClassificationContractError


@dataclass(frozen=True, slots=True)
class SuccessorCandidateBracket:
    """A provisional adjacent PRESENT → ABSENT coarse pair."""

    present_observation_id: str
    absent_observation_id: str
    present_frame_utc: datetime
    absent_frame_utc: datetime


@dataclass(frozen=True, slots=True)
class SuccessorCoarseClassificationResult:
    """Ordered Slice 3 output consumed by future binary narrowing."""

    plan_id: str
    authority_identity: str
    observations: tuple[SuccessorObservation, ...]
    candidate_bracket: SuccessorCandidateBracket | None

    @property
    def visual_counts(self) -> dict[SuccessorObservationState, int]:
        return {
            state: sum(item.state is state for item in self.observations)
            for state in SuccessorObservationState
            if state.is_visual
        }


@dataclass(frozen=True, slots=True)
class SuccessorCoarseClassificationService:
    """Classify each acquired target independently and in plan order."""

    classifier: SuccessorClassifier
    media_decoder: MediaDecoder
    fallback_policy: ObservableFrameFallbackPolicy = field(
        default_factory=ObservableFrameFallbackPolicy
    )

    def classify_plan(
        self,
        plan: MultiSegmentCoarsePlan,
        acquisitions: tuple[SuccessorTargetAcquisitionResult, ...],
        authority: SuccessorClassificationAuthority,
    ) -> SuccessorCoarseClassificationResult:
        self._validate_authority(plan, authority)
        expected_targets = {successor_target_id(plan, target): target for target in plan.targets}
        if len(acquisitions) != len(expected_targets):
            raise SuccessorClassificationContractError
        seen: set[str] = set()
        facts: list[SuccessorObservation] = []
        for acquisition in acquisitions:
            target = expected_targets.get(acquisition.target_id)
            if (
                target is None
                or acquisition.plan_id != plan.plan_id
                or acquisition.target_id in seen
                or acquisition.sequence != target.sequence
                or acquisition.requested_time_utc != target.requested_time_utc
            ):
                raise SuccessorClassificationContractError
            seen.add(acquisition.target_id)
            facts.append(self._classify_target(plan, target, acquisition, authority))
        if seen != set(expected_targets):
            raise SuccessorClassificationContractError
        ordered = tuple(
            sorted(
                facts,
                key=lambda item: (
                    item.frame_utc or item.requested_time_utc,
                    item.observation_id,
                ),
            )
        )
        renumbered = tuple(_with_ordinal(item, index) for index, item in enumerate(ordered, 1))
        return SuccessorCoarseClassificationResult(
            plan.plan_id,
            authority.authority_identity,
            renumbered,
            _candidate_bracket(renumbered),
        )

    def classify_coarse_target(
        self,
        plan: MultiSegmentCoarsePlan,
        target: CoarseTargetAssignment,
        acquisition: SuccessorTargetAcquisitionResult,
        authority: SuccessorClassificationAuthority,
    ) -> SuccessorObservation:
        """Classify one chronological coarse target without requiring a full plan."""
        self._validate_authority(plan, authority)
        if (
            target not in plan.targets
            or acquisition.plan_id != plan.plan_id
            or acquisition.target_id != successor_target_id(plan, target)
            or acquisition.sequence != target.sequence
            or acquisition.requested_time_utc != target.requested_time_utc
            or acquisition.assigned_segment_id != target.segment_id
        ):
            raise SuccessorClassificationContractError
        return self._classify_target(plan, target, acquisition, authority)

    def classify_target(
        self,
        plan: MultiSegmentCoarsePlan,
        target: CoarseTargetAssignment,
        acquisition: SuccessorTargetAcquisitionResult,
        authority: SuccessorClassificationAuthority,
    ) -> SuccessorObservation:
        """Classify one bounded target, including a non-coarse midpoint target."""
        self._validate_authority(plan, authority)
        if (
            target.availability is not TargetAvailability.AVAILABLE
            or acquisition.plan_id != plan.plan_id
            or acquisition.target_id != successor_midpoint_target_id(plan, target)
            or acquisition.sequence != target.sequence
            or acquisition.requested_time_utc != target.requested_time_utc
            or acquisition.assigned_segment_id != target.segment_id
        ):
            raise SuccessorClassificationContractError
        return self._classify_target(plan, target, acquisition, authority)

    def classify_anchor_target(
        self,
        plan: MultiSegmentCoarsePlan,
        target: CoarseTargetAssignment,
        acquisition: SuccessorTargetAcquisitionResult,
        authority: SuccessorClassificationAuthority,
    ) -> SuccessorObservation:
        """Classify the actual frame at the successor search anchor."""
        self._validate_authority(plan, authority)
        if (
            acquisition.plan_id != plan.plan_id
            or acquisition.target_id != successor_anchor_target_id(plan, target)
            or acquisition.sequence != target.sequence
            or acquisition.requested_time_utc != target.requested_time_utc
            or acquisition.assigned_segment_id != target.segment_id
        ):
            raise SuccessorClassificationContractError
        return self._classify_target(plan, target, acquisition, authority)

    def _validate_authority(
        self, plan: MultiSegmentCoarsePlan, authority: SuccessorClassificationAuthority
    ) -> None:
        if authority.successor_plan_id != plan.plan_id:
            raise SuccessorClassificationContractError
        if (
            authority.baseline_image.width != authority.source_width
            or authority.baseline_image.height != authority.source_height
        ):
            raise SuccessorClassificationContractError
        if self.classifier.policy_identity == "":
            raise SuccessorClassificationContractError

    def validate_authority(
        self, plan: MultiSegmentCoarsePlan, authority: SuccessorClassificationAuthority
    ) -> None:
        """Validate Phase 6 authority before a standalone target classification."""
        self._validate_authority(plan, authority)

    def _classify_target(
        self,
        plan: MultiSegmentCoarsePlan,
        target: CoarseTargetAssignment,
        acquisition: SuccessorTargetAcquisitionResult,
        authority: SuccessorClassificationAuthority,
    ) -> SuccessorObservation:
        if acquisition.status is not SuccessorTargetStatus.FRAME_AVAILABLE:
            state = SuccessorObservationState(acquisition.status.value)
            return _observation(
                plan,
                target,
                acquisition,
                authority,
                state,
                f"target_{state.value.lower()}",
                self.classifier.policy_identity,
            )
        if not _roi_valid(authority.roi, authority.source_width, authority.source_height):
            return _observation(
                plan,
                target,
                acquisition,
                authority,
                SuccessorObservationState.INDETERMINATE,
                "invalid_frame_or_roi",
                self.classifier.policy_identity,
            )
        candidates = _ordered_candidates(acquisition, target, self.fallback_policy)
        if not candidates:
            return _observation(
                plan,
                target,
                acquisition,
                authority,
                SuccessorObservationState.INDETERMINATE,
                "invalid_frame_or_roi",
                self.classifier.policy_identity,
            )
        trace: list[CandidateTraceEntry] = []
        saw_occluded = False
        saw_decode_failure = False
        first_decode_reason: str | None = None
        last_comparison: dict[str, object] | None = None
        primary = candidates[0]
        for index, candidate in enumerate(candidates):
            evaluation = self._evaluate_candidate(candidate, authority, acquisition.acquisition_id)
            if evaluation[0] == "decode_failed":
                saw_decode_failure = True
                decode_reason = (
                    evaluation[1] if isinstance(evaluation[1], str) else "frame_decode_failed"
                )
                if first_decode_reason is None:
                    first_decode_reason = decode_reason
                trace.append(
                    (
                        _timestamp(candidate.candidate_time_utc),
                        _timestamp(candidate.frame_utc),
                        (candidate.frame_utc - target.requested_time_utc).total_seconds(),
                        False,
                        decode_reason,
                        False,
                    )
                )
                continue
            if evaluation[0] == "classifier_error":
                error_reason = (
                    evaluation[1] if isinstance(evaluation[1], str) else "classifier_failed"
                )
                classifier_elapsed_ms = evaluation[2] if isinstance(evaluation[2], int) else None
                state = (
                    SuccessorObservationState.CLASSIFIER_TIMEOUT
                    if error_reason == "classifier_timeout"
                    else SuccessorObservationState.CLASSIFIER_FAILED
                )
                trace.append(
                    (
                        _timestamp(candidate.candidate_time_utc),
                        _timestamp(candidate.frame_utc),
                        (candidate.frame_utc - target.requested_time_utc).total_seconds(),
                        False,
                        error_reason,
                        False,
                    )
                )
                return _observation(
                    plan,
                    target,
                    acquisition,
                    authority,
                    state,
                    error_reason,
                    self.classifier.policy_identity,
                    frame_candidate=candidate,
                    fallback_used=index > 0,
                    fallback_reason=(
                        _FALLBACK_REASON
                        if saw_occluded
                        else ("DECODE_UNAVAILABLE" if saw_decode_failure else None)
                    )
                    if index > 0
                    else None,
                    observability="USABLE",
                    candidate_trace=tuple(trace),
                    classifier_stage="timeout"
                    if error_reason == "classifier_timeout"
                    else "failed",
                    classifier_elapsed_ms=classifier_elapsed_ms,
                )
            classified = evaluation[1]
            if not isinstance(classified, SuccessorClassifierResult):
                raise SuccessorClassificationContractError
            comparison = _safe_comparison(classified.comparison)
            if _is_occluded_result(classified):
                saw_occluded = True
                last_comparison = comparison
                trace.append(
                    (
                        _timestamp(candidate.candidate_time_utc),
                        _timestamp(candidate.frame_utc),
                        (candidate.frame_utc - target.requested_time_utc).total_seconds(),
                        False,
                        _FALLBACK_REASON,
                        False,
                    )
                )
                continue
            trace.append(
                (
                    _timestamp(candidate.candidate_time_utc),
                    _timestamp(candidate.frame_utc),
                    (candidate.frame_utc - target.requested_time_utc).total_seconds(),
                    True,
                    classified.reason_code,
                    True,
                )
            )
            return _observation(
                plan,
                target,
                acquisition,
                authority,
                SuccessorObservationState(classified.outcome.value),
                classified.reason_code,
                self.classifier.policy_identity,
                comparison=comparison,
                classifier_stage=classified.stage,
                classifier_elapsed_ms=classified.elapsed_ms,
                frame_candidate=candidate,
                fallback_used=index > 0,
                fallback_reason=(
                    _FALLBACK_REASON
                    if saw_occluded
                    else ("DECODE_UNAVAILABLE" if saw_decode_failure else None)
                )
                if index > 0
                else None,
                observability="USABLE",
                candidate_trace=tuple(trace),
            )
        fallback_reason = _FALLBACK_REASON if saw_occluded else "DECODE_UNAVAILABLE"
        return _observation(
            plan,
            target,
            acquisition,
            authority,
            SuccessorObservationState.INDETERMINATE,
            "roi_occluded" if saw_occluded else (first_decode_reason or "frame_decode_failed"),
            self.classifier.policy_identity,
            comparison=last_comparison,
            frame_candidate=primary,
            fallback_reason=fallback_reason,
            observability="OCCLUDED" if saw_occluded else "DECODE_UNAVAILABLE",
            candidate_trace=tuple(trace),
        )

    def _evaluate_candidate(
        self,
        candidate: SuccessorFrameCandidate,
        authority: SuccessorClassificationAuthority,
        correlation_id: str,
    ) -> tuple[object, ...]:
        if (
            candidate.frame_width != authority.source_width
            or candidate.frame_height != authority.source_height
        ):
            return ("decode_failed", "invalid_frame_or_roi", None)
        try:
            decoded = self.media_decoder.decode(
                candidate.frame_bytes,
                authority.source_width,
                authority.source_height,
            )
        except (OSError, TypeError, ValueError):
            return ("decode_failed", "frame_decode_failed", None)
        if (
            decoded.image.width != authority.source_width
            or decoded.image.height != authority.source_height
        ):
            return ("decode_failed", "frame_resolution_mismatch", None)
        classifier_started = perf_counter()
        try:
            classified = self.classifier.classify(
                authority.baseline_image,
                decoded.image,
                authority.source_width,
                authority.source_height,
                authority.roi,
                correlation_id,
            )
        except SuccessorClassificationError as error:
            return (
                "classifier_error",
                error.reason,
                max(0, round((perf_counter() - classifier_started) * 1000)),
            )
        if not isinstance(classified, SuccessorClassifierResult):
            raise SuccessorClassificationContractError
        return ("classified", classified, classified.elapsed_ms)


def _ordered_candidates(
    acquisition: SuccessorTargetAcquisitionResult,
    target: CoarseTargetAssignment,
    policy: ObservableFrameFallbackPolicy,
) -> tuple[SuccessorFrameCandidate, ...]:
    """Return target-centered candidates inside the one acquired window."""
    if (
        acquisition.frame_bytes is None
        or acquisition.frame_utc is None
        or acquisition.frame_pts_seconds is None
        or acquisition.frame_offset_seconds is None
        or acquisition.frame_sha256 is None
        or acquisition.frame_size_bytes is None
        or acquisition.frame_width is None
        or acquisition.frame_height is None
        or acquisition.replay_window is None
    ):
        return ()
    try:
        primary = SuccessorFrameCandidate(
            target.requested_time_utc,
            acquisition.frame_utc,
            acquisition.frame_pts_seconds,
            acquisition.frame_offset_seconds,
            acquisition.frame_bytes,
            acquisition.frame_sha256,
            acquisition.frame_size_bytes,
            acquisition.frame_width,
            acquisition.frame_height,
            acquisition.frame_warnings,
            acquisition.timing_precision_status,
        )
    except (SuccessorAcquisitionContractError, TypeError, ValueError):
        return ()
    all_candidates = (primary, *acquisition.frame_candidates)
    lower = acquisition.replay_window.start_utc
    upper = acquisition.replay_window.end_utc
    bounded: list[SuccessorFrameCandidate] = [primary]
    seen_requested: set[datetime] = {target.requested_time_utc}
    for candidate in all_candidates[1:]:
        distance = abs((candidate.candidate_time_utc - target.requested_time_utc).total_seconds())
        if (
            candidate.candidate_time_utc in seen_requested
            or distance > policy.max_seconds
            or not math.isclose(
                distance / policy.step_seconds, round(distance / policy.step_seconds)
            )
            or not lower <= candidate.candidate_time_utc <= upper
            or not lower <= candidate.frame_utc <= upper
        ):
            continue
        seen_requested.add(candidate.candidate_time_utc)
        bounded.append(candidate)
    bounded.sort(
        key=lambda item: (
            abs((item.candidate_time_utc - target.requested_time_utc).total_seconds()),
            item.candidate_time_utc,
            item.frame_utc,
        )
    )
    return tuple(bounded[: policy.max_candidates])


def _is_occluded_result(classified: SuccessorClassifierResult) -> bool:
    """Use only existing classifier unusable grounds to trigger fallback."""
    comparison = classified.comparison
    return (
        classified.outcome is ClassificationOutcome.INDETERMINATE
        and comparison is not None
        and comparison.visual_status is VisualStatus.UNUSABLE
        and comparison.unusable_reason is not None
    )


def _observation(
    plan: MultiSegmentCoarsePlan,
    target: CoarseTargetAssignment,
    acquisition: SuccessorTargetAcquisitionResult,
    authority: SuccessorClassificationAuthority,
    state: SuccessorObservationState,
    reason_code: str | None,
    policy_identity: str,
    frame_utc: datetime | None = None,
    frame_pts_seconds: float | None = None,
    frame_offset_seconds: float | None = None,
    *,
    comparison: dict[str, object] | None = None,
    classifier_stage: str | None = None,
    classifier_elapsed_ms: int | None = None,
    frame_candidate: SuccessorFrameCandidate | None = None,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
    observability: str = "USABLE",
    candidate_trace: tuple[CandidateTraceEntry, ...] = (),
) -> SuccessorObservation:
    if frame_candidate is not None:
        frame_utc = frame_candidate.frame_utc
        frame_pts_seconds = frame_candidate.frame_pts_seconds
        frame_offset_seconds = (
            frame_candidate.frame_utc - target.requested_time_utc
        ).total_seconds()
    elif acquisition.frame_utc is not None:
        frame_utc = acquisition.frame_utc
        frame_pts_seconds = acquisition.frame_pts_seconds
        frame_offset_seconds = acquisition.frame_offset_seconds
    frame_sha256 = (
        acquisition.frame_sha256 if frame_candidate is None else frame_candidate.frame_sha256
    )
    frame_bytes = (
        acquisition.frame_bytes if frame_candidate is None else frame_candidate.frame_bytes
    )
    frame_width = (
        acquisition.frame_width if frame_candidate is None else frame_candidate.frame_width
    )
    frame_height = (
        acquisition.frame_height if frame_candidate is None else frame_candidate.frame_height
    )
    payload = {
        "version": _CLASSIFICATION_VERSION,
        "plan_id": plan.plan_id,
        "target_id": acquisition.target_id,
        "acquisition_id": acquisition.acquisition_id,
        "sequence": target.sequence,
        "requested_time_utc": _timestamp(target.requested_time_utc),
        "frame_utc": None if frame_utc is None else _timestamp(frame_utc),
        "frame_pts_seconds": frame_pts_seconds,
        "frame_offset_seconds": frame_offset_seconds,
        "authority_identity": authority.authority_identity,
        "reference_frame_resource_id": authority.reference_frame_resource_id,
        "roi_identity": authority.roi_identity,
        "policy_identity": policy_identity,
        "state": state.value,
        "reason_code": reason_code,
        "timing_precision_status": acquisition.timing_precision_status,
        "frame_sha256": frame_sha256,
        "assigned_segment_id": acquisition.assigned_segment_id,
        "acquisition_mode": acquisition.acquisition_mode,
        "target_delta_ms": acquisition.target_delta_ms,
        "cadence_source": acquisition.cadence_source,
        "cadence_ms": acquisition.cadence_ms,
        "tolerance_ms": acquisition.tolerance_ms,
        "raw_segment_end_utc": (
            None
            if acquisition.raw_segment_end_utc is None
            else _timestamp(acquisition.raw_segment_end_utc)
        ),
        "media_validation_outcome": acquisition.media_validation_outcome,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "observability": observability,
    }
    identity = _digest_identity("successor-observation-v1-", payload)
    _emit_observable_trace(target.requested_time_utc, candidate_trace)
    return SuccessorObservation(
        plan.plan_id,
        acquisition.target_id,
        acquisition.acquisition_id,
        target.sequence,
        target.requested_time_utc,
        frame_utc,
        frame_pts_seconds,
        frame_offset_seconds,
        authority.authority_identity,
        authority.reference_frame_resource_id,
        authority.roi_identity,
        policy_identity,
        acquisition.status,
        state,
        reason_code,
        1,
        identity,
        acquisition.timing_precision_status,
        acquisition.frame_warnings,
        frame_sha256,
        frame_bytes,
        frame_width,
        frame_height,
        comparison,
        classifier_stage,
        classifier_elapsed_ms,
        acquisition.assigned_segment_id,
        acquisition.acquisition_mode,
        acquisition.target_delta_ms,
        acquisition.cadence_source,
        acquisition.cadence_ms,
        acquisition.tolerance_ms,
        acquisition.raw_segment_end_utc,
        acquisition.media_validation_outcome,
        fallback_used,
        fallback_reason,
        observability,
        candidate_trace,
    )


def _with_ordinal(item: SuccessorObservation, ordinal: int) -> SuccessorObservation:
    return SuccessorObservation(
        item.plan_id,
        item.target_id,
        item.acquisition_id,
        item.sequence,
        item.requested_time_utc,
        item.frame_utc,
        item.frame_pts_seconds,
        item.frame_offset_seconds,
        item.authority_identity,
        item.reference_frame_resource_id,
        item.roi_identity,
        item.classifier_policy_identity,
        item.acquisition_status,
        item.state,
        item.reason_code,
        ordinal,
        item.observation_id,
        item.timing_precision_status,
        item.timing_warnings,
        item.frame_sha256,
        item.frame_bytes,
        item.frame_width,
        item.frame_height,
        item.comparison,
        item.classifier_stage,
        item.classifier_elapsed_ms,
        item.assigned_segment_id,
        item.acquisition_mode,
        item.target_delta_ms,
        item.cadence_source,
        item.cadence_ms,
        item.tolerance_ms,
        item.raw_segment_end_utc,
        item.media_validation_outcome,
        item.fallback_used,
        item.fallback_reason,
        item.observability,
        item.candidate_trace,
    )


def reidentify_observation(
    item: SuccessorObservation, *, sequence: int, ordinal: int
) -> SuccessorObservation:
    """Rebind an observation identity after deterministic ordering changes."""
    payload = {
        "version": _CLASSIFICATION_VERSION,
        "plan_id": item.plan_id,
        "target_id": item.target_id,
        "acquisition_id": item.acquisition_id,
        "sequence": sequence,
        "requested_time_utc": _timestamp(item.requested_time_utc),
        "frame_utc": None if item.frame_utc is None else _timestamp(item.frame_utc),
        "frame_pts_seconds": item.frame_pts_seconds,
        "authority_identity": item.authority_identity,
        "reference_frame_resource_id": item.reference_frame_resource_id,
        "roi_identity": item.roi_identity,
        "policy_identity": item.classifier_policy_identity,
        "state": item.state.value,
        "reason_code": item.reason_code,
        "timing_precision_status": item.timing_precision_status,
        "frame_sha256": item.frame_sha256,
        "assigned_segment_id": item.assigned_segment_id,
        "acquisition_mode": item.acquisition_mode,
        "target_delta_ms": item.target_delta_ms,
        "cadence_source": item.cadence_source,
        "cadence_ms": item.cadence_ms,
        "tolerance_ms": item.tolerance_ms,
        "raw_segment_end_utc": (
            None if item.raw_segment_end_utc is None else _timestamp(item.raw_segment_end_utc)
        ),
        "media_validation_outcome": item.media_validation_outcome,
        "fallback_used": item.fallback_used,
        "fallback_reason": item.fallback_reason,
        "observability": item.observability,
    }
    return SuccessorObservation(
        item.plan_id,
        item.target_id,
        item.acquisition_id,
        sequence,
        item.requested_time_utc,
        item.frame_utc,
        item.frame_pts_seconds,
        item.frame_offset_seconds,
        item.authority_identity,
        item.reference_frame_resource_id,
        item.roi_identity,
        item.classifier_policy_identity,
        item.acquisition_status,
        item.state,
        item.reason_code,
        ordinal,
        _digest_identity("successor-observation-v1-", payload),
        item.timing_precision_status,
        item.timing_warnings,
        item.frame_sha256,
        item.frame_bytes,
        item.frame_width,
        item.frame_height,
        item.comparison,
        item.classifier_stage,
        item.classifier_elapsed_ms,
        item.assigned_segment_id,
        item.acquisition_mode,
        item.target_delta_ms,
        item.cadence_source,
        item.cadence_ms,
        item.tolerance_ms,
        item.raw_segment_end_utc,
        item.media_validation_outcome,
        item.fallback_used,
        item.fallback_reason,
        item.observability,
        item.candidate_trace,
    )


def _candidate_bracket(
    observations: tuple[SuccessorObservation, ...],
) -> SuccessorCandidateBracket | None:
    for previous, current in itertools.pairwise(observations):
        if (
            previous.state is SuccessorObservationState.PRESENT
            and current.state is SuccessorObservationState.ABSENT
            and current.sequence == previous.sequence + 1
            and previous.frame_utc is not None
            and current.frame_utc is not None
        ):
            return SuccessorCandidateBracket(
                previous.observation_id,
                current.observation_id,
                previous.frame_utc,
                current.frame_utc,
            )
    return None


def _roi_valid(roi: ConfirmationRoi, width: int, height: int) -> bool:
    return (
        roi.coordinate_space == "source_pixels"
        and roi.x >= 0
        and roi.y >= 0
        and roi.width > 0
        and roi.height > 0
        and roi.x + roi.width <= width
        and roi.y + roi.height <= height
    )


def _emit_observable_trace(
    requested_time_utc: datetime, trace: tuple[CandidateTraceEntry, ...]
) -> None:
    """Emit one bounded, credential-free candidate-resolution profile."""
    if not trace:
        return
    payload = {
        "event": "phase7e.observable_frame",
        "requested_time_utc": _timestamp(requested_time_utc),
        "candidate_trace": [
            {
                "candidate_time_utc": item[0],
                "frame_utc": item[1],
                "candidate_offset_seconds": item[2],
                "observable": item[3],
                "reason": item[4],
                "selected": item[5],
            }
            for item in trace
        ],
    }
    try:
        _OBSERVABILITY_LOGGER.info(
            "phase7e.observable_frame %s",
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    except Exception:  # noqa: BLE001 - diagnostic sinks cannot affect classification.
        return


def _digest_identity(prefix: str, payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return f"{prefix}{hashlib.sha256(encoded).hexdigest()}"


def _safe_comparison(comparison: object) -> dict[str, object] | None:
    """Keep only the bounded numeric classifier evidence matrix."""
    if comparison is None or not hasattr(comparison, "model_dump"):
        return None
    raw = comparison.model_dump(mode="json")
    allowed = {
        "baseline_mask_pixel_count",
        "probe_mask_pixel_count",
        "roi_pixel_count",
        "mask_intersection_pixel_count",
        "mask_union_pixel_count",
        "baseline_mask_coverage",
        "probe_mask_coverage",
        "mask_iou",
        "effective_comparison_area",
        "roi_luma_ncc",
        "comparison_mode",
        "baseline_support_pixel_count",
        "baseline_support_luma_similarity",
        "baseline_support_luma_ncc",
        "baseline_support_edge_similarity",
        "baseline_support_change_ratio",
        "baseline_support_foreground_retention",
        "baseline_support_background_change_ratio",
        "baseline_support_alignment_dx",
        "baseline_support_alignment_dy",
        "baseline_support_alignment_rotation_degrees",
        "baseline_support_alignment_overlap",
        "baseline_support_alignment_score",
        "baseline_support_alignment_margin",
        "baseline_support_stability_pixel_count",
        "baseline_support_stability_changed_pixel_count",
        "baseline_support_stability_valid_pixel_count",
        "baseline_support_stability_excluded_pixel_count",
        "baseline_support_alignment_candidates_generated",
        "baseline_support_alignment_candidates_evaluated",
        "baseline_support_alignment_valid_candidates",
        "baseline_support_alignment_state",
        "baseline_support_scene_stable",
        "baseline_support_scene_stability_veto_reason",
        "baseline_support_present_gate_passed",
        "baseline_support_absent_gate_passed",
        "baseline_support_empty_background_evidence",
        "baseline_support_replacement_evidence",
        "baseline_support_occlusion_evidence",
        "baseline_support_decision_path",
        "baseline_support_decision_reason",
        "visual_status",
        "unusable_reason",
    }
    return {key: raw[key] for key in sorted(allowed) if key in raw}


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timezone.utc.utcoffset(value)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


__all__ = (
    "EfficientSamSuccessorClassifier",
    "ObservableFrameFallbackPolicy",
    "ObservableFrameResolution",
    "SuccessorCandidateBracket",
    "SuccessorClassificationAuthority",
    "SuccessorClassificationContractError",
    "SuccessorClassificationError",
    "SuccessorClassifier",
    "SuccessorClassifierResult",
    "SuccessorCoarseClassificationResult",
    "SuccessorCoarseClassificationService",
    "SuccessorObservation",
    "SuccessorObservationState",
    "reidentify_observation",
)
