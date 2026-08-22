"""Strict Schema 4 terminal reopen validation and safe status projection."""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, TypeVar

from vigi_vision.durable_io import is_safe_contained_path
from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_a2_repository import read_schema2_children
from vigi_vision.recording_search_b2_validation import read_schema3_children
from vigi_vision.recording_search_c1_planner import build_coarse_sampling_plan
from vigi_vision.recording_search_c2_support import coarse_target_id
from vigi_vision.recording_search_d2_enums import D2EvidenceRole
from vigi_vision.recording_search_d2_evidence import (
    D2EvidenceReference,
    D2EvidenceSnapshot,
    D2SourceRevision,
    D2SupportGroup,
)
from vigi_vision.recording_search_d2_identity import evidence_snapshot_digest
from vigi_vision.recording_search_d2_publication_models import (
    PublishedFoundResult,
    PublishedInconclusiveResult,
    PublishedNotFoundResult,
    RecordingSearchManifestV4,
    TerminalEvidenceReference,
)
from vigi_vision.recording_search_d2_status import RecordingSearchStatusV4, terminal_status
from vigi_vision.recording_search_d2_terminal_identity import terminal_result_id
from vigi_vision.recording_search_d2_terminal_models import (
    FoundResult,
    InconclusiveResult,
    NotFoundResult,
    TerminalLimitation,
    TerminalResultKind,
)
from vigi_vision.recording_search_models import (
    RecordingSearchManifestCorruptError,
    RecordingSearchTerminalReopenCategory,
    RecordingSearchTerminalReopenError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vigi_vision.recording_search_a2_models import (
        CanonicalProbeFrameRecord,
        ProbeFrameRequestRecord,
    )
    from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
    from vigi_vision.recording_search_b2_records import (
        ClassificationOperationRecord,
        ConfirmedReferenceBaselineRecord,
        RecordingProbeObservationRecord,
        TargetAliasRecord,
    )

_ValueT = TypeVar("_ValueT")


def reopen_terminal(
    root: object,
    run_path: object,
    manifest: RecordingSearchManifestV4,
) -> RecordingSearchStatusV4:
    """Reopen one terminal manifest under the repository lock boundary."""
    try:
        if not isinstance(root, Path) or not isinstance(run_path, Path):
            _fail(RecordingSearchTerminalReopenCategory.FOREIGN_OWNERSHIP)
        if (
            not is_safe_contained_path(root, run_path, require_target=True)
            or run_path.is_symlink()
            or run_path.parent.parent != root
        ):
            _fail(RecordingSearchTerminalReopenCategory.FOREIGN_OWNERSHIP)
        predecessor = manifest.as_schema3()
        baseline, operations, observations, aliases = read_schema3_children(
            root, run_path, predecessor
        )
        acquisition, frames, requests = read_schema2_children(
            root, run_path, predecessor.as_schema2()
        )
        _validate_children(
            manifest, baseline, operations, observations, aliases, acquisition, frames, requests
        )
        result, references = _reconstruct_result(
            manifest, baseline, operations, observations, aliases, acquisition, frames, requests
        )
        _validate_source_digest(manifest, predecessor)
        _validate_evidence_digest(manifest, references, baseline)
        if terminal_result_id(result) != manifest.terminal_result.result_id:
            _fail(RecordingSearchTerminalReopenCategory.IDENTITY_MISMATCH)
        if terminal_status(manifest).result.result_id != manifest.terminal_result.result_id:
            _fail(RecordingSearchTerminalReopenCategory.VALIDATOR_FAILURE)
        return terminal_status(manifest)
    except RecordingSearchTerminalReopenError:
        raise
    except RecordingSearchManifestCorruptError:
        _fail(RecordingSearchTerminalReopenCategory.MALFORMED_RECORD)
    except (OSError, ValueError, TypeError, KeyError):
        _fail(RecordingSearchTerminalReopenCategory.VALIDATOR_FAILURE)


def _validate_source_digest(
    manifest: RecordingSearchManifestV4, predecessor: RecordingSearchManifestV3
) -> None:
    terminal = manifest.terminal_result
    if (
        terminal.source_manifest_digest
        != sha256(predecessor.canonical_json().encode("utf-8")).hexdigest()
    ):
        _fail(RecordingSearchTerminalReopenCategory.IDENTITY_MISMATCH)


def _validate_children(  # noqa: PLR0913
    manifest: RecordingSearchManifestV4,
    baseline: ConfirmedReferenceBaselineRecord,
    operations: dict[str, ClassificationOperationRecord],
    observations: dict[str, RecordingProbeObservationRecord],
    aliases: dict[str, TargetAliasRecord],
    acquisition: Mapping[str, object],
    frames: dict[str, CanonicalProbeFrameRecord],
    requests: dict[str, ProbeFrameRequestRecord],
) -> None:
    if baseline.observation_id != manifest.baseline_observation_id:
        _fail(RecordingSearchTerminalReopenCategory.EVIDENCE_OWNERSHIP_MISMATCH)
    if set(operations) != set(manifest.classification_operation_ids):
        _fail(RecordingSearchTerminalReopenCategory.MISSING_RECORD)
    if set(observations) != set(manifest.canonical_observation_ids):
        _fail(RecordingSearchTerminalReopenCategory.MISSING_RECORD)
    if set(aliases) != set(manifest.target_alias_ids):
        _fail(RecordingSearchTerminalReopenCategory.MISSING_RECORD)
    if set(frames) != set(manifest.canonical_frame_ids) or set(requests) != set(
        manifest.probe_request_ids
    ):
        _fail(RecordingSearchTerminalReopenCategory.MISSING_RECORD)
    if set(acquisition) != set(manifest.acquisition_operation_ids):
        _fail(RecordingSearchTerminalReopenCategory.MISSING_RECORD)
    if (
        manifest.investigation_id != baseline.investigation_id
        or manifest.search_run_id != baseline.search_run_id
    ):
        _fail(RecordingSearchTerminalReopenCategory.FOREIGN_OWNERSHIP)


def _record_reference(  # noqa: PLR0913
    value: TerminalEvidenceReference,
    baseline: ConfirmedReferenceBaselineRecord,
    classification_operations: dict[str, ClassificationOperationRecord],
    observations: dict[str, RecordingProbeObservationRecord],
    aliases: dict[str, TargetAliasRecord],
    acquisition: Mapping[str, object],
    frames: dict[str, CanonicalProbeFrameRecord],
    requests: dict[str, ProbeFrameRequestRecord],
) -> D2EvidenceReference:
    if value.role is D2EvidenceRole.BASELINE:
        if (
            value.observation_id != baseline.observation_id
            or value.requested_time_utc != baseline.reference_requested_time_utc
        ):
            _fail(RecordingSearchTerminalReopenCategory.EVIDENCE_OWNERSHIP_MISMATCH)
        return D2EvidenceReference(
            role=value.role,
            target_id=None,
            requested_time_utc=value.requested_time_utc,
            acquisition_operation_id=None,
            probe_request_id=None,
            classification_operation_id=None,
            observation_id=value.observation_id,
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
    observation = observations.get(value.observation_id)
    if observation is None or value.canonical_frame_id is None or value.probe_request_id is None:
        _fail(RecordingSearchTerminalReopenCategory.MISSING_RECORD)
    request = requests.get(value.probe_request_id)
    frame = frames.get(value.canonical_frame_id)
    operation = acquisition.get(value.acquisition_operation_id or "")
    classification = observations.get(value.observation_id)
    if (
        request is None
        or frame is None
        or operation is None
        or classification is None
        or value.classification_operation_id not in classification_operations
    ):
        _fail(RecordingSearchTerminalReopenCategory.EVIDENCE_OWNERSHIP_MISMATCH)
    if (
        request.canonical_frame_id != frame.canonical_frame_id
        or request.investigation_id != baseline.investigation_id
        or request.search_run_id != baseline.search_run_id
        or request.requested_time_utc != value.requested_time_utc
        or observation.primary_requested_time_utc != value.requested_time_utc
        or observation.canonical_frame_id != frame.canonical_frame_id
        or observation.primary_probe_request_id != request.probe_request_id
        or frame.operation_id != request.operation_id
        or frame.operation_id != value.acquisition_operation_id
        or observation.classification_operation_id != value.classification_operation_id
        or classification_operations[value.classification_operation_id].probe_request_id
        != request.probe_request_id
        or classification_operations[value.classification_operation_id].canonical_frame_id
        != frame.canonical_frame_id
        or classification_operations[value.classification_operation_id].baseline_observation_id
        != baseline.observation_id
        or observation.state
        not in {
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.INDETERMINATE,
        }
        or frame.decoded_frame_utc != value.decoded_frame_utc
        or frame.decode_session_id != value.decode_session_id
        or frame.decoded_pts != value.decoded_pts
        or frame.decoded_ordinal != value.decoded_ordinal
    ):
        _fail(RecordingSearchTerminalReopenCategory.EVIDENCE_OWNERSHIP_MISMATCH)
    if value.alias_id is not None:
        alias = aliases.get(value.alias_id)
        if (
            alias is None
            or alias.canonical_observation_id != value.observation_id
            or alias.probe_request_id != request.probe_request_id
        ):
            _fail(RecordingSearchTerminalReopenCategory.EVIDENCE_OWNERSHIP_MISMATCH)
    return D2EvidenceReference(
        role=value.role,
        target_id=value.target_id,
        requested_time_utc=value.requested_time_utc,
        acquisition_operation_id=value.acquisition_operation_id,
        probe_request_id=value.probe_request_id,
        classification_operation_id=value.classification_operation_id,
        observation_id=value.observation_id,
        canonical_frame_id=value.canonical_frame_id,
        alias_id=value.alias_id,
        decode_session_id=value.decode_session_id,
        decoded_frame_utc=value.decoded_frame_utc,
        decoded_pts=value.decoded_pts,
        decoded_ordinal=value.decoded_ordinal,
        support_group_id=value.support_group_id,
        support_index=value.support_index,
        is_phase6_baseline=False,
        classification=observation.state,
    )


def _reconstruct_result(  # noqa: PLR0913
    manifest: RecordingSearchManifestV4,
    baseline: ConfirmedReferenceBaselineRecord,
    classification_operations: dict[str, ClassificationOperationRecord],
    observations: dict[str, RecordingProbeObservationRecord],
    aliases: dict[str, TargetAliasRecord],
    acquisition: Mapping[str, object],
    frames: dict[str, CanonicalProbeFrameRecord],
    requests: dict[str, ProbeFrameRequestRecord],
) -> tuple[FoundResult | NotFoundResult | InconclusiveResult, tuple[D2EvidenceReference, ...]]:
    terminal = manifest.terminal_result
    base = _record_reference(
        TerminalEvidenceReference(
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
        ),
        baseline,
        classification_operations,
        observations,
        aliases,
        acquisition,
        frames,
        requests,
    )
    if isinstance(terminal, PublishedNotFoundResult):
        refs = tuple(
            _record_reference(
                item,
                baseline,
                classification_operations,
                observations,
                aliases,
                acquisition,
                frames,
                requests,
            )
            for item in terminal.coarse_grid
        )
        _validate_not_found(manifest, refs)
        result = NotFoundResult(
            result_id=terminal.result_id,
            result_kind=TerminalResultKind.NOT_FOUND,
            investigation_id=terminal.investigation_id,
            search_run_id=terminal.search_run_id,
            phase6_confirmation_id=terminal.phase6_confirmation_id,
            baseline_observation_id=terminal.baseline_observation_id,
            plan_id=terminal.plan_id,
            policy_identity=terminal.policy_identity,
            source_manifest_digest=terminal.source_manifest_digest,
            evidence_snapshot_digest=terminal.evidence_snapshot_digest,
            terminal_reason=terminal.terminal_reason,
            limitations=tuple(TerminalLimitation(item) for item in terminal.limitations),
            search_start_utc=terminal.search_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            search_end_utc=terminal.search_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            coarse_grid=refs,
        )
        return result, (base, *refs)
    if isinstance(terminal, PublishedFoundResult):
        coarse = _reconstruct_coarse_references(
            manifest,
            baseline,
            classification_operations,
            observations,
            aliases,
            acquisition,
            frames,
            requests,
        )
        lower = _record_reference(
            terminal.lower_reference,
            baseline,
            classification_operations,
            observations,
            aliases,
            acquisition,
            frames,
            requests,
        )
        support = tuple(
            _record_reference(
                item,
                baseline,
                classification_operations,
                observations,
                aliases,
                acquisition,
                frames,
                requests,
            )
            for item in terminal.upper_support
        )
        narrowing = tuple(
            _record_reference(
                item,
                baseline,
                classification_operations,
                observations,
                aliases,
                acquisition,
                frames,
                requests,
            )
            for item in terminal.narrowing_evidence
        )
        _validate_found(manifest, lower, support, narrowing)
        result = FoundResult(
            result_id=terminal.result_id,
            result_kind=TerminalResultKind.FOUND,
            investigation_id=terminal.investigation_id,
            search_run_id=terminal.search_run_id,
            phase6_confirmation_id=terminal.phase6_confirmation_id,
            baseline_observation_id=terminal.baseline_observation_id,
            plan_id=terminal.plan_id,
            policy_identity=terminal.policy_identity,
            source_manifest_digest=terminal.source_manifest_digest,
            evidence_snapshot_digest=terminal.evidence_snapshot_digest,
            terminal_reason=terminal.terminal_reason,
            limitations=tuple(TerminalLimitation(item) for item in terminal.limitations),
            source_bracket_id=terminal.source_bracket_id,
            narrowed_bracket_id=terminal.narrowed_bracket_id,
            lower_bound_requested_time_utc=terminal.lower_bound_requested_time_utc.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            upper_bound_requested_time_utc=terminal.upper_bound_requested_time_utc.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            achieved_precision_seconds=terminal.achieved_precision_seconds,
            lower_reference=lower,
            upper_support=support,
            narrowing_evidence=narrowing,
        )
        return result, (base, *coarse, *narrowing, *support)
    refs = tuple(
        _record_reference(
            item,
            baseline,
            classification_operations,
            observations,
            aliases,
            acquisition,
            frames,
            requests,
        )
        for item in terminal.evidence
    )
    _validate_inconclusive(terminal, refs)
    result = InconclusiveResult(
        result_id=terminal.result_id,
        result_kind=TerminalResultKind.INCONCLUSIVE,
        investigation_id=terminal.investigation_id,
        search_run_id=terminal.search_run_id,
        phase6_confirmation_id=terminal.phase6_confirmation_id,
        baseline_observation_id=terminal.baseline_observation_id,
        plan_id=terminal.plan_id,
        policy_identity=terminal.policy_identity,
        source_manifest_digest=terminal.source_manifest_digest,
        evidence_snapshot_digest=terminal.evidence_snapshot_digest,
        terminal_reason=terminal.terminal_reason,
        limitations=tuple(TerminalLimitation(item) for item in terminal.limitations),
        source_stage=terminal.source_stage,
        visual_reason=terminal.visual_reason,
        evidence=refs,
    )
    coarse = _reconstruct_coarse_references(
        manifest,
        baseline,
        classification_operations,
        observations,
        aliases,
        acquisition,
        frames,
        requests,
    )
    return result, (base, *coarse, *refs)


def _reconstruct_coarse_references(  # noqa: PLR0913
    manifest: RecordingSearchManifestV4,
    baseline: ConfirmedReferenceBaselineRecord,
    classification_operations: dict[str, ClassificationOperationRecord],
    observations: dict[str, RecordingProbeObservationRecord],
    aliases: dict[str, TargetAliasRecord],
    acquisition: Mapping[str, object],
    frames: dict[str, CanonicalProbeFrameRecord],
    requests: dict[str, ProbeFrameRequestRecord],
) -> tuple[D2EvidenceReference, ...]:
    plan = build_coarse_sampling_plan(manifest.policy.to_acquisition_policy())
    result: list[D2EvidenceReference] = []
    for requested_time in plan.target_times:
        matching = tuple(
            request
            for request in requests.values()
            if request.requested_time_utc == requested_time
            and request.canonical_frame_id is not None
            and request.alias_of_probe_request_id is None
        )
        if len(matching) != 1:
            _fail(RecordingSearchTerminalReopenCategory.MISSING_RECORD)
        request = matching[0]
        frame = frames.get(request.canonical_frame_id or "")
        if frame is None:
            _fail(RecordingSearchTerminalReopenCategory.MISSING_RECORD)
        matched_observations = tuple(
            observation
            for observation in observations.values()
            if observation.primary_probe_request_id == request.probe_request_id
        )
        if len(matched_observations) != 1:
            _fail(RecordingSearchTerminalReopenCategory.MISSING_RECORD)
        observation = matched_observations[0]
        value = TerminalEvidenceReference(
            role=D2EvidenceRole.COARSE_TARGET,
            target_id=coarse_target_id(
                manifest.investigation_id, manifest.search_run_id, requested_time
            ),
            requested_time_utc=requested_time,
            acquisition_operation_id=frame.operation_id,
            probe_request_id=request.probe_request_id,
            classification_operation_id=observation.classification_operation_id,
            observation_id=observation.observation_id,
            canonical_frame_id=frame.canonical_frame_id,
            alias_id=None,
            decode_session_id=frame.decode_session_id,
            decoded_frame_utc=frame.decoded_frame_utc,
            decoded_pts=frame.decoded_pts,
            decoded_ordinal=frame.decoded_ordinal,
            support_group_id=None,
            support_index=None,
            is_phase6_baseline=False,
        )
        result.append(
            _record_reference(
                value,
                baseline,
                classification_operations,
                observations,
                aliases,
                acquisition,
                frames,
                requests,
            )
        )
    return tuple(result)


def _validate_not_found(
    manifest: RecordingSearchManifestV4, refs: tuple[D2EvidenceReference, ...]
) -> None:
    plan = build_coarse_sampling_plan(manifest.policy.to_acquisition_policy())
    if (
        len(refs) != len(plan.target_times)
        or tuple(item.requested_time_utc for item in refs) != plan.target_times
    ):
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)
    if any(
        item.role is not D2EvidenceRole.COARSE_TARGET
        or item.classification is not ClassificationOutcome.PRESENT
        or item.alias_id is not None
        for item in refs
    ):
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)
    if len({item.canonical_frame_id for item in refs}) != len(refs):
        _fail(RecordingSearchTerminalReopenCategory.SUPPORT_ORDER_VIOLATION)


def _validate_found(
    manifest: RecordingSearchManifestV4,
    lower: D2EvidenceReference,
    support: tuple[D2EvidenceReference, ...],
    narrowing: tuple[D2EvidenceReference, ...],
) -> None:
    policy = manifest.policy.to_acquisition_policy()
    terminal = manifest.terminal_result
    if not isinstance(terminal, PublishedFoundResult):
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)
    if lower.classification is not ClassificationOutcome.PRESENT or lower.alias_id is not None:
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)
    if len(support) != policy.absence_confirmation_frames or any(
        item.classification is not ClassificationOutcome.ABSENT or item.alias_id is not None
        for item in support
    ):
        _fail(RecordingSearchTerminalReopenCategory.SUPPORT_ORDER_VIOLATION)
    decoded = tuple(
        (
            _required(item.decoded_frame_utc),
            _required(item.decoded_pts),
            _required(item.decoded_ordinal),
        )
        for item in support
    )
    if any(
        left.requested_time_utc >= right.requested_time_utc for left, right in pairwise(support)
    ):
        _fail(RecordingSearchTerminalReopenCategory.SUPPORT_ORDER_VIOLATION)
    if any(
        left[0] >= right[0] or left[1] >= right[1] or left[2] >= right[2]
        for left, right in pairwise(decoded)
    ):
        _fail(RecordingSearchTerminalReopenCategory.SUPPORT_ORDER_VIOLATION)
    if (
        len({item.canonical_frame_id for item in support}) != len(support)
        or len({item.decode_session_id for item in support}) != 1
    ):
        _fail(RecordingSearchTerminalReopenCategory.SUPPORT_ORDER_VIOLATION)
    if (
        terminal.upper_bound_requested_time_utc - terminal.lower_bound_requested_time_utc
        > timedelta(seconds=policy.binary_stop_resolution_seconds)
    ):
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)
    if any(item.alias_id is not None for item in narrowing):
        _fail(RecordingSearchTerminalReopenCategory.SUPPORT_ORDER_VIOLATION)


def _validate_inconclusive(
    terminal: PublishedInconclusiveResult, refs: tuple[D2EvidenceReference, ...]
) -> None:
    if not refs or any(item.classification is None for item in refs):
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)
    if terminal.visual_reason.value == "insufficient_visual_evidence" and any(
        item.classification is not ClassificationOutcome.INDETERMINATE for item in refs
    ):
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)
    if terminal.visual_reason.value == "nonmonotonic_visual_evidence" and {
        item.classification for item in refs
    } != {ClassificationOutcome.PRESENT, ClassificationOutcome.ABSENT}:
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)


def _validate_evidence_digest(
    manifest: RecordingSearchManifestV4,
    references: tuple[D2EvidenceReference, ...],
    baseline: ConfirmedReferenceBaselineRecord,
) -> None:
    terminal = manifest.terminal_result
    source_c2 = terminal.source_c2_bracket_id
    source_d1 = terminal.source_d1_bracket_id
    if not source_c2 or not source_d1:
        _fail(RecordingSearchTerminalReopenCategory.IDENTITY_MISMATCH)
    support_refs: tuple[D2EvidenceReference, ...] = tuple(
        item for item in references if item.role is D2EvidenceRole.ABSENCE_SUPPORT
    )
    groups: list[D2SupportGroup] = []
    for group_id in dict.fromkeys(item.support_group_id for item in support_refs):
        members: tuple[D2EvidenceReference, ...] = tuple(
            item for item in support_refs if item.support_group_id == group_id
        )
        first = next(iter(members), None)
        if first is None or group_id is None:
            _fail(RecordingSearchTerminalReopenCategory.SUPPORT_ORDER_VIOLATION)
        groups.append(
            D2SupportGroup(
                support_group_id=group_id,
                origin_target_id=_required(first.target_id),
                support_count=len(members),
                cadence_seconds=(members[1].requested_time_utc - first.requested_time_utc).seconds
                if len(members) > 1
                else 1,
                decode_session_id=_required(first.decode_session_id),
                member_target_ids=tuple(_required(item.target_id) for item in members),
                member_observation_ids=tuple(_required(item.observation_id) for item in members),
                member_canonical_frame_ids=tuple(
                    _required(item.canonical_frame_id) for item in members
                ),
            )
        )
    snapshot = D2EvidenceSnapshot(
        investigation_id=manifest.investigation_id,
        search_run_id=manifest.search_run_id,
        phase6_confirmation_id=terminal.phase6_confirmation_id,
        baseline_observation_id=baseline.observation_id,
        plan_id=terminal.plan_id,
        policy_identity=terminal.policy_identity,
        source_revision=D2SourceRevision(
            manifest_digest=terminal.source_manifest_digest,
            c2_bracket_id=source_c2,
            d1_source_bracket_id=source_d1,
        ),
        references=references,
        support_groups=tuple(groups),
    )
    if evidence_snapshot_digest(snapshot) != terminal.evidence_snapshot_digest:
        _fail(RecordingSearchTerminalReopenCategory.IDENTITY_MISMATCH)


def _fail(category: RecordingSearchTerminalReopenCategory) -> NoReturn:
    raise RecordingSearchTerminalReopenError(category)


def _required(value: _ValueT | None) -> _ValueT:
    if value is None:
        _fail(RecordingSearchTerminalReopenCategory.EVIDENCE_OWNERSHIP_MISMATCH)
    return value
