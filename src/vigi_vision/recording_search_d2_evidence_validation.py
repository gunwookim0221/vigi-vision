# ruff: noqa: D103
"""Strict validation helpers for the D2-0 evidence models."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_d2_enums import D2EvidenceRole

if TYPE_CHECKING:
    from vigi_vision.recording_search_d2_evidence_protocols import (
        EvidenceReferenceLike,
        SnapshotLike,
        SourceRevisionLike,
        SupportGroupLike,
    )


def validate_source_revision(value: SourceRevisionLike, digest_length: int) -> None:
    if (
        type(value.manifest_digest) is not str
        or len(value.manifest_digest) != digest_length
        or any(char not in "0123456789abcdef" for char in value.manifest_digest)
        or not is_id(value.c2_bracket_id)
        or not is_id(value.d1_source_bracket_id)
    ):
        raise ValueError


def validate_reference(value: EvidenceReferenceLike) -> None:
    require_utc(value.requested_time_utc)
    if type(value.role) is not D2EvidenceRole:
        raise TypeError
    if value.classification is not None and type(value.classification) is not ClassificationOutcome:
        raise TypeError
    if type(value.is_phase6_baseline) is not bool:
        raise ValueError
    if value.role is D2EvidenceRole.BASELINE:
        validate_baseline(value)
    else:
        validate_visual(value)


def validate_baseline(value: EvidenceReferenceLike) -> None:
    if not value.is_phase6_baseline or value.target_id is not None:
        raise ValueError
    if not is_id(value.observation_id) or any(
        item is not None
        for item in (
            value.acquisition_operation_id,
            value.probe_request_id,
            value.classification_operation_id,
            value.canonical_frame_id,
            value.alias_id,
            value.decode_session_id,
            value.decoded_frame_utc,
            value.decoded_pts,
            value.decoded_ordinal,
            value.support_group_id,
            value.support_index,
        )
    ):
        raise ValueError


def validate_visual(value: EvidenceReferenceLike) -> None:
    if value.is_phase6_baseline or not all(
        is_id(item)
        for item in (
            value.target_id,
            value.acquisition_operation_id,
            value.probe_request_id,
            value.classification_operation_id,
            value.observation_id,
            value.canonical_frame_id,
            value.decode_session_id,
        )
    ):
        raise ValueError
    require_utc(value.decoded_frame_utc)
    if value.alias_id is not None and not is_id(value.alias_id):
        raise ValueError
    if type(value.decoded_pts) is not int or value.decoded_pts < 0:
        raise ValueError
    if type(value.decoded_ordinal) is not int or value.decoded_ordinal < 0:
        raise ValueError
    if value.role is D2EvidenceRole.ABSENCE_SUPPORT:
        if value.alias_id is not None or not is_id(value.support_group_id):
            raise ValueError
        if type(value.support_index) is not int or value.support_index < 0:
            raise ValueError
    elif value.support_group_id is not None or value.support_index is not None:
        raise ValueError


def validate_support_group_shape(value: SupportGroupLike) -> None:
    if (
        not is_id(value.support_group_id)
        or not is_id(value.origin_target_id)
        or not is_id(value.decode_session_id)
        or type(value.support_count) is not int
        or value.support_count <= 0
        or type(value.cadence_seconds) is not int
        or value.cadence_seconds <= 0
        or not all(
            type(items) is tuple
            for items in (
                value.member_target_ids,
                value.member_observation_ids,
                value.member_canonical_frame_ids,
            )
        )
        or any(
            len(items) != value.support_count
            or any(not is_id(item) for item in items)
            or len(set(items)) != len(items)
            for items in (
                value.member_target_ids,
                value.member_observation_ids,
                value.member_canonical_frame_ids,
            )
        )
    ):
        raise ValueError


def validate_snapshot_shape(value: SnapshotLike) -> None:
    if not all(
        is_id(item)
        for item in (
            value.investigation_id,
            value.search_run_id,
            value.phase6_confirmation_id,
            value.baseline_observation_id,
            value.plan_id,
            value.policy_identity,
        )
    ):
        raise ValueError
    if (
        type(value.references) is not tuple
        or type(value.support_groups) is not tuple
        or not value.references
        or type(value.source_revision).__name__ != "D2SourceRevision"
        or any(type(reference).__name__ != "D2EvidenceReference" for reference in value.references)
        or any(type(group).__name__ != "D2SupportGroup" for group in value.support_groups)
        or value.references[0].role is not D2EvidenceRole.BASELINE
        or sum(reference.role is D2EvidenceRole.BASELINE for reference in value.references) != 1
        or value.references[0].observation_id != value.baseline_observation_id
    ):
        raise ValueError


def validate_reference_order(references: tuple[EvidenceReferenceLike, ...]) -> None:
    ranks = {
        D2EvidenceRole.BASELINE: 0,
        D2EvidenceRole.COARSE_TARGET: 1,
        D2EvidenceRole.D1_MIDPOINT: 2,
        D2EvidenceRole.ABSENCE_SUPPORT: 3,
    }
    if any(ranks[left.role] > ranks[right.role] for left, right in pairwise(references)):
        raise ValueError
    validate_unique_reference_ids(references)
    seen_frames: set[str] = set()
    for reference in references:
        if reference.canonical_frame_id is not None:
            if reference.canonical_frame_id in seen_frames and reference.alias_id is None:
                raise ValueError
            if reference.alias_id is None:
                seen_frames.add(reference.canonical_frame_id)


def validate_unique_reference_ids(references: tuple[EvidenceReferenceLike, ...]) -> None:
    observations: set[str] = set()
    targets: set[str] = set()
    operations: set[str] = set()
    for reference in references:
        observation_id = reference.observation_id
        if (
            type(observation_id) is not str
            or not is_id(observation_id)
            or observation_id in observations
        ):
            raise ValueError
        observations.add(observation_id)
        target_id = reference.target_id
        if target_id is not None:
            if target_id in targets:
                raise ValueError
            targets.add(target_id)
        for operation_id in (
            reference.acquisition_operation_id,
            reference.probe_request_id,
            reference.classification_operation_id,
        ):
            if operation_id is not None:
                if operation_id in operations:
                    raise ValueError
                operations.add(operation_id)


def validate_support_members(
    references: tuple[EvidenceReferenceLike, ...], groups: tuple[SupportGroupLike, ...]
) -> None:
    if len({group.support_group_id for group in groups}) != len(groups):
        raise ValueError
    by_group: dict[str, list[EvidenceReferenceLike]] = {}
    for reference in references:
        if reference.role is D2EvidenceRole.ABSENCE_SUPPORT and reference.support_group_id:
            by_group.setdefault(reference.support_group_id, []).append(reference)
    for group in groups:
        validate_support_group(group, by_group.get(group.support_group_id, []))
    if set(by_group) != {group.support_group_id for group in groups}:
        raise ValueError


def validate_support_group(group: SupportGroupLike, members: list[EvidenceReferenceLike]) -> None:
    if len(members) != group.support_count:
        raise ValueError
    validate_support_id_alignment(group, members)
    if members[0].target_id != group.origin_target_id:
        raise ValueError
    ordered = tuple(support_order_key(member) for member in members)
    if any(left[0] >= right[0] for left, right in pairwise(ordered)):
        raise ValueError
    if any(left[1] >= right[1] for left, right in pairwise(ordered)):
        raise ValueError
    if any(left[2] >= right[2] for left, right in pairwise(ordered)):
        raise ValueError
    if any(left[3] >= right[3] for left, right in pairwise(ordered)):
        raise ValueError
    if not cadence_matches(members, group.cadence_seconds):
        raise ValueError


def validate_support_id_alignment(
    group: SupportGroupLike, members: list[EvidenceReferenceLike]
) -> None:
    if tuple(reference.support_index for reference in members) != tuple(range(group.support_count)):
        raise ValueError
    if tuple(reference.target_id for reference in members) != group.member_target_ids:
        raise ValueError
    if tuple(reference.observation_id for reference in members) != group.member_observation_ids:
        raise ValueError
    if (
        tuple(reference.canonical_frame_id for reference in members)
        != group.member_canonical_frame_ids
    ):
        raise ValueError
    if any(reference.decode_session_id != group.decode_session_id for reference in members):
        raise ValueError


def is_id(value: object) -> bool:
    return type(value) is str and bool(value)


def support_order_key(value: EvidenceReferenceLike) -> tuple[datetime, datetime, int, int]:
    if (
        value.decoded_frame_utc is None
        or value.decoded_pts is None
        or value.decoded_ordinal is None
    ):
        raise ValueError
    return (
        value.requested_time_utc,
        value.decoded_frame_utc,
        value.decoded_pts,
        value.decoded_ordinal,
    )


def cadence_matches(members: list[EvidenceReferenceLike], cadence_seconds: int) -> bool:
    return all(
        right.requested_time_utc - left.requested_time_utc == timedelta(seconds=cadence_seconds)
        for left, right in pairwise(members)
    )


def require_utc(value: datetime | None) -> None:
    if (
        value is None
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.microsecond != 0
    ):
        raise ValueError


def format_utc(value: datetime) -> str:
    require_utc(value)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_optional_utc(value: datetime | None) -> str | None:
    return None if value is None else format_utc(value)
