"""Strict read-only assembly of authoritative coarse evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, Protocol

from vigi_vision.recording_search_a2_models import ProbeRequestStatus
from vigi_vision.recording_search_a2_repository import read_schema2_children
from vigi_vision.recording_search_b2_validation import read_schema3_children
from vigi_vision.recording_search_c1_models import (
    CoarseSampleStatus,
)
from vigi_vision.recording_search_c1_planner import (
    CoarseSamplingIdentity,
    baseline_identity_for,
)
from vigi_vision.recording_search_c2_models import (
    CoarseEvidenceSnapshot,
    CoarseTargetEvidence,
)
from vigi_vision.recording_search_d2_identity import authoritative_source_digest
from vigi_vision.recording_search_models import RecordingSearchManifestCorruptError

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from vigi_vision.recording_search_a2_models import (
        CanonicalProbeFrameRecord,
        ProbeFrameRequestRecord,
    )
    from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
    from vigi_vision.recording_search_b2_records import (
        RecordingProbeObservationRecord,
        TargetAliasRecord,
    )
    from vigi_vision.recording_search_c1_models import (
        CoarseSampleResult,
        CoarseSamplingResult,
    )
    from vigi_vision.recording_search_c1_planner import CoarseSamplingPlan


class _CoarseEvidenceRepository(Protocol):
    @property
    def root(self) -> Path: ...

    def run_path(self, investigation_id: str, search_run_id: str) -> Path: ...


@dataclass(frozen=True, slots=True)
class _EvidenceCollections:
    frames: dict[str, CanonicalProbeFrameRecord]
    observations: dict[str, RecordingProbeObservationRecord]
    aliases: dict[str, TargetAliasRecord]
    all_observations: dict[str, RecordingProbeObservationRecord]


def _capture_coarse_evidence_snapshot(
    repository: _CoarseEvidenceRepository,
    manifest: RecordingSearchManifestV3,
    plan: CoarseSamplingPlan,
    execution: CoarseSamplingResult,
) -> CoarseEvidenceSnapshot:
    if execution.plan != plan:
        _raise_corrupt()
    baseline = manifest.as_schema2().confirmation
    identity = CoarseSamplingIdentity(
        manifest.investigation_id,
        manifest.search_run_id,
        manifest.investigation_id,
        baseline_identity_for(baseline),
    )
    if execution.identity != identity:
        _raise_corrupt()
    run_path = repository.run_path(manifest.investigation_id, manifest.search_run_id)
    _operations, frames, requests = read_schema2_children(
        repository.root, run_path, manifest.as_schema2()
    )
    baseline, _classification_operations, observations, aliases = read_schema3_children(
        repository.root, run_path, manifest
    )
    observation_by_request = {
        observation.primary_probe_request_id: observation for observation in observations.values()
    }
    alias_by_request = {alias.probe_request_id: alias for alias in aliases.values()}
    evidence_specs: list[
        tuple[ProbeFrameRequestRecord, CoarseSampleResult, datetime | None, str | None]
    ] = []
    for sample in execution.samples:
        request = _request_for_sample(requests, sample)
        evidence_specs.append((request, sample, None, None))
    for support in execution.support_results:
        for sample in support.samples:
            request = _request_for_sample(requests, sample)
            evidence_specs.append(
                (request, sample, support.origin_target_utc, support.confirmation_run_id)
            )
    targets = tuple(
        _target_evidence(
            request,
            sample,
            _EvidenceCollections(frames, observation_by_request, alias_by_request, observations),
            origin_target_utc=origin,
            confirmation_run_id=confirmation_id,
            support_identity=identity if origin is not None else None,
        )
        for request, sample, origin, confirmation_id in evidence_specs
    )
    ordered_targets = tuple(
        sorted(
            targets,
            key=lambda target: (
                target.requested_time_utc,
                target.origin_coarse_target_utc or target.requested_time_utc,
            ),
        )
    )
    digest = authoritative_source_digest(repository.root, run_path, manifest)
    return CoarseEvidenceSnapshot(
        investigation_id=manifest.investigation_id,
        search_run_id=manifest.search_run_id,
        identity=identity,
        plan=plan,
        policy_version=manifest.policy.policy_version,
        absence_confirmation_frames=manifest.policy.absence_confirmation_frames,
        absence_cadence_seconds=manifest.policy.absence_cadence_seconds,
        baseline_observation_id=baseline.observation_id,
        manifest_digest=digest,
        execution=execution,
        targets=ordered_targets,
        maximum_consecutive_indeterminate_targets=manifest.policy.maximum_consecutive_indeterminate_targets,
        baseline_requested_time_utc=manifest.confirmation.reference_requested_time_utc,
    )


def _request_for_sample(
    requests: dict[str, ProbeFrameRequestRecord], sample: CoarseSampleResult
) -> ProbeFrameRequestRecord:
    if sample.probe_request_id is None:
        _raise_corrupt()
    request = requests.get(sample.probe_request_id)
    if request is None or request.requested_time_utc != sample.requested_time_utc:
        _raise_corrupt()
    return request


def _target_evidence(  # noqa: PLR0913 - explicit persisted provenance fields.
    request: ProbeFrameRequestRecord,
    sample: CoarseSampleResult | None,
    collections: _EvidenceCollections,
    *,
    origin_target_utc: datetime | None,
    confirmation_run_id: str | None,
    support_identity: CoarseSamplingIdentity | None,
) -> CoarseTargetEvidence:
    if request.status is ProbeRequestStatus.FAILED:
        if sample is not None and sample.status is CoarseSampleStatus.SUCCESS:
            _raise_corrupt()
        status = sample.status if sample is not None else _failure_status(request.failure_reason)
        return CoarseTargetEvidence(
            requested_time_utc=request.requested_time_utc,
            status=status,
            probe_request_id=request.probe_request_id,
            origin_coarse_target_utc=origin_target_utc,
            confirmation_run_id=confirmation_run_id,
            support_identity=support_identity,
        )
    if request.status is not ProbeRequestStatus.SUCCEEDED or request.canonical_frame_id is None:
        if sample is not None and sample.status is CoarseSampleStatus.SUCCESS:
            _raise_corrupt()
        return CoarseTargetEvidence(
            requested_time_utc=request.requested_time_utc,
            status=CoarseSampleStatus.ACQUISITION_FAILED,
            probe_request_id=request.probe_request_id,
            origin_coarse_target_utc=origin_target_utc,
            confirmation_run_id=confirmation_run_id,
            support_identity=support_identity,
        )
    return _successful_target_evidence(
        request,
        sample,
        collections,
        origin_target_utc=origin_target_utc,
        confirmation_run_id=confirmation_run_id,
        support_identity=support_identity,
    )


def _successful_target_evidence(  # noqa: PLR0913 - explicit persisted provenance fields.
    request: ProbeFrameRequestRecord,
    sample: CoarseSampleResult | None,
    collections: _EvidenceCollections,
    *,
    origin_target_utc: datetime | None,
    confirmation_run_id: str | None,
    support_identity: CoarseSamplingIdentity | None,
) -> CoarseTargetEvidence:
    if request.canonical_frame_id is None:
        _raise_corrupt()
    probe_request_id = request.probe_request_id
    frame = collections.frames.get(request.canonical_frame_id)
    if frame is None or frame.canonical_frame_id != request.canonical_frame_id:
        _raise_corrupt()
    observation = collections.observations.get(probe_request_id)
    alias = collections.aliases.get(probe_request_id)
    if observation is None and alias is not None:
        observation = collections.all_observations.get(alias.canonical_observation_id)
    if observation is not None:
        if observation.canonical_frame_id != request.canonical_frame_id:
            _raise_corrupt()
        if sample is not None and sample.status is not CoarseSampleStatus.SUCCESS:
            _raise_corrupt()
        if sample is not None and sample.classification is not observation.state:
            _raise_corrupt()
        return CoarseTargetEvidence(
            requested_time_utc=request.requested_time_utc,
            status=CoarseSampleStatus.SUCCESS,
            classification=observation.state,
            probe_request_id=probe_request_id,
            observation_id=observation.observation_id,
            canonical_frame_id=observation.canonical_frame_id,
            decode_session_id=frame.decode_session_id,
            decoded_frame_utc=frame.decoded_frame_utc,
            decoded_pts=frame.decoded_pts,
            decoded_ordinal=frame.decoded_ordinal,
            is_alias=alias is not None,
            origin_coarse_target_utc=origin_target_utc,
            confirmation_run_id=confirmation_run_id,
            support_identity=support_identity,
        )
    status = sample.status if sample is not None else CoarseSampleStatus.CLASSIFICATION_FAILED
    if status is CoarseSampleStatus.SUCCESS:
        _raise_corrupt()
    return CoarseTargetEvidence(
        requested_time_utc=request.requested_time_utc,
        status=status,
        probe_request_id=probe_request_id,
        origin_coarse_target_utc=origin_target_utc,
        confirmation_run_id=confirmation_run_id,
        support_identity=support_identity,
    )


def _failure_status(reason: str | None) -> CoarseSampleStatus:
    if reason == "recording_unavailable":
        return CoarseSampleStatus.RECORDING_UNAVAILABLE
    return CoarseSampleStatus.ACQUISITION_FAILED


def _raise_corrupt() -> NoReturn:
    raise RecordingSearchManifestCorruptError


CoarseEvidenceRepository = _CoarseEvidenceRepository
capture_coarse_evidence_snapshot = _capture_coarse_evidence_snapshot
