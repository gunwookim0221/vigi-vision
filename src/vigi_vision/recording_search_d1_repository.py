"""Strict repository adapter for Phase 7D-1 bracket evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from typing_extensions import override

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_a2_models import (
    ProbeRequestStatus,
)
from vigi_vision.recording_search_a2_repository import read_schema2_children
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b2_validation import read_schema3_children
from vigi_vision.recording_search_c1_models import CoarseSampleStatus
from vigi_vision.recording_search_c1_planner import build_coarse_sampling_plan
from vigi_vision.recording_search_d1_identity import source_bracket_identity
from vigi_vision.recording_search_d1_models import (
    NarrowingBoundEvidence,
    NarrowingProbeEvidence,
    NarrowingState,
)
from vigi_vision.recording_search_d1_support import NarrowingEvidenceStore, NarrowingHandle
from vigi_vision.recording_search_models import RecordingSearchManifestCorruptError

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from vigi_vision.recording_search_a2_models import (
        AcquisitionOperationRecord,
        CanonicalProbeFrameRecord,
        ProbeFrameRequestRecord,
        RecordingSearchManifestV2,
    )
    from vigi_vision.recording_search_b2_records import (
        ClassificationOperationRecord,
        ConfirmedReferenceBaselineRecord,
        RecordingProbeObservationRecord,
        TargetAliasRecord,
    )
    from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
    from vigi_vision.recording_search_models import RecordingSearchManifest, RecordingSearchPolicy


class D1Repository(Protocol):
    """Minimal repository surface required for strict reopening."""

    @property
    def root(self) -> Path:
        """Return the repository root used for this search store."""
        ...

    def run_path(self, investigation_id: str, search_run_id: str) -> Path:
        """Return the confined run directory for the supplied identities."""
        ...

    def load(
        self, investigation_id: str, search_run_id: str
    ) -> RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchManifestV3:
        """Load one strictly parsed manifest from the existing repository."""
        ...


@dataclass(frozen=True, slots=True)
class _Snapshot:
    manifest: RecordingSearchManifestV3
    operations: dict[str, AcquisitionOperationRecord]
    frames: dict[str, CanonicalProbeFrameRecord]
    requests: dict[str, ProbeFrameRequestRecord]
    baseline: ConfirmedReferenceBaselineRecord
    classification_operations: dict[str, ClassificationOperationRecord]
    observations: dict[str, RecordingProbeObservationRecord]
    aliases: dict[str, TargetAliasRecord]
    digest: str


@dataclass(frozen=True, slots=True)
class RepositoryNarrowingEvidenceStore(NarrowingEvidenceStore[NarrowingHandle]):
    """Resolve Phase 7D-1 evidence through the existing strict repositories."""

    repository: D1Repository

    @override
    def validate_bracket(
        self,
        handle: NarrowingHandle,
        bracket: CoarseCandidateBracket,
        policy: RecordingSearchPolicy,
    ) -> None:
        snapshot = self._snapshot(handle)
        if snapshot.digest != bracket.manifest_digest:
            raise RecordingSearchManifestCorruptError
        manifest_policy = snapshot.manifest.as_schema2().policy
        plan = build_coarse_sampling_plan(manifest_policy)
        if (
            snapshot.manifest.state != "RUNNING"
            or manifest_policy != policy
            or bracket.plan_id != plan.plan_id
            or bracket.policy_version != policy.policy_version
            or bracket.investigation_id != handle.investigation_id
            or bracket.search_run_id != handle.search_run_id
            or bracket.identity.investigation_id != handle.investigation_id
            or bracket.identity.search_run_id != handle.search_run_id
            or bracket.identity.phase6_confirmation_id != handle.phase6_confirmation_id
            or bracket.identity.baseline_identity != handle.baseline_identity
            or handle.phase6_confirmation_id != snapshot.manifest.investigation_id
            or bracket.baseline_observation_id != snapshot.manifest.baseline_observation_id
        ):
            raise RecordingSearchManifestCorruptError
        if not (
            policy.search_start_utc
            <= bracket.last_present_requested_time_utc
            < bracket.first_absent_requested_time_utc
            <= policy.search_end_utc
        ):
            raise RecordingSearchManifestCorruptError
        if len(bracket.support_target_times) != policy.absence_confirmation_frames:
            raise RecordingSearchManifestCorruptError
        if bracket.support_target_times[0] != bracket.first_absent_requested_time_utc:
            raise RecordingSearchManifestCorruptError
        self._validate_lower(snapshot, bracket)
        self._validate_support(snapshot, bracket)

    @override
    def load_state(
        self,
        handle: NarrowingHandle,
        bracket: CoarseCandidateBracket,
    ) -> NarrowingState:
        """Build an in-memory state from strictly reopened bound evidence."""
        snapshot = self._snapshot(handle)
        lower = self._lower_bound(snapshot, bracket)
        support = tuple(
            self._bound_from_request(snapshot, request_id, ClassificationOutcome.ABSENT)
            for request_id in bracket.support_probe_request_ids
        )
        return NarrowingState(
            investigation_id=handle.investigation_id,
            search_run_id=handle.search_run_id,
            phase6_confirmation_id=handle.phase6_confirmation_id,
            baseline_identity=handle.baseline_identity,
            source_bracket_id=_source_id(bracket),
            policy_version=bracket.policy_version,
            lower_bound_utc=bracket.last_present_requested_time_utc,
            upper_bound_utc=bracket.first_absent_requested_time_utc,
            lower_evidence=lower,
            upper_support_evidence=support,
            target_ids=(),
            evidence=(),
            iteration=0,
            manifest_digest=snapshot.digest,
        )

    @override
    def find_existing(
        self,
        handle: NarrowingHandle,
        requested_time_utc: datetime,
        target_id: str,
    ) -> NarrowingProbeEvidence | None:
        snapshot = self._snapshot(handle)
        matches = tuple(
            request
            for request in snapshot.requests.values()
            if request.requested_time_utc == requested_time_utc
        )
        if not matches:
            return None
        if len(matches) != 1:
            raise RecordingSearchManifestCorruptError
        return self._evidence(snapshot, matches[0], target_id)

    @override
    def resolve_request(
        self,
        handle: NarrowingHandle,
        request: ProbeFrameRequestRecord,
        target_id: str,
    ) -> NarrowingProbeEvidence:
        snapshot = self._snapshot(handle)
        persisted = snapshot.requests.get(request.probe_request_id)
        if persisted != request:
            raise RecordingSearchManifestCorruptError
        return self._evidence(snapshot, request, target_id)

    @override
    def current_manifest_digest(self, handle: NarrowingHandle) -> str:
        """Return the current strict evidence revision digest."""
        return self._snapshot(handle).digest

    def _snapshot(self, handle: NarrowingHandle) -> _Snapshot:
        manifest = self.repository.load(handle.investigation_id, handle.search_run_id)
        if not isinstance(manifest, RecordingSearchManifestV3):
            raise RecordingSearchManifestCorruptError
        run_path = self.repository.run_path(handle.investigation_id, handle.search_run_id)
        operations, frames, requests = read_schema2_children(
            self.repository.root, run_path, manifest.as_schema2()
        )
        baseline, class_ops, observations, aliases = read_schema3_children(
            self.repository.root, run_path, manifest
        )
        payload = {
            "manifest": manifest.model_dump(mode="json"),
            "operations": [operations[key].model_dump(mode="json") for key in sorted(operations)],
            "requests": [requests[key].model_dump(mode="json") for key in sorted(requests)],
            "frames": [frames[key].model_dump(mode="json") for key in sorted(frames)],
            "observations": [
                observations[key].model_dump(mode="json") for key in sorted(observations)
            ],
            "aliases": [aliases[key].model_dump(mode="json") for key in sorted(aliases)],
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return _Snapshot(
            manifest,
            operations,
            frames,
            requests,
            baseline,
            class_ops,
            observations,
            aliases,
            digest,
        )

    def _validate_lower(self, snapshot: _Snapshot, bracket: CoarseCandidateBracket) -> None:
        if bracket.last_present_is_baseline:
            if (
                bracket.last_present_requested_time_utc
                != snapshot.manifest.confirmation.reference_requested_time_utc
            ):
                raise RecordingSearchManifestCorruptError
            return
        request = snapshot.requests.get(_required(bracket.last_present_probe_request_id))
        if request is None or request.canonical_frame_id != bracket.last_present_canonical_frame_id:
            raise RecordingSearchManifestCorruptError
        evidence = self._evidence(snapshot, request, "source-lower")
        if evidence.state is not ClassificationOutcome.PRESENT or evidence.alias_id is not None:
            raise RecordingSearchManifestCorruptError
        if evidence.observation_id != bracket.last_present_observation_id:
            raise RecordingSearchManifestCorruptError

    def _validate_support(self, snapshot: _Snapshot, bracket: CoarseCandidateBracket) -> None:
        for index in range(len(bracket.support_probe_request_ids)):
            request_id = bracket.support_probe_request_ids[index]
            observation_id = bracket.support_observation_ids[index]
            frame_id = bracket.support_canonical_frame_ids[index]
            requested_time_utc = bracket.support_target_times[index]
            decoded_frame_utc = bracket.support_decoded_frame_times[index]
            decoded_pts = bracket.support_decoded_pts[index]
            decoded_ordinal = bracket.support_decoded_ordinals[index]
            request = snapshot.requests.get(request_id)
            if (
                request is None
                or request.requested_time_utc != requested_time_utc
                or request.canonical_frame_id != frame_id
            ):
                raise RecordingSearchManifestCorruptError
            evidence = self._evidence(snapshot, request, "source-support")
            if (
                evidence.state is not ClassificationOutcome.ABSENT
                or evidence.alias_id is not None
                or evidence.observation_id != observation_id
                or evidence.canonical_frame_id != frame_id
                or evidence.decode_session_id != bracket.support_decode_session_id
                or evidence.decoded_frame_utc != decoded_frame_utc
                or evidence.decoded_pts != decoded_pts
                or evidence.decoded_ordinal != decoded_ordinal
            ):
                raise RecordingSearchManifestCorruptError

    def _lower_bound(
        self, snapshot: _Snapshot, bracket: CoarseCandidateBracket
    ) -> NarrowingBoundEvidence:
        if bracket.last_present_is_baseline:
            return NarrowingBoundEvidence(
                target_id="source-baseline",
                requested_time_utc=bracket.last_present_requested_time_utc,
                state=ClassificationOutcome.PRESENT,
                observation_id=bracket.last_present_observation_id,
                probe_request_id=None,
                canonical_frame_id=None,
                operation_id=None,
                decode_session_id=None,
                decoded_frame_utc=None,
                decoded_pts=None,
                decoded_ordinal=None,
                is_baseline=True,
            )
        request = snapshot.requests[_required(bracket.last_present_probe_request_id)]
        return self._bound_from_request(
            snapshot, request.probe_request_id, ClassificationOutcome.PRESENT
        )

    def _bound_from_request(
        self,
        snapshot: _Snapshot,
        request_id: str,
        expected: ClassificationOutcome,
    ) -> NarrowingBoundEvidence:
        request = snapshot.requests.get(request_id)
        if request is None:
            raise RecordingSearchManifestCorruptError
        evidence = self._evidence(snapshot, request, f"source-bound-{request_id}")
        if evidence.state is not expected:
            raise RecordingSearchManifestCorruptError
        return _bound(evidence)

    def _evidence(
        self,
        snapshot: _Snapshot,
        request: ProbeFrameRequestRecord,
        target_id: str,
    ) -> NarrowingProbeEvidence:
        if request.status is not ProbeRequestStatus.SUCCEEDED:
            status = (
                CoarseSampleStatus.RECORDING_UNAVAILABLE
                if request.failure_reason == "recording_unavailable"
                else CoarseSampleStatus.ACQUISITION_FAILED
            )
            return NarrowingProbeEvidence(
                target_id=target_id,
                requested_time_utc=request.requested_time_utc,
                status=status,
                probe_request_id=request.probe_request_id,
            )
        if request.canonical_frame_id is None:
            raise RecordingSearchManifestCorruptError
        frame = snapshot.frames.get(request.canonical_frame_id)
        if frame is None:
            raise RecordingSearchManifestCorruptError
        observation = next(
            (
                value
                for value in snapshot.observations.values()
                if value.primary_probe_request_id == request.probe_request_id
            ),
            None,
        )
        alias = next(
            (
                value
                for value in snapshot.aliases.values()
                if value.probe_request_id == request.probe_request_id
            ),
            None,
        )
        if observation is None and alias is not None:
            observation = snapshot.observations.get(alias.canonical_observation_id)
        if observation is None:
            return NarrowingProbeEvidence(
                target_id=target_id,
                requested_time_utc=request.requested_time_utc,
                status=CoarseSampleStatus.SUCCESS,
                probe_request_id=request.probe_request_id,
                canonical_frame_id=request.canonical_frame_id,
                operation_id=request.operation_id,
            )
        return NarrowingProbeEvidence(
            target_id=target_id,
            requested_time_utc=request.requested_time_utc,
            status=CoarseSampleStatus.SUCCESS,
            state=observation.state,
            probe_request_id=request.probe_request_id,
            observation_id=observation.observation_id,
            alias_id=None if alias is None else alias.alias_id,
            canonical_frame_id=observation.canonical_frame_id,
            operation_id=request.operation_id,
            classification_operation_id=observation.classification_operation_id,
            decode_session_id=frame.decode_session_id,
            decoded_frame_utc=frame.decoded_frame_utc,
            decoded_pts=frame.decoded_pts,
            decoded_ordinal=frame.decoded_ordinal,
        )


def _bound(evidence: NarrowingProbeEvidence) -> NarrowingBoundEvidence:
    if evidence.state is None or evidence.observation_id is None:
        raise RecordingSearchManifestCorruptError
    return NarrowingBoundEvidence(
        target_id=evidence.target_id,
        requested_time_utc=evidence.requested_time_utc,
        state=evidence.state,
        observation_id=evidence.observation_id,
        probe_request_id=evidence.probe_request_id,
        canonical_frame_id=evidence.canonical_frame_id,
        operation_id=evidence.operation_id,
        decode_session_id=evidence.decode_session_id,
        decoded_frame_utc=evidence.decoded_frame_utc,
        decoded_pts=evidence.decoded_pts,
        decoded_ordinal=evidence.decoded_ordinal,
    )


def _source_id(bracket: CoarseCandidateBracket) -> str:
    return source_bracket_identity(bracket)


def _required(value: str | None) -> str:
    if value is None:
        raise RecordingSearchManifestCorruptError
    return value
