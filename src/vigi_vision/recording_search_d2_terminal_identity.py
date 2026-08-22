"""Canonical identities for pure D2-2 terminal result proposals."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from vigi_vision.recording_search_d2_terminal_models import (
    FoundResult,
    InconclusiveResult,
    NotFoundResult,
)

if TYPE_CHECKING:
    from vigi_vision.recording_search_d2_terminal_models import TerminalResult


def canonical_terminal_result_payload(result: TerminalResult) -> dict[str, object]:
    """Return the allowlisted identity payload without result ID or clock data."""
    if type(result) not in {FoundResult, NotFoundResult, InconclusiveResult}:
        raise TypeError
    common: dict[str, object] = {
        "baseline_observation_id": result.baseline_observation_id,
        "evidence_snapshot_digest": result.evidence_snapshot_digest,
        "identity_schema": "recording-search-terminal-result-v1",
        "investigation_id": result.investigation_id,
        "limitations": [value.value for value in result.limitations],
        "phase6_confirmation_id": result.phase6_confirmation_id,
        "plan_id": result.plan_id,
        "policy_identity": result.policy_identity,
        "result_kind": result.result_kind.value,
        "search_run_id": result.search_run_id,
        "source_manifest_digest": result.source_manifest_digest,
        "terminal_reason": result.terminal_reason,
    }
    if isinstance(result, FoundResult):
        common["found"] = {
            "achieved_precision_seconds": result.achieved_precision_seconds,
            "d1_input_bracket_id": result.d1_input_bracket_id,
            "history_digest": result.history_digest,
            "iterations": result.iterations,
            "lower_bound_requested_time_utc": result.lower_bound_requested_time_utc,
            "lower_reference": result.lower_reference.to_payload(),
            "narrowed_bracket_id": result.narrowed_bracket_id,
            "narrowing_evidence": [item.to_payload() for item in result.narrowing_evidence],
            "source_bracket_id": result.source_bracket_id,
            "stop_reason": result.stop_reason,
            "upper_bound_requested_time_utc": result.upper_bound_requested_time_utc,
            "upper_support": [item.to_payload() for item in result.upper_support],
            "upper_support_group_id": result.upper_support_group_id,
        }
    elif isinstance(result, NotFoundResult):
        common["not_found"] = {
            "coarse_grid": [item.to_payload() for item in result.coarse_grid],
            "search_end_utc": result.search_end_utc,
            "search_start_utc": result.search_start_utc,
        }
    else:
        common["inconclusive"] = {
            "evidence": [item.to_payload() for item in result.evidence],
            "source_stage": result.source_stage.value,
            "visual_reason": result.visual_reason.value,
        }
    return common


def canonical_terminal_result_json(result: TerminalResult) -> str:
    """Serialize one terminal identity payload with canonical JSON rules."""
    return json.dumps(
        canonical_terminal_result_payload(result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def terminal_result_id(result: TerminalResult) -> str:
    """Return the domain-separated terminal result identity."""
    digest = hashlib.sha256(canonical_terminal_result_json(result).encode("utf-8")).hexdigest()
    return f"recording-search-result-v1-{digest}"
