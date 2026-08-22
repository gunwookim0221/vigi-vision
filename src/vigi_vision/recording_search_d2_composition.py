"""Authoritative assembly of the in-memory D2 snapshot for the service boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_a2_repository import read_schema2_children
from vigi_vision.recording_search_b2_validation import read_schema3_children
from vigi_vision.recording_search_c1_models import CoarseSampleStatus
from vigi_vision.recording_search_c2_service import capture_coarse_evidence_snapshot
from vigi_vision.recording_search_c2_support import coarse_target_id
from vigi_vision.recording_search_d1_identity import policy_identity, source_bracket_identity
from vigi_vision.recording_search_d2_enums import D2EvidenceRole
from vigi_vision.recording_search_d2_evidence import (
    D2EvidenceReference,
    D2EvidenceSnapshot,
    D2SourceRevision,
    D2SupportGroup,
)
from vigi_vision.recording_search_models import RecordingSearchManifestCorruptError

if TYPE_CHECKING:
    from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
    from vigi_vision.recording_search_c1_models import CoarseSamplingResult
    from vigi_vision.recording_search_c1_planner import CoarseSamplingPlan
    from vigi_vision.recording_search_d1_models import NarrowingBoundEvidence, NarrowingResult
    from vigi_vision.recording_search_repository import RecordingSearchRepository


def build_authoritative_d2_snapshot(  # noqa: C901, PLR0912, PLR0915 - bounded D2 assembly
    repository: RecordingSearchRepository,
    manifest: RecordingSearchManifestV3,
    plan: CoarseSamplingPlan,
    execution: CoarseSamplingResult,
    *,
    narrowing: NarrowingResult | None = None,
) -> D2EvidenceSnapshot:
    """Build D2 evidence from strict schema-2/3 records, never caller claims."""
    coarse = capture_coarse_evidence_snapshot(repository, manifest, plan, execution)
    run_path = repository.run_path(manifest.investigation_id, manifest.search_run_id)
    _acquisition, frames, requests = read_schema2_children(
        repository.root, run_path, manifest.as_schema2()
    )
    baseline, operations, observations, aliases = read_schema3_children(
        repository.root, run_path, manifest
    )
    alias_by_request = {item.probe_request_id: item for item in aliases.values()}
    baseline_reference = D2EvidenceReference(
        role=D2EvidenceRole.BASELINE,
        target_id=None,
        requested_time_utc=baseline.reference_requested_time_utc,
        acquisition_operation_id=None,
        probe_request_id=None,
        classification_operation_id=None,
        observation_id=baseline.observation_id,
        canonical_frame_id=None,
        alias_id=None,
        decode_session_id=None,
        decoded_frame_utc=None,
        decoded_pts=None,
        decoded_ordinal=None,
        support_group_id=None,
        support_index=None,
        is_phase6_baseline=True,
        classification=ClassificationOutcome.PRESENT,
    )
    coarse_refs: list[D2EvidenceReference] = []
    support_refs: list[D2EvidenceReference] = []
    support_indices: dict[str, int] = {}
    narrowing_support: dict[str, tuple[int, NarrowingBoundEvidence]] = {}
    narrowing_lower: NarrowingBoundEvidence | None = None
    if narrowing is not None and narrowing.narrowed_bracket is not None:
        narrowed = narrowing.narrowed_bracket
        narrowing_lower = narrowed.lower_evidence
        narrowing_support = {
            item.observation_id: (index, item)
            for index, item in enumerate(narrowed.upper_support_evidence)
        }
    final_support_observation_ids = set(narrowing_support)
    support_probe_request_ids = {
        sample.probe_request_id
        for support in coarse.execution.support_results
        for sample in support.samples
        if sample.probe_request_id is not None
    }
    for target in coarse.targets:
        if target.status is not CoarseSampleStatus.SUCCESS or target.observation_id is None:
            continue
        # The primary ABSENT target is represented again as support index zero
        # by C2.  Keep it only in the support group so the snapshot has one
        # immutable reference per observation/frame.
        if (
            target.origin_coarse_target_utc is None
            and target.probe_request_id in support_probe_request_ids
            and (narrowing is None or target.observation_id in final_support_observation_ids)
        ):
            continue
        obsolete_support_as_coarse = (
            narrowing is not None
            and target.origin_coarse_target_utc is not None
            and target.observation_id not in final_support_observation_ids
        )
        if obsolete_support_as_coarse:
            continue
        request = requests.get(target.probe_request_id or "")
        frame = frames.get(target.canonical_frame_id or "")
        observation = observations.get(target.observation_id)
        if request is None or frame is None or observation is None:
            raise RecordingSearchManifestCorruptError
        group_id = target.confirmation_run_id if target.origin_coarse_target_utc else None
        index = None
        if group_id is not None:
            index = support_indices.get(group_id, 0)
            support_indices[group_id] = index + 1
        binding = narrowing_support.get(target.observation_id)
        if binding is not None and narrowing is not None and narrowing.narrowed_bracket is not None:
            index, bound = binding
            group_id = narrowing.narrowed_bracket.upper_support_group_id
            target_id = bound.target_id
            role = D2EvidenceRole.ABSENCE_SUPPORT
        else:
            target_id = coarse_target_id(
                manifest.investigation_id,
                manifest.search_run_id,
                target.requested_time_utc,
            )
            role = D2EvidenceRole.ABSENCE_SUPPORT if group_id else D2EvidenceRole.COARSE_TARGET
            if (
                narrowing_lower is not None
                and target.observation_id == narrowing_lower.observation_id
            ):
                target_id = narrowing_lower.target_id
        reference = D2EvidenceReference(
            role=role,
            target_id=target_id,
            requested_time_utc=target.requested_time_utc,
            acquisition_operation_id=frame.operation_id,
            probe_request_id=request.probe_request_id,
            classification_operation_id=observation.classification_operation_id,
            observation_id=observation.observation_id,
            canonical_frame_id=frame.canonical_frame_id,
            alias_id=(
                alias_by_request[request.probe_request_id].alias_id
                if request.probe_request_id in alias_by_request
                else None
            ),
            decode_session_id=frame.decode_session_id,
            decoded_frame_utc=frame.decoded_frame_utc,
            decoded_pts=frame.decoded_pts,
            decoded_ordinal=frame.decoded_ordinal,
            support_group_id=group_id,
            support_index=index,
            is_phase6_baseline=False,
            classification=observation.state,
        )
        (support_refs if group_id else coarse_refs).append(reference)

    d1_refs: list[D2EvidenceReference] = []
    source_c2 = "coarse-grid-" + coarse.manifest_digest
    source_d1 = source_c2
    if narrowing is not None and narrowing.narrowed_bracket is not None:
        narrowed = narrowing.narrowed_bracket
        if narrowed.source_bracket is not None:
            source_c2 = source_bracket_identity(narrowed.source_bracket)
        source_d1 = narrowed.source_bracket_id
        existing = {reference.observation_id for reference in (*coarse_refs, *support_refs)}
        for item in (*narrowed.evidence, *narrowed.upper_support_evidence):
            if item.observation_id is None or item.observation_id in existing:
                continue
            probe_request_id = item.probe_request_id
            if not probe_request_id:
                raise RecordingSearchManifestCorruptError
            request = requests.get(probe_request_id)
            frame = frames.get(item.canonical_frame_id or "")
            observation = observations.get(item.observation_id)
            if request is None or frame is None or observation is None:
                raise RecordingSearchManifestCorruptError
            operation = operations.get(observation.classification_operation_id)
            if operation is None:
                raise RecordingSearchManifestCorruptError
            is_support = item in narrowed.upper_support_evidence
            group_id = narrowed.upper_support_group_id if is_support else None
            index = tuple(narrowed.upper_support_evidence).index(item) if is_support else None
            d1_refs.append(
                D2EvidenceReference(
                    role=(
                        D2EvidenceRole.ABSENCE_SUPPORT if is_support else D2EvidenceRole.D1_MIDPOINT
                    ),
                    target_id=item.target_id,
                    requested_time_utc=item.requested_time_utc,
                    acquisition_operation_id=item.operation_id,
                    probe_request_id=item.probe_request_id,
                    classification_operation_id=observation.classification_operation_id,
                    observation_id=item.observation_id,
                    canonical_frame_id=item.canonical_frame_id,
                    alias_id=(
                        alias_by_request[request.probe_request_id].alias_id
                        if request.probe_request_id in alias_by_request
                        else None
                    ),
                    decode_session_id=item.decode_session_id,
                    decoded_frame_utc=item.decoded_frame_utc,
                    decoded_pts=item.decoded_pts,
                    decoded_ordinal=item.decoded_ordinal,
                    support_group_id=group_id,
                    support_index=index,
                    is_phase6_baseline=False,
                    classification=observation.state,
                )
            )
            existing.add(item.observation_id)

    all_support: tuple[D2EvidenceReference, ...] = (
        *support_refs,
        *(item for item in d1_refs if item.role is D2EvidenceRole.ABSENCE_SUPPORT),
    )
    groups: list[D2SupportGroup] = []
    for group_id in dict.fromkeys(item.support_group_id for item in all_support):
        if group_id is None:
            raise RecordingSearchManifestCorruptError
        members: tuple[D2EvidenceReference, ...] = tuple(
            item for item in all_support if item.support_group_id == group_id
        )
        if not members:
            raise RecordingSearchManifestCorruptError
        first = next(iter(members), None)
        if first is None:
            raise RecordingSearchManifestCorruptError
        groups.append(
            D2SupportGroup(
                support_group_id=group_id,
                origin_target_id=first.target_id or "",
                support_count=len(members),
                cadence_seconds=(
                    (members[1].requested_time_utc - members[0].requested_time_utc).seconds
                    if len(members) > 1
                    else plan.absence_cadence_seconds
                ),
                decode_session_id=first.decode_session_id or "",
                member_target_ids=tuple(item.target_id or "" for item in members),
                member_observation_ids=tuple(item.observation_id or "" for item in members),
                member_canonical_frame_ids=tuple(item.canonical_frame_id or "" for item in members),
            )
        )
    return D2EvidenceSnapshot(
        investigation_id=manifest.investigation_id,
        search_run_id=manifest.search_run_id,
        phase6_confirmation_id=manifest.investigation_id,
        baseline_observation_id=baseline.observation_id,
        plan_id=plan.plan_id,
        policy_identity=policy_identity(manifest.as_schema2().policy),
        source_revision=D2SourceRevision(
            manifest_digest=coarse.manifest_digest,
            c2_bracket_id=source_c2,
            d1_source_bracket_id=source_d1,
        ),
        references=(baseline_reference, *coarse_refs, *d1_refs, *support_refs),
        support_groups=tuple(groups),
    )
