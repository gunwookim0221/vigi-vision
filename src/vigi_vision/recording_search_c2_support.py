"""Validation and bracket construction for coarse evidence."""

from __future__ import annotations

import hashlib
import json
from itertools import pairwise
from typing import TYPE_CHECKING

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_c1_models import CoarseSampleStatus
from vigi_vision.recording_search_c1_planner import (
    SupportDirection,
    confirmation_run_id_for,
    support_target_times,
)
from vigi_vision.recording_search_c2_models import (
    CoarseCandidateBracket,
    CoarseEvidenceSnapshot,
    CoarseTargetEvidence,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime


def _validate_execution(
    snapshot: CoarseEvidenceSnapshot,
    by_target: Mapping[tuple[datetime, datetime | None], CoarseTargetEvidence],
) -> None:
    if len(by_target) != len(snapshot.targets):
        raise ValueError
    _validate_coarse_samples(snapshot, by_target)
    _validate_support_samples(snapshot, by_target)


def _validate_coarse_samples(
    snapshot: CoarseEvidenceSnapshot,
    by_target: Mapping[tuple[datetime, datetime | None], CoarseTargetEvidence],
) -> None:
    for sample in snapshot.execution.samples:
        target = by_target.get((sample.requested_time_utc, None))
        if target is None:
            raise ValueError
        if (
            sample.probe_request_id is not None
            and sample.probe_request_id != target.probe_request_id
        ):
            raise ValueError
        match sample.status:
            case CoarseSampleStatus.SUCCESS:
                if (
                    target.status is not CoarseSampleStatus.SUCCESS
                    or target.classification is not sample.classification
                ):
                    raise ValueError
            case (
                CoarseSampleStatus.RECORDING_UNAVAILABLE
                | CoarseSampleStatus.ACQUISITION_FAILED
                | CoarseSampleStatus.TIMEOUT
                | CoarseSampleStatus.CLASSIFICATION_FAILED
                | CoarseSampleStatus.INTERRUPTED
                | CoarseSampleStatus.UNEXPECTED_ERROR
            ):
                if target.status is CoarseSampleStatus.SUCCESS:
                    raise ValueError


def _validate_support_samples(  # noqa: C901 - strict direction-aware evidence checks.
    snapshot: CoarseEvidenceSnapshot,
    by_target: Mapping[tuple[datetime, datetime | None], CoarseTargetEvidence],
) -> None:
    coarse_samples = {sample.requested_time_utc: sample for sample in snapshot.execution.samples}
    for support in snapshot.execution.support_results:
        if (
            support.identity != snapshot.identity
            or support.support_indices != tuple(range(snapshot.absence_confirmation_frames))
            or len(support.samples) != snapshot.absence_confirmation_frames
        ):
            raise ValueError
        first = support.samples[0]
        primary = by_target.get((support.origin_target_utc, None))
        if primary is None or support.confirmation_run_id != confirmation_run_id_for(
            snapshot.plan, support.origin_target_utc, snapshot.identity
        ):
            raise ValueError
        if (
            snapshot.plan.support_direction is SupportDirection.FORWARD
            and first != coarse_samples.get(support.origin_target_utc)
        ):
            raise ValueError
        if (
            snapshot.plan.support_direction is SupportDirection.BACKWARD_FROM_END
            and support.origin_target_utc != snapshot.plan.search_end_utc
        ):
            raise ValueError
        for sample in support.samples:
            target = by_target.get((sample.requested_time_utc, support.origin_target_utc))
            if (
                target is None
                or target.confirmation_run_id != support.confirmation_run_id
                or target.support_identity != snapshot.identity
            ):
                raise ValueError
            if sample.probe_request_id != target.probe_request_id:
                raise ValueError
            if sample.status is CoarseSampleStatus.SUCCESS:
                if (
                    target.status is not CoarseSampleStatus.SUCCESS
                    or target.classification is not sample.classification
                ):
                    raise ValueError
            elif target.status is CoarseSampleStatus.SUCCESS:
                raise ValueError


def _absence_support(
    snapshot: CoarseEvidenceSnapshot,
    first: CoarseTargetEvidence,
    by_target: Mapping[tuple[datetime, datetime | None], CoarseTargetEvidence],
) -> tuple[CoarseTargetEvidence, ...] | None:
    resolved_list: list[CoarseTargetEvidence] = []
    requested_times = support_target_times(snapshot.plan, first.requested_time_utc)
    if len(requested_times) != snapshot.absence_confirmation_frames:
        return None
    for requested in requested_times:
        target = by_target.get((requested, first.requested_time_utc))
        if target is None:
            return None
        resolved_list.append(target)
    resolved = tuple(resolved_list)
    return resolved if _valid_support(resolved) else None


def _valid_support(
    resolved: tuple[CoarseTargetEvidence, ...],
) -> bool:
    if any(target.is_alias for target in resolved):
        return False
    if any(
        target.status is not CoarseSampleStatus.SUCCESS
        or target.classification is not ClassificationOutcome.ABSENT
        for target in resolved
    ):
        return False
    if len({target.observation_id for target in resolved}) != len(resolved):
        return False
    if len({target.canonical_frame_id for target in resolved}) != len(resolved):
        return False
    if len({target.decode_session_id for target in resolved}) != 1:
        return False
    return _strict_decoded_order(resolved)


def _strict_decoded_order(
    resolved: tuple[CoarseTargetEvidence, ...],
) -> bool:
    return all(
        left.decoded_frame_utc is not None
        and right.decoded_frame_utc is not None
        and left.decoded_frame_utc < right.decoded_frame_utc
        and left.decoded_pts is not None
        and right.decoded_pts is not None
        and left.decoded_pts < right.decoded_pts
        and left.decoded_ordinal is not None
        and right.decoded_ordinal is not None
        and left.decoded_ordinal < right.decoded_ordinal
        for left, right in pairwise(resolved)
    )


def _has_later_present(
    ordered: tuple[CoarseTargetEvidence, ...], first_absent: CoarseTargetEvidence
) -> bool:
    return any(
        target.requested_time_utc > first_absent.requested_time_utc
        and not target.is_alias
        and target.status is CoarseSampleStatus.SUCCESS
        and target.classification is ClassificationOutcome.PRESENT
        for target in ordered
    )


def _build_bracket(
    snapshot: CoarseEvidenceSnapshot,
    last_present: CoarseTargetEvidence,
    support: tuple[CoarseTargetEvidence, ...],
) -> CoarseCandidateBracket:
    return CoarseCandidateBracket(
        investigation_id=snapshot.investigation_id,
        search_run_id=snapshot.search_run_id,
        identity=snapshot.identity,
        plan_id=snapshot.plan.plan_id,
        policy_version=snapshot.policy_version,
        baseline_observation_id=snapshot.baseline_observation_id,
        last_present_observation_id=_required_str(last_present.observation_id),
        last_present_probe_request_id=(
            None if last_present.is_baseline else _required_str(last_present.probe_request_id)
        ),
        last_present_canonical_frame_id=(
            None if last_present.is_baseline else _required_str(last_present.canonical_frame_id)
        ),
        last_present_requested_time_utc=last_present.requested_time_utc,
        first_absent_requested_time_utc=support[0].requested_time_utc,
        support_target_times=tuple(target.requested_time_utc for target in support),
        support_probe_request_ids=tuple(
            _required_str(target.probe_request_id) for target in support
        ),
        support_observation_ids=tuple(_required_str(target.observation_id) for target in support),
        support_canonical_frame_ids=tuple(
            _required_str(target.canonical_frame_id) for target in support
        ),
        support_decode_session_id=_required_str(support[0].decode_session_id),
        support_decoded_frame_times=tuple(
            _required_datetime(target.decoded_frame_utc) for target in support
        ),
        support_decoded_pts=tuple(_required_int(target.decoded_pts) for target in support),
        support_decoded_ordinals=tuple(_required_int(target.decoded_ordinal) for target in support),
        manifest_digest=snapshot.manifest_digest,
        last_present_is_baseline=last_present.is_baseline,
        last_present_target_id=(
            None
            if last_present.is_baseline
            else coarse_target_id(
                snapshot.investigation_id,
                snapshot.search_run_id,
                last_present.requested_time_utc,
            )
        ),
        support_group_id=_required_str(support[0].confirmation_run_id),
    )


def _required_str(value: str | None) -> str:
    if value is None:
        raise ValueError
    return value


def _required_datetime(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError
    return value


def _required_int(value: int | None) -> int:
    if value is None:
        raise ValueError
    return value


def _coarse_target_id(
    investigation_id: str, search_run_id: str, requested_time_utc: datetime
) -> str:
    payload = {
        "investigation_id": investigation_id,
        "requested_time_utc": requested_time_utc.isoformat(),
        "search_run_id": search_run_id,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"coarse-target-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


coarse_target_id = _coarse_target_id


validate_execution = _validate_execution
absence_support = _absence_support
has_later_present = _has_later_present
build_bracket = _build_bracket
