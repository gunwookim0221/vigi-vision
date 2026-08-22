"""Strict, non-sensitive status projection for reopened terminal runs."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from vigi_vision.durable_io import CanonicalUtc  # noqa: TC001
from vigi_vision.recording_search_d2_enums import VisualStopReason
from vigi_vision.recording_search_d2_publication_models import (
    PublishedFoundResult,
    RecordingSearchManifestV4,
)
from vigi_vision.recording_search_d2_terminal_models import TerminalLimitation, TerminalResultKind
from vigi_vision.recording_search_models import Phase8HandoffStatus, RecordingSearchState

SCHEMA_VERSION = 4


class RecordingSearchTerminalInterval(BaseModel):
    """Public interval for a validated FOUND result."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    lower_requested_time_utc: CanonicalUtc
    upper_requested_time_utc: CanonicalUtc

    @model_validator(mode="after")
    def ordered(self) -> RecordingSearchTerminalInterval:
        """Reject an empty or reversed public interval."""
        if self.lower_requested_time_utc >= self.upper_requested_time_utc:
            raise ValueError
        return self


class RecordingSearchTerminalProjection(BaseModel):
    """Allowlisted terminal result fields safe for API consumers."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    result_id: StrictStr = Field(pattern=r"^recording-search-result-v1-[0-9a-f]{64}$")
    kind: TerminalResultKind
    terminal_reason: StrictStr
    interval: RecordingSearchTerminalInterval | None
    achieved_precision_seconds: StrictInt | None
    limitations: tuple[TerminalLimitation, ...]
    review_anchor_utc: CanonicalUtc | None

    @model_validator(mode="after")
    def semantic_shape(self) -> RecordingSearchTerminalProjection:  # noqa: C901
        """Enforce the result-kind-specific public shape."""
        if len(set(self.limitations)) != len(self.limitations):
            raise ValueError
        if self.kind is TerminalResultKind.FOUND:
            if self.interval is None or self.achieved_precision_seconds is None:
                raise ValueError
            if self.terminal_reason != "candidate_interval_found":
                raise ValueError
            if self.review_anchor_utc != self.interval.upper_requested_time_utc:
                raise ValueError
        elif self.kind is TerminalResultKind.NOT_FOUND:
            if self.interval is not None or self.achieved_precision_seconds is not None:
                raise ValueError
            if (
                self.terminal_reason != "no_transition_in_window"
                or self.review_anchor_utc is not None
            ):
                raise ValueError
        else:
            if self.interval is not None or self.achieved_precision_seconds is not None:
                raise ValueError
            if self.terminal_reason not in {reason.value for reason in VisualStopReason}:
                raise ValueError
            if self.review_anchor_utc is not None:
                raise ValueError
        return self


class RecordingSearchStatusV4(BaseModel):
    """Strict public schema-4 status projection.

    Schema 4 is the terminal boundary.  Its persisted manifest necessarily
    contains the evidence needed for strict reopen, but the public status must
    never serialize those reconstruction inputs.  Keep this model deliberately
    small and explicit; callers that need evidence use the internal reopen
    boundary instead of this API projection.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[4] = 4
    investigation_id: StrictStr
    search_run_id: StrictStr
    state: RecordingSearchState
    created_at_utc: CanonicalUtc
    started_at_utc: CanonicalUtc
    completed_at_utc: CanonicalUtc
    failure_reason: None = None
    result: RecordingSearchTerminalProjection
    phase8_handoff_status: Phase8HandoffStatus

    @model_validator(mode="after")
    def semantic_state(self) -> RecordingSearchStatusV4:
        """Bind lifecycle state and Phase 8 handoff status to the result kind."""
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError
        if self.state is RecordingSearchState.FOUND:
            expected = TerminalResultKind.FOUND
        elif self.state is RecordingSearchState.NOT_FOUND:
            expected = TerminalResultKind.NOT_FOUND
        elif self.state is RecordingSearchState.INDETERMINATE:
            expected = TerminalResultKind.INCONCLUSIVE
        else:
            raise ValueError
        if self.result.kind is not expected:
            raise ValueError
        allowed_handoff = (
            {Phase8HandoffStatus.PENDING, Phase8HandoffStatus.READY}
            if expected is TerminalResultKind.FOUND
            else {Phase8HandoffStatus.NOT_APPLICABLE}
        )
        if self.phase8_handoff_status not in allowed_handoff:
            raise ValueError
        return self


def terminal_status(
    manifest: RecordingSearchManifestV4,
    *,
    phase8_handoff_status: Phase8HandoffStatus = Phase8HandoffStatus.PENDING,
) -> RecordingSearchStatusV4:
    """Project a validated terminal manifest into the public status contract."""
    terminal = manifest.terminal_result
    if isinstance(terminal, PublishedFoundResult):
        projection = RecordingSearchTerminalProjection(
            result_id=terminal.result_id,
            kind=terminal.result_kind,
            terminal_reason=terminal.terminal_reason,
            interval=RecordingSearchTerminalInterval(
                lower_requested_time_utc=terminal.lower_bound_requested_time_utc,
                upper_requested_time_utc=terminal.upper_bound_requested_time_utc,
            ),
            achieved_precision_seconds=terminal.achieved_precision_seconds,
            limitations=tuple(TerminalLimitation(item) for item in terminal.limitations),
            review_anchor_utc=terminal.upper_bound_requested_time_utc,
        )
    else:
        projection = RecordingSearchTerminalProjection(
            result_id=terminal.result_id,
            kind=terminal.result_kind,
            terminal_reason=terminal.terminal_reason,
            interval=None,
            achieved_precision_seconds=None,
            limitations=tuple(TerminalLimitation(item) for item in terminal.limitations),
            review_anchor_utc=None,
        )
    return RecordingSearchStatusV4(
        investigation_id=manifest.investigation_id,
        search_run_id=manifest.search_run_id,
        state=RecordingSearchState(manifest.state),
        created_at_utc=manifest.created_at_utc,
        started_at_utc=manifest.started_at_utc,
        completed_at_utc=manifest.completed_at_utc,
        result=projection,
        phase8_handoff_status=(
            phase8_handoff_status
            if projection.kind is TerminalResultKind.FOUND
            else Phase8HandoffStatus.NOT_APPLICABLE
        ),
    )
