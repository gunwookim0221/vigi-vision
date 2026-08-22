"""Strict persistence helpers for authoritative D1 terminal evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, TypeVar, cast

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_c1_models import CoarseSampleStatus
from vigi_vision.recording_search_c1_planner import CoarseSamplingIdentity
from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
from vigi_vision.recording_search_c2_support import coarse_target_id
from vigi_vision.recording_search_d1_history import (
    D1BracketState,
    HistoryEntryKind,
    HistoryEvidence,
    NarrowingHistoryEntry,
)
from vigi_vision.recording_search_d1_identity import (
    D1InputBracket,
    D1LowerBoundReference,
    D1SourceRevision,
    D1SupportGroup,
    source_bracket_payload,
)
from vigi_vision.recording_search_d1_models import (
    NarrowedBracket,
    NarrowingBoundEvidence,
    NarrowingProbeEvidence,
    NarrowingStopReason,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import JsonValue


_WHOLE_UTC = "%Y-%m-%dT%H:%M:%SZ"
_EnumT = TypeVar("_EnumT")


def persisted_narrowed_bracket(value: NarrowedBracket) -> dict[str, JsonValue]:
    """Return the complete allowlisted D1 reconstruction payload."""
    payload = _persisted_narrowed_bracket(value)
    validate_persisted_narrowed_bracket(payload)
    return payload


def _persisted_narrowed_bracket(value: NarrowedBracket) -> dict[str, JsonValue]:
    """Encode D1 evidence without recursively invoking validation."""
    if value.source_bracket is None or value.d1_input_bracket is None:
        raise ValueError
    payload: dict[str, JsonValue] = {
        "identity_schema": "phase7d-d1-reconstruction-v1",
        "source_bracket": source_bracket_payload(value.source_bracket),
        "d1_input_bracket": value.d1_input_bracket.to_payload(),
        "narrowed_bracket": {
            "investigation_id": value.investigation_id,
            "search_run_id": value.search_run_id,
            "phase6_confirmation_id": value.phase6_confirmation_id,
            "baseline_identity": value.baseline_identity,
            "source_bracket_id": value.source_bracket_id,
            "policy_version": value.policy_version,
            "lower_bound_utc": _format_whole(value.lower_bound_utc),
            "upper_bound_utc": _format_whole(value.upper_bound_utc),
            "lower_evidence": _bound_payload(value.lower_evidence),
            "upper_support_evidence": [
                _bound_payload(item) for item in value.upper_support_evidence
            ],
            "target_ids": list(value.target_ids),
            "evidence": [_probe_payload(item) for item in value.evidence],
            "iterations": value.iterations,
            "achieved_precision_seconds": value.achieved_precision_seconds,
            "stop_reason": value.stop_reason.value,
            "manifest_digest": value.manifest_digest,
            "upper_support_group_id": value.upper_support_group_id,
            "history": [_history_payload(item) for item in value.history],
            "history_digest": value.history_digest,
            "narrowed_bracket_id": value.narrowed_bracket_id,
        },
    }
    return payload


def validate_persisted_narrowed_bracket(payload: object) -> None:
    """Strictly validate the persisted D1 envelope without trusting it."""
    _ = decode_persisted_narrowed_bracket(payload)


def decode_persisted_narrowed_bracket(payload: object) -> NarrowedBracket:
    """Decode one strict D1 envelope into the existing immutable domain model."""
    root = _mapping(payload)
    _keys(root, {"identity_schema", "source_bracket", "d1_input_bracket", "narrowed_bracket"})
    if root["identity_schema"] != "phase7d-d1-reconstruction-v1":
        raise ValueError
    source = _decode_source_bracket(root["source_bracket"])
    input_bracket = _decode_input_bracket(root["d1_input_bracket"])
    value = _mapping(root["narrowed_bracket"])
    _keys(
        value,
        {
            "investigation_id",
            "search_run_id",
            "phase6_confirmation_id",
            "baseline_identity",
            "source_bracket_id",
            "policy_version",
            "lower_bound_utc",
            "upper_bound_utc",
            "lower_evidence",
            "upper_support_evidence",
            "target_ids",
            "evidence",
            "iterations",
            "achieved_precision_seconds",
            "stop_reason",
            "manifest_digest",
            "upper_support_group_id",
            "history",
            "history_digest",
            "narrowed_bracket_id",
        },
    )
    result = NarrowedBracket(
        investigation_id=_str(value["investigation_id"]),
        search_run_id=_str(value["search_run_id"]),
        phase6_confirmation_id=_str(value["phase6_confirmation_id"]),
        baseline_identity=_str(value["baseline_identity"]),
        source_bracket_id=_str(value["source_bracket_id"]),
        policy_version=_str(value["policy_version"]),
        lower_bound_utc=_whole(value["lower_bound_utc"]),
        upper_bound_utc=_whole(value["upper_bound_utc"]),
        lower_evidence=_decode_bound(value["lower_evidence"]),
        upper_support_evidence=tuple(
            _decode_bound(item) for item in _sequence(value["upper_support_evidence"])
        ),
        target_ids=tuple(_str(item) for item in _sequence(value["target_ids"])),
        evidence=tuple(_decode_probe(item) for item in _sequence(value["evidence"])),
        iterations=_int(value["iterations"]),
        achieved_precision_seconds=_int(value["achieved_precision_seconds"]),
        stop_reason=_enum(NarrowingStopReason, value["stop_reason"]),
        manifest_digest=_str(value["manifest_digest"]),
        d1_input_bracket=input_bracket,
        source_bracket=source,
        upper_support_group_id=_optional_str(value["upper_support_group_id"]),
        history=tuple(_decode_history(item) for item in _sequence(value["history"])),
        history_digest=_optional_str(value["history_digest"]),
        narrowed_bracket_id=_optional_str(value["narrowed_bracket_id"]),
    )
    if _persisted_narrowed_bracket(result) != root:
        raise ValueError
    return result


def _decode_source_bracket(payload: object) -> CoarseCandidateBracket:
    value = _mapping(payload)
    _keys(
        value,
        {
            "investigation_id",
            "search_run_id",
            "identity",
            "plan_id",
            "policy_version",
            "baseline_observation_id",
            "last_present_observation_id",
            "last_present_probe_request_id",
            "last_present_canonical_frame_id",
            "last_present_target_id",
            "last_present_requested_time_utc",
            "first_absent_requested_time_utc",
            "support_target_times",
            "support_probe_request_ids",
            "support_observation_ids",
            "support_canonical_frame_ids",
            "support_decode_session_id",
            "support_decoded_frame_times",
            "support_decoded_pts",
            "support_decoded_ordinals",
            "manifest_digest",
            "last_present_is_baseline",
            "support_group_id",
        },
    )
    identity = _mapping(value["identity"])
    _keys(
        identity,
        {"investigation_id", "search_run_id", "phase6_confirmation_id", "baseline_identity"},
    )
    return CoarseCandidateBracket(
        investigation_id=_str(value["investigation_id"]),
        search_run_id=_str(value["search_run_id"]),
        identity=CoarseSamplingIdentity(
            investigation_id=_str(identity["investigation_id"]),
            search_run_id=_str(identity["search_run_id"]),
            phase6_confirmation_id=_str(identity["phase6_confirmation_id"]),
            baseline_identity=_str(identity["baseline_identity"]),
        ),
        plan_id=_str(value["plan_id"]),
        policy_version=_str(value["policy_version"]),
        baseline_observation_id=_str(value["baseline_observation_id"]),
        last_present_observation_id=_str(value["last_present_observation_id"]),
        last_present_probe_request_id=_optional_str(value["last_present_probe_request_id"]),
        last_present_canonical_frame_id=_optional_str(value["last_present_canonical_frame_id"]),
        last_present_requested_time_utc=_whole(value["last_present_requested_time_utc"]),
        first_absent_requested_time_utc=_whole(value["first_absent_requested_time_utc"]),
        support_target_times=tuple(
            _whole(item) for item in _sequence(value["support_target_times"])
        ),
        support_probe_request_ids=tuple(
            _str(item) for item in _sequence(value["support_probe_request_ids"])
        ),
        support_observation_ids=tuple(
            _str(item) for item in _sequence(value["support_observation_ids"])
        ),
        support_canonical_frame_ids=tuple(
            _str(item) for item in _sequence(value["support_canonical_frame_ids"])
        ),
        support_decode_session_id=_str(value["support_decode_session_id"]),
        support_decoded_frame_times=tuple(
            _fractional(item) for item in _sequence(value["support_decoded_frame_times"])
        ),
        support_decoded_pts=tuple(_int(item) for item in _sequence(value["support_decoded_pts"])),
        support_decoded_ordinals=tuple(
            _int(item) for item in _sequence(value["support_decoded_ordinals"])
        ),
        manifest_digest=_str(value["manifest_digest"]),
        last_present_is_baseline=_bool(value["last_present_is_baseline"]),
        last_present_target_id=_optional_str(value["last_present_target_id"]),
        support_group_id=_optional_str(value["support_group_id"]),
    )


def _decode_input_bracket(payload: object) -> D1InputBracket:
    value = _mapping(payload)
    _keys(
        value,
        {
            "identity_schema",
            "investigation_id",
            "search_run_id",
            "phase6_confirmation_id",
            "baseline_identity",
            "plan_id",
            "policy_identity",
            "source_revision",
            "lower_bound",
            "upper_absence_support",
        },
    )
    source = _mapping(value["source_revision"])
    _keys(source, {"c2_bracket_id", "c2_manifest_digest"})
    return D1InputBracket(
        investigation_id=_str(value["investigation_id"]),
        search_run_id=_str(value["search_run_id"]),
        phase6_confirmation_id=_str(value["phase6_confirmation_id"]),
        baseline_identity=_str(value["baseline_identity"]),
        plan_id=_str(value["plan_id"]),
        policy_identity=_str(value["policy_identity"]),
        source_revision=D1SourceRevision(
            c2_bracket_id=_str(source["c2_bracket_id"]),
            c2_manifest_digest=_str(source["c2_manifest_digest"]),
        ),
        lower_bound=_decode_lower_reference(value["lower_bound"]),
        upper_support=_decode_support_group(
            value["upper_absence_support"],
            investigation_id=_str(value["investigation_id"]),
            search_run_id=_str(value["search_run_id"]),
        ),
    )


def _decode_lower_reference(payload: object) -> D1LowerBoundReference:
    value = _mapping(payload)
    _keys(
        value,
        {
            "kind",
            "target_id",
            "requested_time_utc",
            "observation_id",
            "probe_request_id",
            "canonical_frame_id",
        },
    )
    return D1LowerBoundReference(
        kind=_str(value["kind"]),
        target_id=_optional_str(value["target_id"]),
        requested_time_utc=_whole(value["requested_time_utc"]),
        observation_id=_str(value["observation_id"]),
        probe_request_id=_optional_str(value["probe_request_id"]),
        canonical_frame_id=_optional_str(value["canonical_frame_id"]),
    )


def _decode_support_group(
    payload: object, *, investigation_id: str, search_run_id: str
) -> D1SupportGroup:
    value = _mapping(payload)
    _keys(
        value,
        {
            "c2_support_group_id",
            "origin_requested_time_utc",
            "upper_bound_requested_time_utc",
            "support_count",
            "cadence_seconds",
            "requested_time_utc",
            "probe_request_ids",
            "observation_ids",
            "canonical_frame_ids",
            "decode_session_id",
            "decoded_frame_utc",
            "decoded_pts",
            "decoded_ordinals",
            "kind",
        },
    )
    count = _int(value["support_count"])
    return D1SupportGroup(
        support_group_id=_str(value["c2_support_group_id"]),
        origin_target_id=coarse_target_id(
            investigation_id, search_run_id, _whole(value["origin_requested_time_utc"])
        ),
        support_count=count,
        cadence_seconds=_int(value["cadence_seconds"]),
        requested_support_times=tuple(
            _whole(item) for item in _sequence(value["requested_time_utc"])
        ),
        probe_request_ids=tuple(_str(item) for item in _sequence(value["probe_request_ids"])),
        observation_ids=tuple(_str(item) for item in _sequence(value["observation_ids"])),
        canonical_frame_ids=tuple(_str(item) for item in _sequence(value["canonical_frame_ids"])),
        decode_session_id=_str(value["decode_session_id"]),
        decoded_frame_utc=tuple(
            _fractional(item) for item in _sequence(value["decoded_frame_utc"])
        ),
        decoded_pts=tuple(_int(item) for item in _sequence(value["decoded_pts"])),
        decoded_ordinals=tuple(_int(item) for item in _sequence(value["decoded_ordinals"])),
        origin_midpoint_requested_time_utc=_whole(value["origin_requested_time_utc"]),
    )


def _decode_bound(payload: object) -> NarrowingBoundEvidence:
    value = _mapping(payload)
    _keys(
        value,
        {
            "target_id",
            "requested_time_utc",
            "state",
            "observation_id",
            "probe_request_id",
            "canonical_frame_id",
            "operation_id",
            "decode_session_id",
            "decoded_frame_utc",
            "decoded_pts",
            "decoded_ordinal",
            "is_baseline",
        },
    )
    return NarrowingBoundEvidence(
        target_id=_str(value["target_id"]),
        requested_time_utc=_whole(value["requested_time_utc"]),
        state=_enum(ClassificationOutcome, value["state"]),
        observation_id=_str(value["observation_id"]),
        probe_request_id=_optional_str(value["probe_request_id"]),
        canonical_frame_id=_optional_str(value["canonical_frame_id"]),
        operation_id=_optional_str(value["operation_id"]),
        decode_session_id=_optional_str(value["decode_session_id"]),
        decoded_frame_utc=_optional_fractional(value["decoded_frame_utc"]),
        decoded_pts=_optional_int(value["decoded_pts"]),
        decoded_ordinal=_optional_int(value["decoded_ordinal"]),
        is_baseline=_bool(value["is_baseline"]),
    )


def _decode_probe(payload: object) -> NarrowingProbeEvidence:
    value = _mapping(payload)
    _keys(
        value,
        {
            "target_id",
            "requested_time_utc",
            "status",
            "state",
            "probe_request_id",
            "observation_id",
            "alias_id",
            "canonical_frame_id",
            "operation_id",
            "decode_session_id",
            "decoded_frame_utc",
            "decoded_pts",
            "decoded_ordinal",
            "classification_operation_id",
            "operational_reason",
        },
    )
    return NarrowingProbeEvidence(
        target_id=_str(value["target_id"]),
        requested_time_utc=_whole(value["requested_time_utc"]),
        status=_enum(CoarseSampleStatus, value["status"]),
        state=_optional_enum(ClassificationOutcome, value["state"]),
        probe_request_id=_optional_str(value["probe_request_id"]),
        observation_id=_optional_str(value["observation_id"]),
        alias_id=_optional_str(value["alias_id"]),
        canonical_frame_id=_optional_str(value["canonical_frame_id"]),
        operation_id=_optional_str(value["operation_id"]),
        decode_session_id=_optional_str(value["decode_session_id"]),
        decoded_frame_utc=_optional_fractional(value["decoded_frame_utc"]),
        decoded_pts=_optional_int(value["decoded_pts"]),
        decoded_ordinal=_optional_int(value["decoded_ordinal"]),
        classification_operation_id=_optional_str(value["classification_operation_id"]),
        operational_reason=_optional_str(value["operational_reason"]),
    )


def _decode_bracket_state(payload: object) -> D1BracketState:
    value = _mapping(payload)
    _keys(
        value,
        {
            "lower_reference",
            "lower_requested_time_utc",
            "upper_requested_time_utc",
            "upper_support_group_id",
        },
    )
    lower = _decode_lower_reference(value["lower_reference"])
    return D1BracketState(
        lower_requested_time_utc=_whole(value["lower_requested_time_utc"]),
        upper_requested_time_utc=_whole(value["upper_requested_time_utc"]),
        lower_reference=lower,
        upper_support_group_id=_str(value["upper_support_group_id"]),
    )


def _decode_history(payload: object) -> NarrowingHistoryEntry:
    value = _mapping(payload)
    _keys(
        value,
        {
            "bracket_after",
            "bracket_before",
            "classification",
            "entry_kind",
            "evidence",
            "iteration",
            "midpoint_requested_time_utc",
            "operational_stop_reason",
            "support_group_id",
            "support_indexes",
            "target_id",
            "visual_stop_reason",
        },
    )
    return NarrowingHistoryEntry(
        iteration=_int(value["iteration"]),
        entry_kind=_enum(HistoryEntryKind, value["entry_kind"]),
        target_id=_str(value["target_id"]),
        midpoint_requested_time_utc=_whole(value["midpoint_requested_time_utc"]),
        bracket_before=_decode_bracket_state(value["bracket_before"]),
        evidence=tuple(_decode_history_evidence(item) for item in _sequence(value["evidence"])),
        classification=_optional_enum(ClassificationOutcome, value["classification"]),
        support_group_id=_optional_str(value["support_group_id"]),
        support_indexes=tuple(_int(item) for item in _sequence(value["support_indexes"])),
        bracket_after=_decode_bracket_state(value["bracket_after"]),
        visual_stop_reason=_optional_str(value["visual_stop_reason"]),
        operational_stop_reason=_optional_str(value["operational_stop_reason"]),
    )


def _decode_history_evidence(payload: object) -> HistoryEvidence:
    value = _mapping(payload)
    _keys(
        value,
        {
            "acquisition_operation_id",
            "canonical_frame_id",
            "classification",
            "classification_operation_id",
            "decode_session_id",
            "decoded_frame_utc",
            "decoded_ordinal",
            "decoded_pts",
            "observation_id",
            "probe_request_id",
            "requested_time_utc",
            "role",
            "target_id",
        },
    )
    return HistoryEvidence(
        role=_str(value["role"]),
        target_id=_str(value["target_id"]),
        probe_request_id=_str(value["probe_request_id"]),
        observation_id=_str(value["observation_id"]),
        canonical_frame_id=_str(value["canonical_frame_id"]),
        acquisition_operation_id=_str(value["acquisition_operation_id"]),
        classification_operation_id=_str(value["classification_operation_id"]),
        decode_session_id=_str(value["decode_session_id"]),
        decoded_frame_utc=_fractional(value["decoded_frame_utc"]),
        decoded_pts=_int(value["decoded_pts"]),
        decoded_ordinal=_int(value["decoded_ordinal"]),
        classification=_enum(ClassificationOutcome, value["classification"]),
        requested_time_utc=_whole(value["requested_time_utc"]),
    )


def _bound_payload(value: NarrowingBoundEvidence) -> dict[str, JsonValue]:
    return {
        "target_id": value.target_id,
        "requested_time_utc": _format_whole(value.requested_time_utc),
        "state": value.state.value,
        "observation_id": value.observation_id,
        "probe_request_id": value.probe_request_id,
        "canonical_frame_id": value.canonical_frame_id,
        "operation_id": value.operation_id,
        "decode_session_id": value.decode_session_id,
        "decoded_frame_utc": _format_optional_fractional(value.decoded_frame_utc),
        "decoded_pts": value.decoded_pts,
        "decoded_ordinal": value.decoded_ordinal,
        "is_baseline": value.is_baseline,
    }


def _probe_payload(value: NarrowingProbeEvidence) -> dict[str, JsonValue]:
    return {
        "target_id": value.target_id,
        "requested_time_utc": _format_whole(value.requested_time_utc),
        "status": value.status.value,
        "state": None if value.state is None else value.state.value,
        "probe_request_id": value.probe_request_id,
        "observation_id": value.observation_id,
        "alias_id": value.alias_id,
        "canonical_frame_id": value.canonical_frame_id,
        "operation_id": value.operation_id,
        "decode_session_id": value.decode_session_id,
        "decoded_frame_utc": _format_optional_fractional(value.decoded_frame_utc),
        "decoded_pts": value.decoded_pts,
        "decoded_ordinal": value.decoded_ordinal,
        "classification_operation_id": value.classification_operation_id,
        "operational_reason": value.operational_reason,
    }


def _history_payload(value: NarrowingHistoryEntry) -> dict[str, JsonValue]:
    payload = cast("dict[str, JsonValue]", value.to_payload())
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, list) or len(raw_evidence) != len(value.evidence):
        raise ValueError
    payload["evidence"] = [
        {
            **cast("dict[str, JsonValue]", item),
            "requested_time_utc": _format_whole(e.requested_time_utc),
        }
        for item, e in zip(cast("list[JsonValue]", raw_evidence), value.evidence, strict=True)
    ]
    return payload


def _keys(value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError
    raw = cast("dict[object, object]", value)
    if any(not isinstance(key, str) for key in raw):
        raise ValueError
    return {cast("str", key): item for key, item in raw.items()}


def _sequence(value: object) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError
    return tuple(cast("list[object] | tuple[object, ...]", value))


def _str(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _str(value)


def _int(value: object) -> int:
    if type(value) is not int:
        raise ValueError
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _int(value)


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError
    return value


def _enum(enum_type: type[_EnumT], value: object) -> _EnumT:
    if not isinstance(value, str):
        raise TypeError
    try:
        constructor = cast("Callable[[str], _EnumT]", enum_type)
        return constructor(value)
    except (TypeError, ValueError):
        raise ValueError from None


def _optional_enum(enum_type: type[_EnumT], value: object) -> _EnumT | None:
    if value is None:
        return None
    return _enum(enum_type, value)


def _whole(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or parsed.microsecond != 0:
        raise ValueError
    return parsed.astimezone(timezone.utc)


def _fractional(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError
    return parsed.astimezone(timezone.utc)


def _optional_fractional(value: object) -> datetime | None:
    if value is None:
        return None
    return _fractional(value)


def _format_whole(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise ValueError
    return value.astimezone(timezone.utc).strftime(_WHOLE_UTC)


def _format_optional_fractional(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
