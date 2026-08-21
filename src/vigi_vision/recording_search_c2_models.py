"""Typed non-persistent outcomes for coarse recording interpretation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from itertools import pairwise
from typing import TYPE_CHECKING

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_c1_models import CoarseSampleStatus

if TYPE_CHECKING:
    from vigi_vision.recording_search_c1_models import CoarseSamplingResult
    from vigi_vision.recording_search_c1_planner import CoarseSamplingIdentity, CoarseSamplingPlan

_DIGEST_LENGTH = 64


class _CoarseInterpretationStatus(str, Enum):
    BRACKET_READY = "BRACKET_READY"
    INCOMPLETE = "INCOMPLETE"
    INTERRUPTED = "INTERRUPTED"
    NO_CANDIDATE = "NO_CANDIDATE"
    INCONCLUSIVE = "INCONCLUSIVE"
    CORRUPT = "CORRUPT"


@dataclass(frozen=True, slots=True)
class _CoarseTargetEvidence:
    requested_time_utc: datetime
    status: CoarseSampleStatus
    classification: ClassificationOutcome | None = None
    probe_request_id: str | None = None
    observation_id: str | None = None
    canonical_frame_id: str | None = None
    decode_session_id: str | None = None
    decoded_frame_utc: datetime | None = None
    decoded_pts: int | None = None
    decoded_ordinal: int | None = None
    is_alias: bool = False
    origin_coarse_target_utc: datetime | None = None
    confirmation_run_id: str | None = None
    support_identity: CoarseSamplingIdentity | None = None
    is_baseline: bool = False

    def __post_init__(self) -> None:
        _require_whole_utc(self.requested_time_utc)
        _validate_target_identity(self)
        _validate_target_binding(self)


def _validate_target_identity(value: _CoarseTargetEvidence) -> None:
    if value.is_baseline:
        if (
            value.status is not CoarseSampleStatus.SUCCESS
            or value.classification is not ClassificationOutcome.PRESENT
            or not value.observation_id
            or value.probe_request_id is not None
            or value.canonical_frame_id is not None
            or value.decode_session_id is not None
            or value.decoded_frame_utc is not None
            or value.decoded_pts is not None
            or value.decoded_ordinal is not None
            or value.is_alias
        ):
            raise ValueError
        return
    if value.status is CoarseSampleStatus.SUCCESS:
        if (
            value.classification is None
            or not value.probe_request_id
            or not value.observation_id
            or not value.canonical_frame_id
            or not value.decode_session_id
            or value.decoded_frame_utc is None
            or value.decoded_pts is None
            or value.decoded_ordinal is None
        ):
            raise ValueError
        if (
            value.decoded_frame_utc.tzinfo is None
            or value.decoded_frame_utc.utcoffset() != timedelta(0)
        ):
            raise ValueError
        if value.decoded_pts < 0 or value.decoded_ordinal < 0:
            raise ValueError
    elif any(
        value is not None
        for value in (
            value.classification,
            value.observation_id,
            value.canonical_frame_id,
            value.decode_session_id,
            value.decoded_frame_utc,
            value.decoded_pts,
            value.decoded_ordinal,
        )
    ):
        raise ValueError
    if value.probe_request_id is not None and not value.probe_request_id:
        raise ValueError


def _validate_target_binding(value: _CoarseTargetEvidence) -> None:
    bound = (
        value.origin_coarse_target_utc,
        value.confirmation_run_id,
        value.support_identity,
    )
    if any(item is None for item in bound) and any(item is not None for item in bound):
        raise ValueError
    if value.origin_coarse_target_utc is not None:
        _require_whole_utc(value.origin_coarse_target_utc)
        if not value.confirmation_run_id or value.support_identity is None:
            raise ValueError


@dataclass(frozen=True, slots=True)
class _CoarseEvidenceSnapshot:
    investigation_id: str
    search_run_id: str
    identity: CoarseSamplingIdentity
    plan: CoarseSamplingPlan
    policy_version: str
    absence_confirmation_frames: int
    absence_cadence_seconds: int
    baseline_observation_id: str
    manifest_digest: str
    execution: CoarseSamplingResult
    targets: tuple[CoarseTargetEvidence, ...]
    maximum_consecutive_indeterminate_targets: int = 3
    baseline_requested_time_utc: datetime | None = None

    def __post_init__(self) -> None:
        if (
            not self.investigation_id
            or not self.search_run_id
            or not self.policy_version
            or not self.baseline_observation_id
        ):
            raise ValueError
        if len(self.manifest_digest) != _DIGEST_LENGTH or any(
            character not in "0123456789abcdef" for character in self.manifest_digest
        ):
            raise ValueError
        if (
            self.absence_confirmation_frames <= 0
            or self.absence_cadence_seconds <= 0
            or self.maximum_consecutive_indeterminate_targets <= 0
            or self.absence_confirmation_frames != self.plan.absence_confirmation_frames
            or self.absence_cadence_seconds != self.plan.absence_cadence_seconds
            or self.identity.investigation_id != self.investigation_id
            or self.identity.search_run_id != self.search_run_id
        ):
            raise ValueError
        if self.execution.plan != self.plan:
            raise ValueError
        if self.execution.identity != self.identity:
            raise ValueError
        if len(
            {
                (target.requested_time_utc, target.origin_coarse_target_utc)
                for target in self.targets
            }
        ) != len(self.targets):
            raise ValueError
        if self.baseline_requested_time_utc is not None:
            _require_whole_utc(self.baseline_requested_time_utc)
            if self.baseline_requested_time_utc != self.plan.search_start_utc:
                raise ValueError


@dataclass(frozen=True, slots=True)
class _CoarseCandidateBracket:
    investigation_id: str
    search_run_id: str
    identity: CoarseSamplingIdentity
    plan_id: str
    policy_version: str
    baseline_observation_id: str
    last_present_observation_id: str
    last_present_probe_request_id: str | None
    last_present_canonical_frame_id: str | None
    last_present_requested_time_utc: datetime
    first_absent_requested_time_utc: datetime
    support_target_times: tuple[datetime, ...]
    support_probe_request_ids: tuple[str, ...]
    support_observation_ids: tuple[str, ...]
    support_canonical_frame_ids: tuple[str, ...]
    support_decode_session_id: str
    support_decoded_frame_times: tuple[datetime, ...]
    support_decoded_pts: tuple[int, ...]
    support_decoded_ordinals: tuple[int, ...]
    manifest_digest: str
    last_present_is_baseline: bool = False

    def __post_init__(self) -> None:
        _validate_bracket_identity(self)
        _validate_bracket_order(self)


def _validate_bracket_identity(value: _CoarseCandidateBracket) -> None:
    _require_whole_utc(value.last_present_requested_time_utc)
    _require_whole_utc(value.first_absent_requested_time_utc)
    if value.last_present_requested_time_utc >= value.first_absent_requested_time_utc:
        raise ValueError
    if (
        value.identity.investigation_id != value.investigation_id
        or value.identity.search_run_id != value.search_run_id
    ):
        raise ValueError
    if value.last_present_is_baseline:
        if (
            value.last_present_probe_request_id is not None
            or value.last_present_canonical_frame_id is not None
        ):
            raise ValueError
    elif not value.last_present_probe_request_id or not value.last_present_canonical_frame_id:
        raise ValueError
    lengths = {
        len(value.support_target_times),
        len(value.support_probe_request_ids),
        len(value.support_observation_ids),
        len(value.support_canonical_frame_ids),
        len(value.support_decoded_frame_times),
        len(value.support_decoded_pts),
        len(value.support_decoded_ordinals),
    }
    if len(lengths) != 1 or not lengths.pop():
        raise ValueError
    if any(
        len(set(values)) != len(values)
        for values in (
            value.support_probe_request_ids,
            value.support_observation_ids,
            value.support_canonical_frame_ids,
        )
    ):
        raise ValueError
    if value.support_target_times[0] != value.first_absent_requested_time_utc:
        raise ValueError
    if len(value.manifest_digest) != _DIGEST_LENGTH or any(
        character not in "0123456789abcdef" for character in value.manifest_digest
    ):
        raise ValueError


def _validate_bracket_order(value: _CoarseCandidateBracket) -> None:
    if any(
        frame.tzinfo is None or frame.utcoffset() != timedelta(0)
        for frame in value.support_decoded_frame_times
    ):
        raise ValueError
    if any(left >= right for left, right in pairwise(value.support_target_times)):
        raise ValueError
    if any(left >= right for left, right in pairwise(value.support_decoded_frame_times)):
        raise ValueError
    if any(left >= right for left, right in pairwise(value.support_decoded_pts)):
        raise ValueError
    if any(left >= right for left, right in pairwise(value.support_decoded_ordinals)):
        raise ValueError


@dataclass(frozen=True, slots=True)
class _CoarseInterpretationResult:
    status: CoarseInterpretationStatus
    bracket: CoarseCandidateBracket | None = None
    safe_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is CoarseInterpretationStatus.BRACKET_READY:
            if self.bracket is None or self.safe_reason is not None:
                raise ValueError
        elif self.bracket is not None or not self.safe_reason:
            raise ValueError


def _require_whole_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise ValueError


CoarseInterpretationStatus = _CoarseInterpretationStatus
CoarseTargetEvidence = _CoarseTargetEvidence
CoarseEvidenceSnapshot = _CoarseEvidenceSnapshot


@dataclass(frozen=True, slots=True)
class CoarseCandidateBracket(_CoarseCandidateBracket):
    """Public typed view of the immutable C2 candidate bracket."""

    investigation_id: str
    search_run_id: str
    identity: CoarseSamplingIdentity
    plan_id: str
    policy_version: str
    baseline_observation_id: str
    last_present_observation_id: str
    last_present_probe_request_id: str | None
    last_present_canonical_frame_id: str | None
    last_present_requested_time_utc: datetime
    first_absent_requested_time_utc: datetime
    support_target_times: tuple[datetime, ...]
    support_probe_request_ids: tuple[str, ...]
    support_observation_ids: tuple[str, ...]
    support_canonical_frame_ids: tuple[str, ...]
    support_decode_session_id: str
    support_decoded_frame_times: tuple[datetime, ...]
    support_decoded_pts: tuple[int, ...]
    support_decoded_ordinals: tuple[int, ...]
    manifest_digest: str
    last_present_is_baseline: bool = False


CoarseInterpretationResult = _CoarseInterpretationResult
