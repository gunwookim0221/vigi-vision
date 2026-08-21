"""Public D2-2 pure terminal interpretation API."""

from vigi_vision.recording_search_d2_terminal_identity import (
    canonical_terminal_result_json,
    canonical_terminal_result_payload,
    terminal_result_id,
)
from vigi_vision.recording_search_d2_terminal_interpreter import (
    interpret_terminal,
    interpret_terminal_candidate,
)
from vigi_vision.recording_search_d2_terminal_models import (
    CoarseTerminalCandidate,
    FoundCandidate,
    FoundResult,
    InconclusiveResult,
    NarrowingVisualTerminalCandidate,
    NonTerminalOutcome,
    NotFoundResult,
    OperationalOutcome,
    TerminalInputSnapshot,
    TerminalizationCandidate,
    TerminalLimitation,
    TerminalNonTerminalReason,
    TerminalOutcome,
    TerminalResult,
    TerminalResultKind,
    TerminalSourceStage,
)

__all__ = [
    "CoarseTerminalCandidate",
    "FoundCandidate",
    "FoundResult",
    "InconclusiveResult",
    "NarrowingVisualTerminalCandidate",
    "NonTerminalOutcome",
    "NotFoundResult",
    "OperationalOutcome",
    "TerminalInputSnapshot",
    "TerminalLimitation",
    "TerminalNonTerminalReason",
    "TerminalOutcome",
    "TerminalResult",
    "TerminalResultKind",
    "TerminalSourceStage",
    "TerminalizationCandidate",
    "canonical_terminal_result_json",
    "canonical_terminal_result_payload",
    "interpret_terminal",
    "interpret_terminal_candidate",
    "terminal_result_id",
]
