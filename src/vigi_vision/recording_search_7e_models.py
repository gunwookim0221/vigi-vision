# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportUntypedBaseClass=false, reportUnannotatedClassAttribute=false, reportUnnecessaryIsInstance=false, reportUnreachable=false, reportUnusedCallResult=false, reportGeneralTypeIssues=false
# ruff: noqa: ANN401, C901, D102, EM101, FBT001, FBT003, PLR0912, PLR2004, RUF021, RUF022, SIM102, TRY003, TRY301
"""Strict, immutable Phase 7E value models.

This module contains no storage or media behavior.  It is intentionally a
small contract layer: identity envelopes validate their allow-listed payloads,
while classifier evidence delegates its arithmetic invariants to the already
approved Phase 7B domain.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    model_validator,
)
from typing_extensions import Self

from vigi_vision.object_presence_evidence import RawComparison
from vigi_vision.object_presence_values import (
    ClassificationOutcome,
    VisualReason,
)
from vigi_vision.recording_search_7e_identity import (
    IDENTITY_DOMAINS,
    IdentityValidationError,
    canonical_payload,
    identity_for,
    validate_identity,
)


class Phase7EBase(BaseModel):
    """Frozen strict Pydantic base used by every Phase 7E model."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )


class TimingMode(str, Enum):
    """Request-relative timing mode; physical UTC is deliberately absent."""

    REQUEST_RELATIVE_ESTIMATE = "REQUEST_RELATIVE_ESTIMATE"


class PhysicalTimeBias(str, Enum):
    """Closed physical-time calibration states."""

    UNKNOWN_UNBOUNDED = "UNKNOWN_UNBOUNDED"


class RunState(str, Enum):
    """Schema 5/6 lifecycle states."""

    RUNNING = "RUNNING"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class Schema5PhaseState(str, Enum):
    """Schema 5 phase-state union."""

    PLANNED = "PLANNED"
    ACQUIRING = "ACQUIRING"
    ACQUISITION_FAILED = "ACQUISITION_FAILED"
    ACQUIRED = "ACQUIRED"
    INTERRUPTED = "INTERRUPTED"


class Schema6TargetState(str, Enum):
    """Schema 6 target-state union."""

    REQUESTED = "REQUESTED"
    DECODING = "DECODING"
    ACQUISITION_FAILED = "ACQUISITION_FAILED"
    FRAME_READY = "FRAME_READY"
    CLASSIFYING = "CLASSIFYING"
    CLASSIFICATION_FAILED = "CLASSIFICATION_FAILED"
    OBSERVED = "OBSERVED"
    INTERRUPTED = "INTERRUPTED"


class ResultKind(str, Enum):
    """Terminal result kinds."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INCONCLUSIVE = "INCONCLUSIVE"


class Phase8State(str, Enum):
    """Closed Phase 8 manifest states."""

    RETRYABLE = "RETRYABLE"
    CLIP_READY = "CLIP_READY"
    READY = "READY"
    DELETING = "DELETING"
    DELETED = "DELETED"


class OperationalReason(str, Enum):
    """Operational reasons admitted by Phase 7E classification records."""

    CLASSIFIER_TIMEOUT = "classifier_timeout"
    CLASSIFICATION_FAILED = "classification_failed"
    INVALID_CLASSIFIER_RESULT = "invalid_classifier_result"
    ACQUISITION_FAILED = "acquisition_failed"
    DECODE_FAILED = "decode_failed"
    RECORDING_UNAVAILABLE = "recording_unavailable"
    INTERRUPTED = "interrupted"


class RecordingSearchRequest(Phase7EBase):
    """Validated internal request with server-owned baseline facts excluded."""

    investigation_id: str
    search_end_time_text: str
    source_timezone: str

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if not self.investigation_id or not self.search_end_time_text or not self.source_timezone:
            raise ValueError
        if any(character in self.investigation_id for character in "\\/\0"):
            raise ValueError
        return self


class StrictIdentityEnvelope(Phase7EBase):
    """A family, identity, and canonical payload with no trusted extras."""

    family: str
    identity: str
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        try:
            if self.family not in IDENTITY_DOMAINS:
                raise IdentityValidationError("unknown family")
            validate_identity(self.family, self.identity, self.payload)
        except IdentityValidationError as exc:
            raise ValueError(str(exc)) from exc
        return self

    @classmethod
    def from_payload(cls, family: str, payload: dict[str, Any]) -> Self:
        """Construct an envelope with its identity computed once."""
        return cls(family=family, payload=payload, identity=identity_for(family, payload))

    @property
    def canonical_json(self) -> str:
        """Return the exact canonical payload bytes as text."""
        return canonical_payload(self.payload, self.family)


class _PayloadModel(Phase7EBase):
    """Base for typed family wrappers that expose a payload value object."""

    payload: dict[str, Any]
    family: ClassVar[str]

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        try:
            canonical_payload(self.payload, self.family)
        except IdentityValidationError as exc:
            raise ValueError(str(exc)) from exc
        _validate_family_fields(self.family, self.payload)
        return self

    @property
    def identity(self) -> str:
        """Compute this immutable value object's Phase 7E identity."""
        return identity_for(self.family, self.payload)


def _payload_model(name: str, family: str) -> type[_PayloadModel]:
    return type(name, (_PayloadModel,), {"family": family, "__module__": __name__})


RequestRelativePolicy = _payload_model("RequestRelativePolicy", "policy")
ClassifierPolicy = _payload_model("ClassifierPolicy", "classifier-policy")
MediaGenerationPolicy = _payload_model("MediaGenerationPolicy", "media-generation-policy")
CoarsePlan = _payload_model("CoarsePlan", "coarse-plan")
ReplayOperation = _payload_model("ReplayOperation", "replay-operation")
TargetRequest = _payload_model("TargetRequest", "target-request")
Schema5Manifest = _payload_model("Schema5Manifest", "schema5-manifest")
CommonSession = _payload_model("CommonSession", "common-session")
DecoderOperation = _payload_model("DecoderOperation", "decoder-operation")
DecodedFrame = _payload_model("DecodedFrame", "frame")
ClassificationOperationEnvelope = _payload_model(
    "ClassificationOperationEnvelope", "classification-operation"
)
ObservationEnvelope = _payload_model("ObservationEnvelope", "observation")
Alias = _payload_model("Alias", "alias")
SupportGroup = _payload_model("SupportGroup", "support-group")
C2Bracket = _payload_model("C2Bracket", "c2-bracket")
D1Input = _payload_model("D1Input", "d1-input")
D1History = _payload_model("D1History", "d1-history")
NarrowedBracket = _payload_model("NarrowedBracket", "narrowed-bracket")
Schema6Manifest = _payload_model("Schema6Manifest", "schema6-manifest")
SourceRecordSet = _payload_model("SourceRecordSet", "source-record-set")
EvidenceSnapshot = _payload_model("EvidenceSnapshot", "evidence-snapshot")
TerminalResult = _payload_model("TerminalResult", "terminal-result")
Schema7Manifest = _payload_model("Schema7Manifest", "schema7-manifest")
SourceClip = _payload_model("SourceClip", "source-clip")
Phase8Request = _payload_model("Phase8Request", "phase8-request")


class ComparableEvidence(Phase7EBase):
    """The complete, comparable B4 evidence row."""

    baseline_mask_pixel_count: StrictInt
    probe_mask_pixel_count: StrictInt
    roi_pixel_count: StrictInt = Field(gt=0)
    mask_intersection_pixel_count: StrictInt
    mask_union_pixel_count: StrictInt
    baseline_mask_coverage: StrictFloat | str
    probe_mask_coverage: StrictFloat | str
    mask_iou: StrictFloat | str
    effective_comparison_area: StrictInt
    roi_luma_ncc: StrictFloat | str
    visual_status: str = "comparable"
    unusable_reason: None = None

    @model_validator(mode="after")
    def validate_comparable(self) -> Self:
        _validate_evidence_status(self.visual_status, self.unusable_reason, True)
        _raw_comparison(self.model_dump())
        return self

    def to_raw(self) -> RawComparison:
        """Return the already validated Phase 7B raw comparison."""
        return _raw_comparison(self.model_dump())


class UnusableEvidence(Phase7EBase):
    """One of the five closed production unusable evidence shapes."""

    baseline_mask_pixel_count: StrictInt | None
    probe_mask_pixel_count: StrictInt | None
    roi_pixel_count: StrictInt = Field(gt=0)
    mask_intersection_pixel_count: StrictInt | None
    mask_union_pixel_count: StrictInt | None
    baseline_mask_coverage: StrictFloat | str | None
    probe_mask_coverage: StrictFloat | str | None
    mask_iou: StrictFloat | str | None
    effective_comparison_area: StrictInt | None
    roi_luma_ncc: StrictFloat | str | None
    visual_status: str = "unusable"
    unusable_reason: VisualReason | str

    @model_validator(mode="after")
    def validate_unusable(self) -> Self:
        _validate_evidence_status(self.visual_status, self.unusable_reason, False)
        _raw_comparison(self.model_dump())
        return self

    def to_raw(self) -> RawComparison:
        """Return the already validated Phase 7B raw comparison."""
        return _raw_comparison(self.model_dump())


class ClassifierEvidence(Phase7EBase):
    """Closed discriminated evidence union used in operation/observation rows."""

    baseline_mask_pixel_count: StrictInt | None
    probe_mask_pixel_count: StrictInt | None
    roi_pixel_count: StrictInt = Field(gt=0)
    mask_intersection_pixel_count: StrictInt | None
    mask_union_pixel_count: StrictInt | None
    baseline_mask_coverage: StrictFloat | str | None
    probe_mask_coverage: StrictFloat | str | None
    mask_iou: StrictFloat | str | None
    effective_comparison_area: StrictInt | None
    roi_luma_ncc: StrictFloat | str | None
    visual_status: str
    unusable_reason: VisualReason | str | None

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        _validate_evidence_status(
            self.visual_status, self.unusable_reason, self.visual_status == "comparable"
        )
        _raw_comparison(self.model_dump())
        return self

    def to_raw(self) -> RawComparison:
        """Return the validated production evidence model."""
        return _raw_comparison(self.model_dump())


class ClassificationOperation(Phase7EBase):
    """Strict visual or operational classifier completion record."""

    investigation_id: str
    run_id: str
    frame_id: str
    target_request_id: str
    baseline_identity: str
    classifier_policy_id: str
    attempt: StrictInt = Field(ge=1)
    result_kind: str
    outcome: ClassificationOutcome | str | None
    reason_code: VisualReason | str | None
    classifier_evidence: ClassifierEvidence | None
    operational_reason: OperationalReason | str | None

    @model_validator(mode="after")
    def validate_result_shape(self) -> Self:
        if self.result_kind == "VISUAL":
            if (
                self.outcome is None
                or self.operational_reason is not None
                or self.classifier_evidence is None
            ):
                raise ValueError
            outcome = _outcome(self.outcome)
            if outcome is None:
                raise ValueError
            result = _classification_result(
                outcome, _reason(self.reason_code), self.classifier_evidence
            )
            if result is None:
                raise ValueError
        elif self.result_kind == "OPERATIONAL":
            if (
                self.outcome is not None
                or self.reason_code is not None
                or self.classifier_evidence is not None
            ):
                raise ValueError
            if self.operational_reason is None:
                raise ValueError
            try:
                OperationalReason(self.operational_reason)
            except ValueError as exc:
                raise ValueError from exc
        else:
            raise ValueError
        return self


class Observation(Phase7EBase):
    """Strict visual observation bound to a completed classification operation."""

    investigation_id: str
    run_id: str
    common_session_id: str
    classification_operation_id: str
    frame_id: str
    target_request_id: str
    classifier_policy_id: str
    outcome: ClassificationOutcome | str
    reason_code: VisualReason | str | None
    classifier_evidence: ClassifierEvidence

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        outcome = _outcome(self.outcome)
        if outcome is None:
            raise ValueError
        _classification_result(outcome, _reason(self.reason_code), self.classifier_evidence)
        return self


class ClipIntegrity(Phase7EBase):
    """Separate binary integrity metadata for a Phase 8 clip."""

    sha256: str
    size_bytes: StrictInt = Field(gt=0)
    observed_duration_ticks: StrictInt = Field(ge=0)
    observed_time_base_num: StrictInt = Field(gt=0)
    observed_time_base_den: StrictInt = Field(gt=0)
    video_stream_index: StrictInt = Field(ge=0)
    codec: str
    profile: str
    level: StrictInt = Field(ge=0)
    pixel_format: str
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)
    average_frame_rate_num: StrictInt = Field(gt=0)
    average_frame_rate_den: StrictInt = Field(gt=0)
    audio_stream_count: StrictInt = Field(ge=0)
    generation_outcome: str

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError
        return self


class Phase8Manifest(Phase7EBase):
    """Strict state envelope for the separate Phase 8 repository."""

    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        try:
            canonical_payload(self.payload, "phase8-manifest")
        except IdentityValidationError as exc:
            raise ValueError(str(exc)) from exc
        _validate_phase8_payload(self.payload)
        return self

    @property
    def identity(self) -> str:
        """Return the state envelope identity."""
        return identity_for("phase8-manifest", self.payload)


SearchPolicy = RequestRelativePolicy
Policy = RequestRelativePolicy
MediaPolicy = MediaGenerationPolicy
ClassificationOperationRecord = ClassificationOperationEnvelope
CanonicalFrame = DecodedFrame
ObservationRecord = ObservationEnvelope
Phase8RequestModel = Phase8Request
Phase8ManifestModel = Phase8Manifest


def _validate_evidence_status(
    status: str, reason: VisualReason | str | None, comparable: bool
) -> None:
    if status not in {"comparable", "unusable"} or (status == "comparable") is not comparable:
        raise ValueError
    if comparable and reason is not None:
        raise ValueError
    normalized = _reason(reason)
    if not comparable and (
        normalized is None or normalized is VisualReason.INSUFFICIENT_VISUAL_EVIDENCE
    ):
        raise ValueError


def _validate_family_fields(family: str, payload: dict[str, Any]) -> None:
    expected_versions = {
        "schema5-manifest": 5,
        "schema6-manifest": 6,
        "schema7-manifest": 7,
        "source-record-set": 1,
        "evidence-snapshot": 1,
        "terminal-result": 1,
        "source-clip": 1,
        "phase8-request": 1,
        "phase8-manifest": 1,
    }
    if family in expected_versions and payload.get("schema_version") != expected_versions[family]:
        raise ValueError
    if family == "policy":
        if (
            payload.get("schema_family") != [5, 6, 7]
            or payload.get("provenance_level") != "REQUEST_RELATIVE_ESTIMATE"
            or payload.get("maximum_search_duration_seconds") != 600
        ):
            raise ValueError
        if payload.get("default_search_duration_seconds", 0) > payload.get(
            "maximum_search_duration_seconds", 0
        ):
            raise ValueError
    if family == "coarse-plan":
        targets = payload.get("target_requested_times_utc")
        if (
            not isinstance(targets, list)
            or not targets
            or targets != sorted(targets)
            or len(targets) != len(set(targets))
        ):
            raise ValueError


def _number(value: StrictFloat | str | None) -> float | None:
    if value is None:
        return None
    if type(value) is float:
        return value
    if type(value) is str and re.fullmatch(r"-?(?:0|[1-9]\d*)\.\d{6}", value):
        return float(value)
    raise ValueError


def _raw_comparison(data: dict[str, Any]) -> RawComparison:
    for name in ("baseline_mask_coverage", "probe_mask_coverage", "mask_iou", "roi_luma_ncc"):
        data[name] = _number(data[name])
    from vigi_vision.object_presence_values import VisualStatus  # noqa: PLC0415

    data["visual_status"] = VisualStatus(data["visual_status"])
    data["unusable_reason"] = _reason(data["unusable_reason"])
    return RawComparison.model_validate(data)


def _classification_result(
    outcome: ClassificationOutcome,
    reason: VisualReason | None,
    evidence: ClassifierEvidence,
) -> Any:
    from vigi_vision.object_presence_evidence import ClassificationResult  # noqa: PLC0415

    return ClassificationResult(outcome=outcome, reason_code=reason, comparison=evidence.to_raw())


def _outcome(value: ClassificationOutcome | str | None) -> ClassificationOutcome | None:
    if value is None:
        return None
    return value if isinstance(value, ClassificationOutcome) else ClassificationOutcome(value)


def _reason(value: VisualReason | str | None) -> VisualReason | None:
    if value is None:
        return None
    return value if isinstance(value, VisualReason) else VisualReason(value)


def _validate_phase8_payload(payload: dict[str, Any]) -> None:
    state = payload.get("state")
    if state not in {member.value for member in Phase8State}:
        raise ValueError
    clip = payload.get("clip_integrity")
    if clip is not None:
        ClipIntegrity.model_validate(clip)
    if state == Phase8State.RETRYABLE:
        if (
            not payload.get("failure_reason")
            or payload.get("source_clip_id") is None
            and clip is not None
        ):
            raise ValueError
    elif state == Phase8State.CLIP_READY:
        if payload.get("source_clip_id") is None or clip is None:
            raise ValueError
        if payload.get("failure_reason") is not None:
            raise ValueError
    elif state == Phase8State.READY:
        if (
            payload.get("source_clip_id") is None
            or clip is None
            or payload.get("phase8_request_id") is None
        ):
            raise ValueError
        if payload.get("failure_reason") is not None:
            raise ValueError
    elif state == Phase8State.DELETING:
        if (
            payload.get("source_clip_tombstone_name") is None
            or payload.get("common_media_tombstone_name") is None
        ):
            raise ValueError
    elif state == Phase8State.DELETED:
        if payload.get("deletion_result") != "DELETED":
            raise ValueError


__all__ = [
    "Alias",
    "CanonicalFrame",
    "C2Bracket",
    "ClassifierEvidence",
    "ClassifierPolicy",
    "ClassificationOperation",
    "ClassificationOperationRecord",
    "CoarsePlan",
    "CommonSession",
    "ComparableEvidence",
    "D1History",
    "D1Input",
    "DecodedFrame",
    "DecoderOperation",
    "EvidenceSnapshot",
    "MediaGenerationPolicy",
    "MediaPolicy",
    "NarrowedBracket",
    "Observation",
    "ObservationRecord",
    "OperationalReason",
    "Phase7EBase",
    "Phase8Manifest",
    "Phase8Request",
    "Phase8State",
    "Phase8ManifestModel",
    "Phase8RequestModel",
    "Policy",
    "PhysicalTimeBias",
    "ReplayOperation",
    "RequestRelativePolicy",
    "RecordingSearchRequest",
    "ResultKind",
    "RunState",
    "Schema5Manifest",
    "Schema5PhaseState",
    "Schema6Manifest",
    "Schema6TargetState",
    "Schema7Manifest",
    "SourceClip",
    "SourceRecordSet",
    "SearchPolicy",
    "StrictIdentityEnvelope",
    "SupportGroup",
    "TargetRequest",
    "TerminalResult",
    "TimingMode",
    "UnusableEvidence",
]
