"""Closed D2-0 enum and protocol values."""

from __future__ import annotations

from enum import Enum
from typing import Protocol


class OperationalStopReason(str, Enum):
    """Closed reasons that cannot become a terminal visual result."""

    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    CLASSIFICATION_TIMEOUT = "classification_timeout"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    RECORDING_COVERAGE_GAP = "recording_coverage_gap"
    ACQUISITION_FAILED = "acquisition_failed"
    DECODE_FAILED = "decode_failed"
    CLASSIFICATION_FAILED = "classification_failed"
    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    STALE_AUTHORITY = "stale_authority"
    INACTIVE_AUTHORITY = "inactive_authority"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    CORRUPT_PERSISTED_EVIDENCE = "corrupt_persisted_evidence"
    ADAPTER_UNKNOWN_RESULT = "adapter_unknown_result"
    PUBLICATION_IN_PROGRESS = "publication_in_progress"
    PUBLICATION_READBACK_FAILED = "publication_readback_failed"
    PUBLICATION_INVARIANT_FAILURE = "publication_invariant_failure"
    UNEXPECTED_ERROR = "unexpected_error"


class VisualStopReason(str, Enum):
    """Closed reasons supported by visual terminal outcomes."""

    INSUFFICIENT_VISUAL_EVIDENCE = "insufficient_visual_evidence"
    NONMONOTONIC_VISUAL_EVIDENCE = "nonmonotonic_visual_evidence"
    INSUFFICIENT_DISTINCT_VISUAL_SUPPORT = "insufficient_distinct_visual_support"


class D2EvidenceRole(str, Enum):
    """Stable ordering roles in an evidence snapshot."""

    BASELINE = "BASELINE"
    COARSE_TARGET = "COARSE_TARGET"
    D1_MIDPOINT = "D1_MIDPOINT"
    ABSENCE_SUPPORT = "ABSENCE_SUPPORT"


class D1NonTerminalReason(str, Enum):
    """D1 outcomes that are not eligible for terminal publication."""

    NO_DISTINCT_MIDPOINT = "no_distinct_midpoint"
    MAXIMUM_ITERATIONS = "maximum_iterations"
    INCOMPLETE_EVIDENCE = "incomplete_evidence"


class D2HistoryEntry(Protocol):
    """Opaque complete history entry supplied by the later D2-1 slice."""
