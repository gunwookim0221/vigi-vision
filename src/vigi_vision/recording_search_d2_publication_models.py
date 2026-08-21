"""Strict schema-4 terminal publication models for Phase 7D-2."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import ClassVar, Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_serializer,
    model_validator,
)

from vigi_vision.durable_io import CanonicalUtc  # noqa: TC001
from vigi_vision.recording_search_a2_models import CanonicalFractionalUtc  # noqa: TC001
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b2_policy import RecordingSearchPolicyV3  # noqa: TC001
from vigi_vision.recording_search_d2_enums import D2EvidenceRole, VisualStopReason
from vigi_vision.recording_search_d2_terminal_identity import canonical_terminal_result_payload
from vigi_vision.recording_search_d2_terminal_models import (
    FoundResult,
    InconclusiveResult,
    NotFoundResult,
    TerminalResult,
    TerminalResultKind,
    TerminalSourceStage,
)
from vigi_vision.recording_search_models import RecordingSearchBaseline  # noqa: TC001


class TerminalEvidenceReference(BaseModel):
    """One allowlisted terminal evidence reference."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: D2EvidenceRole
    target_id: StrictStr | None
    requested_time_utc: CanonicalUtc
    acquisition_operation_id: StrictStr | None
    probe_request_id: StrictStr | None
    classification_operation_id: StrictStr | None
    observation_id: StrictStr
    canonical_frame_id: StrictStr | None
    alias_id: StrictStr | None
    decode_session_id: StrictStr | None
    decoded_frame_utc: CanonicalFractionalUtc | None
    decoded_pts: StrictInt | None
    decoded_ordinal: StrictInt | None
    support_group_id: StrictStr | None
    support_index: StrictInt | None
    is_phase6_baseline: StrictBool

    @field_serializer("decoded_frame_utc")
    def serialize_decoded_frame(self, value: datetime | None) -> str | None:
        """Serialize decoded evidence timestamps with fixed fractional precision."""
        if value is None:
            return None
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @model_validator(mode="after")
    def validate_reference(self) -> TerminalEvidenceReference:
        """Require baseline and recording references to retain their roles."""
        if self.role is D2EvidenceRole.BASELINE:
            if not self.is_phase6_baseline or any(
                value is not None
                for value in (
                    self.target_id,
                    self.acquisition_operation_id,
                    self.probe_request_id,
                    self.classification_operation_id,
                    self.canonical_frame_id,
                    self.alias_id,
                    self.decode_session_id,
                    self.decoded_frame_utc,
                    self.decoded_pts,
                    self.decoded_ordinal,
                )
            ):
                raise ValueError
        elif self.is_phase6_baseline or self.target_id is None or self.canonical_frame_id is None:
            raise ValueError
        if self.decoded_pts is not None and self.decoded_pts < 0:
            raise ValueError
        if self.decoded_ordinal is not None and self.decoded_ordinal < 0:
            raise ValueError
        return self


class _PublishedCommon(BaseModel):
    """Common immutable identity fields for a published terminal result."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    result_schema_version: Literal[1]
    result_id: StrictStr = Field(pattern=r"^recording-search-result-v1-[0-9a-f]{64}$")
    investigation_id: StrictStr
    search_run_id: StrictStr
    phase6_confirmation_id: StrictStr
    baseline_observation_id: StrictStr
    plan_id: StrictStr
    policy_identity: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_manifest_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_snapshot_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    terminal_reason: StrictStr
    limitations: tuple[StrictStr, ...]
    published_at_utc: CanonicalFractionalUtc

    @field_serializer("published_at_utc")
    def serialize_published_at(self, value: datetime) -> str:
        """Serialize the publication timestamp with fixed fractional precision."""
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class PublishedFoundResult(_PublishedCommon):
    """Strict persisted FOUND result."""

    result_kind: Literal[TerminalResultKind.FOUND]
    source_bracket_id: StrictStr
    narrowed_bracket_id: StrictStr
    lower_bound_requested_time_utc: CanonicalUtc
    upper_bound_requested_time_utc: CanonicalUtc
    achieved_precision_seconds: StrictInt = Field(gt=0)
    lower_reference: TerminalEvidenceReference
    upper_support: tuple[TerminalEvidenceReference, ...]
    narrowing_evidence: tuple[TerminalEvidenceReference, ...]


class PublishedNotFoundResult(_PublishedCommon):
    """Strict persisted NOT_FOUND result."""

    result_kind: Literal[TerminalResultKind.NOT_FOUND]
    search_start_utc: CanonicalUtc
    search_end_utc: CanonicalUtc
    coarse_grid: tuple[TerminalEvidenceReference, ...]


class PublishedInconclusiveResult(_PublishedCommon):
    """Strict persisted INCONCLUSIVE result."""

    result_kind: Literal[TerminalResultKind.INCONCLUSIVE]
    source_stage: TerminalSourceStage
    visual_reason: VisualStopReason
    evidence: tuple[TerminalEvidenceReference, ...]


PublishedTerminalResult: TypeAlias = (
    PublishedFoundResult | PublishedNotFoundResult | PublishedInconclusiveResult
)


class RecordingSearchManifestV4(BaseModel):
    """Closed schema-4 manifest produced by one terminal replacement."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[4]
    investigation_id: StrictStr
    search_run_id: StrictStr
    state: Literal["FOUND", "NOT_FOUND", "INDETERMINATE"]
    created_at_utc: CanonicalUtc
    started_at_utc: CanonicalUtc
    completed_at_utc: CanonicalUtc
    confirmation: RecordingSearchBaseline
    policy: RecordingSearchPolicyV3
    acquisition_operation_ids: tuple[StrictStr, ...]
    probe_request_ids: tuple[StrictStr, ...]
    canonical_frame_ids: tuple[StrictStr, ...]
    baseline_observation_id: StrictStr
    classification_operation_ids: tuple[StrictStr, ...]
    canonical_observation_ids: tuple[StrictStr, ...]
    target_alias_ids: tuple[StrictStr, ...]
    failure_reason: None = None
    terminal_result: PublishedTerminalResult = Field(discriminator="result_kind")

    @model_validator(mode="after")
    def validate_terminal(self) -> RecordingSearchManifestV4:
        """Bind lifecycle, ownership, and result identity without migration."""
        if self.investigation_id != self.terminal_result.investigation_id:
            raise ValueError
        if self.search_run_id != self.terminal_result.search_run_id:
            raise ValueError
        if self.baseline_observation_id != self.terminal_result.baseline_observation_id:
            raise ValueError
        if self.completed_at_utc != self.terminal_result.published_at_utc.replace(microsecond=0):
            raise ValueError
        expected_state = self.terminal_result.result_kind.value
        if self.state == "INDETERMINATE":
            expected_state = "INDETERMINATE"
        if self.state != expected_state:
            raise ValueError
        return self

    def canonical_json(self) -> str:
        """Serialize the manifest deterministically for atomic publication."""
        return (
            json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        )

    def as_schema3(self) -> RecordingSearchManifestV3:
        """Reconstruct the immutable schema-3 predecessor for strict validation."""
        return RecordingSearchManifestV3(
            schema_version=3,
            investigation_id=self.investigation_id,
            search_run_id=self.search_run_id,
            state="RUNNING",
            created_at_utc=self.created_at_utc,
            started_at_utc=self.started_at_utc,
            completed_at_utc=None,
            confirmation=self.confirmation,
            policy=self.policy,
            acquisition_operation_ids=self.acquisition_operation_ids,
            probe_request_ids=self.probe_request_ids,
            canonical_frame_ids=self.canonical_frame_ids,
            baseline_observation_id=self.baseline_observation_id,
            classification_operation_ids=self.classification_operation_ids,
            canonical_observation_ids=self.canonical_observation_ids,
            target_alias_ids=self.target_alias_ids,
            failure_reason=None,
        )


_REFERENCE_KEYS = (
    "lower_reference",
    "upper_support",
    "narrowing_evidence",
    "coarse_grid",
    "evidence",
)


def _normalize_reference(value: object) -> object:
    if not isinstance(value, dict):
        return value
    value = cast("dict[str, object]", value)
    role = value.get("role")
    decoded = value.get("decoded_frame_utc")
    if isinstance(role, str):
        value["role"] = D2EvidenceRole(role)
    if isinstance(decoded, str) and "." not in decoded:
        value["decoded_frame_utc"] = decoded.removesuffix("Z") + ".000000Z"
    return value


def _normalize_variant(payload: dict[str, object], variant_key: str) -> None:
    variant = payload.pop(variant_key, None)
    if not isinstance(variant, dict):
        raise TypeError
    variant = cast("dict[str, object]", variant)
    for key in _REFERENCE_KEYS:
        if key not in variant:
            continue
        value = variant.get(key)
        if isinstance(value, list):
            items = cast("list[object]", value)
            variant[key] = tuple(_normalize_reference(item) for item in items)
        elif key == "lower_reference":
            variant[key] = _normalize_reference(value)
    source_stage = variant.get("source_stage")
    if isinstance(source_stage, str):
        variant["source_stage"] = TerminalSourceStage(source_stage)
    visual_reason = variant.get("visual_reason")
    if isinstance(visual_reason, str):
        variant["visual_reason"] = VisualStopReason(visual_reason)
    payload.update(variant)


def published_terminal_result(
    result: TerminalResult, published_at_utc: datetime
) -> PublishedTerminalResult:
    """Convert one validated D2-2 result into a strict persisted result."""
    payload = canonical_terminal_result_payload(result)
    _ = payload.pop("identity_schema", None)
    payload.update(
        result_id=result.result_id,
        limitations=tuple(result.limitations),
        result_schema_version=1,
        published_at_utc=published_at_utc,
    )
    variant_key = {
        FoundResult: "found",
        NotFoundResult: "not_found",
        InconclusiveResult: "inconclusive",
    }.get(type(result))
    if variant_key is None:
        raise TypeError
    _normalize_variant(payload, variant_key)
    if isinstance(result, FoundResult):
        return PublishedFoundResult.model_validate(payload, strict=True)
    if isinstance(result, NotFoundResult):
        return PublishedNotFoundResult.model_validate(payload, strict=True)
    return PublishedInconclusiveResult.model_validate(payload, strict=True)
