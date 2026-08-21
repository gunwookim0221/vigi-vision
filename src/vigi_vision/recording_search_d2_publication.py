"""Atomic Schema 4 publication primitives for D2-3."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from vigi_vision.recording_search_d2_publication_models import (
    PublishedTerminalResult,
    RecordingSearchManifestV4,
    published_terminal_result,
)
from vigi_vision.recording_search_d2_publication_validation import (
    validate_terminal_publication,
)

if TYPE_CHECKING:
    from datetime import datetime

    from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
    from vigi_vision.recording_search_d2_terminal_models import (
        TerminalInputSnapshot,
        TerminalResult,
    )


class TerminalPublicationOutcome(str, Enum):
    """Safe externally observable result of a terminal publication attempt."""

    CREATED = "created"
    REUSED = "reused"


@dataclass(frozen=True, slots=True)
class TerminalPublicationResult:
    """Committed Schema 4 result and its publication disposition."""

    manifest: RecordingSearchManifestV4
    result: PublishedTerminalResult
    outcome: TerminalPublicationOutcome


def build_schema4_successor(
    predecessor: RecordingSearchManifestV3,
    snapshot: TerminalInputSnapshot,
    result: TerminalResult,
    published_at_utc: datetime,
) -> RecordingSearchManifestV4:
    """Validate a D2-2 result and construct its immutable Schema 4 successor."""
    if predecessor.state != "RUNNING":
        raise ValueError
    validate_terminal_publication(snapshot, result)
    terminal = published_terminal_result(result, published_at_utc)
    state = (
        "INDETERMINATE"
        if terminal.result_kind.value == "INCONCLUSIVE"
        else terminal.result_kind.value
    )
    return RecordingSearchManifestV4(
        schema_version=4,
        investigation_id=predecessor.investigation_id,
        search_run_id=predecessor.search_run_id,
        state=state,
        created_at_utc=predecessor.created_at_utc,
        started_at_utc=predecessor.started_at_utc,
        completed_at_utc=published_at_utc.replace(microsecond=0),
        confirmation=predecessor.confirmation,
        policy=predecessor.policy,
        acquisition_operation_ids=predecessor.acquisition_operation_ids,
        probe_request_ids=predecessor.probe_request_ids,
        canonical_frame_ids=predecessor.canonical_frame_ids,
        baseline_observation_id=predecessor.baseline_observation_id,
        classification_operation_ids=predecessor.classification_operation_ids,
        canonical_observation_ids=predecessor.canonical_observation_ids,
        target_alias_ids=predecessor.target_alias_ids,
        terminal_result=terminal,
    )
