"""Closed result unions returned by the pure D2-0 adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
    from vigi_vision.recording_search_d1_models import NarrowedBracket
    from vigi_vision.recording_search_d2_enums import (
        D1NonTerminalReason,
        D2HistoryEntry,
        OperationalStopReason,
        VisualStopReason,
    )
    from vigi_vision.recording_search_d2_evidence import D2EvidenceReference


@dataclass(frozen=True, slots=True)
class C2BracketReady:
    """Validated C2 candidate bracket with its immutable evidence digest."""

    bracket: CoarseCandidateBracket
    evidence_snapshot_digest: str


@dataclass(frozen=True, slots=True)
class C2NoCandidate:
    """Validated complete PRESENT grid proving no supported transition."""

    complete_present_grid: tuple[D2EvidenceReference, ...]
    evidence_snapshot_digest: str


@dataclass(frozen=True, slots=True)
class C2VisualInconclusive:
    """Validated visual evidence for a closed C2 inconclusive reason."""

    reason: VisualStopReason
    evidence: tuple[D2EvidenceReference, ...]
    evidence_snapshot_digest: str


@dataclass(frozen=True, slots=True)
class C2OperationalStop:
    """Fail-closed C2 outcome without visual evidence or a digest."""

    reason: OperationalStopReason
    attempted_target_ids: tuple[str, ...]


C2Result: TypeAlias = C2BracketReady | C2NoCandidate | C2VisualInconclusive | C2OperationalStop


@dataclass(frozen=True, slots=True)
class D1BracketReady:
    """Validated D1 narrowed bracket with its immutable evidence digest."""

    narrowed_bracket: NarrowedBracket
    evidence_snapshot_digest: str


@dataclass(frozen=True, slots=True)
class D1VisualTerminal:
    """Validated D1 visual terminal evidence and opaque complete history."""

    reason: VisualStopReason
    narrowing_history: tuple[D2HistoryEntry, ...]
    blocking_evidence: tuple[D2EvidenceReference, ...]
    evidence_snapshot_digest: str


@dataclass(frozen=True, slots=True)
class D1OperationalStop:
    """Fail-closed D1 outcome without visual evidence or a digest."""

    reason: OperationalStopReason
    attempted_target_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class D1NonTerminalStop:
    """D1 non-terminal stop carrying history supplied by a later slice."""

    reason: D1NonTerminalReason
    narrowing_history: tuple[D2HistoryEntry, ...]


D1Result: TypeAlias = D1BracketReady | D1VisualTerminal | D1OperationalStop | D1NonTerminalStop
