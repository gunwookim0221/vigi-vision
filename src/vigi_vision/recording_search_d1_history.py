# ruff: noqa: D100, D101, D102, D103, D105, C901, PLR0912, PLR0913, PLR2004

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_d1_identity import (
    D1InputBracket,
    D1LowerBoundReference,
    D1SupportGroup,
    JsonValue,
    d1_input_bracket_id,
    identity_digest,
    source_bracket_identity,
    source_bracket_payload,
    support_group_id,
)

if TYPE_CHECKING:
    from vigi_vision.recording_search_c2_models import CoarseCandidateBracket


class HistoryEntryKind(str, Enum):
    PRESENT_TRANSITION = "PRESENT_TRANSITION"
    ABSENT_TRANSITION = "ABSENT_TRANSITION"
    VISUAL_STOP = "VISUAL_STOP"
    OPERATIONAL_STOP = "OPERATIONAL_STOP"


@dataclass(frozen=True, slots=True)
class D1BracketState:
    lower_requested_time_utc: datetime
    upper_requested_time_utc: datetime
    lower_reference: D1LowerBoundReference
    upper_support_group_id: str

    def __post_init__(self) -> None:
        _whole(self.lower_requested_time_utc)
        _whole(self.upper_requested_time_utc)
        if (
            self.lower_requested_time_utc >= self.upper_requested_time_utc
            or type(self.upper_support_group_id) is not str
            or not self.upper_support_group_id
            or self.lower_reference.requested_time_utc != self.lower_requested_time_utc
        ):
            raise ValueError

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "lower_reference": self.lower_reference.to_payload(),
            "lower_requested_time_utc": _format(self.lower_requested_time_utc),
            "upper_requested_time_utc": _format(self.upper_requested_time_utc),
            "upper_support_group_id": self.upper_support_group_id,
        }


@dataclass(frozen=True, slots=True)
class HistoryEvidence:
    role: str
    target_id: str
    probe_request_id: str
    observation_id: str
    canonical_frame_id: str
    acquisition_operation_id: str
    classification_operation_id: str
    decode_session_id: str
    decoded_frame_utc: datetime
    decoded_pts: int
    decoded_ordinal: int
    classification: ClassificationOutcome
    requested_time_utc: datetime

    def __post_init__(self) -> None:
        _whole(self.requested_time_utc)
        if self.role not in {"MIDPOINT", "ABSENCE_SUPPORT"} or not all(
            type(value) is str and value
            for value in (
                self.target_id,
                self.probe_request_id,
                self.observation_id,
                self.canonical_frame_id,
                self.acquisition_operation_id,
                self.classification_operation_id,
                self.decode_session_id,
            )
        ):
            raise ValueError
        if self.decoded_frame_utc.tzinfo is None or self.decoded_frame_utc.utcoffset() != timedelta(
            0
        ):
            raise ValueError
        if (
            type(self.decoded_pts) is not int
            or type(self.decoded_ordinal) is not int
            or self.decoded_pts < 0
            or self.decoded_ordinal < 0
        ):
            raise ValueError

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "acquisition_operation_id": self.acquisition_operation_id,
            "canonical_frame_id": self.canonical_frame_id,
            "classification": self.classification.value,
            "classification_operation_id": self.classification_operation_id,
            "decode_session_id": self.decode_session_id,
            "decoded_frame_utc": _format_fractional(self.decoded_frame_utc),
            "decoded_ordinal": self.decoded_ordinal,
            "decoded_pts": self.decoded_pts,
            "observation_id": self.observation_id,
            "probe_request_id": self.probe_request_id,
            "role": self.role,
            "target_id": self.target_id,
        }


@dataclass(frozen=True, slots=True)
class NarrowingHistoryEntry:
    iteration: int
    entry_kind: HistoryEntryKind
    target_id: str
    midpoint_requested_time_utc: datetime
    bracket_before: D1BracketState
    evidence: tuple[HistoryEvidence, ...]
    classification: ClassificationOutcome | None
    support_group_id: str | None
    support_indexes: tuple[int, ...]
    bracket_after: D1BracketState
    visual_stop_reason: str | None
    operational_stop_reason: str | None

    def __post_init__(self) -> None:
        _whole(self.midpoint_requested_time_utc)
        if (
            type(self.iteration) is not int
            or self.iteration < 0
            or type(self.target_id) is not str
            or not self.target_id
        ):
            raise ValueError
        if self.entry_kind is not HistoryEntryKind.OPERATIONAL_STOP and (
            self.bracket_before.lower_requested_time_utc >= self.midpoint_requested_time_utc
            or self.midpoint_requested_time_utc >= self.bracket_before.upper_requested_time_utc
        ):
            raise ValueError
        if self.entry_kind is HistoryEntryKind.OPERATIONAL_STOP and not (
            self.bracket_before.lower_requested_time_utc
            <= self.midpoint_requested_time_utc
            <= self.bracket_before.upper_requested_time_utc
        ):
            raise ValueError
        if any(type(index) is not int or index < 0 for index in self.support_indexes):
            raise ValueError
        if len(set(self.support_indexes)) != len(self.support_indexes):
            raise ValueError
        if self.visual_stop_reason is not None and self.operational_stop_reason is not None:
            raise ValueError
        midpoint = tuple(value for value in self.evidence if value.role == "MIDPOINT")
        support = tuple(value for value in self.evidence if value.role == "ABSENCE_SUPPORT")
        if midpoint and midpoint[0].target_id != self.target_id:
            raise ValueError
        if self.entry_kind is HistoryEntryKind.PRESENT_TRANSITION:
            if (
                self.classification is not ClassificationOutcome.PRESENT
                or len(midpoint) != 1
                or midpoint[0].classification is not ClassificationOutcome.PRESENT
                or self.support_group_id is not None
                or self.support_indexes
                or support
                or self.visual_stop_reason is not None
                or self.operational_stop_reason is not None
                or self.bracket_after.upper_requested_time_utc
                != self.bracket_before.upper_requested_time_utc
                or self.bracket_after.lower_requested_time_utc != self.midpoint_requested_time_utc
            ):
                raise ValueError
        elif self.entry_kind is HistoryEntryKind.ABSENT_TRANSITION:
            if (
                self.classification is not ClassificationOutcome.ABSENT
                or not self.support_group_id
                or self.visual_stop_reason is not None
                or self.operational_stop_reason is not None
                or len(midpoint) != 1
                or midpoint[0].classification is not ClassificationOutcome.ABSENT
                or len(support) == 0
                or self.support_indexes != tuple(range(len(support)))
                or self.bracket_after.lower_requested_time_utc
                != self.bracket_before.lower_requested_time_utc
                or self.bracket_after.upper_requested_time_utc != self.midpoint_requested_time_utc
            ):
                raise ValueError
        elif self.entry_kind is HistoryEntryKind.VISUAL_STOP:
            if (
                not self.visual_stop_reason
                or self.operational_stop_reason is not None
                or self.classification is not ClassificationOutcome.INDETERMINATE
                or not self.evidence
                or self.bracket_after != self.bracket_before
            ):
                raise ValueError
        elif self.entry_kind is HistoryEntryKind.OPERATIONAL_STOP:
            if (
                not self.operational_stop_reason
                or self.visual_stop_reason is not None
                or self.classification is not None
                or self.evidence
                or self.support_group_id is not None
                or self.support_indexes
                or self.bracket_after != self.bracket_before
            ):
                raise ValueError
        else:
            raise ValueError
        if any(value.requested_time_utc != self.midpoint_requested_time_utc for value in midpoint):
            raise ValueError
        if any(
            value.role != "ABSENCE_SUPPORT"
            or value.classification is not ClassificationOutcome.ABSENT
            for value in support
        ):
            raise ValueError

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "bracket_after": self.bracket_after.to_payload(),
            "bracket_before": self.bracket_before.to_payload(),
            "classification": None if self.classification is None else self.classification.value,
            "entry_kind": self.entry_kind.value,
            "evidence": [value.to_payload() for value in self.evidence],
            "iteration": self.iteration,
            "midpoint_requested_time_utc": _format(self.midpoint_requested_time_utc),
            "operational_stop_reason": self.operational_stop_reason,
            "support_group_id": self.support_group_id,
            "support_indexes": list(self.support_indexes),
            "target_id": self.target_id,
            "visual_stop_reason": self.visual_stop_reason,
        }


@dataclass(frozen=True, slots=True)
class D1Reconstruction:
    input_bracket: D1InputBracket
    entries: tuple[NarrowingHistoryEntry, ...]
    history_digest: str
    final_bracket: D1BracketState
    narrowed_bracket_id: str | None


def history_digest(
    value: D1InputBracket,
    input_identity: str,
    entries: tuple[NarrowingHistoryEntry, ...],
) -> str:
    if input_identity != d1_input_bracket_id(value):
        raise ValueError
    payload: dict[str, JsonValue] = {
        "baseline_identity": value.baseline_identity,
        "d1_input_bracket_id": input_identity,
        "entries": [entry.to_payload() for entry in entries],
        "identity_schema": "phase7d-d1-history-v1",
        "investigation_id": value.investigation_id,
        "phase6_confirmation_id": value.phase6_confirmation_id,
        "plan_id": value.plan_id,
        "policy_identity": value.policy_identity,
        "search_run_id": value.search_run_id,
        "source_revision": value.source_revision.to_payload(),
    }
    return identity_digest("d1-history-v1", payload)


def narrowed_bracket_id(
    value: D1InputBracket,
    entries: tuple[NarrowingHistoryEntry, ...],
    final_bracket: D1BracketState,
    digest: str,
    iteration_count: int,
    achieved_precision_seconds: int,
    stop_reason: str,
    manifest_digest: str,
    *,
    source_bracket: CoarseCandidateBracket | None = None,
) -> str:
    if digest != history_digest(value, d1_input_bracket_id(value), entries):
        raise ValueError
    if (
        source_bracket is not None
        and source_bracket_identity(source_bracket) != value.source_revision.c2_bracket_id
    ):
        raise ValueError
    source_payload: JsonValue
    if source_bracket is None:
        source_payload = value.to_payload()
    else:
        source_payload = _source_bracket_payload(source_bracket)
    payload: dict[str, JsonValue] = {
        "achieved_precision_seconds": achieved_precision_seconds,
        "d1_input_bracket_id": d1_input_bracket_id(value),
        "entries": [entry.to_payload() for entry in entries],
        "final_bracket": final_bracket.to_payload(),
        "history_digest": digest,
        "identity_schema": "narrowed-bracket-v1",
        "iteration_count": iteration_count,
        "manifest_digest": manifest_digest,
        "plan_id": value.plan_id,
        "policy_identity": value.policy_identity,
        "source_c2_bracket": source_payload,
        "stop_reason": stop_reason,
    }
    return identity_digest("narrowed-bracket-v1", payload)


def reconstruct_history(
    value: D1InputBracket,
    entries: tuple[NarrowingHistoryEntry, ...],
    expected_digest: str | None,
    expected_narrowed_id: str | None,
    *,
    final_bracket: D1BracketState | None = None,
    iteration_count: int | None = None,
    achieved_precision_seconds: int | None = None,
    stop_reason: str | None = None,
    manifest_digest: str | None = None,
    source_bracket: CoarseCandidateBracket | None = None,
) -> D1Reconstruction:
    input_identity = d1_input_bracket_id(value)
    if (
        source_bracket is not None
        and source_bracket_identity(source_bracket) != value.source_revision.c2_bracket_id
    ):
        raise ValueError
    if type(entries) is not tuple:
        raise ValueError
    initial = D1BracketState(
        value.lower_bound.requested_time_utc,
        value.upper_support.requested_support_times[0],
        value.lower_bound,
        value.upper_support.support_group_id,
    )
    previous = initial
    stopped = False
    target_ids: set[str] = set()
    for expected_iteration, entry in enumerate(entries):
        if (
            entry.iteration != expected_iteration
            or stopped
            or entry.bracket_before != previous
            or entry.target_id in target_ids
        ):
            raise ValueError
        target_ids.add(entry.target_id)
        if entry.entry_kind is HistoryEntryKind.ABSENT_TRANSITION:
            _validate_history_support(value, input_identity, entry)
        previous = entry.bracket_after
        stopped = entry.entry_kind in {
            HistoryEntryKind.VISUAL_STOP,
            HistoryEntryKind.OPERATIONAL_STOP,
        }
    computed_digest = history_digest(value, input_identity, entries)
    if expected_digest is not None and expected_digest != computed_digest:
        raise ValueError
    if final_bracket is not None and final_bracket != previous:
        raise ValueError
    final = final_bracket or previous
    narrowed_id: str | None = None
    if expected_narrowed_id is not None:
        if (
            iteration_count is None
            or achieved_precision_seconds is None
            or stop_reason is None
            or manifest_digest is None
        ):
            raise ValueError
        narrowed_id = narrowed_bracket_id(
            value,
            entries,
            final,
            computed_digest,
            iteration_count,
            achieved_precision_seconds,
            stop_reason,
            manifest_digest,
            source_bracket=source_bracket,
        )
        if narrowed_id != expected_narrowed_id:
            raise ValueError
    return D1Reconstruction(value, entries, computed_digest, final, narrowed_id)


def _validate_history_support(
    value: D1InputBracket,
    input_identity: str,
    entry: NarrowingHistoryEntry,
) -> None:
    support = tuple(item for item in entry.evidence if item.role == "ABSENCE_SUPPORT")
    if not support:
        raise ValueError
    group = D1SupportGroup(
        support_group_id=entry.support_group_id or "unresolved",
        origin_target_id=entry.target_id,
        support_count=len(support),
        cadence_seconds=_cadence(support),
        requested_support_times=tuple(item.requested_time_utc for item in support),
        probe_request_ids=tuple(item.probe_request_id for item in support),
        observation_ids=tuple(item.observation_id for item in support),
        canonical_frame_ids=tuple(item.canonical_frame_id for item in support),
        decode_session_id=support[0].decode_session_id,
        decoded_frame_utc=tuple(item.decoded_frame_utc for item in support),
        decoded_pts=tuple(item.decoded_pts for item in support),
        decoded_ordinals=tuple(item.decoded_ordinal for item in support),
        origin_midpoint_requested_time_utc=entry.midpoint_requested_time_utc,
    )
    computed = support_group_id(
        investigation_id=value.investigation_id,
        search_run_id=value.search_run_id,
        phase6_confirmation_id=value.phase6_confirmation_id,
        baseline_identity=value.baseline_identity,
        plan_id=value.plan_id,
        policy_identity=value.policy_identity,
        source_revision=value.source_revision,
        d1_input_bracket_id=input_identity,
        iteration=entry.iteration,
        group=group,
    )
    if computed != entry.support_group_id:
        raise ValueError


def _source_bracket_payload(value: CoarseCandidateBracket) -> dict[str, JsonValue]:
    return source_bracket_payload(value)


def _cadence(values: tuple[HistoryEvidence, ...]) -> int:
    if len(values) < 2:
        return 1
    delta = values[1].requested_time_utc - values[0].requested_time_utc
    seconds = delta.days * 86_400 + delta.seconds
    if delta.microseconds or seconds <= 0:
        raise ValueError
    if any(
        values[index].requested_time_utc
        != values[0].requested_time_utc + timedelta(seconds=index * seconds)
        for index in range(len(values))
    ):
        raise ValueError
    return seconds


def _whole(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise ValueError


def _format(value: datetime) -> str:
    _whole(value)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_fractional(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
