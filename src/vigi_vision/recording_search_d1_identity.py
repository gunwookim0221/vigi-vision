# ruff: noqa: D100, D101, D102, D103, D105, C901, PLR0913

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from typing import TYPE_CHECKING, TypeAlias

from vigi_vision.recording_search_c2_support import coarse_target_id

if TYPE_CHECKING:
    from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
    from vigi_vision.recording_search_models import RecordingSearchPolicy

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class D1SourceRevision:
    c2_bracket_id: str
    c2_manifest_digest: str

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value
            for value in (self.c2_bracket_id, self.c2_manifest_digest)
        ):
            raise ValueError

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "c2_bracket_id": self.c2_bracket_id,
            "c2_manifest_digest": self.c2_manifest_digest,
        }


@dataclass(frozen=True, slots=True)
class D1LowerBoundReference:
    kind: str
    target_id: str | None
    requested_time_utc: datetime
    observation_id: str
    probe_request_id: str | None
    canonical_frame_id: str | None

    def __post_init__(self) -> None:
        _require_whole_utc(self.requested_time_utc)
        if (
            type(self.kind) is not str
            or self.kind not in {"PHASE6_BASELINE", "PRESENT_PROBE"}
            or type(self.observation_id) is not str
            or not self.observation_id
        ):
            raise ValueError
        if self.kind == "PHASE6_BASELINE":
            if any(
                value is not None
                for value in (self.target_id, self.probe_request_id, self.canonical_frame_id)
            ):
                raise ValueError
        elif not all(
            type(value) is str and value
            for value in (self.target_id, self.probe_request_id, self.canonical_frame_id)
        ):
            raise ValueError

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "canonical_frame_id": self.canonical_frame_id,
            "kind": self.kind,
            "observation_id": self.observation_id,
            "probe_request_id": self.probe_request_id,
            "requested_time_utc": _format_utc(self.requested_time_utc),
            "target_id": self.target_id,
        }


@dataclass(frozen=True, slots=True)
class D1SupportGroup:
    support_group_id: str
    origin_target_id: str
    support_count: int
    cadence_seconds: int
    requested_support_times: tuple[datetime, ...]
    probe_request_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    canonical_frame_ids: tuple[str, ...]
    decode_session_id: str
    decoded_frame_utc: tuple[datetime, ...]
    decoded_pts: tuple[int, ...]
    decoded_ordinals: tuple[int, ...]
    origin_midpoint_requested_time_utc: datetime | None = None

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not str or not value
                for value in (
                    self.support_group_id,
                    self.origin_target_id,
                    self.decode_session_id,
                )
            )
            or type(self.support_count) is not int
            or self.support_count <= 0
            or type(self.cadence_seconds) is not int
            or self.cadence_seconds <= 0
        ):
            raise ValueError
        values = (
            self.requested_support_times,
            self.probe_request_ids,
            self.observation_ids,
            self.canonical_frame_ids,
            self.decoded_frame_utc,
            self.decoded_pts,
            self.decoded_ordinals,
        )
        if any(len(value) != self.support_count for value in values):
            raise ValueError
        if any(
            not value
            for value in (
                *self.probe_request_ids,
                *self.observation_ids,
                *self.canonical_frame_ids,
            )
        ):
            raise ValueError
        if (
            len(set(self.probe_request_ids)) != self.support_count
            or len(set(self.observation_ids)) != self.support_count
            or len(set(self.canonical_frame_ids)) != self.support_count
        ):
            raise ValueError
        for index, value in enumerate(self.requested_support_times):
            _require_whole_utc(value)
            expected = self.requested_support_times[0] + timedelta(
                seconds=index * self.cadence_seconds
            )
            if value != expected:
                raise ValueError
        for value in self.decoded_frame_utc:
            _require_utc(value)
        if any(
            type(value) is not int or value < 0
            for value in (*self.decoded_pts, *self.decoded_ordinals)
        ):
            raise ValueError
        if not _strictly_increasing_datetime(self.decoded_frame_utc):
            raise ValueError
        if not _strictly_increasing_int(self.decoded_pts) or not _strictly_increasing_int(
            self.decoded_ordinals
        ):
            raise ValueError
        if self.origin_midpoint_requested_time_utc is not None:
            _require_whole_utc(self.origin_midpoint_requested_time_utc)
            if self.origin_midpoint_requested_time_utc != self.requested_support_times[0]:
                raise ValueError

    def to_input_payload(self) -> dict[str, JsonValue]:
        return {
            "canonical_frame_ids": list(self.canonical_frame_ids),
            "cadence_seconds": self.cadence_seconds,
            "c2_support_group_id": self.support_group_id,
            "decode_session_id": self.decode_session_id,
            "decoded_frame_utc": [_format_utc(value) for value in self.decoded_frame_utc],
            "decoded_ordinals": list(self.decoded_ordinals),
            "decoded_pts": list(self.decoded_pts),
            "kind": "C2_ABSENCE_SUPPORT",
            "observation_ids": list(self.observation_ids),
            "origin_requested_time_utc": _format_utc(self.requested_support_times[0]),
            "probe_request_ids": list(self.probe_request_ids),
            "requested_time_utc": [_format_utc(value) for value in self.requested_support_times],
            "support_count": self.support_count,
            "upper_bound_requested_time_utc": _format_utc(self.requested_support_times[0]),
        }


@dataclass(frozen=True, slots=True)
class D1InputBracket:
    investigation_id: str
    search_run_id: str
    phase6_confirmation_id: str
    baseline_identity: str
    plan_id: str
    policy_identity: str
    source_revision: D1SourceRevision
    lower_bound: D1LowerBoundReference
    upper_support: D1SupportGroup

    def __post_init__(self) -> None:
        if not all(
            type(value) is str and value
            for value in (
                self.investigation_id,
                self.search_run_id,
                self.phase6_confirmation_id,
                self.baseline_identity,
                self.plan_id,
                self.policy_identity,
            )
        ):
            raise ValueError
        if self.lower_bound.requested_time_utc >= self.upper_support.requested_support_times[0]:
            raise ValueError

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "baseline_identity": self.baseline_identity,
            "identity_schema": "phase7d-d1-input-bracket-v1",
            "investigation_id": self.investigation_id,
            "lower_bound": self.lower_bound.to_payload(),
            "phase6_confirmation_id": self.phase6_confirmation_id,
            "plan_id": self.plan_id,
            "policy_identity": self.policy_identity,
            "search_run_id": self.search_run_id,
            "source_revision": self.source_revision.to_payload(),
            "upper_absence_support": self.upper_support.to_input_payload(),
        }


def canonical_json(payload: dict[str, JsonValue]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def d1_input_bracket_id(value: D1InputBracket) -> str:
    return identity_digest("d1-input-bracket-v1", value.to_payload())


def source_bracket_identity(bracket: CoarseCandidateBracket) -> str:
    serialized = canonical_json(source_bracket_payload(bracket))
    return f"coarse-bracket-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def source_bracket_payload(bracket: CoarseCandidateBracket) -> dict[str, JsonValue]:
    return {
        "investigation_id": bracket.investigation_id,
        "search_run_id": bracket.search_run_id,
        "identity": {
            "investigation_id": bracket.identity.investigation_id,
            "search_run_id": bracket.identity.search_run_id,
            "phase6_confirmation_id": bracket.identity.phase6_confirmation_id,
            "baseline_identity": bracket.identity.baseline_identity,
        },
        "plan_id": bracket.plan_id,
        "policy_version": bracket.policy_version,
        "baseline_observation_id": bracket.baseline_observation_id,
        "last_present_observation_id": bracket.last_present_observation_id,
        "last_present_probe_request_id": bracket.last_present_probe_request_id,
        "last_present_canonical_frame_id": bracket.last_present_canonical_frame_id,
        "last_present_target_id": bracket.last_present_target_id,
        "last_present_requested_time_utc": bracket.last_present_requested_time_utc.isoformat(),
        "first_absent_requested_time_utc": bracket.first_absent_requested_time_utc.isoformat(),
        "support_target_times": [value.isoformat() for value in bracket.support_target_times],
        "support_probe_request_ids": list(bracket.support_probe_request_ids),
        "support_observation_ids": list(bracket.support_observation_ids),
        "support_canonical_frame_ids": list(bracket.support_canonical_frame_ids),
        "support_decode_session_id": bracket.support_decode_session_id,
        "support_decoded_frame_times": [
            value.isoformat() for value in bracket.support_decoded_frame_times
        ],
        "support_decoded_pts": list(bracket.support_decoded_pts),
        "support_decoded_ordinals": list(bracket.support_decoded_ordinals),
        "manifest_digest": bracket.manifest_digest,
        "last_present_is_baseline": bracket.last_present_is_baseline,
        "support_group_id": bracket.support_group_id,
    }


def policy_identity(policy: RecordingSearchPolicy) -> str:
    return hashlib.sha256(
        canonical_json(policy.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def build_d1_input_bracket(
    bracket: CoarseCandidateBracket,
    *,
    phase6_confirmation_id: str,
    baseline_identity: str,
    policy: RecordingSearchPolicy,
) -> D1InputBracket:
    if bracket.last_present_is_baseline:
        lower = D1LowerBoundReference(
            kind="PHASE6_BASELINE",
            target_id=None,
            requested_time_utc=bracket.last_present_requested_time_utc,
            observation_id=bracket.baseline_observation_id,
            probe_request_id=None,
            canonical_frame_id=None,
        )
    else:
        if bracket.last_present_target_id is None:
            raise ValueError
        lower = D1LowerBoundReference(
            kind="PRESENT_PROBE",
            target_id=bracket.last_present_target_id,
            requested_time_utc=bracket.last_present_requested_time_utc,
            observation_id=bracket.last_present_observation_id,
            probe_request_id=bracket.last_present_probe_request_id,
            canonical_frame_id=bracket.last_present_canonical_frame_id,
        )
    if bracket.support_group_id is None:
        raise ValueError
    if any(value > policy.search_end_utc for value in bracket.support_target_times):
        raise ValueError
    upper = D1SupportGroup(
        support_group_id=bracket.support_group_id,
        origin_target_id=coarse_target_id(
            bracket.investigation_id,
            bracket.search_run_id,
            bracket.first_absent_requested_time_utc,
        ),
        support_count=len(bracket.support_target_times),
        cadence_seconds=policy.absence_cadence_seconds,
        requested_support_times=bracket.support_target_times,
        probe_request_ids=bracket.support_probe_request_ids,
        observation_ids=bracket.support_observation_ids,
        canonical_frame_ids=bracket.support_canonical_frame_ids,
        decode_session_id=bracket.support_decode_session_id,
        decoded_frame_utc=bracket.support_decoded_frame_times,
        decoded_pts=bracket.support_decoded_pts,
        decoded_ordinals=bracket.support_decoded_ordinals,
        origin_midpoint_requested_time_utc=bracket.first_absent_requested_time_utc,
    )
    return D1InputBracket(
        investigation_id=bracket.investigation_id,
        search_run_id=bracket.search_run_id,
        phase6_confirmation_id=phase6_confirmation_id,
        baseline_identity=baseline_identity,
        plan_id=bracket.plan_id,
        policy_identity=policy_identity(policy),
        source_revision=D1SourceRevision(
            c2_bracket_id=_bracket_identity(bracket),
            c2_manifest_digest=bracket.manifest_digest,
        ),
        lower_bound=lower,
        upper_support=upper,
    )


def _bracket_identity(bracket: CoarseCandidateBracket) -> str:
    return source_bracket_identity(bracket)


def support_group_id(
    *,
    investigation_id: str,
    search_run_id: str,
    phase6_confirmation_id: str,
    baseline_identity: str,
    plan_id: str,
    policy_identity: str,
    source_revision: D1SourceRevision,
    d1_input_bracket_id: str,
    iteration: int,
    group: D1SupportGroup,
) -> str:
    if type(iteration) is not int or iteration < 0:
        raise ValueError
    origin = group.origin_midpoint_requested_time_utc or group.requested_support_times[0]
    payload: dict[str, JsonValue] = {
        "baseline_identity": baseline_identity,
        "cadence_seconds": group.cadence_seconds,
        "d1_input_bracket_id": d1_input_bracket_id,
        "identity_schema": "phase7d-d1-support-group-v1",
        "investigation_id": investigation_id,
        "iteration": iteration,
        "origin_midpoint_requested_time_utc": origin.isoformat(),
        "phase6_confirmation_id": phase6_confirmation_id,
        "plan_id": plan_id,
        "policy_identity": policy_identity,
        "requested_support_times": [value.isoformat() for value in group.requested_support_times],
        "search_run_id": search_run_id,
        "source_revision": source_revision.to_payload(),
        "support_count": group.support_count,
    }
    return identity_digest("d1-support-group-v1", payload)


def identity_digest(tag: str, payload: dict[str, JsonValue]) -> str:
    return f"{tag}-{hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()}"


def _format_utc(value: datetime) -> str:
    _require_utc(value)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError


def _require_whole_utc(value: datetime) -> None:
    _require_utc(value)
    if value.microsecond != 0:
        raise ValueError


def _strictly_increasing_datetime(values: tuple[datetime, ...]) -> bool:
    return bool(values) and all(left < right for left, right in pairwise(values))


def _strictly_increasing_int(values: tuple[int, ...]) -> bool:
    return bool(values) and all(left < right for left, right in pairwise(values))
