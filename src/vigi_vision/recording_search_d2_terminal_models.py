"""Typed pure terminal-result proposals and nonterminal outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from vigi_vision.recording_search_c1_planner import CoarseSamplingPlan
    from vigi_vision.recording_search_d1_identity import D1InputBracket
    from vigi_vision.recording_search_d1_models import NarrowedBracket
    from vigi_vision.recording_search_d2_enums import OperationalStopReason, VisualStopReason
    from vigi_vision.recording_search_d2_evidence import D2EvidenceReference, D2EvidenceSnapshot
    from vigi_vision.recording_search_d2_results import (
        C2NoCandidate,
        C2Result,
        C2VisualInconclusive,
        D1Result,
        D1VisualTerminal,
    )
    from vigi_vision.recording_search_models import RecordingSearchPolicy


class TerminalResultKind(str, Enum):
    """Stable visual result kinds exposed by the D2-2 boundary."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INCONCLUSIVE = "INCONCLUSIVE"


class TerminalSourceStage(str, Enum):
    """Origin stage for an accepted visual limitation."""

    COARSE = "COARSE"
    NARROWING = "NARROWING"


class TerminalLimitation(str, Enum):
    """Closed limitations that participate in result identity."""

    REQUESTED_TIME_INTERVAL_NOT_EXACT_EVENT = "requested_time_interval_not_exact_event"
    CONFIGURED_SAMPLES_ONLY = "configured_samples_only"
    DECODED_TIME_DIFFERS_FROM_REQUESTED = "decoded_time_differs_from_requested"
    CAMERA_CONTINUITY_UNVERIFIED = "camera_continuity_unverified"
    POLICY_PENDING_PHASE7E_VALIDATION = "policy_pending_phase7e_validation"
    INSUFFICIENT_VISUAL_EVIDENCE = "insufficient_visual_evidence"
    NONMONOTONIC_VISUAL_EVIDENCE = "nonmonotonic_visual_evidence"
    INSUFFICIENT_DISTINCT_VISUAL_SUPPORT = "insufficient_distinct_visual_support"


class TerminalNonTerminalReason(str, Enum):
    """Closed reasons for valid nonterminal outcomes."""

    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    NO_DISTINCT_MIDPOINT = "no_distinct_midpoint"
    MAXIMUM_ITERATIONS = "maximum_iterations"
    NOT_A_TERMINAL_CANDIDATE = "not_a_terminal_candidate"


@dataclass(frozen=True, slots=True)
class TerminalInputSnapshot:
    """Pure interpreter input assembled from validated C2/D1 facts."""

    evidence_snapshot: D2EvidenceSnapshot
    plan: CoarseSamplingPlan
    policy: RecordingSearchPolicy
    c2_result: C2Result
    d1_result: D1Result | None = None
    d1_input_bracket: D1InputBracket | None = None


@dataclass(frozen=True, slots=True)
class FoundCandidate:
    """Candidate for a valid narrowed FOUND interval."""

    narrowed_bracket: NarrowedBracket


@dataclass(frozen=True, slots=True)
class CoarseTerminalCandidate:
    """Candidate supplied by a C2 coarse terminal result."""

    result: C2NoCandidate | C2VisualInconclusive


@dataclass(frozen=True, slots=True)
class NarrowingVisualTerminalCandidate:
    """Candidate supplied by a D1 visual stop."""

    narrowing_result: D1VisualTerminal


TerminalizationCandidate: TypeAlias = (
    FoundCandidate | CoarseTerminalCandidate | NarrowingVisualTerminalCandidate
)


@dataclass(frozen=True, slots=True)
class OperationalOutcome:
    """Nonvisual operational outcome without terminal identity."""

    reason: OperationalStopReason
    attempted_target_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NonTerminalOutcome:
    """Incomplete or otherwise nonterminal outcome without a digest."""

    reason: TerminalNonTerminalReason


@dataclass(frozen=True, slots=True)
class FoundResult:
    """Immutable in-memory FOUND proposal without publication metadata."""

    result_id: str
    result_kind: TerminalResultKind
    investigation_id: str
    search_run_id: str
    phase6_confirmation_id: str
    baseline_observation_id: str
    plan_id: str
    policy_identity: str
    source_manifest_digest: str
    evidence_snapshot_digest: str
    terminal_reason: str
    limitations: tuple[TerminalLimitation, ...]
    source_bracket_id: str
    narrowed_bracket_id: str
    lower_bound_requested_time_utc: str
    upper_bound_requested_time_utc: str
    achieved_precision_seconds: int
    lower_reference: D2EvidenceReference
    upper_support: tuple[D2EvidenceReference, ...]
    narrowing_evidence: tuple[D2EvidenceReference, ...]


@dataclass(frozen=True, slots=True)
class NotFoundResult:
    """Immutable in-memory NOT_FOUND proposal."""

    result_id: str
    result_kind: TerminalResultKind
    investigation_id: str
    search_run_id: str
    phase6_confirmation_id: str
    baseline_observation_id: str
    plan_id: str
    policy_identity: str
    source_manifest_digest: str
    evidence_snapshot_digest: str
    terminal_reason: str
    limitations: tuple[TerminalLimitation, ...]
    search_start_utc: str
    search_end_utc: str
    coarse_grid: tuple[D2EvidenceReference, ...]


@dataclass(frozen=True, slots=True)
class InconclusiveResult:
    """Immutable in-memory visual INCONCLUSIVE proposal."""

    result_id: str
    result_kind: TerminalResultKind
    investigation_id: str
    search_run_id: str
    phase6_confirmation_id: str
    baseline_observation_id: str
    plan_id: str
    policy_identity: str
    source_manifest_digest: str
    evidence_snapshot_digest: str
    terminal_reason: str
    limitations: tuple[TerminalLimitation, ...]
    source_stage: TerminalSourceStage
    visual_reason: VisualStopReason
    evidence: tuple[D2EvidenceReference, ...]


TerminalResult: TypeAlias = FoundResult | NotFoundResult | InconclusiveResult
TerminalOutcome: TypeAlias = TerminalResult | OperationalOutcome | NonTerminalOutcome
