# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportUnusedCallResult=false, reportOptionalMemberAccess=false
# ruff: noqa: I001, PT011, SIM102
"""Focused Phase 7E-1A contract tests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_7e_identity import (
    IdentityValidationError,
    canonical_payload,
    identity_for,
    strict_json_loads,
    validate_identity,
)
from vigi_vision.recording_search_7e_models import (
    ClassifierEvidence,
    ClassificationOperation,
    Phase8Manifest,
)
from vigi_vision.recording_search_7e_validation import (
    Phase7EValidationError,
    Schema5Envelope,
    Schema6Envelope,
    validate_dependency_graph,
    validate_golden_vectors,
)


_DOC = Path(__file__).parents[1] / "docs" / "design" / "object-disappearance-recording-search.md"


def _vectors() -> list[dict[str, Any]]:
    text = _DOC.read_text(encoding="utf-8")
    vectors: list[dict[str, Any]] = []
    for match in re.finditer(r"```json", text):
        end = text.find("```", match.end())
        if end < 0:
            continue
        try:
            value = json.loads(text[match.end() : end])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            if all({"family", "domain", "expected_id", "payload"} <= set(item) for item in value):
                vectors.extend(value)
    return vectors


def test_all_59_vectors_and_26_families_reproduce() -> None:
    assert validate_golden_vectors(_vectors()) == (59, 26)


def test_every_family_requires_its_complete_exact_key_set() -> None:
    representatives: dict[str, dict[str, Any]] = {}
    for vector in _vectors():
        representatives.setdefault(vector["family"], vector["payload"])
    assert len(representatives) == 26
    for family, payload in representatives.items():
        for key in payload:
            if family == "target-request" and key == "origin_target_request_id":
                continue
            with pytest.raises(IdentityValidationError):
                identity_for(
                    family, {name: value for name, value in payload.items() if name != key}
                )


def test_supplemental_b4_operations_and_observations_bind_run_ownership() -> None:
    supplemental = _vectors()[49:]
    affected = [
        vector
        for vector in supplemental
        if vector["family"] in {"classification-operation", "observation"}
    ]
    assert len(affected) == 10
    assert all(vector["payload"]["run_id"] == "run-01" for vector in affected)
    for vector in affected:
        with pytest.raises(IdentityValidationError, match="key set"):
            identity_for(
                vector["family"],
                {key: value for key, value in vector["payload"].items() if key != "run_id"},
            )


def test_canonical_json_is_sorted_utf8_and_domain_separated() -> None:
    payload = {"z": "한글", "a": [1, 2]}
    assert canonical_payload(payload) == '{"a":[1,2],"z":"한글"}'
    first = identity_for(
        "alias",
        {
            "investigation_id": "i",
            "run_id": "r",
            "target_request_id": "t",
            "frame_id": "f",
            "alias_of_target_request_id": "o",
        },
    )
    second = identity_for(
        "alias",
        {
            "investigation_id": "i",
            "run_id": "r",
            "target_request_id": "t",
            "frame_id": "f",
            "alias_of_target_request_id": "x",
        },
    )
    assert first != second


def test_unknown_and_self_identity_fields_are_rejected() -> None:
    payload = {
        "investigation_id": "i",
        "run_id": "r",
        "target_request_id": "t",
        "frame_id": "f",
        "alias_of_target_request_id": "o",
    }
    with pytest.raises(IdentityValidationError):
        identity_for("alias", {**payload, "unexpected": True})
    with pytest.raises(IdentityValidationError):
        identity_for("alias", {**payload, "alias_id": "rr-alias-v1-" + "0" * 64})
    with pytest.raises(IdentityValidationError):
        strict_json_loads('{"a":1,"a":2}')


def test_identity_validation_rejects_family_substitution_and_mutation() -> None:
    payload = {
        "investigation_id": "i",
        "run_id": "r",
        "target_request_id": "t",
        "frame_id": "f",
        "alias_of_target_request_id": "o",
    }
    identity = identity_for("alias", payload)
    assert validate_identity("alias", identity, payload) == identity
    with pytest.raises(IdentityValidationError):
        validate_identity("frame", identity, payload)
    with pytest.raises(IdentityValidationError):
        validate_identity("alias", identity, {**payload, "run_id": "other"})


def test_comparable_and_unusable_evidence_use_production_b4_matrix() -> None:
    comparable = {
        "baseline_mask_pixel_count": 64,
        "probe_mask_pixel_count": 64,
        "roi_pixel_count": 128,
        "mask_intersection_pixel_count": 32,
        "mask_union_pixel_count": 96,
        "baseline_mask_coverage": "0.500000",
        "probe_mask_coverage": "0.500000",
        "mask_iou": "0.333333",
        "effective_comparison_area": 32,
        "roi_luma_ncc": "0.700000",
        "visual_status": "comparable",
        "unusable_reason": None,
    }
    assert (
        ClassifierEvidence.model_validate(comparable).to_raw().visual_status.value == "comparable"
    )
    unusable = {
        **comparable,
        "visual_status": "unusable",
        "unusable_reason": "invalid_mask",
        "baseline_mask_pixel_count": None,
        "probe_mask_pixel_count": None,
        "mask_intersection_pixel_count": None,
        "mask_union_pixel_count": None,
        "baseline_mask_coverage": None,
        "probe_mask_coverage": None,
        "mask_iou": None,
        "effective_comparison_area": None,
        "roi_luma_ncc": None,
    }
    assert (
        ClassifierEvidence.model_validate(unusable).to_raw().unusable_reason.value == "invalid_mask"
    )
    with pytest.raises(ValueError):
        ClassifierEvidence.model_validate(
            {**unusable, "unusable_reason": "insufficient_visual_evidence"}
        )


def test_operational_classification_cannot_be_visual() -> None:
    record = {
        "investigation_id": "i",
        "run_id": "r",
        "frame_id": "f",
        "target_request_id": "t",
        "baseline_identity": "b",
        "classifier_policy_id": "p",
        "attempt": 1,
        "result_kind": "OPERATIONAL",
        "outcome": None,
        "reason_code": None,
        "classifier_evidence": None,
        "operational_reason": "classifier_timeout",
    }
    assert ClassificationOperation.model_validate(record).result_kind == "OPERATIONAL"
    with pytest.raises(ValueError):
        ClassificationOperation.model_validate({**record, "outcome": ClassificationOutcome.PRESENT})


def test_schema5_and_schema6_state_matrices_are_closed() -> None:
    assert Schema5Envelope.model_validate(
        {
            "run_state": "RUNNING",
            "phase_state": "PLANNED",
            "active_replay_operation_id": None,
            "reason_code": None,
            "attempt_count": 0,
        }
    )
    assert Schema5Envelope.model_validate(
        {
            "run_state": "FAILED",
            "phase_state": "ACQUISITION_FAILED",
            "active_replay_operation_id": "op",
            "reason_code": "acquisition_failed",
            "attempt_count": 1,
        }
    )
    with pytest.raises(ValueError):
        Schema5Envelope.model_validate(
            {
                "run_state": "RUNNING",
                "phase_state": "PLANNED",
                "active_replay_operation_id": "op",
                "reason_code": None,
                "attempt_count": 0,
            }
        )
    row = Schema6Envelope.model_validate(
        {
            "run_state": "RUNNING",
            "target_state": "REQUESTED",
            "active_target_request_id": "target",
            "active_decoder_operation_id": None,
            "active_frame_id": None,
            "active_classification_attempt_id": None,
            "active_classification_operation_id": None,
            "active_observation_id": None,
            "reason_code": None,
            "attempt_count": 0,
            "predecessor_target_state": None,
        }
    )
    assert row.target_state.value == "REQUESTED"
    with pytest.raises(ValueError):
        Schema6Envelope.model_validate(
            {
                "run_state": "RUNNING",
                "target_state": "REQUESTED",
                "active_target_request_id": "target",
                "active_decoder_operation_id": "decoder",
                "active_frame_id": None,
                "active_classification_attempt_id": None,
                "active_classification_operation_id": None,
                "active_observation_id": None,
                "reason_code": None,
                "attempt_count": 0,
                "predecessor_target_state": None,
            }
        )


def test_dependency_graph_rejects_duplicate_missing_foreign_and_cycles() -> None:
    payload = {
        "investigation_id": "i",
        "run_id": "r",
        "target_request_id": "t",
        "frame_id": "f",
        "alias_of_target_request_id": "o",
    }
    identity = identity_for("alias", payload)
    record = {"family": "alias", "identity": identity, "payload": payload}
    assert validate_dependency_graph([record]) == (identity,)
    with pytest.raises(Phase7EValidationError):
        validate_dependency_graph([record, record])
    with pytest.raises(Phase7EValidationError):
        validate_dependency_graph(
            [{**record, "payload": {**payload, "frame_id": "rr-observation-v1-" + "0" * 64}}]
        )


@pytest.mark.parametrize("state", ["RETRYABLE", "CLIP_READY", "READY", "DELETING", "DELETED"])
def test_phase8_manifest_state_union_is_closed(state: str) -> None:
    base: dict[str, Any] = {
        "schema_version": 1,
        "state": state,
        "investigation_id": "i",
        "run_id": "r",
        "terminal_result_id": "result",
        "common_session_id": "session",
        "previous_phase8_manifest_id": None,
        "source_clip_id": None,
        "clip_integrity": None,
        "phase8_request_id": None,
        "failure_reason": "phase8_clip_failed",
    }
    if state == "RETRYABLE":
        base["failure_reason"] = "phase8_clip_failed"
    elif state in {"CLIP_READY", "READY", "DELETING"}:
        base.update(
            {
                "source_clip_id": "clip",
                "clip_integrity": {
                    "sha256": "0" * 64,
                    "size_bytes": 1,
                    "observed_duration_ticks": 1,
                    "observed_time_base_num": 1,
                    "observed_time_base_den": 1,
                    "video_stream_index": 0,
                    "codec": "h264",
                    "profile": "High",
                    "level": 41,
                    "pixel_format": "yuv420p",
                    "width": 1,
                    "height": 1,
                    "average_frame_rate_num": 1,
                    "average_frame_rate_den": 1,
                    "audio_stream_count": 0,
                    "generation_outcome": "REENCODED",
                },
                "failure_reason": None,
            }
        )
        if state == "READY":
            base["phase8_request_id"] = "request"
        if state == "DELETING":
            base.update(
                {
                    "source_clip_tombstone_name": ".delete-clip",
                    "common_media_tombstone_name": ".delete-session",
                }
            )
        base.pop("failure_reason")
        if state == "CLIP_READY":
            base.pop("phase8_request_id")
    elif state == "DELETED":
        base = dict(
            next(
                item["payload"]
                for item in _vectors()
                if item["family"] == "phase8-manifest" and item["payload"]["state"] == "DELETED"
            )
        )
    assert Phase8Manifest(payload=base).payload["state"] == state
