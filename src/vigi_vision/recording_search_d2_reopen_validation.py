"""Strict Schema 4 terminal reopen validation and safe status projection."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, TypeVar

from vigi_vision.durable_io import is_safe_contained_path
from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_a2_repository import read_schema2_children
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b2_validation import read_schema3_children
from vigi_vision.recording_search_c1_planner import (
    baseline_identity_for,
    build_coarse_sampling_plan,
)
from vigi_vision.recording_search_c2_support import coarse_target_id
from vigi_vision.recording_search_d1_identity import policy_identity, source_bracket_identity
from vigi_vision.recording_search_d1_models import NarrowingStatus
from vigi_vision.recording_search_d1_repository import RepositoryNarrowingEvidenceStore
from vigi_vision.recording_search_d1_service import execute_binary_narrowing
from vigi_vision.recording_search_d2_enums import D2EvidenceRole
from vigi_vision.recording_search_d2_evidence import (
    D2EvidenceReference,
    D2EvidenceSnapshot,
    D2SourceRevision,
    D2SupportGroup,
)
from vigi_vision.recording_search_d2_identity import (
    authoritative_source_digest,
    evidence_snapshot_digest,
)
from vigi_vision.recording_search_d2_persistence import decode_persisted_narrowed_bracket
from vigi_vision.recording_search_d2_publication_models import (
    PublishedFoundResult,
    PublishedInconclusiveResult,
    PublishedNotFoundResult,
    RecordingSearchManifestV4,
    TerminalEvidenceReference,
)
from vigi_vision.recording_search_d2_results import C2BracketReady, D1BracketReady
from vigi_vision.recording_search_d2_status import RecordingSearchStatusV4, terminal_status
from vigi_vision.recording_search_d2_terminal import TerminalInputSnapshot, interpret_terminal
from vigi_vision.recording_search_d2_terminal_identity import terminal_result_id
from vigi_vision.recording_search_d2_terminal_models import (
    FoundResult,
    InconclusiveResult,
    NotFoundResult,
    TerminalLimitation,
    TerminalResult,
    TerminalResultKind,
)
from vigi_vision.recording_search_models import (
    RecordingSearchManifestCorruptError,
    RecordingSearchTerminalReopenCategory,
    RecordingSearchTerminalReopenError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

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
    from vigi_vision.recording_search_b3_models import ClassifyRecordingProbeRequest
    from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
    from vigi_vision.recording_search_d1_models import NarrowingProbeEvidence, NarrowingState
    from vigi_vision.recording_search_models import RecordingSearchPolicy

_ValueT = TypeVar("_ValueT")


@dataclass(frozen=True, slots=True)
class _ReopenHandle:
    """Read-only identity adapter used by the existing D1 repository validator."""

    investigation_id: str
    search_run_id: str
    phase6_confirmation_id: str
    baseline_identity: str
    closed: bool = False


def reopen_terminal(
    root: object,
    run_path: object,
    manifest: RecordingSearchManifestV4,
) -> RecordingSearchStatusV4:
    """Reopen one terminal manifest and expose its safe status projection."""
    _ = reopen_terminal_result(root, run_path, manifest)
    if terminal_status(manifest).result.result_id != manifest.terminal_result.result_id:
        _fail(RecordingSearchTerminalReopenCategory.VALIDATOR_FAILURE)
    return terminal_status(manifest)


def reopen_terminal_result(
    root: object,
    run_path: object,
    manifest: RecordingSearchManifestV4,
) -> TerminalResult:
    """Reopen one terminal manifest and return its strictly reconstructed result."""
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
        source_digest = authoritative_evidence_digest(root, run_path, predecessor)
        _validate_common_bindings(manifest, predecessor, baseline, source_digest)
        _validate_source_digest(manifest, predecessor, root, run_path)
        _validate_evidence_digest(manifest, references, baseline, source_digest)
        if isinstance(manifest.terminal_result, PublishedFoundResult):
            _validate_found_reconstruction(
                manifest,
                predecessor,
                root,
                run_path,
                baseline,
                references,
                source_digest,
                result,
            )
        if terminal_result_id(result) != manifest.terminal_result.result_id:
            _fail(RecordingSearchTerminalReopenCategory.IDENTITY_MISMATCH)
    except RecordingSearchTerminalReopenError:
        raise
    except RecordingSearchManifestCorruptError:
        _fail(RecordingSearchTerminalReopenCategory.MALFORMED_RECORD)
    except (OSError, ValueError, TypeError, KeyError):
        _fail(RecordingSearchTerminalReopenCategory.VALIDATOR_FAILURE)
    else:
        return result


def validate_authoritative_snapshot(  # noqa: C901 - strict field-by-field admission
    root: Path,
    run_path: Path,
    predecessor: RecordingSearchManifestV3,
    snapshot: D2EvidenceSnapshot,
) -> None:
    """Admit a proposed snapshot only when every reference is repository-owned.

    D2 snapshots are intentionally in-memory.  They are not authority merely
    because their dataclasses validate, so publication performs this strict
    conversion against the current schema-3 children before replacing the
    manifest.  The same child reader used by terminal reopen rejects missing,
    foreign, duplicate, unordered, or unindexed records.
    """
    try:
        if snapshot.investigation_id != predecessor.investigation_id:
            _fail(RecordingSearchTerminalReopenCategory.FOREIGN_OWNERSHIP)
        if snapshot.search_run_id != predecessor.search_run_id:
            _fail(RecordingSearchTerminalReopenCategory.FOREIGN_OWNERSHIP)
        if snapshot.baseline_observation_id != predecessor.baseline_observation_id:
            _fail(RecordingSearchTerminalReopenCategory.EVIDENCE_OWNERSHIP_MISMATCH)
        expected_plan = build_coarse_sampling_plan(predecessor.as_schema2().policy)
        if snapshot.plan_id != expected_plan.plan_id:
            _fail(RecordingSearchTerminalReopenCategory.IDENTITY_MISMATCH)
        if snapshot.policy_identity != policy_identity(predecessor.as_schema2().policy):
            _fail(RecordingSearchTerminalReopenCategory.IDENTITY_MISMATCH)
        if snapshot.phase6_confirmation_id != predecessor.investigation_id:
            _fail(RecordingSearchTerminalReopenCategory.FOREIGN_OWNERSHIP)
        if snapshot.source_revision.manifest_digest != authoritative_evidence_digest(
            root, run_path, predecessor
        ):
            _fail(RecordingSearchTerminalReopenCategory.IDENTITY_MISMATCH)
        baseline, operations, observations, aliases = read_schema3_children(
            root, run_path, predecessor
        )
        acquisition, frames, requests = read_schema2_children(
            root, run_path, predecessor.as_schema2()
        )
        converted: list[D2EvidenceReference] = []
        for reference in snapshot.references:
            persisted = TerminalEvidenceReference(
                role=reference.role,
                target_id=reference.target_id,
                requested_time_utc=reference.requested_time_utc,
                acquisition_operation_id=reference.acquisition_operation_id,
                probe_request_id=reference.probe_request_id,
                classification_operation_id=reference.classification_operation_id,
                observation_id=reference.observation_id or "",
                canonical_frame_id=reference.canonical_frame_id,
                alias_id=reference.alias_id,
                decode_session_id=reference.decode_session_id,
                decoded_frame_utc=reference.decoded_frame_utc,
                decoded_pts=reference.decoded_pts,
                decoded_ordinal=reference.decoded_ordinal,
                support_group_id=reference.support_group_id,
                support_index=reference.support_index,
                is_phase6_baseline=reference.is_phase6_baseline,
            )
            converted.append(
                _record_reference(
                    persisted,
                    baseline,
                    operations,
                    observations,
                    aliases,
                    acquisition,
                    frames,
                    requests,
                )
            )
        if tuple(converted) != snapshot.references:
            _fail(RecordingSearchTerminalReopenCategory.EVIDENCE_OWNERSHIP_MISMATCH)
        # Construction of ``snapshot`` already enforces role ordering,
        # support-group membership, distinct IDs, and decoded-frame ordering.
        # Recompute its digest here so a caller cannot substitute a mutable
        # object after admission.
        _ = evidence_snapshot_digest(snapshot)
    except RecordingSearchTerminalReopenError:
        raise
    except (RecordingSearchManifestCorruptError, OSError, ValueError, TypeError, KeyError):
        _fail(RecordingSearchTerminalReopenCategory.VALIDATOR_FAILURE)


def _validate_common_bindings(
    manifest: RecordingSearchManifestV4,
    predecessor: RecordingSearchManifestV3,
    baseline: ConfirmedReferenceBaselineRecord,
    source_digest: str,
) -> None:
    """Bind persisted terminal common fields to the schema-3 authority."""
    terminal = manifest.terminal_result
    expected_plan = build_coarse_sampling_plan(predecessor.as_schema2().policy)
    expected_policy = policy_identity(predecessor.as_schema2().policy)
    if (
        terminal.investigation_id != predecessor.investigation_id
        or terminal.search_run_id != predecessor.search_run_id
        or terminal.phase6_confirmation_id != predecessor.investigation_id
        or terminal.baseline_observation_id != baseline.observation_id
        or terminal.plan_id != expected_plan.plan_id
        or terminal.policy_identity != expected_policy
        or terminal.source_manifest_digest != source_digest
    ):
        _fail(RecordingSearchTerminalReopenCategory.IDENTITY_MISMATCH)


def _validate_found_reconstruction(  # noqa: PLR0913 - explicit authority inputs
    manifest: RecordingSearchManifestV4,
    predecessor: RecordingSearchManifestV3,
    root: Path,
    run_path: Path,
    baseline: ConfirmedReferenceBaselineRecord,
    references: tuple[D2EvidenceReference, ...],
    source_digest: str,
    persisted_result: TerminalResult,
) -> None:
    """Rebuild FOUND from persisted D1 facts and the live schema-3 tree."""
    envelope = manifest.d1_reconstruction
    if envelope is None:
        _fail(RecordingSearchTerminalReopenCategory.MISSING_RECORD)
    narrowed = decode_persisted_narrowed_bracket(envelope)
    source_bracket = narrowed.source_bracket
    input_bracket = narrowed.d1_input_bracket
    if source_bracket is None or input_bracket is None:
        _fail(RecordingSearchTerminalReopenCategory.MISSING_RECORD)
    if narrowed.manifest_digest != source_digest:
        _fail(RecordingSearchTerminalReopenCategory.IDENTITY_MISMATCH)
    try:
        store = RepositoryNarrowingEvidenceStore(_RepositoryView(root, run_path, predecessor))
        current_source = replace(source_bracket, manifest_digest=source_digest)
        store.validate_bracket(
            _ReopenHandle(
                investigation_id=predecessor.investigation_id,
                search_run_id=predecessor.search_run_id,
                phase6_confirmation_id=predecessor.investigation_id,
                baseline_identity=baseline_identity_for(predecessor.confirmation),
            ),
            current_source,
            predecessor.as_schema2().policy,
        )
    except (RecordingSearchManifestCorruptError, ValueError, OSError):
        _fail(RecordingSearchTerminalReopenCategory.EVIDENCE_OWNERSHIP_MISMATCH)
    handle = _ReopenHandle(
        investigation_id=predecessor.investigation_id,
        search_run_id=predecessor.search_run_id,
        phase6_confirmation_id=predecessor.investigation_id,
        baseline_identity=baseline_identity_for(predecessor.confirmation),
    )
    replayed = execute_binary_narrowing(
        _ReopenReplayHost(_replay_requests(root, run_path, predecessor)),
        handle,
        source_bracket,
        predecessor.as_schema2().policy,
        _ReopenEvidenceStore(store, source_digest),
    )
    replayed_bracket = replayed.narrowed_bracket
    if replayed.status is not NarrowingStatus.READY or replayed_bracket is None:
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)
    if replayed_bracket != narrowed:
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)
    plan = build_coarse_sampling_plan(predecessor.as_schema2().policy)
    snapshot = _authoritative_snapshot(
        manifest,
        references,
        baseline,
        source_digest,
        source_bracket_id=source_bracket_identity(source_bracket),
        d1_source_bracket_id=narrowed.source_bracket_id,
    )
    digest = evidence_snapshot_digest(snapshot)
    expected = interpret_terminal(
        TerminalInputSnapshot(
            evidence_snapshot=snapshot,
            plan=plan,
            policy=predecessor.as_schema2().policy,
            c2_result=C2BracketReady(source_bracket, digest),
            d1_result=D1BracketReady(replayed_bracket, digest),
            d1_input_bracket=input_bracket,
        )
    )
    if not isinstance(expected, FoundResult) or expected != persisted_result:
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)


@dataclass(frozen=True, slots=True)
class _RepositoryView:
    """Minimal repository view required by the strict D1 validator."""

    root: Path
    path: Path
    manifest: RecordingSearchManifestV3

    def run_path(self, investigation_id: str, search_run_id: str) -> Path:
        if (self.path.name, self.path.parent.name) != (search_run_id, investigation_id):
            raise RecordingSearchManifestCorruptError
        return self.path

    def load(self, investigation_id: str, search_run_id: str) -> RecordingSearchManifestV3:
        if (
            self.manifest.investigation_id != investigation_id
            or self.manifest.search_run_id != search_run_id
        ):
            raise RecordingSearchManifestCorruptError
        return self.manifest


@dataclass(frozen=True, slots=True)
class _ReopenEvidenceStore:
    """Adapt the live store while retaining the original C2 revision identity."""

    store: RepositoryNarrowingEvidenceStore
    source_digest: str

    def validate_bracket(
        self,
        handle: _ReopenHandle,
        bracket: CoarseCandidateBracket,
        policy: RecordingSearchPolicy,
    ) -> None:
        self.store.validate_bracket(
            handle,
            replace(bracket, manifest_digest=self.source_digest),
            policy,
        )

    def load_state(self, handle: _ReopenHandle, bracket: CoarseCandidateBracket) -> NarrowingState:
        return self.store.load_state(
            handle,
            replace(bracket, manifest_digest=self.source_digest),
        )

    def find_existing(
        self, handle: _ReopenHandle, requested_time_utc: datetime, target_id: str
    ) -> NarrowingProbeEvidence | None:
        return self.store.find_existing(handle, requested_time_utc, target_id)

    def resolve_request(
        self, handle: _ReopenHandle, request: ProbeFrameRequestRecord, target_id: str
    ) -> NarrowingProbeEvidence:
        return self.store.resolve_request(handle, request, target_id)

    def current_manifest_digest(self, handle: _ReopenHandle) -> str:
        return self.store.current_manifest_digest(handle)


@dataclass(frozen=True, slots=True)
class _ReopenReplayHost:
    """Read-only host for replaying D1 solely from persisted repository records."""

    requests: tuple[ProbeFrameRequestRecord, ...]

    def a2_mutation(self, handle: _ReopenHandle) -> AbstractContextManager[None]:
        _ = handle
        return nullcontext()

    def acquire_targets(
        self, handle: _ReopenHandle, requested_times: tuple[datetime, ...]
    ) -> tuple[ProbeFrameRequestRecord, ...]:
        _ = handle
        by_time: dict[datetime, ProbeFrameRequestRecord] = {}
        for request in self.requests:
            requested_time = request.requested_time_utc
            if requested_time in by_time:
                raise RecordingSearchManifestCorruptError
            by_time[requested_time] = request
        try:
            return tuple(by_time[value] for value in requested_times)
        except KeyError:
            raise RecordingSearchManifestCorruptError from None

    def classify(self, handle: _ReopenHandle, request: ClassifyRecordingProbeRequest) -> NoReturn:
        _ = handle
        _ = request
        raise RecordingSearchManifestCorruptError


def _replay_requests(
    root: Path, run_path: Path, predecessor: RecordingSearchManifestV3
) -> tuple[ProbeFrameRequestRecord, ...]:
    """Load the immutable request records needed by the read-only D1 replay."""
    _, _, requests = read_schema2_children(root, run_path, predecessor.as_schema2())
    return tuple(requests.values())


def _authoritative_snapshot(  # noqa: PLR0913 - explicit persisted authority inputs
    manifest: RecordingSearchManifestV4,
    references: tuple[D2EvidenceReference, ...],
    baseline: ConfirmedReferenceBaselineRecord,
    source_digest: str,
    *,
    source_bracket_id: str,
    d1_source_bracket_id: str,
) -> D2EvidenceSnapshot:
    groups: list[D2SupportGroup] = []
    support_refs = tuple(item for item in references if item.role is D2EvidenceRole.ABSENCE_SUPPORT)
    for group_id in dict.fromkeys(item.support_group_id for item in support_refs):
        if group_id is None:
            _fail(RecordingSearchTerminalReopenCategory.SUPPORT_ORDER_VIOLATION)
        members = tuple(item for item in support_refs if item.support_group_id == group_id)
        first = next(iter(members), None)
        if first is None or first.decode_session_id is None:
            _fail(RecordingSearchTerminalReopenCategory.SUPPORT_ORDER_VIOLATION)
        second = members[1] if len(members) > 1 else None
        cadence = (
            int((second.requested_time_utc - first.requested_time_utc).total_seconds())
            if second is not None
            else 1
        )
        groups.append(
            D2SupportGroup(
                support_group_id=group_id,
                origin_target_id=first.target_id or "",
                support_count=len(members),
                cadence_seconds=cadence,
                decode_session_id=first.decode_session_id,
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
        plan_id=build_coarse_sampling_plan(manifest.policy.to_acquisition_policy()).plan_id,
        policy_identity=policy_identity(manifest.policy.to_acquisition_policy()),
        source_revision=D2SourceRevision(
            manifest_digest=source_digest,
            c2_bracket_id=source_bracket_id,
            d1_source_bracket_id=d1_source_bracket_id,
        ),
        references=references,
        support_groups=tuple(groups),
    )


def _validate_source_digest(
    manifest: RecordingSearchManifestV4,
    predecessor: RecordingSearchManifestV3,
    root: Path,
    run_path: Path,
) -> None:
    terminal = manifest.terminal_result
    expected = authoritative_evidence_digest(root, run_path, predecessor)
    if terminal.source_manifest_digest != expected:
        _fail(RecordingSearchTerminalReopenCategory.IDENTITY_MISMATCH)


def authoritative_evidence_digest(
    root: Path, run_path: Path, predecessor: RecordingSearchManifestV3
) -> str:
    """Return the versioned digest of every strict indexed source record."""
    return authoritative_source_digest(root, run_path, predecessor)


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
        excluded_probe_request_ids = {
            item.probe_request_id
            for item in (*support, *narrowing)
            if item.probe_request_id is not None
        }
        coarse = _reconstruct_coarse_references(
            manifest,
            baseline,
            classification_operations,
            observations,
            aliases,
            acquisition,
            frames,
            requests,
            exclude_probe_request_ids=excluded_probe_request_ids,
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
            d1_input_bracket_id=terminal.d1_input_bracket_id,
            history_digest=terminal.history_digest,
            iterations=terminal.iterations,
            stop_reason=terminal.stop_reason,
            upper_support_group_id=terminal.upper_support_group_id,
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
    *,
    exclude_probe_request_ids: set[str] | None = None,
) -> tuple[D2EvidenceReference, ...]:
    plan = build_coarse_sampling_plan(manifest.policy.to_acquisition_policy())
    result: list[D2EvidenceReference] = []
    excluded: set[str] = (
        set() if exclude_probe_request_ids is None else set(exclude_probe_request_ids)
    )
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
        if request.probe_request_id in excluded:
            continue
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


def _validate_found(  # noqa: C901 - strict terminal field and reference checks
    manifest: RecordingSearchManifestV4,
    lower: D2EvidenceReference,
    support: tuple[D2EvidenceReference, ...],
    narrowing: tuple[D2EvidenceReference, ...],
) -> None:
    policy = manifest.policy.to_acquisition_policy()
    terminal = manifest.terminal_result
    if not isinstance(terminal, PublishedFoundResult):
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)
    if (
        terminal.source_bracket_id != terminal.source_d1_bracket_id
        or terminal.d1_input_bracket_id is None
        or terminal.history_digest is None
        or terminal.iterations is None
        or terminal.stop_reason != "target_precision_reached"
        or terminal.upper_support_group_id is None
    ):
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
    if (
        terminal.lower_bound_requested_time_utc != lower.requested_time_utc
        or not support
        or terminal.upper_bound_requested_time_utc != support[0].requested_time_utc
        or terminal.upper_support_group_id != support[0].support_group_id
        or any(item.support_group_id != terminal.upper_support_group_id for item in support)
        or terminal.achieved_precision_seconds
        != int(
            (
                terminal.upper_bound_requested_time_utc - terminal.lower_bound_requested_time_utc
            ).total_seconds()
        )
        or terminal.lower_reference != _as_terminal_reference(lower)
        or terminal.upper_support != tuple(_as_terminal_reference(item) for item in support)
        or terminal.narrowing_evidence != tuple(_as_terminal_reference(item) for item in narrowing)
    ):
        _fail(RecordingSearchTerminalReopenCategory.TERMINAL_CONTRADICTION)
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


def _as_terminal_reference(value: D2EvidenceReference) -> TerminalEvidenceReference:
    """Project a reopened reference into the persisted allowlisted shape."""
    return TerminalEvidenceReference(
        role=value.role,
        target_id=value.target_id,
        requested_time_utc=value.requested_time_utc,
        acquisition_operation_id=value.acquisition_operation_id,
        probe_request_id=value.probe_request_id,
        classification_operation_id=value.classification_operation_id,
        observation_id=value.observation_id or "",
        canonical_frame_id=value.canonical_frame_id,
        alias_id=value.alias_id,
        decode_session_id=value.decode_session_id,
        decoded_frame_utc=value.decoded_frame_utc,
        decoded_pts=value.decoded_pts,
        decoded_ordinal=value.decoded_ordinal,
        support_group_id=value.support_group_id,
        support_index=value.support_index,
        is_phase6_baseline=value.is_phase6_baseline,
    )


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
    source_digest: str,
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
            manifest_digest=source_digest,
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
