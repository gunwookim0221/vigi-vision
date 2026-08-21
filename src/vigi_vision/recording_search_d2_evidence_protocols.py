"""Structural input types used by isolated evidence validation helpers."""
# ruff: noqa: D101, D102

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from vigi_vision.object_presence_values import ClassificationOutcome
    from vigi_vision.recording_search_d2_enums import D2EvidenceRole


class SourceRevisionLike(Protocol):
    @property
    def manifest_digest(self) -> str: ...

    @property
    def c2_bracket_id(self) -> str: ...

    @property
    def d1_source_bracket_id(self) -> str: ...


class EvidenceReferenceLike(Protocol):
    @property
    def role(self) -> D2EvidenceRole: ...

    @property
    def target_id(self) -> str | None: ...

    @property
    def requested_time_utc(self) -> datetime: ...

    @property
    def acquisition_operation_id(self) -> str | None: ...

    @property
    def probe_request_id(self) -> str | None: ...

    @property
    def classification_operation_id(self) -> str | None: ...

    @property
    def observation_id(self) -> str | None: ...

    @property
    def canonical_frame_id(self) -> str | None: ...

    @property
    def alias_id(self) -> str | None: ...

    @property
    def decode_session_id(self) -> str | None: ...

    @property
    def decoded_frame_utc(self) -> datetime | None: ...

    @property
    def decoded_pts(self) -> int | None: ...

    @property
    def decoded_ordinal(self) -> int | None: ...

    @property
    def support_group_id(self) -> str | None: ...

    @property
    def support_index(self) -> int | None: ...

    @property
    def is_phase6_baseline(self) -> bool: ...

    @property
    def classification(self) -> ClassificationOutcome | None: ...


class SupportGroupLike(Protocol):
    @property
    def support_group_id(self) -> str: ...

    @property
    def origin_target_id(self) -> str: ...

    @property
    def support_count(self) -> int: ...

    @property
    def cadence_seconds(self) -> int: ...

    @property
    def decode_session_id(self) -> str: ...

    @property
    def member_target_ids(self) -> tuple[str, ...]: ...

    @property
    def member_observation_ids(self) -> tuple[str, ...]: ...

    @property
    def member_canonical_frame_ids(self) -> tuple[str, ...]: ...


class SnapshotLike(Protocol):
    @property
    def investigation_id(self) -> str: ...

    @property
    def search_run_id(self) -> str: ...

    @property
    def phase6_confirmation_id(self) -> str: ...

    @property
    def baseline_observation_id(self) -> str: ...

    @property
    def plan_id(self) -> str: ...

    @property
    def policy_identity(self) -> str: ...

    @property
    def source_revision(self) -> SourceRevisionLike: ...

    @property
    def references(self) -> tuple[EvidenceReferenceLike, ...]: ...

    @property
    def support_groups(self) -> tuple[SupportGroupLike, ...]: ...
