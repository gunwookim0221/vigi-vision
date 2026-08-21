"""Strict in-memory evidence snapshot models for the D2-0 adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vigi_vision.recording_search_d2_evidence_validation import (
    format_optional_utc,
    format_utc,
    validate_reference,
    validate_reference_order,
    validate_snapshot_shape,
    validate_source_revision,
    validate_support_group_shape,
    validate_support_members,
)

if TYPE_CHECKING:
    from datetime import datetime

    from vigi_vision.object_presence_values import ClassificationOutcome
    from vigi_vision.recording_search_d2_enums import D2EvidenceRole
    from vigi_vision.recording_search_d2_types import (
        EvidenceReferencePayload,
        EvidenceSnapshotPayload,
        SourceRevisionPayload,
        SupportGroupPayload,
    )


_DIGEST_LENGTH = 64


@dataclass(frozen=True, slots=True)
class D2SourceRevision:
    """Stable source identities bound into an evidence snapshot."""

    manifest_digest: str
    c2_bracket_id: str
    d1_source_bracket_id: str

    def __post_init__(self) -> None:
        """Validate the source digest and non-empty bracket identities."""
        validate_source_revision(self, _DIGEST_LENGTH)

    def to_payload(self) -> SourceRevisionPayload:
        """Return the exact canonical payload shape."""
        return {
            "manifest_digest": self.manifest_digest,
            "c2_bracket_id": self.c2_bracket_id,
            "d1_source_bracket_id": self.d1_source_bracket_id,
        }


@dataclass(frozen=True, slots=True)
class D2EvidenceReference:
    """One strictly owned reference included in the digest input."""

    role: D2EvidenceRole
    target_id: str | None
    requested_time_utc: datetime
    acquisition_operation_id: str | None
    probe_request_id: str | None
    classification_operation_id: str | None
    observation_id: str | None
    canonical_frame_id: str | None
    alias_id: str | None
    decode_session_id: str | None
    decoded_frame_utc: datetime | None
    decoded_pts: int | None
    decoded_ordinal: int | None
    support_group_id: str | None
    support_index: int | None
    is_phase6_baseline: bool
    classification: ClassificationOutcome | None = None

    def __post_init__(self) -> None:
        """Validate role-specific ownership and decoded-frame provenance."""
        validate_reference(self)

    def to_payload(self) -> EvidenceReferencePayload:
        """Return the allowlisted digest payload without diagnostics."""
        return {
            "role": self.role.value,
            "target_id": self.target_id,
            "requested_time_utc": format_utc(self.requested_time_utc),
            "acquisition_operation_id": self.acquisition_operation_id,
            "probe_request_id": self.probe_request_id,
            "classification_operation_id": self.classification_operation_id,
            "observation_id": self.observation_id,
            "canonical_frame_id": self.canonical_frame_id,
            "alias_id": self.alias_id,
            "decode_session_id": self.decode_session_id,
            "decoded_frame_utc": format_optional_utc(self.decoded_frame_utc),
            "decoded_pts": self.decoded_pts,
            "decoded_ordinal": self.decoded_ordinal,
            "support_group_id": self.support_group_id,
            "support_index": self.support_index,
            "is_phase6_baseline": self.is_phase6_baseline,
        }


@dataclass(frozen=True, slots=True)
class D2SupportGroup:
    """Ordered absence-support identity included in a snapshot."""

    support_group_id: str
    origin_target_id: str
    support_count: int
    cadence_seconds: int
    decode_session_id: str
    member_target_ids: tuple[str, ...]
    member_observation_ids: tuple[str, ...]
    member_canonical_frame_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate support count, cadence, and distinct member identities."""
        validate_support_group_shape(self)

    def to_payload(self) -> SupportGroupPayload:
        """Return the exact allowlisted digest payload."""
        return {
            "support_group_id": self.support_group_id,
            "origin_target_id": self.origin_target_id,
            "support_count": self.support_count,
            "cadence_seconds": self.cadence_seconds,
            "decode_session_id": self.decode_session_id,
            "member_target_ids": list(self.member_target_ids),
            "member_observation_ids": list(self.member_observation_ids),
            "member_canonical_frame_ids": list(self.member_canonical_frame_ids),
        }


@dataclass(frozen=True, slots=True)
class D2EvidenceSnapshot:
    """Complete in-memory authoritative evidence snapshot for D2-0."""

    investigation_id: str
    search_run_id: str
    phase6_confirmation_id: str
    baseline_observation_id: str
    plan_id: str
    policy_identity: str
    source_revision: D2SourceRevision
    references: tuple[D2EvidenceReference, ...]
    support_groups: tuple[D2SupportGroup, ...]

    def __post_init__(self) -> None:
        """Validate ordered references and support-group membership."""
        validate_snapshot_shape(self)
        validate_reference_order(self.references)
        validate_support_members(self.references, self.support_groups)

    def to_payload(self) -> EvidenceSnapshotPayload:
        """Return the exact canonical evidence payload."""
        return {
            "identity_schema": "recording-search-evidence-snapshot-v1",
            "investigation_id": self.investigation_id,
            "search_run_id": self.search_run_id,
            "phase6_confirmation_id": self.phase6_confirmation_id,
            "baseline_observation_id": self.baseline_observation_id,
            "plan_id": self.plan_id,
            "policy_identity": self.policy_identity,
            "source_revision": self.source_revision.to_payload(),
            "references": [reference.to_payload() for reference in self.references],
            "support_groups": [group.to_payload() for group in self.support_groups],
        }
