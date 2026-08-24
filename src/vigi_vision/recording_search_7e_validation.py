# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportUntypedBaseClass=false, reportUnannotatedClassAttribute=false, reportUnnecessaryIsInstance=false, reportUnreachable=false, reportUnusedCallResult=false, reportGeneralTypeIssues=false
# ruff: noqa: ANN401, C901, D102, EM101, I001, PLR0912, PLR2004, RUF022, SIM102, TRY003, UP037
"""Pure Phase 7E field, dependency, evidence, and transition validators."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from vigi_vision.recording_search_7e_identity import (
    IdentityValidationError,
    family_from_identity,
    identity_for,
    validate_identity,
)
from vigi_vision.recording_search_7e_models import (
    ClassifierEvidence,
    ClassificationOperation,
    Phase8Manifest,
    Schema5PhaseState,
    Schema6TargetState,
)


class Phase7EValidationError(ValueError):
    """Raised for a safe, deterministic contract violation."""


class Schema5Envelope(BaseModel):
    """The exact five-key schema-5 lifecycle envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    run_state: str
    phase_state: Schema5PhaseState
    active_replay_operation_id: str | None
    reason_code: str | None
    attempt_count: StrictInt = Field(ge=0, le=1)

    @field_validator("phase_state", mode="before")
    @classmethod
    def parse_phase_state(cls, value: Any) -> Schema5PhaseState:
        if isinstance(value, Schema5PhaseState):
            return value
        if type(value) is str:
            return Schema5PhaseState(value)
        raise ValueError

    @model_validator(mode="after")
    def validate_matrix(self) -> "Schema5Envelope":
        _validate_schema5_matrix(self)
        return self


class Schema6Envelope(BaseModel):
    """The exact eleven-key schema-6 target lifecycle envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    run_state: str
    target_state: Schema6TargetState
    active_target_request_id: str | None
    active_decoder_operation_id: str | None
    active_frame_id: str | None
    active_classification_attempt_id: str | None
    active_classification_operation_id: str | None
    active_observation_id: str | None
    reason_code: str | None
    attempt_count: StrictInt = Field(ge=0, le=1)
    predecessor_target_state: Schema6TargetState | None

    @field_validator("target_state", "predecessor_target_state", mode="before")
    @classmethod
    def parse_target_state(cls, value: Any) -> Schema6TargetState | None:
        if value is None:
            return None
        if isinstance(value, Schema6TargetState):
            return value
        if type(value) is str:
            return Schema6TargetState(value)
        raise ValueError

    @model_validator(mode="after")
    def validate_matrix(self) -> "Schema6Envelope":
        _validate_schema6_matrix(self)
        return self


def validate_schema5_state(value: Mapping[str, Any] | Schema5Envelope) -> Schema5Envelope:
    """Validate one schema-5 state row without performing I/O."""
    try:
        return (
            value if isinstance(value, Schema5Envelope) else Schema5Envelope.model_validate(value)
        )
    except (ValueError, TypeError) as exc:
        raise Phase7EValidationError("invalid schema-5 state") from exc


def validate_schema6_state(value: Mapping[str, Any] | Schema6Envelope) -> Schema6Envelope:
    """Validate one schema-6 state row without performing I/O."""
    try:
        return (
            value if isinstance(value, Schema6Envelope) else Schema6Envelope.model_validate(value)
        )
    except (ValueError, TypeError) as exc:
        raise Phase7EValidationError("invalid schema-6 state") from exc


def parse_schema_envelope(value: Mapping[str, Any]) -> Schema5Envelope | Schema6Envelope:
    """Dispatch only the approved pure schema-5/6 state envelopes."""
    if not isinstance(value, Mapping):
        raise Phase7EValidationError("state envelope must be an object")
    if "phase_state" in value:
        return validate_schema5_state(value)
    if "target_state" in value:
        return validate_schema6_state(value)
    raise Phase7EValidationError("unsupported state envelope")


def validate_phase8_manifest(value: Mapping[str, Any] | Phase8Manifest) -> Phase8Manifest:
    """Validate a Phase 8 state envelope and its state-dependent shape."""
    try:
        return value if isinstance(value, Phase8Manifest) else Phase8Manifest(payload=dict(value))
    except (ValueError, TypeError) as exc:
        raise Phase7EValidationError("invalid phase-8 state") from exc


def validate_classifier_evidence(
    value: Mapping[str, Any] | ClassifierEvidence,
) -> ClassifierEvidence:
    """Validate the complete B4 evidence union through the production model."""
    try:
        return (
            value
            if isinstance(value, ClassifierEvidence)
            else ClassifierEvidence.model_validate(value)
        )
    except (ValueError, TypeError) as exc:
        raise Phase7EValidationError("invalid classifier evidence") from exc


def validate_classification_operation(
    value: Mapping[str, Any] | ClassificationOperation,
) -> ClassificationOperation:
    """Validate visual/operational operation separation."""
    try:
        return (
            value
            if isinstance(value, ClassificationOperation)
            else ClassificationOperation.model_validate(value)
        )
    except (ValueError, TypeError) as exc:
        raise Phase7EValidationError("invalid classification operation") from exc


def validate_dependency_graph(records: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """Validate family references, uniqueness, membership, order, and acyclicity.

    Records are envelopes with ``family``, ``identity`` and ``payload`` keys.
    References to non-Phase-7E baseline identities are intentionally allowed;
    they are external immutable Phase 6 inputs, not graph nodes.
    """
    by_id: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for record in records:
        if set(record) != {"family", "identity", "payload"}:
            raise Phase7EValidationError("invalid identity envelope")
        family = record["family"]
        identity = record["identity"]
        payload = record["payload"]
        if (
            not isinstance(family, str)
            or not isinstance(identity, str)
            or not isinstance(payload, Mapping)
        ):
            raise Phase7EValidationError("invalid identity envelope")
        try:
            validate_identity(family, identity, payload)
        except (IdentityValidationError, TypeError) as exc:
            raise Phase7EValidationError("invalid identity envelope") from exc
        if identity in by_id:
            raise Phase7EValidationError("duplicate identity")
        by_id[identity] = (family, payload)

    edges: dict[str, set[str]] = {identity: set() for identity in by_id}
    for identity, (family, payload) in by_id.items():
        _validate_ordered_arrays(payload)
        if family == "source-record-set":
            _validate_source_record_groups(payload, by_id)
        for key, referenced in _iter_references(payload):
            expected_family = _reference_family(key)
            if expected_family is not None and _is_phase7_identity(referenced):
                try:
                    actual = family_from_identity(referenced)
                except IdentityValidationError as exc:
                    raise Phase7EValidationError("foreign identity family") from exc
                if actual != expected_family:
                    raise Phase7EValidationError("foreign identity family")
                if referenced not in by_id:
                    raise Phase7EValidationError("missing identity reference")
                edges[identity].add(referenced)
    _reject_cycles(edges)
    return tuple(by_id)


def validate_golden_vectors(vectors: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """Reproduce the approved 59-vector inventory and its 26 families."""
    if len(vectors) != 59:
        raise Phase7EValidationError("golden-vector inventory must contain 59 vectors")
    families: set[str] = set()
    for vector in vectors:
        if set(vector) != {"family", "domain", "expected_id", "payload"}:
            raise Phase7EValidationError("invalid golden vector envelope")
        family = vector["family"]
        if not isinstance(family, str) or vector["domain"] is None:
            raise Phase7EValidationError("invalid golden vector")
        try:
            actual = identity_for(family, vector["payload"])
            if actual != vector["expected_id"]:
                raise Phase7EValidationError("golden-vector digest mismatch")
        except (IdentityValidationError, TypeError) as exc:
            raise Phase7EValidationError("invalid golden vector") from exc
        families.add(family)
    if len(families) != 26:
        raise Phase7EValidationError("golden-vector family inventory must contain 26 families")
    return len(vectors), len(families)


def _validate_schema5_matrix(row: Schema5Envelope) -> None:
    if row.run_state not in {"RUNNING", "FAILED", "INTERRUPTED"}:
        raise ValueError
    state = row.phase_state
    if state is Schema5PhaseState.PLANNED:
        if (
            row.run_state != "RUNNING"
            or row.active_replay_operation_id is not None
            or row.reason_code is not None
            or row.attempt_count != 0
        ):
            raise ValueError
    elif state is Schema5PhaseState.ACQUIRING:
        if (
            row.run_state != "RUNNING"
            or not row.active_replay_operation_id
            or row.reason_code is not None
            or row.attempt_count != 1
        ):
            raise ValueError
    elif state is Schema5PhaseState.ACQUISITION_FAILED:
        if (
            row.run_state != "FAILED"
            or not row.active_replay_operation_id
            or not row.reason_code
            or row.attempt_count != 1
        ):
            raise ValueError
    elif state is Schema5PhaseState.ACQUIRED:
        if (
            row.run_state != "RUNNING"
            or not row.active_replay_operation_id
            or row.reason_code is not None
            or row.attempt_count != 1
        ):
            raise ValueError
    elif state is Schema5PhaseState.INTERRUPTED:
        if (
            row.run_state != "INTERRUPTED"
            or row.reason_code != "interrupted"
            or row.attempt_count not in {0, 1}
        ):
            raise ValueError


def _validate_schema6_matrix(row: Schema6Envelope) -> None:
    if row.run_state not in {"RUNNING", "FAILED", "INTERRUPTED"}:
        raise ValueError
    if not row.active_target_request_id and row.target_state is not Schema6TargetState.REQUESTED:
        raise ValueError
    if row.target_state is Schema6TargetState.REQUESTED:
        if (
            row.run_state != "RUNNING"
            or row.attempt_count != 0
            or any(
                value is not None
                for value in (
                    row.active_decoder_operation_id,
                    row.active_frame_id,
                    row.active_classification_attempt_id,
                    row.active_classification_operation_id,
                    row.active_observation_id,
                    row.reason_code,
                )
            )
        ):
            raise ValueError
    elif row.target_state is Schema6TargetState.DECODING:
        if (
            row.run_state != "RUNNING"
            or row.attempt_count != 1
            or not row.active_decoder_operation_id
            or any(
                value is not None
                for value in (
                    row.active_frame_id,
                    row.active_classification_attempt_id,
                    row.active_classification_operation_id,
                    row.active_observation_id,
                    row.reason_code,
                )
            )
        ):
            raise ValueError
    elif row.target_state is Schema6TargetState.ACQUISITION_FAILED:
        if (
            row.run_state != "FAILED"
            or row.attempt_count != 1
            or not row.active_decoder_operation_id
            or not row.reason_code
            or any(
                value is not None
                for value in (
                    row.active_frame_id,
                    row.active_classification_attempt_id,
                    row.active_classification_operation_id,
                    row.active_observation_id,
                )
            )
        ):
            raise ValueError
    elif row.target_state is Schema6TargetState.FRAME_READY:
        if (
            row.run_state != "RUNNING"
            or row.attempt_count != 1
            or not row.active_decoder_operation_id
            or not row.active_frame_id
            or any(
                value is not None
                for value in (
                    row.active_classification_attempt_id,
                    row.active_classification_operation_id,
                    row.active_observation_id,
                    row.reason_code,
                )
            )
        ):
            raise ValueError
    elif row.target_state is Schema6TargetState.CLASSIFYING:
        if (
            row.run_state != "RUNNING"
            or row.attempt_count != 1
            or not row.active_decoder_operation_id
            or not row.active_frame_id
            or not row.active_classification_attempt_id
            or any(
                value is not None
                for value in (
                    row.active_classification_operation_id,
                    row.active_observation_id,
                    row.reason_code,
                )
            )
        ):
            raise ValueError
    elif row.target_state is Schema6TargetState.CLASSIFICATION_FAILED:
        if (
            row.run_state != "FAILED"
            or row.attempt_count != 1
            or not row.active_decoder_operation_id
            or not row.active_frame_id
            or not row.active_classification_operation_id
            or not row.reason_code
            or any(
                value is not None
                for value in (row.active_classification_attempt_id, row.active_observation_id)
            )
        ):
            raise ValueError
    elif row.target_state is Schema6TargetState.OBSERVED:
        if (
            row.run_state != "RUNNING"
            or row.attempt_count != 1
            or not row.active_decoder_operation_id
            or not row.active_frame_id
            or not row.active_classification_operation_id
            or not row.active_observation_id
            or any(
                value is not None
                for value in (row.active_classification_attempt_id, row.reason_code)
            )
        ):
            raise ValueError
    elif row.target_state is Schema6TargetState.INTERRUPTED:
        if row.run_state != "INTERRUPTED" or row.reason_code != "interrupted":
            raise ValueError


_REFERENCE_FAMILIES = {
    "policy_id": "policy",
    "classifier_policy_id": "classifier-policy",
    "media_generation_policy_id": "media-generation-policy",
    "plan_id": "coarse-plan",
    "replay_operation_id": "replay-operation",
    "target_request_id": "target-request",
    "origin_target_request_id": "target-request",
    "schema5_predecessor_manifest_id": "schema5-manifest",
    "common_session_id": "common-session",
    "decoder_operation_id": "decoder-operation",
    "frame_id": "frame",
    "classification_operation_id": "classification-operation",
    "alias_of_target_request_id": "target-request",
    "upper_support_group_id": "support-group",
    "c2_bracket_id": "c2-bracket",
    "d1_input_id": "d1-input",
    "d1_history_id": "d1-history",
    "narrowed_bracket_id": "narrowed-bracket",
    "schema6_manifest_id": "schema6-manifest",
    "source_record_set_id": "source-record-set",
    "evidence_snapshot_id": "evidence-snapshot",
    "terminal_result_id": "terminal-result",
    "schema7_manifest_id": "schema7-manifest",
    "source_clip_id": "source-clip",
    "phase8_request_id": "phase8-request",
    "previous_phase8_manifest_id": "phase8-manifest",
    "coarse_target_request_ids": "target-request",
    "target_request_ids": "target-request",
    "member_target_request_ids": "target-request",
    "member_frame_ids": "frame",
    "member_observation_ids": "observation",
    "selected_observation_ids": "observation",
    "selected_support_group_ids": "support-group",
    "classification_operation_ids": "classification-operation",
    "decoder_operation_ids": "decoder-operation",
    "frame_ids": "frame",
    "observation_ids": "observation",
    "alias_ids": "alias",
    "support_group_ids": "support-group",
    "c2_bracket_ids": "c2-bracket",
    "d1_input_ids": "d1-input",
    "d1_history_ids": "d1-history",
    "narrowed_bracket_ids": "narrowed-bracket",
}


def _reference_family(key: str) -> str | None:
    if key in _REFERENCE_FAMILIES:
        return _REFERENCE_FAMILIES[key]
    if key.endswith("_ids"):
        singular = key[:-1]
        return _REFERENCE_FAMILIES.get(singular)
    return None


def _iter_references(value: Mapping[str, Any]) -> Iterable[tuple[str, str]]:
    for key, child in value.items():
        if isinstance(child, str) and _reference_family(key) is not None:
            yield key, child
        elif isinstance(child, list) and _reference_family(key) is not None:
            for item in child:
                if not isinstance(item, str):
                    raise Phase7EValidationError("reference list is not ordered strings")
                yield key, item
        elif isinstance(child, Mapping):
            yield from _iter_references(child)


_GROUP_FAMILIES = {
    "policy": "policy",
    "classifier_policy": "classifier-policy",
    "schema5_manifest": "schema5-manifest",
    "coarse_plan": "coarse-plan",
    "replay_operation": "replay-operation",
    "common_session": "common-session",
    "target_requests": "target-request",
    "decoder_operations": "decoder-operation",
    "frames": "frame",
    "classification_operations": "classification-operation",
    "observations": "observation",
    "aliases": "alias",
    "support_groups": "support-group",
    "c2_brackets": "c2-bracket",
    "d1_inputs": "d1-input",
    "d1_histories": "d1-history",
    "narrowed_brackets": "narrowed-bracket",
}


def _validate_source_record_groups(
    payload: Mapping[str, Any], records: Mapping[str, tuple[str, Mapping[str, Any]]]
) -> None:
    groups = payload.get("record_groups")
    count = payload.get("record_count")
    if not isinstance(groups, list) or type(count) is not int:
        raise Phase7EValidationError("invalid source-record membership")
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, Mapping) or set(group) != {"type", "ids"}:
            raise Phase7EValidationError("invalid source-record group")
        group_type = group.get("type")
        expected = _GROUP_FAMILIES.get(group_type) if isinstance(group_type, str) else None
        ids = group.get("ids")
        if (
            expected is None
            or not isinstance(ids, list)
            or any(not isinstance(item, str) for item in ids)
        ):
            raise Phase7EValidationError("invalid source-record group")
        for item in ids:
            if item in seen or item not in records or records[item][0] != expected:
                raise Phase7EValidationError("invalid source-record membership")
            seen.add(item)
    if count != len(seen):
        raise Phase7EValidationError("invalid source-record count")


def _validate_ordered_arrays(value: Mapping[str, Any]) -> None:
    for key, child in value.items():
        if isinstance(child, list) and (
            key.endswith("_ids") or key in {"steps", "target_requested_times_utc"}
        ):
            if len(child) != len(set(map(repr, child))):
                raise Phase7EValidationError("duplicate ordered member")
            if isinstance(child, list) and any(isinstance(x, (dict, list)) for x in child):
                for item in child:
                    if isinstance(item, Mapping):
                        _validate_ordered_arrays(item)
        elif isinstance(child, Mapping):
            _validate_ordered_arrays(child)


def _is_phase7_identity(value: str) -> bool:
    return value.startswith("rr-")


def _reject_cycles(edges: Mapping[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise Phase7EValidationError("identity graph contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for child in edges[node]:
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)


__all__ = [
    "Phase7EValidationError",
    "Schema5Envelope",
    "Schema6Envelope",
    "parse_schema_envelope",
    "validate_classifier_evidence",
    "validate_classification_operation",
    "validate_dependency_graph",
    "validate_golden_vectors",
    "validate_phase8_manifest",
    "validate_schema5_state",
    "validate_schema6_state",
]
