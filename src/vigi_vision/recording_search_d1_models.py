"""Typed non-persistent state and outcomes for Phase 7D-1 narrowing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_c1_models import CoarseSampleStatus

_DIGEST_LENGTH = 64


class NarrowingStatus(str, Enum):
    """Internal, non-persistent narrowing outcomes."""

    READY = "NARROWED_BRACKET_READY"
    INDETERMINATE = "INDETERMINATE"
    INTERRUPTED = "INTERRUPTED"
    CORRUPT = "CORRUPT"
    INCOMPLETE = "INCOMPLETE"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"


class NarrowingStopReason(str, Enum):
    """Reasons for returning a non-persistent narrowed bracket."""

    TARGET_PRECISION_REACHED = "target_precision_reached"
    NO_DISTINCT_MIDPOINT = "no_distinct_midpoint"
    MAXIMUM_ITERATIONS = "maximum_iterations"


@dataclass(frozen=True, slots=True)
class NarrowingTarget:
    """One deterministic interior midpoint request."""

    target_id: str
    requested_time_utc: datetime
    lower_bound_utc: datetime
    upper_bound_utc: datetime
    iteration: int
    source_bracket_id: str
    policy_version: str

    def __post_init__(self) -> None:
        """Validate whole-second UTC bounds and strict interior identity."""
        _require_whole_utc(self.requested_time_utc)
        _require_whole_utc(self.lower_bound_utc)
        _require_whole_utc(self.upper_bound_utc)
        if (
            not self.target_id
            or not self.source_bracket_id
            or not self.policy_version
            or type(self.iteration) is not int
            or self.iteration < 0
            or not self.lower_bound_utc < self.requested_time_utc < self.upper_bound_utc
        ):
            raise ValueError


@dataclass(frozen=True, slots=True)
class NarrowingProbeEvidence:
    """Strictly admitted request, frame, and observation facts."""

    target_id: str
    requested_time_utc: datetime
    status: CoarseSampleStatus
    state: ClassificationOutcome | None = None
    probe_request_id: str | None = None
    observation_id: str | None = None
    alias_id: str | None = None
    canonical_frame_id: str | None = None
    operation_id: str | None = None
    decode_session_id: str | None = None
    decoded_frame_utc: datetime | None = None
    decoded_pts: int | None = None
    decoded_ordinal: int | None = None

    def __post_init__(self) -> None:
        """Validate admitted request and optional visual provenance."""
        _require_whole_utc(self.requested_time_utc)
        if not self.target_id or not self.probe_request_id:
            raise ValueError
        if self.status is CoarseSampleStatus.SUCCESS:
            if self.canonical_frame_id is None or self.operation_id is None:
                raise ValueError
            if self.state is not None and self.observation_id is None:
                raise ValueError
            if self.state is not None:
                if self.decode_session_id is None or self.decoded_frame_utc is None:
                    raise ValueError
                _require_whole_or_fractional_utc(self.decoded_frame_utc)
                if self.decoded_pts is None or self.decoded_ordinal is None:
                    raise ValueError
                if self.decoded_pts < 0 or self.decoded_ordinal < 0:
                    raise ValueError
        elif any(
            value is not None
            for value in (
                self.state,
                self.observation_id,
                self.alias_id,
                self.canonical_frame_id,
                self.decode_session_id,
                self.decoded_frame_utc,
                self.decoded_pts,
                self.decoded_ordinal,
            )
        ):
            raise ValueError


@dataclass(frozen=True, slots=True)
class NarrowingBoundEvidence:
    """Evidence retained for one current visual bound."""

    target_id: str
    requested_time_utc: datetime
    state: ClassificationOutcome
    observation_id: str
    probe_request_id: str | None
    canonical_frame_id: str | None
    operation_id: str | None
    decode_session_id: str | None
    decoded_frame_utc: datetime | None
    decoded_pts: int | None
    decoded_ordinal: int | None
    is_baseline: bool = False

    def __post_init__(self) -> None:
        """Validate PRESENT/ABSENT bound provenance and baseline shape."""
        _require_whole_utc(self.requested_time_utc)
        if not self.target_id or not self.observation_id:
            raise ValueError
        if self.state not in {
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.ABSENT,
        }:
            raise ValueError
        if self.is_baseline:
            if (
                any(
                    value is not None
                    for value in (
                        self.probe_request_id,
                        self.canonical_frame_id,
                        self.operation_id,
                        self.decode_session_id,
                        self.decoded_frame_utc,
                        self.decoded_pts,
                        self.decoded_ordinal,
                    )
                )
                or self.state is not ClassificationOutcome.PRESENT
            ):
                raise ValueError
        elif (
            self.probe_request_id is None
            or self.canonical_frame_id is None
            or self.operation_id is None
            or self.decode_session_id is None
            or self.decoded_frame_utc is None
            or self.decoded_pts is None
            or self.decoded_ordinal is None
        ):
            raise ValueError


@dataclass(frozen=True, slots=True)
class NarrowingState:
    """In-memory interval and immutable evidence accumulated so far."""

    investigation_id: str
    search_run_id: str
    phase6_confirmation_id: str
    baseline_identity: str
    source_bracket_id: str
    policy_version: str
    lower_bound_utc: datetime
    upper_bound_utc: datetime
    lower_evidence: NarrowingBoundEvidence
    upper_support_evidence: tuple[NarrowingBoundEvidence, ...]
    target_ids: tuple[str, ...]
    evidence: tuple[NarrowingProbeEvidence, ...]
    iteration: int
    manifest_digest: str

    def __post_init__(self) -> None:
        """Validate interval bounds, evidence states, and manifest identity."""
        _require_whole_utc(self.lower_bound_utc)
        _require_whole_utc(self.upper_bound_utc)
        if (
            not self.investigation_id
            or not self.search_run_id
            or not self.phase6_confirmation_id
            or not self.baseline_identity
            or not self.source_bracket_id
            or not self.policy_version
            or self.lower_bound_utc >= self.upper_bound_utc
            or self.lower_evidence.state is not ClassificationOutcome.PRESENT
            or not self.upper_support_evidence
            or any(
                evidence.state is not ClassificationOutcome.ABSENT
                for evidence in self.upper_support_evidence
            )
            or len(set(self.target_ids)) != len(self.target_ids)
            or len(self.manifest_digest) != _DIGEST_LENGTH
            or any(character not in "0123456789abcdef" for character in self.manifest_digest)
            or type(self.iteration) is not int
            or self.iteration < 0
        ):
            raise ValueError

    @property
    def interval_seconds(self) -> int:
        """Return the exact whole-second interval width."""
        delta = self.upper_bound_utc - self.lower_bound_utc
        return delta.days * 86_400 + delta.seconds


@dataclass(frozen=True, slots=True)
class NarrowedBracket:
    """Non-persistent bracket handoff for the future Phase 7D-2 slice."""

    investigation_id: str
    search_run_id: str
    phase6_confirmation_id: str
    baseline_identity: str
    source_bracket_id: str
    policy_version: str
    lower_bound_utc: datetime
    upper_bound_utc: datetime
    lower_evidence: NarrowingBoundEvidence
    upper_support_evidence: tuple[NarrowingBoundEvidence, ...]
    target_ids: tuple[str, ...]
    evidence: tuple[NarrowingProbeEvidence, ...]
    iterations: int
    achieved_precision_seconds: int
    stop_reason: NarrowingStopReason
    manifest_digest: str

    def __post_init__(self) -> None:
        """Reapply strict state validation to the non-persistent handoff."""
        state = NarrowingState(
            investigation_id=self.investigation_id,
            search_run_id=self.search_run_id,
            phase6_confirmation_id=self.phase6_confirmation_id,
            baseline_identity=self.baseline_identity,
            source_bracket_id=self.source_bracket_id,
            policy_version=self.policy_version,
            lower_bound_utc=self.lower_bound_utc,
            upper_bound_utc=self.upper_bound_utc,
            lower_evidence=self.lower_evidence,
            upper_support_evidence=self.upper_support_evidence,
            target_ids=self.target_ids,
            evidence=self.evidence,
            iteration=self.iterations,
            manifest_digest=self.manifest_digest,
        )
        if (
            state.interval_seconds != self.achieved_precision_seconds
            or self.achieved_precision_seconds <= 0
        ):
            raise ValueError


@dataclass(frozen=True, slots=True)
class NarrowingResult:
    """Typed non-terminal result; no persisted or final search status."""

    status: NarrowingStatus
    narrowed_bracket: NarrowedBracket | None = None
    current_state: NarrowingState | None = None
    safe_reason: str | None = None

    def __post_init__(self) -> None:
        """Enforce READY versus safe-failure result shape."""
        if self.status is NarrowingStatus.READY:
            if self.narrowed_bracket is None or self.safe_reason is not None:
                raise ValueError
        elif self.narrowed_bracket is not None or not self.safe_reason:
            raise ValueError


def _require_whole_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise ValueError


def _require_whole_or_fractional_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError
