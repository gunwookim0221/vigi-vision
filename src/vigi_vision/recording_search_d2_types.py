"""Typed JSON shapes for the Phase 7D evidence identity boundary."""

from __future__ import annotations

from typing import TypedDict


class SourceRevisionPayload(TypedDict):
    """Canonical source-revision fields."""

    manifest_digest: str
    c2_bracket_id: str
    d1_source_bracket_id: str


class EvidenceReferencePayload(TypedDict):
    """Canonical reference fields."""

    role: str
    target_id: str | None
    requested_time_utc: str
    acquisition_operation_id: str | None
    probe_request_id: str | None
    classification_operation_id: str | None
    observation_id: str | None
    canonical_frame_id: str | None
    alias_id: str | None
    decode_session_id: str | None
    decoded_frame_utc: str | None
    decoded_pts: int | None
    decoded_ordinal: int | None
    support_group_id: str | None
    support_index: int | None
    is_phase6_baseline: bool


class SupportGroupPayload(TypedDict):
    """Canonical absence-support fields."""

    support_group_id: str
    origin_target_id: str
    support_count: int
    cadence_seconds: int
    decode_session_id: str
    member_target_ids: list[str]
    member_observation_ids: list[str]
    member_canonical_frame_ids: list[str]


class EvidenceSnapshotPayload(TypedDict):
    """Canonical evidence-snapshot fields."""

    identity_schema: str
    investigation_id: str
    search_run_id: str
    phase6_confirmation_id: str
    baseline_observation_id: str
    plan_id: str
    policy_identity: str
    source_revision: SourceRevisionPayload
    references: list[EvidenceReferencePayload]
    support_groups: list[SupportGroupPayload]
