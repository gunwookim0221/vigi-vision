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
from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_7e_b4_process import (
    B4ProcessError,
    B4ProcessTimeout,
    EfficientSamWorkerSpec,
    run_b4_in_process,
)
from vigi_vision.recording_search_b3_models import ClassificationPreparationError
from vigi_vision.recording_search_successor import TargetAvailability
from vigi_vision.recording_search_successor_acquisition import (
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
    }
)


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

    def __post_init__(self) -> None:
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
        ):
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
        if (
            acquisition.frame_bytes is None
            or acquisition.frame_width != authority.source_width
            or acquisition.frame_height != authority.source_height
            or not _roi_valid(authority.roi, authority.source_width, authority.source_height)
        ):
            return _observation(
                plan,
                target,
                acquisition,
                authority,
                SuccessorObservationState.INDETERMINATE,
                "invalid_frame_or_roi",
                self.classifier.policy_identity,
            )
        try:
            decoded = self.media_decoder.decode(
                acquisition.frame_bytes,
                authority.source_width,
                authority.source_height,
            )
        except (OSError, TypeError, ValueError):
            return _observation(
                plan,
                target,
                acquisition,
                authority,
                SuccessorObservationState.INDETERMINATE,
                "frame_decode_failed",
                self.classifier.policy_identity,
            )
        if (
            decoded.image.width != authority.source_width
            or decoded.image.height != authority.source_height
        ):
            return _observation(
                plan,
                target,
                acquisition,
                authority,
                SuccessorObservationState.INDETERMINATE,
                "frame_resolution_mismatch",
                self.classifier.policy_identity,
            )
        classifier_started = perf_counter()
        try:
            classified = self.classifier.classify(
                authority.baseline_image,
                decoded.image,
                authority.source_width,
                authority.source_height,
                authority.roi,
                acquisition.acquisition_id,
            )
        except SuccessorClassificationError as error:
            state = (
                SuccessorObservationState.CLASSIFIER_TIMEOUT
                if error.reason == "classifier_timeout"
                else SuccessorObservationState.CLASSIFIER_FAILED
            )
            return _observation(
                plan,
                target,
                acquisition,
                authority,
                state,
                error.reason,
                self.classifier.policy_identity,
                classifier_stage="timeout" if error.reason == "classifier_timeout" else "failed",
                classifier_elapsed_ms=max(0, round((perf_counter() - classifier_started) * 1000)),
            )
        if not isinstance(classified, SuccessorClassifierResult):
            raise SuccessorClassificationContractError
        return _observation(
            plan,
            target,
            acquisition,
            authority,
            SuccessorObservationState(classified.outcome.value),
            classified.reason_code,
            self.classifier.policy_identity,
            comparison=_safe_comparison(classified.comparison),
            classifier_stage=classified.stage,
            classifier_elapsed_ms=classified.elapsed_ms,
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
) -> SuccessorObservation:
    if acquisition.frame_utc is not None:
        frame_utc = acquisition.frame_utc
        frame_pts_seconds = acquisition.frame_pts_seconds
        frame_offset_seconds = acquisition.frame_offset_seconds
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
        "frame_sha256": acquisition.frame_sha256,
        "assigned_segment_id": acquisition.assigned_segment_id,
    }
    identity = _digest_identity("successor-observation-v1-", payload)
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
        acquisition.frame_sha256,
        acquisition.frame_bytes,
        acquisition.frame_width,
        acquisition.frame_height,
        comparison,
        classifier_stage,
        classifier_elapsed_ms,
        acquisition.assigned_segment_id,
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
