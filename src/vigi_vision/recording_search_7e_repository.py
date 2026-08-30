# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportUnknownMemberType=false, reportUnannotatedClassAttribute=false, reportUnusedCallResult=false, reportUnnecessaryIsInstance=false, reportUnreachable=false, reportInvalidTypeForm=false, reportAttributeAccessIssue=false, reportDeprecated=false, reportUnusedParameter=false, reportArgumentType=false, reportUnusedFunction=false
# ruff: noqa: ARG002, C901, EM101, FURB171, PLR0912, PLR0913, PLR0915, PLR2004, PLW0108, PTH105, RET504, RUF022, SIM102, SIM108, TRY003, TRY300
"""Strict local persistence for the Phase 7E schema-5/6/7 boundary.

This module does not acquire recordings, decode media, invoke B4, or create
Phase 8 records.  A manifest replacement is the only visible commit point;
children are immutable and are indexed only by the successor manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import Enum
from io import BytesIO
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, ClassVar, Protocol

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from vigi_vision.durable_io import (
    DurableJsonError,
    is_safe_contained_path,
    is_safe_path,
)
from vigi_vision.recording_search_7e_identity import (
    IdentityValidationError,
    family_from_identity,
    strict_json_loads,
)
from vigi_vision.recording_search_7e_models import (
    Schema5Manifest,
    Schema5PhaseState,
    Schema6Manifest,
    Schema6TargetState,
    Schema7Manifest,
    StrictIdentityEnvelope,
)
from vigi_vision.recording_search_7e_validation import (
    Phase7EValidationError,
    Schema5Envelope,
    Schema6Envelope,
    validate_dependency_graph,
    validate_schema5_state,
    validate_schema6_state,
)
from vigi_vision.recording_search_lock import LocalInvestigationLock


class Phase7ERepositoryError(RuntimeError):
    """Safe base error for the schema-5/6 persistence boundary."""


class Phase7ENotFoundError(Phase7ERepositoryError):
    """The requested run does not exist."""


class Phase7EConflictError(Phase7ERepositoryError):
    """An identical retry was not identical to the authoritative record."""


class Phase7EInProgressError(Phase7ERepositoryError):
    """The run is currently protected by another process."""


class Phase7ECorruptError(Phase7ERepositoryError):
    """The durable tree is malformed, foreign, incomplete, or unsafe."""


class Phase7EReadbackError(Phase7ERepositoryError):
    """A committed replacement could not be strictly read back."""


@dataclass(slots=True)
class Phase7EInvocationOwnership:
    """One live invocation's continuously held per-investigation OS lock."""

    repository: RecordingSearch7ERepository = field(repr=False)
    investigation_id: str
    run_id: str
    lock: LocalInvestigationLock = field(repr=False)
    active: bool = True

    def validate(
        self,
        repository: RecordingSearch7ERepository,
        investigation_id: str,
        run_id: str | None = None,
    ) -> None:
        """Fail closed unless this exact live owner protects the requested run."""
        if (
            not self.active
            or not self.lock.held
            or self.repository is not repository
            or self.investigation_id != investigation_id
            or (run_id is not None and self.run_id != run_id)
        ):
            raise Phase7EInProgressError

    def release(self) -> None:
        """Release the OS handle exactly once."""
        if not self.active:
            return
        self.active = False
        self.lock.release()


class PublicationStatus(str, Enum):
    """Deterministic result of an idempotent publication attempt."""

    CREATED = "created"
    REUSED = "reused"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class PublicationResult:
    """Publication outcome and the strictly reopened authoritative run."""

    status: PublicationStatus
    run: Phase7ERun

    @property
    def outcome(self) -> str:
        """Return the stable lowercase outcome used by callers."""
        return self.status.value


class _ManifestDocument(BaseModel):
    """Closed on-disk manifest envelope."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )

    schema_version: StrictInt
    manifest: dict[str, Any]
    state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Phase7ERun:
    """A strictly reopened schema-5, schema-6, or schema-7 tree."""

    root: Path = field(repr=False)
    investigation_id: str
    run_id: str
    schema_version: int
    manifest: StrictIdentityEnvelope
    state: Schema5Envelope | Schema6Envelope
    records: tuple[StrictIdentityEnvelope, ...] = field(default_factory=tuple)
    frame_bytes: Mapping[str, bytes] = field(default_factory=dict, repr=False)

    @property
    def manifest_id(self) -> str:
        """Return the authoritative manifest identity."""
        return self.manifest.identity

    @property
    def identities(self) -> tuple[str, ...]:
        """Return child identities in deterministic path order."""
        return tuple(record.identity for record in self.records)

    @property
    def is_schema5(self) -> bool:
        """Return whether this is a schema-5 run."""
        return self.schema_version == 5

    @property
    def is_schema6(self) -> bool:
        """Return whether this is a schema-6 run."""
        return self.schema_version == 6

    @property
    def is_schema7(self) -> bool:
        """Return whether this is an immutable schema-7 run."""
        return self.schema_version == 7

    @property
    def result_kind(self) -> str | None:
        """Return the strictly reopened schema-7 terminal kind."""
        if not self.is_schema7:
            return None
        terminal = next(
            (record for record in self.records if record.family == "terminal-result"), None
        )
        return None if terminal is None else str(terminal.payload["result_kind"])


@dataclass(frozen=True, slots=True)
class _Child:
    envelope: StrictIdentityEnvelope
    relative_directory: str

    @property
    def relative_path(self) -> str:
        """Return the deterministic child path."""
        return f"{self.relative_directory}/{self.envelope.identity}.json"


class DurableMediaProbe(Protocol):
    """Structural probe used to prove retained common-session media on reopen."""

    def probe(self, path: Path, timeout_seconds: float) -> object:
        """Return strict media facts for one confined regular MP4."""
        ...


_SCHEMA5_DIRECTORIES = frozenset({"policy", "plans", "requests", "operations", "manifests"})
_SCHEMA6_DIRECTORIES = frozenset(
    {
        *_SCHEMA5_DIRECTORIES,
        "classifier-policies",
        "sessions",
        "decoder-operations",
        "frames",
        "classification-operations",
        "observations",
        "aliases",
        "support-groups",
        "c2-brackets",
        "d1-inputs",
        "d1-histories",
        "narrowed-brackets",
    }
)
_SCHEMA7_DIRECTORIES = frozenset({*_SCHEMA6_DIRECTORIES, "terminal"})
_FAMILY_DIRECTORY = {
    "policy": "policy",
    "coarse-plan": "plans",
    "target-request": "requests",
    "replay-operation": "operations",
    "classifier-policy": "classifier-policies",
    "common-session": "sessions",
    "decoder-operation": "decoder-operations",
    "frame": "frames",
    "classification-operation": "classification-operations",
    "observation": "observations",
    "alias": "aliases",
    "support-group": "support-groups",
    "c2-bracket": "c2-brackets",
    "d1-input": "d1-inputs",
    "d1-history": "d1-histories",
    "narrowed-bracket": "narrowed-brackets",
}
_SCHEMA6_INDEX_KEYS = (
    "target_request_ids",
    "decoder_operation_ids",
    "frame_ids",
    "classification_operation_ids",
    "observation_ids",
    "alias_ids",
    "support_group_ids",
    "c2_bracket_ids",
    "d1_input_ids",
    "d1_history_ids",
    "narrowed_bracket_ids",
)
_TERMINAL_FILES = {
    "source-record-set": "source-record-set.json",
    "evidence-snapshot": "evidence-snapshot.json",
    "terminal-result": "result.json",
}
_IDENTITY_RE = r"^rr-[a-z0-9-]+-v1-[0-9a-f]{64}$"


@dataclass
class RecordingSearch7ERepository:
    """Filesystem repository for one confined Phase 7E namespace."""

    root: Path = field(repr=False)
    lock_timeout_seconds: float = field(default=1.0, repr=False)
    media_root: Path | None = field(default=None, repr=False)
    media_probe: DurableMediaProbe | None = field(default=None, repr=False)
    media_probe_timeout_seconds: float = field(default=20.0, repr=False)
    _guard: RLock = field(default_factory=RLock, init=False, repr=False)

    def ensure_root(self) -> None:
        """Create and validate the repository and its lock/staging roots."""
        try:
            if not is_safe_path(self.root):
                raise Phase7ECorruptError
            if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
                raise Phase7ECorruptError
            self.root.mkdir(parents=True, exist_ok=True)
            if not is_safe_contained_path(self.root, self.root, require_target=True):
                raise Phase7ECorruptError
            for directory in (self.root / ".locks", self.root / ".staging"):
                if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
                    raise Phase7ECorruptError
                directory.mkdir(exist_ok=True)
                if not is_safe_contained_path(self.root, directory, require_target=True):
                    raise Phase7ECorruptError
        except (OSError, ValueError):
            raise Phase7ECorruptError from None

    def run_path(self, investigation_id: str, run_id: str) -> Path:
        """Return a confined deterministic run path without creating it."""
        if not _safe_component(investigation_id) or not _safe_component(run_id):
            raise Phase7ECorruptError
        path = self.root / investigation_id / run_id
        if path.parent.parent != self.root or not is_safe_contained_path(self.root, path):
            raise Phase7ECorruptError
        return path

    def lock_path(self, investigation_id: str) -> Path:
        """Return the per-investigation OS lock path."""
        if not _safe_component(investigation_id):
            raise Phase7ECorruptError
        self.ensure_root()
        path = self.root / ".locks" / f"{investigation_id}.lock"
        if not is_safe_contained_path(self.root, path):
            raise Phase7ECorruptError
        return path

    @contextmanager
    def invocation_ownership(
        self,
        investigation_id: str,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterator[Phase7EInvocationOwnership]:
        """Hold the approved OS lock across one complete live invocation.

        Repository mutations performed with the yielded owner take only the
        short in-process guard and never recursively reacquire the OS lock.
        """
        self.ensure_root()
        _ = self.run_path(investigation_id, run_id)
        lock = LocalInvestigationLock(self.lock_path(investigation_id))
        timeout = self.lock_timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout < 0 or not lock.try_acquire(timeout):
            raise Phase7EInProgressError
        ownership = Phase7EInvocationOwnership(self, investigation_id, run_id, lock)
        try:
            yield ownership
        finally:
            ownership.release()

    def create_schema5(
        self,
        manifest: StrictIdentityEnvelope | Schema5Manifest | Mapping[str, Any],
        state: Schema5Envelope | Mapping[str, Any],
        records: Iterable[object] | Mapping[str, object] = (),
        *,
        investigation_id: str | None = None,
        run_id: str | None = None,
        ownership: Phase7EInvocationOwnership | None = None,
    ) -> PublicationResult:
        """Atomically create a strict pre-acquisition schema-5 run."""
        envelope = _coerce_envelope(manifest, "schema5-manifest")
        phase = validate_schema5_state(state)
        if phase.phase_state is not Schema5PhaseState.PLANNED:
            raise Phase7EValidationError("schema-5 creation must start PLANNED")
        inv, run = _manifest_ids(envelope, investigation_id, run_id)
        children = _coerce_children(records)
        self._validate_schema5_proposal(envelope, phase, children)
        with self._locked(inv, ownership, run):
            final = self.run_path(inv, run)
            if final.exists() or final.is_symlink():
                existing = self._reopen_unlocked(inv, run, expected_schema=5)
                if _run_fingerprint(existing) == _proposal_fingerprint(
                    5, envelope, phase, children
                ):
                    return PublicationResult(PublicationStatus.REUSED, existing)
                raise Phase7EConflictError
            staging = self._new_staging(inv, run)
            try:
                self._write_tree(staging, 5, envelope, phase, children)
                checked = self._reopen_at(staging, inv, run, expected_schema=5)
                _remove_owned_file(self.root, staging / "target.json")
                self._publish_new_tree(staging, final)
                reopened = self._reopen_unlocked(inv, run, expected_schema=5)
                if reopened.manifest_id != checked.manifest_id:
                    raise Phase7EReadbackError
                return PublicationResult(PublicationStatus.CREATED, reopened)
            finally:
                _remove_owned_directory(self.root, staging)

    persist_schema5 = create_schema5

    def admit_schema5(
        self,
        investigation_id: str,
        run_id: str,
        proposal: StrictIdentityEnvelope | Schema5Manifest | Mapping[str, Any],
        state: Schema5Envelope | Mapping[str, Any],
        records: Iterable[object] | Mapping[str, object] = (),
        *,
        expected_manifest_id: str | None = None,
        ownership: Phase7EInvocationOwnership | None = None,
    ) -> PublicationResult:
        """Persist one legal successor in the mutable schema-5 lifecycle."""
        envelope = _coerce_envelope(proposal, "schema5-manifest")
        next_state = validate_schema5_state(state)
        children = _coerce_children(records)
        with self._locked(investigation_id, ownership, run_id):
            current = self._reopen_unlocked(investigation_id, run_id, expected_schema=5)
            if not isinstance(current.state, Schema5Envelope):
                raise Phase7ECorruptError
            if expected_manifest_id is not None and current.manifest_id != expected_manifest_id:
                raise Phase7EConflictError
            if current.manifest_id != envelope.identity:
                raise Phase7EConflictError
            if not _legal_schema5_successor(current.state, next_state):
                current_children = {
                    (child.family, child.identity, json.dumps(child.payload, sort_keys=True))
                    for child in current.records
                }
                proposed_children = {
                    (
                        child.envelope.family,
                        child.envelope.identity,
                        json.dumps(child.envelope.payload, sort_keys=True),
                    )
                    for child in children
                }
                if current.state == next_state and current_children == proposed_children:
                    return PublicationResult(PublicationStatus.REUSED, current)
                raise Phase7EConflictError
            self._validate_schema5_proposal(envelope, next_state, children)
            existing_ids = {child.identity for child in current.records}
            proposed_ids = {child.envelope.identity for child in children}
            if not existing_ids.issubset(proposed_ids):
                raise Phase7EValidationError("schema-5 proposal lost children")
            self._publish_schema5_successor(current, envelope, next_state, children)
            return PublicationResult(
                PublicationStatus.CREATED,
                self._reopen_unlocked(investigation_id, run_id, expected_schema=5),
            )

    transition_schema5 = admit_schema5

    def reopen_schema5(
        self,
        investigation_id: str,
        run_id: str,
        *,
        ownership: Phase7EInvocationOwnership | None = None,
    ) -> Phase7ERun:
        """Strictly reopen schema 5 without recovery or mutation."""
        with self._locked(investigation_id, ownership, run_id):
            return self._reopen_unlocked(investigation_id, run_id, expected_schema=5)

    strict_reopen_schema5 = reopen_schema5
    read_schema5 = reopen_schema5

    def transition_schema5_to_schema6(
        self,
        investigation_id: str,
        run_id: str,
        proposal: StrictIdentityEnvelope | Schema6Manifest | Mapping[str, Any],
        state: Schema6Envelope | Mapping[str, Any],
        records: Iterable[object] | Mapping[str, object] = (),
        *,
        expected_schema5_manifest_id: str | None = None,
        ownership: Phase7EInvocationOwnership | None = None,
    ) -> PublicationResult:
        """Atomically publish the approved schema-5 to schema-6 successor."""
        envelope = _coerce_envelope(proposal, "schema6-manifest")
        target_state = validate_schema6_state(state)
        children = _coerce_children(records)
        with self._locked(investigation_id, ownership, run_id):
            current = self._reopen_unlocked(investigation_id, run_id, expected_schema=None)
            if current.is_schema6:
                if current.manifest_id != envelope.identity or current.state != target_state:
                    raise Phase7EConflictError
                if not _same_child_set(current.records, children):
                    raise Phase7EConflictError
                return PublicationResult(PublicationStatus.REUSED, current)
            if not isinstance(current.state, Schema5Envelope):
                raise Phase7ECorruptError
            if current.state.phase_state is not Schema5PhaseState.ACQUIRED:
                raise Phase7EValidationError("schema-5 predecessor is not ACQUIRED")
            if (
                expected_schema5_manifest_id is not None
                and current.manifest_id != expected_schema5_manifest_id
            ):
                raise Phase7EConflictError
            if target_state.target_state is not Schema6TargetState.REQUESTED:
                raise Phase7EValidationError("schema-5 transition must start REQUESTED")
            if target_state.predecessor_target_state is not None:
                raise Phase7EValidationError("schema-5 transition has no target predecessor")
            self._validate_schema6_proposal(envelope, target_state, children, current)
            self._publish_schema6_successor(
                investigation_id,
                run_id,
                current,
                envelope,
                target_state,
                children,
            )
            return PublicationResult(
                PublicationStatus.CREATED,
                self._reopen_unlocked(investigation_id, run_id, expected_schema=6),
            )

    transition_to_schema6 = transition_schema5_to_schema6

    def admit_schema6(
        self,
        investigation_id: str,
        run_id: str,
        proposal: StrictIdentityEnvelope | Schema6Manifest | Mapping[str, Any],
        state: Schema6Envelope | Mapping[str, Any],
        records: Iterable[object] | Mapping[str, object] = (),
        *,
        expected_manifest_id: str | None = None,
        binary_records: Mapping[str, bytes] | None = None,
        ownership: Phase7EInvocationOwnership | None = None,
    ) -> PublicationResult:
        """Admit one validated schema-6 successor and its immutable children."""
        envelope = _coerce_envelope(proposal, "schema6-manifest")
        target_state = validate_schema6_state(state)
        children = _coerce_children(records)
        binary = dict(binary_records or {})
        with self._locked(investigation_id, ownership, run_id):
            current = self._reopen_unlocked(investigation_id, run_id, expected_schema=6)
            if not isinstance(current.state, Schema6Envelope):
                raise Phase7ECorruptError
            if expected_manifest_id is not None and current.manifest_id != expected_manifest_id:
                raise Phase7EConflictError
            analysis_append = _legal_schema6_analysis_append(
                current,
                envelope,
                target_state,
                children,
            )
            if not _legal_schema6_successor(current.state, target_state) and not analysis_append:
                if current.manifest_id == envelope.identity:
                    existing = self._existing_schema6_result(
                        investigation_id, run_id, envelope, target_state, children
                    )
                    if existing is not None:
                        return existing
                raise Phase7EConflictError
            self._validate_schema6_proposal(envelope, target_state, children, current)
            existing_frame_ids = {
                child.identity for child in current.records if child.family == "frame"
            }
            _validate_frame_bytes(
                children,
                binary,
                required_ids={
                    child.envelope.identity
                    for child in children
                    if child.envelope.family == "frame"
                }
                - existing_frame_ids,
            )
            existing = self._existing_schema6_result(
                investigation_id, run_id, envelope, target_state, children
            )
            if existing is not None:
                return existing
            self._publish_schema6_successor(
                investigation_id,
                run_id,
                current,
                envelope,
                target_state,
                children,
                binary_records=binary,
            )
            return PublicationResult(
                PublicationStatus.CREATED,
                self._reopen_unlocked(investigation_id, run_id, expected_schema=6),
            )

    persist_schema6 = admit_schema6

    def reopen_schema6(
        self,
        investigation_id: str,
        run_id: str,
        *,
        ownership: Phase7EInvocationOwnership | None = None,
    ) -> Phase7ERun:
        """Strictly reopen schema 6 without recovery, cleanup, or rewriting."""
        with self._locked(investigation_id, ownership, run_id):
            return self._reopen_unlocked(investigation_id, run_id, expected_schema=6)

    strict_reopen_schema6 = reopen_schema6
    read_schema6 = reopen_schema6

    def publish_schema7(
        self,
        investigation_id: str,
        run_id: str,
        proposal: StrictIdentityEnvelope | Schema7Manifest | Mapping[str, Any],
        source_record_set: object,
        evidence_snapshot: object,
        terminal_result: object,
        *,
        expected_schema6_manifest_id: str | None = None,
        ownership: Phase7EInvocationOwnership | None = None,
        publication_check: Callable[[], None] | None = None,
        readback_check: Callable[[], None] | None = None,
    ) -> PublicationResult:
        """Atomically replace a complete schema-6 tree with immutable schema 7."""
        envelope = _coerce_envelope(proposal, "schema7-manifest")
        terminal_children = tuple(
            _coerce_terminal_child(value, family)
            for value, family in (
                (source_record_set, "source-record-set"),
                (evidence_snapshot, "evidence-snapshot"),
                (terminal_result, "terminal-result"),
            )
        )
        with self._locked(investigation_id, ownership, run_id):
            run_path = self.run_path(investigation_id, run_id)
            if _is_schema7_root(run_path):
                if readback_check is not None:
                    readback_check()
                current7 = self._reopen_schema7_unlocked(investigation_id, run_id)
                if readback_check is not None:
                    readback_check()
                if _same_schema7_proposal(current7, envelope, terminal_children):
                    return PublicationResult(PublicationStatus.REUSED, current7)
                raise Phase7EConflictError
            current = self._reopen_unlocked(investigation_id, run_id, expected_schema=6)
            if not isinstance(current.state, Schema6Envelope):
                raise Phase7ECorruptError
            if (
                current.state.run_state != "RUNNING"
                or current.state.target_state is not Schema6TargetState.OBSERVED
            ):
                raise Phase7EValidationError("schema-7 predecessor is not complete")
            if (
                expected_schema6_manifest_id is not None
                and current.manifest_id != expected_schema6_manifest_id
            ):
                raise Phase7EConflictError
            self._validate_schema7_proposal(current, envelope, terminal_children)
            self._publish_schema7_successor(
                current,
                envelope,
                terminal_children,
                publication_check=publication_check,
            )
            if readback_check is not None:
                readback_check()
            reopened = self._reopen_schema7_unlocked(investigation_id, run_id)
            if readback_check is not None:
                readback_check()
            if reopened.manifest_id != envelope.identity:
                raise Phase7EReadbackError
            return PublicationResult(PublicationStatus.CREATED, reopened)

    transition_schema6_to_schema7 = publish_schema7
    persist_schema7 = publish_schema7

    def reopen_schema7(
        self,
        investigation_id: str,
        run_id: str,
        *,
        ownership: Phase7EInvocationOwnership | None = None,
    ) -> Phase7ERun:
        """Strictly reopen immutable schema 7 without cleanup or repair."""
        with self._locked(investigation_id, ownership, run_id):
            return self._reopen_schema7_unlocked(investigation_id, run_id)

    strict_reopen_schema7 = reopen_schema7
    read_schema7 = reopen_schema7

    def reopen_current(
        self,
        investigation_id: str,
        run_id: str,
        *,
        ownership: Phase7EInvocationOwnership | None = None,
    ) -> Phase7ERun:
        """Strictly dispatch the current schema without mutation or recovery."""
        with self._locked(investigation_id, ownership, run_id):
            path = self.run_path(investigation_id, run_id)
            if _is_schema7_root(path):
                return self._reopen_schema7_unlocked(investigation_id, run_id)
            return self._reopen_unlocked(investigation_id, run_id, expected_schema=None)

    strict_reopen_current = reopen_current

    def inspect_current_read_only(
        self,
        investigation_id: str,
        run_id: str,
        *,
        timeout_seconds: float = 0.05,
    ) -> Phase7ERun:
        """Strictly inspect a run without acquiring or changing execution authority."""
        if timeout_seconds < 0 or not self._guard.acquire(timeout=timeout_seconds):
            raise Phase7EInProgressError
        try:
            path = self.run_path(investigation_id, run_id)
            try:
                if _is_schema7_root(path):
                    return self._reopen_schema7_unlocked(investigation_id, run_id)
                return self._reopen_unlocked(investigation_id, run_id, expected_schema=None)
            except (Phase7ENotFoundError, Phase7ECorruptError):
                lock = LocalInvestigationLock(self.lock_path(investigation_id))
                if not lock.try_acquire(0):
                    raise Phase7EInProgressError from None
                lock.release()
                raise
        finally:
            self._guard.release()

    def recover_active(self, investigation_id: str, run_id: str) -> Phase7ERun:
        """Inspect and interrupt an unowned active run under the OS lock."""
        with self._locked(investigation_id):
            self._clean_owned_staging(investigation_id, run_id)
            run = (
                self._reopen_schema7_unlocked(investigation_id, run_id)
                if _is_schema7_root(self.run_path(investigation_id, run_id))
                else self._reopen_unlocked(investigation_id, run_id, expected_schema=None)
            )
            if run.is_schema7:
                return run
            if not isinstance(run.state, (Schema5Envelope, Schema6Envelope)):
                raise Phase7ECorruptError
            if run.state.run_state not in {"RUNNING"}:
                return run
            if run.is_schema5:
                previous = run.state
                if not isinstance(previous, Schema5Envelope):
                    raise Phase7ECorruptError
                interrupted = Schema5Envelope(
                    run_state="INTERRUPTED",
                    phase_state=Schema5PhaseState.INTERRUPTED,
                    active_replay_operation_id=(
                        previous.active_replay_operation_id
                        if previous.phase_state is Schema5PhaseState.ACQUIRING
                        else None
                    ),
                    reason_code="interrupted",
                    attempt_count=previous.attempt_count,
                )
            else:
                previous6 = run.state
                if not isinstance(previous6, Schema6Envelope):
                    raise Phase7ECorruptError
                interrupted = Schema6Envelope(
                    run_state="INTERRUPTED",
                    target_state=Schema6TargetState.INTERRUPTED,
                    active_target_request_id=previous6.active_target_request_id,
                    active_decoder_operation_id=previous6.active_decoder_operation_id,
                    active_frame_id=previous6.active_frame_id,
                    active_classification_attempt_id=(
                        previous6.active_classification_attempt_id
                        if previous6.target_state is Schema6TargetState.CLASSIFYING
                        else None
                    ),
                    active_classification_operation_id=previous6.active_classification_operation_id,
                    active_observation_id=previous6.active_observation_id,
                    reason_code="interrupted",
                    attempt_count=previous6.attempt_count,
                    predecessor_target_state=previous6.target_state,
                )
            self._replace_manifest(run.root, run.schema_version, run.manifest, interrupted)
            reopened = self._reopen_unlocked(
                investigation_id, run_id, expected_schema=run.schema_version
            )
            return reopened

    inspect_and_recover = recover_active
    recover = recover_active

    @contextmanager
    def _locked(
        self,
        investigation_id: str,
        ownership: Phase7EInvocationOwnership | None = None,
        run_id: str | None = None,
    ) -> Iterator[None]:
        """Acquire OS lock before the in-process repository guard."""
        if ownership is not None:
            ownership.validate(self, investigation_id, run_id)
            with self._guard:
                ownership.validate(self, investigation_id, run_id)
                yield
            return
        lock = LocalInvestigationLock(self.lock_path(investigation_id))
        if not lock.try_acquire(self.lock_timeout_seconds):
            raise Phase7EInProgressError
        try:
            with self._guard:
                yield
        finally:
            lock.release()

    def _new_staging(self, investigation_id: str, run_id: str) -> Path:
        self.ensure_root()
        try:
            path = Path(
                tempfile.mkdtemp(prefix=f"{investigation_id}-{run_id}-", dir=self.root / ".staging")
            )
            marker = path / "target.json"
            _write_bytes_no_replace(
                marker,
                _canonical_json({"investigation_id": investigation_id, "run_id": run_id}).encode(),
            )
            return path
        except OSError:
            raise Phase7ERepositoryError from None

    def _write_tree(
        self,
        directory: Path,
        schema: int,
        manifest: StrictIdentityEnvelope,
        state: Schema5Envelope | Schema6Envelope,
        records: Sequence[_Child],
    ) -> None:
        self._ensure_tree_directories(directory, schema)
        document = _ManifestDocument(
            schema_version=schema,
            manifest=manifest.model_dump(mode="json"),
            state=state.model_dump(mode="json"),
        )
        _write_bytes_no_replace(
            directory / "manifest.json", _canonical_json(document.model_dump(mode="json")).encode()
        )
        for child in records:
            target = directory / child.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes_no_replace(
                target, _canonical_json(child.envelope.model_dump(mode="json")).encode()
            )

    def _publish_new_tree(self, staging: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise Phase7EConflictError
        if not is_safe_contained_path(self.root, staging, require_target=True):
            raise Phase7ECorruptError
        try:
            staging.rename(destination)
        except OSError:
            raise Phase7ERepositoryError from None

    def _publish_schema6_successor(
        self,
        investigation_id: str,
        run_id: str,
        current: Phase7ERun,
        manifest: StrictIdentityEnvelope,
        state: Schema6Envelope,
        records: Sequence[_Child],
        *,
        binary_records: Mapping[str, bytes] | None = None,
    ) -> None:
        self._publish_successor(
            current,
            manifest,
            state,
            records,
            schema=6,
            binary_records=binary_records,
            archive_predecessor=current.is_schema5,
        )

    def _publish_schema5_successor(
        self,
        current: Phase7ERun,
        manifest: StrictIdentityEnvelope,
        state: Schema5Envelope,
        records: Sequence[_Child],
    ) -> None:
        self._publish_successor(current, manifest, state, records, schema=5)

    def _publish_successor(
        self,
        current: Phase7ERun,
        manifest: StrictIdentityEnvelope,
        state: Schema5Envelope | Schema6Envelope,
        records: Sequence[_Child],
        *,
        schema: int,
        binary_records: Mapping[str, bytes] | None = None,
        archive_predecessor: bool = False,
    ) -> None:
        """Stage and strictly read a successor before moving its delta."""
        run_path = current.root
        staging = self._new_staging(current.investigation_id, current.run_id)
        created: list[str] = []
        created_directories: list[str] = []
        committed = False
        try:
            self._copy_tree(current.root, staging)
            self._ensure_tree_directories(staging, schema)
            if schema == 6 and archive_predecessor:
                archive = staging / "manifests" / f"{current.manifest_id}.json"
                archive_payload = _canonical_json(_manifest_document(current)).encode()
                if archive.exists():
                    if archive.is_symlink() or archive.read_bytes() != archive_payload:
                        raise Phase7ECorruptError
                else:
                    _write_bytes_no_replace(archive, archive_payload)
            for child in records:
                target = staging / child.relative_path
                payload = _canonical_json(child.envelope.model_dump(mode="json")).encode()
                if target.exists():
                    if target.is_symlink() or target.read_bytes() != payload:
                        raise Phase7EConflictError
                else:
                    _write_bytes_no_replace(target, payload)
                if (
                    binary_records
                    and child.envelope.family == "frame"
                    and child.envelope.identity in binary_records
                ):
                    frame_path = staging / "frames" / f"{child.envelope.identity}.jpg"
                    raw = bytes(binary_records[child.envelope.identity])
                    if frame_path.exists():
                        if frame_path.is_symlink() or frame_path.read_bytes() != raw:
                            raise Phase7EConflictError
                    else:
                        _write_bytes_no_replace(frame_path, raw)
            _remove_owned_file(self.root, staging / "manifest.json")
            self._write_manifest_file(staging, schema, manifest, state)
            checked = self._reopen_at(
                staging, current.investigation_id, current.run_id, expected_schema=schema
            )
            if checked.manifest_id != manifest.identity:
                raise Phase7EReadbackError
            for source in sorted(staging.rglob("*"), key=lambda value: str(value)):
                if not source.is_file() or source.name in {
                    "target.json",
                    "publication.json",
                    "manifest.json",
                }:
                    continue
                relative = source.relative_to(staging).as_posix()
                target = run_path / Path(relative)
                if not is_safe_contained_path(self.root, target):
                    raise Phase7ECorruptError
                if target.exists():
                    if target.is_symlink() or target.read_bytes() != source.read_bytes():
                        raise Phase7EConflictError
                    continue
                created.append(relative)
            _write_bytes_no_replace(
                staging / "publication.json",
                _canonical_json(
                    {
                        "investigation_id": current.investigation_id,
                        "run_id": current.run_id,
                        "manifest_id": manifest.identity,
                        "paths": created,
                    }
                ).encode(),
            )
            for relative in created:
                source = staging / Path(relative)
                target = run_path / Path(relative)
                if not is_safe_contained_path(self.root, target):
                    raise Phase7ECorruptError
                missing_parents: list[Path] = []
                parent = target.parent
                while parent != run_path and not parent.exists():
                    missing_parents.append(parent)
                    parent = parent.parent
                for missing in reversed(missing_parents):
                    missing.mkdir()
                    created_directories.append(missing.relative_to(run_path).as_posix())
                if target.exists() or target.is_symlink():
                    if target.is_symlink() or target.read_bytes() != source.read_bytes():
                        raise Phase7EConflictError
                    continue
                os.replace(source, target)
                _fsync_directory(target.parent)
            self._replace_manifest(run_path, schema, manifest, state)
            committed = True
        finally:
            if not committed:
                _remove_owned_paths(self.root, run_path, created)
                _remove_owned_empty_directories(self.root, run_path, created_directories)
            _remove_owned_directory(self.root, staging)

    def _copy_tree(self, source: Path, destination: Path) -> None:
        """Copy a strictly reopened tree into invocation-owned staging."""
        for item in source.rglob("*"):
            relative = item.relative_to(source)
            if relative.parts and relative.parts[0] in {".staging", ".locks"}:
                continue
            target = destination / relative
            if item.is_symlink() or not is_safe_contained_path(
                self.root, item, require_target=True
            ):
                raise Phase7ECorruptError
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_bytes_no_replace(target, item.read_bytes())

    def _write_manifest_file(
        self,
        directory: Path,
        schema: int,
        manifest: StrictIdentityEnvelope,
        state: Schema5Envelope | Schema6Envelope,
    ) -> None:
        document = _ManifestDocument(
            schema_version=schema,
            manifest=manifest.model_dump(mode="json"),
            state=state.model_dump(mode="json"),
        )
        _write_bytes_no_replace(
            directory / "manifest.json", _canonical_json(document.model_dump(mode="json")).encode()
        )

    def _replace_manifest(
        self,
        run_path: Path,
        schema: int,
        manifest: StrictIdentityEnvelope,
        state: Schema5Envelope | Schema6Envelope,
    ) -> None:
        document = _ManifestDocument(
            schema_version=schema,
            manifest=manifest.model_dump(mode="json"),
            state=state.model_dump(mode="json"),
        )
        path = run_path / "manifest.json"
        temporary = path.with_name(f".manifest-{manifest.identity}.tmp")
        _write_bytes_no_replace(
            temporary, _canonical_json(document.model_dump(mode="json")).encode()
        )
        try:
            os.replace(temporary, path)
        except OSError:
            _remove_owned_file(self.root, temporary)
            raise Phase7ERepositoryError from None

    def _existing_schema6_result(
        self,
        investigation_id: str,
        run_id: str,
        manifest: StrictIdentityEnvelope,
        state: Schema6Envelope,
        records: Sequence[_Child],
    ) -> PublicationResult | None:
        path = self.run_path(investigation_id, run_id)
        current_path = path / "manifest.json"
        if not current_path.exists():
            return None
        current = self._reopen_unlocked(investigation_id, run_id, expected_schema=6)
        if current.manifest_id != manifest.identity:
            return None
        if current.state == state and _same_child_set(current.records, records):
            return PublicationResult(PublicationStatus.REUSED, current)
        # A lifecycle-only successor (for example FRAME_READY -> CLASSIFYING)
        # may retain the same indexed child set.  Its state is still a distinct
        # atomic manifest replacement, not an idempotent retry.
        if current.state != state and _same_child_set(current.records, records):
            return None
        raise Phase7EConflictError

    def _reopen_unlocked(
        self,
        investigation_id: str,
        run_id: str,
        *,
        expected_schema: int | None,
        path_override: Path | None = None,
    ) -> Phase7ERun:
        path = (
            path_override if path_override is not None else self.run_path(investigation_id, run_id)
        )
        if not path.exists():
            raise Phase7ENotFoundError
        if (
            path.is_symlink()
            or not path.is_dir()
            or not is_safe_contained_path(self.root, path, require_target=True)
        ):
            raise Phase7ECorruptError
        manifest_path = path / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise Phase7ECorruptError
        if not is_safe_contained_path(self.root, manifest_path, require_target=True):
            raise Phase7ECorruptError
        try:
            document = _parse_document(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, DurableJsonError, ValidationError, ValueError):
            raise Phase7ECorruptError from None
        if expected_schema is not None and document.schema_version != expected_schema:
            raise Phase7ECorruptError
        envelope = _coerce_envelope(document.manifest, f"schema{document.schema_version}-manifest")
        state: Schema5Envelope | Schema6Envelope
        if document.schema_version == 5:
            state = validate_schema5_state(document.state)
            if not isinstance(envelope, StrictIdentityEnvelope):
                raise Phase7ECorruptError
        elif document.schema_version == 6:
            state = validate_schema6_state(document.state)
        else:
            raise Phase7ECorruptError
        _validate_manifest_binding(
            envelope, state, investigation_id, run_id, document.schema_version
        )
        try:
            records = self._read_children(path, document.schema_version, envelope, state)
            frame_bytes = (
                self._read_frame_bytes(path, records) if document.schema_version == 6 else {}
            )
            if document.schema_version == 6:
                self._validate_common_session_media(investigation_id, run_id, records)
        except (OSError, UnicodeError, DurableJsonError, ValidationError, ValueError, TypeError):
            raise Phase7ECorruptError from None
        return Phase7ERun(
            path,
            investigation_id,
            run_id,
            document.schema_version,
            envelope,
            state,
            records,
            MappingProxyType(frame_bytes),
        )

    def _read_frame_bytes(
        self, path: Path, records: Sequence[StrictIdentityEnvelope]
    ) -> dict[str, bytes]:
        """Read and fully verify every indexed JPEG owned by a schema-6 run."""
        result: dict[str, bytes] = {}
        for record in records:
            if record.family != "frame":
                continue
            jpeg = path / "frames" / f"{record.identity}.jpg"
            try:
                if (
                    jpeg.is_symlink()
                    or not jpeg.is_file()
                    or not is_safe_contained_path(self.root, jpeg, require_target=True)
                ):
                    raise Phase7ECorruptError
                raw = jpeg.read_bytes()
                _validate_one_frame_bytes(record, raw, Phase7ECorruptError)
            except (OSError, ValueError, UnidentifiedImageError):
                raise Phase7ECorruptError from None
            result[record.identity] = raw
        return result

    def _validate_common_session_media(
        self,
        investigation_id: str,
        run_id: str,
        records: Sequence[StrictIdentityEnvelope],
    ) -> None:
        """Rehash and reprobe retained MP4 bytes when the production media boundary is bound."""
        sessions = [record for record in records if record.family == "common-session"]
        if len(sessions) != 1:
            raise Phase7ECorruptError
        if self.media_root is None and self.media_probe is None:
            return
        if self.media_root is None or self.media_probe is None:
            raise Phase7ECorruptError
        expected_media_root = self.root / ".media"
        if self.media_root != expected_media_root:
            raise Phase7ECorruptError
        session = sessions[0]
        media = self.media_root / investigation_id / run_id / f"{session.identity}.mp4"
        try:
            if (
                self.media_root.is_symlink()
                or not self.media_root.is_dir()
                or not is_safe_contained_path(self.root, self.media_root, require_target=True)
                or media.is_symlink()
                or not media.is_file()
                or not is_safe_contained_path(self.media_root, media, require_target=True)
                or media.stat().st_nlink != 1
                or media.stat().st_size != session.payload["mp4_size_bytes"]
                or _sha256_path(media) != session.payload["mp4_sha256"]
            ):
                raise Phase7ECorruptError
            facts = self.media_probe.probe(media, self.media_probe_timeout_seconds)
        except (OSError, RuntimeError, ValueError, TypeError):
            raise Phase7ECorruptError from None
        expected = {
            "selected_video_stream_index": session.payload["selected_video_stream_index"],
            "container_start_pts": session.payload["container_start_pts"],
            "time_base_num": session.payload["time_base_num"],
            "time_base_den": session.payload["time_base_den"],
            "duration_ticks": session.payload["duration_ticks"],
        }
        if any(getattr(facts, key, None) != value for key, value in expected.items()):
            raise Phase7ECorruptError

    def _reopen_at(
        self, path: Path, investigation_id: str, run_id: str, *, expected_schema: int
    ) -> Phase7ERun:
        return self._reopen_unlocked(
            investigation_id,
            run_id,
            expected_schema=expected_schema,
            path_override=path,
        )

    def _read_children(
        self,
        path: Path,
        schema: int,
        manifest: StrictIdentityEnvelope,
        state: Schema5Envelope | Schema6Envelope,
    ) -> tuple[StrictIdentityEnvelope, ...]:
        allowed = _SCHEMA5_DIRECTORIES if schema == 5 else _SCHEMA6_DIRECTORIES
        entries = tuple(path.iterdir())
        actual_files = {entry.name for entry in entries if entry.is_file()}
        if path.parent.name == ".staging":
            if "manifest.json" not in actual_files or "target.json" not in actual_files:
                raise Phase7ECorruptError
            if actual_files - {"manifest.json", "target.json", "publication.json"}:
                raise Phase7ECorruptError
        elif actual_files != {"manifest.json"}:
            raise Phase7ECorruptError
        if any(
            entry.is_symlink() or not is_safe_contained_path(self.root, entry, require_target=True)
            for entry in entries
        ):
            raise Phase7ECorruptError
        actual_directories = {entry.name for entry in entries if entry.is_dir()}
        if not actual_directories.issubset(allowed):
            raise Phase7ECorruptError
        children: list[StrictIdentityEnvelope] = []
        frame_binary_ids: set[str] = set()
        for directory in sorted(actual_directories):
            if directory == "manifests":
                children.extend(self._read_archives(path / directory, schema, manifest))
                continue
            folder = path / directory
            if any(item.is_dir() or item.is_symlink() for item in folder.iterdir()):
                raise Phase7ECorruptError
            for item in sorted(folder.iterdir(), key=lambda value: value.name):
                if (
                    item.suffix != ".json"
                    or item.stem != item.name[:-5]
                    or not _safe_identity_name(item.stem)
                ):
                    if (
                        schema == 6
                        and directory == "frames"
                        and item.suffix == ".jpg"
                        and _safe_identity_name(item.stem)
                    ):
                        if not is_safe_contained_path(self.root, item, require_target=True):
                            raise Phase7ECorruptError
                        frame_binary_ids.add(item.stem)
                        continue
                    raise Phase7ECorruptError
                if not is_safe_contained_path(self.root, item, require_target=True):
                    raise Phase7ECorruptError
                child = _read_envelope(item)
                expected_directory = _FAMILY_DIRECTORY.get(child.family)
                if expected_directory != directory or child.identity != item.stem:
                    raise Phase7ECorruptError
                children.append(child)
        children.sort(key=lambda value: (_FAMILY_DIRECTORY.get(value.family, ""), value.identity))
        self._validate_membership(schema, manifest, state, children, path, frame_binary_ids)
        try:
            validate_dependency_graph(
                [
                    {"family": child.family, "identity": child.identity, "payload": child.payload}
                    for child in children
                ]
            )
        except Phase7EValidationError as exc:
            raise Phase7ECorruptError from exc
        return tuple(children)

    def _validate_membership(
        self,
        schema: int,
        manifest: StrictIdentityEnvelope,
        state: Schema5Envelope | Schema6Envelope,
        children: Sequence[StrictIdentityEnvelope],
        path: Path,
        frame_binary_ids: set[str],
    ) -> None:
        by_family: dict[str, set[str]] = {}
        for child in children:
            by_family.setdefault(child.family, set()).add(child.identity)
        payload = manifest.payload
        required = {payload["policy_id"]: "policy", payload["plan_id"]: "coarse-plan"}
        for identity, family in required.items():
            if identity not in by_family.get(family, set()):
                raise Phase7ECorruptError
        request_ids = set(payload.get("coarse_target_request_ids", ()))
        if schema == 5:
            if by_family.get("policy", set()) != {payload["policy_id"]}:
                raise Phase7ECorruptError
            if by_family.get("coarse-plan", set()) != {payload["plan_id"]}:
                raise Phase7ECorruptError
            if by_family.get("target-request", set()) != request_ids:
                raise Phase7ECorruptError
            active = (
                state.active_replay_operation_id if isinstance(state, Schema5Envelope) else None
            )
            operations = by_family.get("replay-operation", set())
            if active is None and operations:
                raise Phase7ECorruptError
            if active is not None and operations != {active}:
                raise Phase7ECorruptError
            forbidden = set(by_family) - {
                "policy",
                "coarse-plan",
                "target-request",
                "replay-operation",
            }
            if forbidden:
                raise Phase7ECorruptError
            return
        if not isinstance(state, Schema6Envelope):
            raise Phase7ECorruptError
        indexes = payload.get("indexes")
        if not isinstance(indexes, Mapping) or set(indexes) != set(_SCHEMA6_INDEX_KEYS):
            raise Phase7ECorruptError
        expected_by_family = {
            "target-request": set(indexes["target_request_ids"]),
            "decoder-operation": set(indexes["decoder_operation_ids"]),
            "frame": set(indexes["frame_ids"]),
            "classification-operation": set(indexes["classification_operation_ids"]),
            "observation": set(indexes["observation_ids"]),
            "alias": set(indexes["alias_ids"]),
            "support-group": set(indexes["support_group_ids"]),
            "c2-bracket": set(indexes["c2_bracket_ids"]),
            "d1-input": set(indexes["d1_input_ids"]),
            "d1-history": set(indexes["d1_history_ids"]),
            "narrowed-bracket": set(indexes["narrowed_bracket_ids"]),
        }
        if request_ids and expected_by_family["target-request"] != request_ids:
            raise Phase7ECorruptError
        for family, expected in expected_by_family.items():
            if by_family.get(family, set()) != expected:
                raise Phase7ECorruptError
        active_membership = {
            "active_target_request_id": "target-request",
            "active_decoder_operation_id": "decoder-operation",
            "active_frame_id": "frame",
            "active_classification_operation_id": "classification-operation",
            "active_observation_id": "observation",
        }
        for field_name, family in active_membership.items():
            value = getattr(state, field_name)
            if value is not None and value not in by_family.get(family, set()):
                raise Phase7ECorruptError
        if by_family.get("policy", set()) != {payload["policy_id"]}:
            raise Phase7ECorruptError
        if by_family.get("coarse-plan", set()) != {payload["plan_id"]}:
            raise Phase7ECorruptError
        if by_family.get("classifier-policy", set()) != {payload["classifier_policy_id"]}:
            raise Phase7ECorruptError
        if by_family.get("common-session", set()) != {payload["common_session_id"]}:
            raise Phase7ECorruptError
        if by_family.get("replay-operation", set()) != {payload["replay_operation_id"]}:
            raise Phase7ECorruptError
        if by_family.get("schema5-manifest", set()) != {payload["schema5_predecessor_manifest_id"]}:
            raise Phase7ECorruptError
        for frame_id in expected_by_family["frame"]:
            jpeg = path / "frames" / f"{frame_id}.jpg"
            if frame_id not in frame_binary_ids:
                raise Phase7ECorruptError
            if (
                jpeg.is_symlink()
                or not jpeg.is_file()
                or not is_safe_contained_path(self.root, jpeg, require_target=True)
            ):
                raise Phase7ECorruptError

    def _validate_schema5_proposal(
        self,
        manifest: StrictIdentityEnvelope,
        state: Schema5Envelope,
        children: Sequence[_Child],
    ) -> None:
        if manifest.family != "schema5-manifest" or not isinstance(manifest.payload, Mapping):
            raise Phase7EValidationError("invalid schema-5 manifest")
        _validate_child_families(
            children, {"policy", "coarse-plan", "target-request", "replay-operation"}
        )
        self._validate_manifest_bindings(manifest, children)
        target_ids = set(manifest.payload.get("coarse_target_request_ids", ()))
        actual_targets = {
            child.envelope.identity
            for child in children
            if child.envelope.family == "target-request"
        }
        if target_ids != actual_targets:
            raise Phase7EValidationError("schema-5 request membership mismatch")
        operations = [child for child in children if child.envelope.family == "replay-operation"]
        if state.active_replay_operation_id is None and operations:
            raise Phase7EValidationError("unexpected schema-5 operation")
        if state.active_replay_operation_id is not None and {
            item.envelope.identity for item in operations
        } != {state.active_replay_operation_id}:
            raise Phase7EValidationError("schema-5 operation mismatch")

    def _validate_schema6_proposal(
        self,
        manifest: StrictIdentityEnvelope,
        state: Schema6Envelope,
        children: Sequence[_Child],
        predecessor: Phase7ERun,
    ) -> None:
        if manifest.family != "schema6-manifest" or not isinstance(manifest.payload, Mapping):
            raise Phase7EValidationError("invalid schema-6 manifest")
        payload = manifest.payload
        predecessor_schema5_id = (
            predecessor.manifest_id
            if predecessor.is_schema5
            else predecessor.manifest.payload.get("schema5_predecessor_manifest_id")
        )
        if payload.get("schema5_predecessor_manifest_id") != predecessor_schema5_id:
            raise Phase7EConflictError
        if (
            payload.get("investigation_id") != predecessor.investigation_id
            or payload.get("run_id") != predecessor.run_id
        ):
            raise Phase7EValidationError("foreign schema-6 manifest")
        prior_ids = {
            child.identity for child in predecessor.records if child.family != "schema5-manifest"
        }
        proposed = {child.envelope.identity for child in children}
        if not prior_ids.issubset(proposed):
            raise Phase7EValidationError("schema-6 proposal lost schema-5 children")
        _validate_child_families(children, _SCHEMA6_FAMILIES)
        indexes = payload.get("indexes")
        if not isinstance(indexes, Mapping) or set(indexes) != set(_SCHEMA6_INDEX_KEYS):
            raise Phase7EValidationError("schema-6 indexes are not closed")
        for key in _SCHEMA6_INDEX_KEYS:
            values = indexes[key]
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) for value in values)
                or len(values) != len(set(values))
            ):
                raise Phase7EValidationError("schema-6 indexes are not deterministic")
        self._validate_manifest_bindings(manifest, children)
        if (
            predecessor.is_schema6
            and isinstance(predecessor.state, Schema6Envelope)
            and predecessor.state.target_state
            in {Schema6TargetState.REQUESTED, Schema6TargetState.DECODING}
            and state.target_state is Schema6TargetState.OBSERVED
        ):
            _validate_alias_observation_successor(predecessor, state, children)
        if (
            state.predecessor_target_state is not None
            and state.target_state is Schema6TargetState.REQUESTED
        ):
            if state.predecessor_target_state is not Schema6TargetState.OBSERVED:
                raise Phase7EValidationError("invalid target predecessor")

    def _validate_schema7_proposal(
        self,
        predecessor: Phase7ERun,
        manifest: StrictIdentityEnvelope,
        terminal_children: Sequence[StrictIdentityEnvelope],
    ) -> None:
        """Validate the complete immutable terminal proposal against schema 6."""
        if manifest.family != "schema7-manifest" or predecessor.schema_version != 6:
            raise Phase7EValidationError("invalid schema-7 proposal")
        by_family = {child.family: child for child in terminal_children}
        if set(by_family) != set(_TERMINAL_FILES) or len(by_family) != len(terminal_children):
            raise Phase7EValidationError("invalid schema-7 terminal membership")
        payload = manifest.payload
        source_set = by_family["source-record-set"]
        snapshot = by_family["evidence-snapshot"]
        terminal = by_family["terminal-result"]
        if (
            payload.get("investigation_id") != predecessor.investigation_id
            or payload.get("run_id") != predecessor.run_id
            or payload.get("schema6_predecessor_manifest_id") != predecessor.manifest_id
            or payload.get("source_record_set_id") != source_set.identity
            or payload.get("evidence_snapshot_id") != snapshot.identity
            or payload.get("terminal_result_id") != terminal.identity
        ):
            raise Phase7EConflictError
        for child in terminal_children:
            if (
                child.payload.get("investigation_id") != predecessor.investigation_id
                or child.payload.get("run_id") != predecessor.run_id
            ):
                raise Phase7EValidationError("foreign schema-7 terminal record")
        expected_groups = _source_record_groups(predecessor)
        if (
            source_set.payload.get("schema6_manifest_id") != predecessor.manifest_id
            or source_set.payload.get("record_groups") != expected_groups
            or source_set.payload.get("record_count") != len(predecessor.records)
        ):
            raise Phase7EValidationError("source reconstruction mismatch")
        predecessor_payload = predecessor.manifest.payload
        indexes = predecessor_payload.get("indexes")
        if not isinstance(indexes, Mapping):
            raise Phase7EValidationError("schema-6 indexes are missing")
        narrowed_id = snapshot.payload.get("narrowed_bracket_id")
        if (
            snapshot.payload.get("source_record_set_id") != source_set.identity
            or snapshot.payload.get("policy_id") != predecessor_payload.get("policy_id")
            or snapshot.payload.get("classifier_policy_id")
            != predecessor_payload.get("classifier_policy_id")
            or not _ordered_subset(
                snapshot.payload.get("selected_observation_ids"), indexes["observation_ids"]
            )
            or not _ordered_subset(
                snapshot.payload.get("selected_support_group_ids"), indexes["support_group_ids"]
            )
            or (narrowed_id is not None and narrowed_id not in indexes["narrowed_bracket_ids"])
        ):
            raise Phase7EValidationError("terminal evidence snapshot mismatch")
        if (
            terminal.payload.get("source_record_set_id") != source_set.identity
            or terminal.payload.get("evidence_snapshot_id") != snapshot.identity
            or terminal.payload.get("common_session_id")
            != predecessor_payload.get("common_session_id")
        ):
            raise Phase7EValidationError("terminal result mismatch")
        _validate_schema7_terminal_matrix(predecessor, snapshot, terminal)
        graph = (
            *predecessor.records,
            predecessor.manifest,
            *terminal_children,
            manifest,
        )
        try:
            validate_dependency_graph([item.model_dump(mode="json") for item in graph])
        except Phase7EValidationError as exc:
            raise Phase7EValidationError("invalid schema-7 identity graph") from exc

    def _publish_schema7_successor(
        self,
        current: Phase7ERun,
        manifest: StrictIdentityEnvelope,
        terminal_children: Sequence[StrictIdentityEnvelope],
        *,
        publication_check: Callable[[], None] | None = None,
    ) -> None:
        """Stage all terminal records, then replace the root manifest last."""
        staging = self._new_staging(current.investigation_id, current.run_id)
        created: list[str] = []
        created_directories: list[str] = []
        committed = False
        try:
            if publication_check is not None:
                publication_check()
            self._copy_tree(current.root, staging)
            if publication_check is not None:
                publication_check()
            terminal_dir = staging / "terminal"
            terminal_dir.mkdir()
            archive_path = staging / "manifests" / f"{current.manifest_id}.json"
            archive_bytes = _canonical_json(_manifest_document(current)).encode()
            _write_bytes_no_replace(archive_path, archive_bytes)
            for child in terminal_children:
                if publication_check is not None:
                    publication_check()
                filename = _TERMINAL_FILES[child.family]
                _write_bytes_no_replace(
                    terminal_dir / filename,
                    _canonical_json(child.model_dump(mode="json")).encode(),
                )
            _remove_owned_file(self.root, staging / "manifest.json")
            _write_bytes_no_replace(
                staging / "manifest.json",
                _canonical_json(manifest.model_dump(mode="json")).encode(),
            )
            checked = self._reopen_schema7_unlocked(
                current.investigation_id,
                current.run_id,
                path_override=staging,
            )
            if checked.manifest_id != manifest.identity:
                raise Phase7EReadbackError
            if publication_check is not None:
                publication_check()
            for relative in (
                f"manifests/{current.manifest_id}.json",
                "terminal/source-record-set.json",
                "terminal/evidence-snapshot.json",
                "terminal/result.json",
            ):
                target = current.root / Path(relative)
                if target.exists() or target.is_symlink():
                    raise Phase7EConflictError
                created.append(relative)
            _write_bytes_no_replace(
                staging / "publication.json",
                _canonical_json(
                    {
                        "investigation_id": current.investigation_id,
                        "run_id": current.run_id,
                        "manifest_id": manifest.identity,
                        "paths": created,
                    }
                ).encode(),
            )
            for relative in created:
                if publication_check is not None:
                    publication_check()
                source = staging / Path(relative)
                target = current.root / Path(relative)
                missing: list[Path] = []
                parent = target.parent
                while parent != current.root and not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    created_directories.append(directory.relative_to(current.root).as_posix())
                os.replace(source, target)
                _fsync_directory(target.parent)
            if publication_check is not None:
                publication_check()
            _replace_schema7_manifest(self.root, current.root, manifest)
            committed = True
        finally:
            if not committed:
                _remove_owned_paths(self.root, current.root, created)
                _remove_owned_empty_directories(self.root, current.root, created_directories)
            _remove_owned_directory(self.root, staging)

    def _reopen_schema7_unlocked(
        self,
        investigation_id: str,
        run_id: str,
        *,
        path_override: Path | None = None,
    ) -> Phase7ERun:
        """Strictly reconstruct schema 7 from fixed membership and archives."""
        path = path_override or self.run_path(investigation_id, run_id)
        _validate_schema7_root(self.root, path)
        root_files = {entry.name for entry in path.iterdir() if entry.is_file()}
        expected_root_files = {"manifest.json"}
        if path.parent.name == ".staging":
            expected_root_files.add("target.json")
            if (path / "publication.json").exists():
                expected_root_files.add("publication.json")
        if root_files != expected_root_files:
            raise Phase7ECorruptError
        actual_directories = {entry.name for entry in path.iterdir() if entry.is_dir()}
        if actual_directories != set(_SCHEMA7_DIRECTORIES):
            raise Phase7ECorruptError
        try:
            manifest = _read_envelope(path / "manifest.json")
            if manifest.family != "schema7-manifest":
                raise Phase7ECorruptError
            if (
                manifest.payload.get("investigation_id") != investigation_id
                or manifest.payload.get("run_id") != run_id
            ):
                raise Phase7ECorruptError
            schema6_manifest, schema6_state, schema5_manifest = _read_schema7_archives(
                self.root, path, manifest
            )
            source_records, frame_binary_ids = _read_schema7_source_records(
                self.root, path, schema5_manifest
            )
            self._validate_membership(
                6,
                schema6_manifest,
                schema6_state,
                source_records,
                path,
                frame_binary_ids,
            )
            validate_dependency_graph([record.model_dump(mode="json") for record in source_records])
            frame_bytes = self._read_frame_bytes(path, source_records)
            self._validate_common_session_media(investigation_id, run_id, source_records)
            terminal_children = _read_terminal_children(self.root, path, manifest)
            predecessor = Phase7ERun(
                path,
                investigation_id,
                run_id,
                6,
                schema6_manifest,
                schema6_state,
                source_records,
                MappingProxyType(frame_bytes),
            )
            self._validate_schema7_proposal(predecessor, manifest, terminal_children)
        except Phase7ERepositoryError:
            raise
        except (
            OSError,
            UnicodeError,
            DurableJsonError,
            ValidationError,
            ValueError,
            TypeError,
            IdentityValidationError,
            Phase7EValidationError,
        ):
            raise Phase7ECorruptError from None
        return Phase7ERun(
            path,
            investigation_id,
            run_id,
            7,
            manifest,
            schema6_state,
            (*source_records, schema6_manifest, *terminal_children),
            MappingProxyType(frame_bytes),
        )

    def _validate_manifest_bindings(
        self, manifest: StrictIdentityEnvelope, children: Sequence[_Child]
    ) -> None:
        ids = {child.envelope.identity: child.envelope.family for child in children}
        for key, value in manifest.payload.items():
            if key.endswith("_id") and isinstance(value, str) and value.startswith("rr-"):
                try:
                    family = family_from_identity(value)
                except IdentityValidationError as exc:
                    raise Phase7EValidationError("invalid manifest reference") from exc
                if key == "schema5_predecessor_manifest_id" and family == "schema5-manifest":
                    continue
                if family in _FAMILY_DIRECTORY and value not in ids:
                    raise Phase7EValidationError("missing manifest reference")

    def _ensure_tree_directories(self, directory: Path, schema: int) -> None:
        allowed = _SCHEMA5_DIRECTORIES if schema == 5 else _SCHEMA6_DIRECTORIES
        directory.mkdir(parents=True, exist_ok=True)
        for name in allowed:
            child = directory / name
            if child.exists() and (child.is_symlink() or not child.is_dir()):
                raise Phase7ECorruptError
            child.mkdir(exist_ok=True)
            if not is_safe_contained_path(self.root, child, require_target=True):
                raise Phase7ECorruptError

    def _read_archives(
        self, directory: Path, schema: int, manifest: StrictIdentityEnvelope
    ) -> list[StrictIdentityEnvelope]:
        archives: list[StrictIdentityEnvelope] = []
        for item in directory.iterdir():
            if (
                item.is_symlink()
                or item.is_dir()
                or item.suffix != ".json"
                or not _safe_identity_name(item.stem)
            ):
                raise Phase7ECorruptError
            expected = "schema5-manifest" if schema == 6 else "schema6-manifest"
            try:
                document = _parse_document(item.read_text(encoding="utf-8"))
                archived = _coerce_envelope(document.manifest, expected)
                if document.schema_version != 5:
                    raise Phase7ECorruptError
                archived_state = validate_schema5_state(document.state)
                if archived_state.phase_state is not Schema5PhaseState.ACQUIRED:
                    raise Phase7ECorruptError
            except (
                OSError,
                UnicodeError,
                DurableJsonError,
                ValidationError,
                ValueError,
                TypeError,
            ):
                raise Phase7ECorruptError from None
            if archived.family != expected or archived.identity != item.stem:
                raise Phase7ECorruptError
            _validate_manifest_binding(
                archived,
                archived_state,
                manifest.payload["investigation_id"],
                manifest.payload["run_id"],
                5,
            )
            if archived_state.active_replay_operation_id != manifest.payload.get(
                "replay_operation_id"
            ):
                raise Phase7ECorruptError
            if item.stem != manifest.payload.get("schema5_predecessor_manifest_id", item.stem):
                if schema == 6:
                    raise Phase7ECorruptError
            archives.append(archived)
        return archives

    def _clean_owned_staging(self, investigation_id: str, run_id: str) -> None:
        staging_root = self.root / ".staging"
        if not staging_root.exists():
            return
        for directory in tuple(staging_root.iterdir()):
            marker = directory / "target.json"
            try:
                if (
                    not directory.is_dir()
                    or directory.is_symlink()
                    or not is_safe_contained_path(self.root, directory, require_target=True)
                    or not marker.is_file()
                    or marker.is_symlink()
                    or not is_safe_contained_path(self.root, marker, require_target=True)
                ):
                    continue
                data = strict_json_loads(marker.read_text(encoding="utf-8"))
                if data.get("investigation_id") != investigation_id or data.get("run_id") != run_id:
                    continue
                journal = directory / "publication.json"
                if journal.is_file() and not journal.is_symlink():
                    publication = strict_json_loads(journal.read_text(encoding="utf-8"))
                    if (
                        publication.get("investigation_id") == investigation_id
                        and publication.get("run_id") == run_id
                    ):
                        current_identity = _read_manifest_identity(
                            self.run_path(investigation_id, run_id)
                        )
                        if current_identity != publication.get("manifest_id"):
                            paths = publication.get("paths")
                            if isinstance(paths, list) and all(
                                isinstance(item, str) for item in paths
                            ):
                                _remove_owned_paths(
                                    self.root, self.run_path(investigation_id, run_id), paths
                                )
                _remove_owned_directory(self.root, directory)
            except (OSError, UnicodeError, ValueError, IdentityValidationError):
                continue


def _coerce_terminal_child(value: object, family: str) -> StrictIdentityEnvelope:
    envelope = _coerce_any_envelope(value)
    if envelope.family != family:
        raise Phase7EValidationError("invalid terminal record family")
    return envelope


def _is_schema7_root(run_path: Path) -> bool:
    manifest_path = run_path / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return False
    try:
        value = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, DurableJsonError, IdentityValidationError):
        return False
    return value.get("family") == "schema7-manifest"


def _same_schema7_proposal(
    current: Phase7ERun,
    manifest: StrictIdentityEnvelope,
    terminal_children: Sequence[StrictIdentityEnvelope],
) -> bool:
    if current.manifest_id != manifest.identity:
        return False
    expected = {(item.family, item.identity) for item in terminal_children}
    actual = {
        (item.family, item.identity) for item in current.records if item.family in _TERMINAL_FILES
    }
    return actual == expected


def _validate_schema7_root(root: Path, path: Path) -> None:
    try:
        if (
            path.is_symlink()
            or not path.is_dir()
            or not is_safe_contained_path(root, path, require_target=True)
        ):
            raise Phase7ECorruptError
        for entry in path.iterdir():
            if entry.is_symlink() or not is_safe_contained_path(root, entry, require_target=True):
                raise Phase7ECorruptError
    except OSError:
        raise Phase7ECorruptError from None


def _read_schema7_archives(
    root: Path,
    path: Path,
    manifest: StrictIdentityEnvelope,
) -> tuple[StrictIdentityEnvelope, Schema6Envelope, StrictIdentityEnvelope]:
    directory = path / "manifests"
    schema6_id = manifest.payload["schema6_predecessor_manifest_id"]
    schema6_path = directory / f"{schema6_id}.json"
    try:
        schema6_document = _parse_document(schema6_path.read_text(encoding="utf-8"))
        schema6_manifest = _coerce_envelope(schema6_document.manifest, "schema6-manifest")
        schema6_state = validate_schema6_state(schema6_document.state)
        schema5_id = schema6_manifest.payload["schema5_predecessor_manifest_id"]
        schema5_path = directory / f"{schema5_id}.json"
        schema5_document = _parse_document(schema5_path.read_text(encoding="utf-8"))
        schema5_manifest = _coerce_envelope(schema5_document.manifest, "schema5-manifest")
        schema5_state = validate_schema5_state(schema5_document.state)
        actual = {entry.name for entry in directory.iterdir()}
        if actual != {schema6_path.name, schema5_path.name}:
            raise Phase7ECorruptError
        if any(
            entry.is_symlink()
            or not entry.is_file()
            or not is_safe_contained_path(root, entry, require_target=True)
            for entry in directory.iterdir()
        ):
            raise Phase7ECorruptError
        if (
            schema6_document.schema_version != 6
            or schema6_manifest.identity != schema6_id
            or schema6_state.run_state != "RUNNING"
            or schema6_state.target_state is not Schema6TargetState.OBSERVED
            or schema5_document.schema_version != 5
            or schema5_manifest.identity != schema5_id
            or schema5_state.phase_state is not Schema5PhaseState.ACQUIRED
            or schema5_state.active_replay_operation_id
            != schema6_manifest.payload["replay_operation_id"]
        ):
            raise Phase7ECorruptError
        _validate_manifest_binding(
            schema6_manifest,
            schema6_state,
            manifest.payload["investigation_id"],
            manifest.payload["run_id"],
            6,
        )
        _validate_manifest_binding(
            schema5_manifest,
            schema5_state,
            manifest.payload["investigation_id"],
            manifest.payload["run_id"],
            5,
        )
    except Phase7ERepositoryError:
        raise
    except (
        OSError,
        UnicodeError,
        DurableJsonError,
        ValidationError,
        ValueError,
        TypeError,
        KeyError,
    ):
        raise Phase7ECorruptError from None
    return schema6_manifest, schema6_state, schema5_manifest


def _read_schema7_source_records(
    root: Path,
    path: Path,
    schema5_manifest: StrictIdentityEnvelope,
) -> tuple[tuple[StrictIdentityEnvelope, ...], set[str]]:
    records: list[StrictIdentityEnvelope] = [schema5_manifest]
    frame_binary_ids: set[str] = set()
    for directory_name in sorted(_SCHEMA6_DIRECTORIES - {"manifests"}):
        directory = path / directory_name
        try:
            entries = tuple(directory.iterdir())
        except OSError:
            raise Phase7ECorruptError from None
        if any(
            entry.is_symlink()
            or entry.is_dir()
            or not is_safe_contained_path(root, entry, require_target=True)
            for entry in entries
        ):
            raise Phase7ECorruptError
        for item in sorted(entries, key=lambda entry: entry.name):
            if (
                directory_name == "frames"
                and item.suffix == ".jpg"
                and _safe_identity_name(item.stem)
            ):
                frame_binary_ids.add(item.stem)
                continue
            if item.suffix != ".json" or not _safe_identity_name(item.stem):
                raise Phase7ECorruptError
            child = _read_envelope(item)
            if _FAMILY_DIRECTORY.get(child.family) != directory_name or child.identity != item.stem:
                raise Phase7ECorruptError
            records.append(child)
    records.sort(key=lambda item: (_FAMILY_DIRECTORY.get(item.family, "manifests"), item.identity))
    return tuple(records), frame_binary_ids


def _read_terminal_children(
    root: Path,
    path: Path,
    manifest: StrictIdentityEnvelope,
) -> tuple[StrictIdentityEnvelope, ...]:
    directory = path / "terminal"
    expected_names = set(_TERMINAL_FILES.values())
    try:
        entries = tuple(directory.iterdir())
        if {entry.name for entry in entries} != expected_names:
            raise Phase7ECorruptError
        if any(
            entry.is_symlink()
            or not entry.is_file()
            or not is_safe_contained_path(root, entry, require_target=True)
            for entry in entries
        ):
            raise Phase7ECorruptError
        children = tuple(
            _read_envelope(directory / filename)
            for filename in (
                "source-record-set.json",
                "evidence-snapshot.json",
                "result.json",
            )
        )
    except OSError:
        raise Phase7ECorruptError from None
    expected_ids = {
        "source-record-set": manifest.payload["source_record_set_id"],
        "evidence-snapshot": manifest.payload["evidence_snapshot_id"],
        "terminal-result": manifest.payload["terminal_result_id"],
    }
    if any(child.identity != expected_ids.get(child.family) for child in children):
        raise Phase7ECorruptError
    return children


def _source_record_groups(run: Phase7ERun) -> list[dict[str, object]]:
    payload = run.manifest.payload
    indexes = payload["indexes"]
    schema5 = next(record for record in run.records if record.family == "schema5-manifest")
    groups = [
        ("policy", [payload["policy_id"]]),
        ("classifier_policy", [payload["classifier_policy_id"]]),
        ("schema5_manifest", [schema5.identity]),
        ("coarse_plan", [payload["plan_id"]]),
        ("replay_operation", [payload["replay_operation_id"]]),
        ("common_session", [payload["common_session_id"]]),
        ("target_requests", list(indexes["target_request_ids"])),
        ("decoder_operations", list(indexes["decoder_operation_ids"])),
        ("frames", list(indexes["frame_ids"])),
        ("classification_operations", list(indexes["classification_operation_ids"])),
        ("observations", list(indexes["observation_ids"])),
        ("aliases", list(indexes["alias_ids"])),
        ("support_groups", list(indexes["support_group_ids"])),
        ("c2_brackets", list(indexes["c2_bracket_ids"])),
        ("d1_inputs", list(indexes["d1_input_ids"])),
        ("d1_histories", list(indexes["d1_history_ids"])),
        ("narrowed_brackets", list(indexes["narrowed_bracket_ids"])),
    ]
    return [{"type": name, "ids": ids} for name, ids in groups]


def _ordered_subset(value: object, authoritative: object) -> bool:
    return (
        isinstance(value, list)
        and isinstance(authoritative, list)
        and len(value) == len(set(value))
        and all(item in authoritative for item in value)
    )


def _record_map(run: Phase7ERun) -> dict[str, StrictIdentityEnvelope]:
    return {record.identity: record for record in run.records}


def _validate_schema7_terminal_matrix(
    predecessor: Phase7ERun,
    snapshot: StrictIdentityEnvelope,
    terminal: StrictIdentityEnvelope,
) -> None:
    records = _record_map(predecessor)
    kind = terminal.payload["result_kind"]
    reason = terminal.payload["reason_code"]
    start = terminal.payload["interval_start_requested_time_utc"]
    end = terminal.payload["interval_end_requested_time_utc"]
    narrowed_id = snapshot.payload["narrowed_bracket_id"]
    selected_observations = snapshot.payload["selected_observation_ids"]
    selected_support = snapshot.payload["selected_support_group_ids"]
    observations = [records[item] for item in selected_observations]
    if any(item.family != "observation" for item in observations):
        raise Phase7EValidationError("invalid terminal observation reference")
    if kind == "FOUND":
        if (
            reason != "SUPPORTED_TRANSITION"
            or start is None
            or end is None
            or narrowed_id is None
            or len(selected_support) != 1
            or len(selected_observations) < 2
        ):
            raise Phase7EValidationError("invalid FOUND terminal matrix")
        narrowed = records.get(narrowed_id)
        support = records.get(selected_support[0])
        if narrowed is None or support is None:
            raise Phase7EValidationError("missing FOUND evidence")
        if (
            narrowed.payload["interval_start_requested_time_utc"] != start
            or narrowed.payload["interval_end_requested_time_utc"] != end
            or narrowed.payload["stop_reason"] != "TARGET_PRECISION_REACHED"
            or narrowed.payload["lower_observation_id"] not in selected_observations
            or narrowed.payload["upper_observation_id"] not in selected_observations
            or narrowed.payload["upper_support_group_id"] != support.identity
        ):
            raise Phase7EValidationError("FOUND bracket mismatch")
        _validate_distinct_absence_support(predecessor, support)
        lower = records[narrowed.payload["lower_observation_id"]]
        upper = records[narrowed.payload["upper_observation_id"]]
        if lower.payload["outcome"] != "PRESENT" or upper.payload["outcome"] != "ABSENT":
            raise Phase7EValidationError("FOUND transition is not supported")
    elif kind == "NOT_FOUND":
        if (
            reason != "COMPLETE_PRESENT_GRID"
            or start is not None
            or end is not None
            or narrowed_id is not None
            or selected_support
            or not observations
            or any(item.payload["outcome"] != "PRESENT" for item in observations)
        ):
            raise Phase7EValidationError("invalid NOT_FOUND terminal matrix")
    elif kind == "INCONCLUSIVE":
        if (
            reason
            not in {
                "BASELINE_ONLY_LOWER_BOUND",
                "VISUAL_INDETERMINATE",
                "INCOMPLETE_VISUAL_EVIDENCE",
            }
            or start is not None
            or end is not None
        ):
            raise Phase7EValidationError("invalid INCONCLUSIVE terminal matrix")
        if any(
            item.payload.get("classifier_evidence", {}).get("visual_status") == "unusable"
            for item in observations
        ):
            raise Phase7EValidationError("operational evidence cannot terminalize visually")
    else:
        raise Phase7EValidationError("invalid terminal kind")


def _validate_distinct_absence_support(run: Phase7ERun, support: StrictIdentityEnvelope) -> None:
    records = _record_map(run)
    frame_ids = support.payload["member_frame_ids"]
    observation_ids = support.payload["member_observation_ids"]
    target_ids = support.payload["member_target_request_ids"]
    if (
        len(frame_ids) != 3
        or len(set(frame_ids)) != 3
        or len(set(observation_ids)) != 3
        or len(set(target_ids)) != 3
    ):
        raise Phase7EValidationError("absence support is not distinct")
    frames = [records[item] for item in frame_ids]
    observations = [records[item] for item in observation_ids]
    session_ids = {item.payload["common_session_id"] for item in frames}
    rgb_digests = {item.payload["rgb24_sha256"] for item in frames}
    if (
        len(session_ids) != 1
        or len(rgb_digests) != 3
        or any(item.payload["outcome"] != "ABSENT" for item in observations)
        or any(
            item.payload["classifier_evidence"]["visual_status"] != "comparable"
            for item in observations
        )
        or any(
            frames[index - 1].payload["raw_pts"] >= frames[index].payload["raw_pts"]
            or frames[index - 1].payload["ordinal"] >= frames[index].payload["ordinal"]
            for index in range(1, len(frames))
        )
    ):
        raise Phase7EValidationError("invalid absence support")


def _replace_schema7_manifest(root: Path, run_path: Path, manifest: StrictIdentityEnvelope) -> None:
    path = run_path / "manifest.json"
    temporary = path.with_name(f".manifest-{manifest.identity}.tmp")
    _write_bytes_no_replace(temporary, _canonical_json(manifest.model_dump(mode="json")).encode())
    try:
        os.replace(temporary, path)
        _fsync_directory(run_path)
    except OSError:
        _remove_owned_file(root, temporary)
        raise Phase7ERepositoryError from None


_SCHEMA6_FAMILIES = frozenset(_FAMILY_DIRECTORY) | {"schema5-manifest"}


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _coerce_envelope(value: object, expected_family: str) -> StrictIdentityEnvelope:
    if isinstance(value, StrictIdentityEnvelope):
        result = value
    elif hasattr(value, "family") and hasattr(value, "payload"):
        result = StrictIdentityEnvelope.from_payload(value.family, dict(value.payload))
    elif isinstance(value, Mapping):
        try:
            if set(value) == {"family", "identity", "payload"}:
                result = StrictIdentityEnvelope.model_validate(value)
            else:
                result = StrictIdentityEnvelope.from_payload(expected_family, dict(value))
        except (IdentityValidationError, ValidationError, TypeError, ValueError) as exc:
            raise Phase7EValidationError("invalid identity envelope") from exc
    else:
        raise Phase7EValidationError("invalid identity envelope")
    if result.family != expected_family:
        raise Phase7EValidationError("identity family mismatch")
    return result


def _coerce_children(records: Iterable[object] | Mapping[str, object]) -> tuple[_Child, ...]:
    values: list[object]
    if isinstance(records, Mapping):
        values = list(records.values())
    else:
        values = list(records)
    children: list[_Child] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, _Child):
            child = value
        else:
            envelope = _coerce_any_envelope(value)
            directory = _FAMILY_DIRECTORY.get(envelope.family)
            if directory is None and envelope.family != "schema5-manifest":
                raise Phase7EValidationError("unsupported child family")
            if directory is None:
                directory = "manifests"
            child = _Child(envelope, directory)
        if child.envelope.identity in seen:
            raise Phase7EValidationError("duplicate child identity")
        seen.add(child.envelope.identity)
        children.append(child)
    children.sort(key=lambda value: (value.relative_directory, value.envelope.identity))
    return tuple(children)


def _coerce_any_envelope(value: object) -> StrictIdentityEnvelope:
    if isinstance(value, StrictIdentityEnvelope):
        return value
    if hasattr(value, "family") and hasattr(value, "payload"):
        return StrictIdentityEnvelope.from_payload(value.family, dict(value.payload))
    if isinstance(value, Mapping) and set(value) == {"family", "identity", "payload"}:
        try:
            return StrictIdentityEnvelope.model_validate(value)
        except ValidationError as exc:
            raise Phase7EValidationError("invalid identity envelope") from exc
    raise Phase7EValidationError("child must be a typed identity envelope")


def _manifest_ids(
    envelope: StrictIdentityEnvelope, investigation_id: str | None, run_id: str | None
) -> tuple[str, str]:
    payload = envelope.payload
    actual_inv = payload.get("investigation_id")
    actual_run = payload.get("run_id")
    if not isinstance(actual_inv, str) or not isinstance(actual_run, str):
        raise Phase7EValidationError("manifest identity binding missing")
    if investigation_id is not None and investigation_id != actual_inv:
        raise Phase7EValidationError("foreign investigation")
    if run_id is not None and run_id != actual_run:
        raise Phase7EValidationError("foreign run")
    if not _safe_component(actual_inv) or not _safe_component(actual_run):
        raise Phase7EValidationError("unsafe run identity")
    return actual_inv, actual_run


def _validate_child_families(
    children: Sequence[_Child], allowed: set[str] | frozenset[str]
) -> None:
    for child in children:
        if child.envelope.family not in allowed:
            raise Phase7EValidationError("unsupported child family")
        if child.envelope.family == "schema5-manifest" and child.relative_directory == "manifests":
            continue
        if _FAMILY_DIRECTORY.get(child.envelope.family) != child.relative_directory:
            raise Phase7EValidationError("child directory mismatch")


def _manifest_binding_values(manifest: StrictIdentityEnvelope) -> set[str]:
    values: set[str] = set()
    for key, value in manifest.payload.items():
        if key.endswith("_id") and isinstance(value, str) and value.startswith("rr-"):
            values.add(value)
        elif key.endswith("_ids") and isinstance(value, list):
            values.update(item for item in value if isinstance(item, str))
    return values


def _validate_manifest_binding(
    envelope: StrictIdentityEnvelope, state: object, investigation_id: str, run_id: str, schema: int
) -> None:
    expected = "schema5-manifest" if schema == 5 else "schema6-manifest"
    if envelope.family != expected:
        raise Phase7ECorruptError
    payload = envelope.payload
    if payload.get("investigation_id") != investigation_id or payload.get("run_id") != run_id:
        raise Phase7ECorruptError
    if schema == 5 and not isinstance(state, Schema5Envelope):
        raise Phase7ECorruptError
    if schema == 6 and not isinstance(state, Schema6Envelope):
        raise Phase7ECorruptError


def _parse_document(raw: str) -> _ManifestDocument:
    value = strict_json_loads(raw)
    return _ManifestDocument.model_validate(value, strict=True)


def _read_envelope(path: Path) -> StrictIdentityEnvelope:
    if path.is_symlink() or not path.is_file():
        raise Phase7ECorruptError
    try:
        return StrictIdentityEnvelope.model_validate(
            strict_json_loads(path.read_text(encoding="utf-8")), strict=True
        )
    except (
        OSError,
        UnicodeError,
        IdentityValidationError,
        ValidationError,
        ValueError,
        TypeError,
    ) as exc:
        raise Phase7ECorruptError from exc


def _manifest_document(run: Phase7ERun) -> dict[str, Any]:
    if not isinstance(run.state, (Schema5Envelope, Schema6Envelope)):
        raise Phase7ECorruptError
    return {
        "schema_version": run.schema_version,
        "manifest": run.manifest.model_dump(mode="json"),
        "state": run.state.model_dump(mode="json"),
    }


def _canonical_json(value: Mapping[str, Any]) -> str:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, separators=(",", ": "))
        + "\n"
    )


def _write_bytes_no_replace(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise Phase7EConflictError from None
    except OSError:
        raise Phase7ERepositoryError from None


def _remove_owned_file(root: Path, path: Path) -> None:
    with suppress(OSError):
        if is_safe_contained_path(root, path, require_target=True) and not path.is_symlink():
            path.unlink()


def _remove_owned_directory(root: Path, path: Path | None) -> None:
    if path is None:
        return
    with suppress(OSError):
        if (
            not path.exists()
            or path.is_symlink()
            or not is_safe_contained_path(root, path, require_target=True)
        ):
            return
        for item in path.rglob("*"):
            if item.is_symlink() or not is_safe_contained_path(root, item, require_target=True):
                return
        shutil.rmtree(path)


def _remove_owned_paths(root: Path, run_path: Path, paths: Iterable[str]) -> None:
    """Remove only journaled files still confined to the owning run."""
    for relative in paths:
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            continue
        target = run_path / Path(relative)
        if is_safe_contained_path(root, target, require_target=True) and not target.is_symlink():
            with suppress(OSError):
                if target.is_file():
                    target.unlink()


def _remove_owned_empty_directories(root: Path, run_path: Path, paths: Iterable[str]) -> None:
    """Remove only empty directories created by one failed publication."""
    for relative in sorted(paths, key=lambda item: item.count("/"), reverse=True):
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            continue
        target = run_path / Path(relative)
        if not is_safe_contained_path(root, target, require_target=True) or target.is_symlink():
            continue
        with suppress(OSError):
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory durability barrier on supported platforms."""
    with suppress(OSError):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _read_manifest_identity(run_path: Path) -> str | None:
    """Read only the current envelope identity for journal recovery."""
    manifest = run_path / "manifest.json"
    try:
        if manifest.is_symlink() or not manifest.is_file():
            return None
        document = _parse_document(manifest.read_text(encoding="utf-8"))
        value = document.manifest.get("identity")
        return value if isinstance(value, str) else None
    except (OSError, UnicodeError, DurableJsonError, ValidationError, ValueError, TypeError):
        return None


def _safe_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\0" not in value
    )


def _safe_identity_name(value: str) -> bool:
    return (
        bool(value)
        and value.startswith("rr-")
        and value.count("-") >= 3
        and all(character.isalnum() or character == "-" for character in value)
    )


def _run_fingerprint(run: Phase7ERun) -> tuple[Any, ...]:
    return (
        run.schema_version,
        run.manifest.identity,
        run.state,
        tuple((child.family, child.identity, child.payload) for child in run.records),
    )


def _same_child_set(current: Sequence[StrictIdentityEnvelope], proposed: Sequence[_Child]) -> bool:
    """Compare complete non-archive membership for an idempotent retry."""
    left = {
        (child.family, child.identity, json.dumps(child.payload, sort_keys=True))
        for child in current
        if child.family != "schema5-manifest"
    }
    right = {
        (
            child.envelope.family,
            child.envelope.identity,
            json.dumps(child.envelope.payload, sort_keys=True),
        )
        for child in proposed
        if child.envelope.family != "schema5-manifest"
    }
    return left == right


def _proposal_fingerprint(
    schema: int, manifest: StrictIdentityEnvelope, state: object, children: Sequence[_Child]
) -> tuple[Any, ...]:
    return (
        schema,
        manifest.identity,
        state,
        tuple(
            (child.envelope.family, child.envelope.identity, child.envelope.payload)
            for child in children
        ),
    )


def _validate_frame_bytes(
    children: Sequence[_Child], binary: Mapping[str, bytes], *, required_ids: set[str]
) -> None:
    frame_ids = {child.envelope.identity for child in children if child.envelope.family == "frame"}
    if set(binary) - frame_ids:
        raise Phase7EValidationError("foreign frame bytes")
    if required_ids - set(binary):
        raise Phase7EValidationError("frame bytes are required for frame admission")
    for child in children:
        if child.envelope.family != "frame" or child.envelope.identity not in binary:
            continue
        raw = bytes(binary[child.envelope.identity])
        _validate_one_frame_bytes(child.envelope, raw, Phase7EValidationError)


def _validate_one_frame_bytes(
    frame: StrictIdentityEnvelope,
    raw: bytes,
    error_type: type[Phase7EValidationError | Phase7ECorruptError],
) -> None:
    """Prove one JPEG's bytes, structure, dimensions, and decoded RGB24 digest."""
    payload = frame.payload
    if (
        type(raw) is not bytes
        or len(raw) != payload.get("jpeg_size_bytes")
        or hashlib.sha256(raw).hexdigest() != payload.get("jpeg_sha256")
    ):
        raise error_type("frame bytes do not match metadata")
    try:
        with Image.open(BytesIO(raw)) as image:
            image.load()
            if (
                image.format != "JPEG"
                or image.mode != "RGB"
                or image.size
                != (
                    payload.get("width"),
                    payload.get("height"),
                )
            ):
                raise error_type("frame JPEG structure does not match metadata")
            rgb24 = image.tobytes()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise error_type("frame JPEG is not decodable") from exc
    if hashlib.sha256(rgb24).hexdigest() != payload.get("rgb24_sha256"):
        raise error_type("frame JPEG pixels do not match metadata")


def _legal_schema5_successor(current: Schema5Envelope, proposed: Schema5Envelope) -> bool:
    allowed = {
        Schema5PhaseState.PLANNED: {Schema5PhaseState.ACQUIRING},
        Schema5PhaseState.ACQUIRING: {
            Schema5PhaseState.ACQUIRED,
            Schema5PhaseState.ACQUISITION_FAILED,
            Schema5PhaseState.INTERRUPTED,
        },
    }
    return proposed.phase_state in allowed.get(current.phase_state, set())


def _legal_schema6_successor(current: Schema6Envelope, proposed: Schema6Envelope) -> bool:
    if proposed.predecessor_target_state is not current.target_state:
        return False
    allowed = {
        Schema6TargetState.REQUESTED: {
            Schema6TargetState.DECODING,
            Schema6TargetState.OBSERVED,
        },
        Schema6TargetState.DECODING: {
            Schema6TargetState.FRAME_READY,
            Schema6TargetState.ACQUISITION_FAILED,
            Schema6TargetState.INTERRUPTED,
            Schema6TargetState.OBSERVED,
        },
        Schema6TargetState.FRAME_READY: {
            Schema6TargetState.CLASSIFYING,
            Schema6TargetState.INTERRUPTED,
        },
        Schema6TargetState.CLASSIFYING: {
            Schema6TargetState.OBSERVED,
            Schema6TargetState.CLASSIFICATION_FAILED,
            Schema6TargetState.INTERRUPTED,
        },
        Schema6TargetState.OBSERVED: {Schema6TargetState.REQUESTED},
    }
    return proposed.target_state in allowed.get(current.target_state, set())


def _legal_schema6_analysis_append(  # noqa: PLR0911 - fail-closed append checks.
    current: Phase7ERun,
    proposed_manifest: StrictIdentityEnvelope,
    proposed_state: Schema6Envelope,
    proposed_children: Sequence[_Child],
) -> bool:
    """Allow only immutable C2/D1 analysis records after an observed target."""
    if (
        not isinstance(current.state, Schema6Envelope)
        or current.state.target_state is not Schema6TargetState.OBSERVED
        or proposed_state != current.state
    ):
        return False
    current_indexes = current.manifest.payload.get("indexes")
    proposed_indexes = proposed_manifest.payload.get("indexes")
    if not isinstance(current_indexes, Mapping) or not isinstance(proposed_indexes, Mapping):
        return False
    analysis_indexes = {
        "support_group_ids": "support-group",
        "c2_bracket_ids": "c2-bracket",
        "d1_input_ids": "d1-input",
        "d1_history_ids": "d1-history",
        "narrowed_bracket_ids": "narrowed-bracket",
    }
    current_ids = {item.identity for item in current.records if item.family != "schema5-manifest"}
    proposed = {item.envelope.identity: item.envelope for item in proposed_children}
    additions = tuple(item for identity, item in proposed.items() if identity not in current_ids)
    if not additions or any(item.family not in analysis_indexes.values() for item in additions):
        return False
    if not current_ids.issubset(proposed):
        return False
    for key in _SCHEMA6_INDEX_KEYS:
        before = current_indexes.get(key)
        after = proposed_indexes.get(key)
        if not isinstance(before, list) or not isinstance(after, list):
            return False
        if key not in analysis_indexes:
            if after != before:
                return False
            continue
        appended = [item.identity for item in additions if item.family == analysis_indexes[key]]
        if after != [*before, *appended]:
            return False
    return True


def _validate_alias_observation_successor(
    predecessor: Phase7ERun,
    proposed_state: Schema6Envelope,
    children: Sequence[_Child],
) -> None:
    """Allow direct REQUESTED→OBSERVED only for one proven frame alias reuse."""
    if not isinstance(predecessor.state, Schema6Envelope):
        raise Phase7EValidationError("alias predecessor is not schema 6")
    previous = {item.identity: item for item in predecessor.records}
    proposed = {item.envelope.identity: item.envelope for item in children}
    additions = [item for identity, item in proposed.items() if identity not in previous]
    aliases = [item for item in additions if item.family == "alias"]
    decoders = [item for item in additions if item.family == "decoder-operation"]
    if len(aliases) != 1 or len(decoders) > 1 or len(additions) != 1 + len(decoders):
        raise Phase7EValidationError("alias reuse has unexpected evidence")
    alias = aliases[0]
    decoder = proposed.get(str(proposed_state.active_decoder_operation_id))
    target_id = predecessor.state.active_target_request_id
    frame = previous.get(str(proposed_state.active_frame_id))
    observation = previous.get(str(proposed_state.active_observation_id))
    operation = previous.get(str(proposed_state.active_classification_operation_id))
    if (
        target_id is None
        or alias.payload.get("target_request_id") != target_id
        or alias.payload.get("frame_id") != proposed_state.active_frame_id
        or decoder is None
        or decoder.family != "decoder-operation"
        or decoder.identity != proposed_state.active_decoder_operation_id
        or target_id not in decoder.payload.get("target_request_ids", [])
        or frame is None
        or frame.family != "frame"
        or observation is None
        or observation.family != "observation"
        or operation is None
        or operation.family != "classification-operation"
        or observation.payload.get("frame_id") != frame.identity
        or observation.payload.get("classification_operation_id") != operation.identity
        or operation.payload.get("frame_id") != frame.identity
        or alias.payload.get("alias_of_target_request_id")
        != observation.payload.get("target_request_id")
    ):
        raise Phase7EValidationError("alias reuse is not authoritative")


__all__ = [
    "Phase7ECorruptError",
    "Phase7EConflictError",
    "Phase7EInProgressError",
    "Phase7ENotFoundError",
    "Phase7EReadbackError",
    "Phase7ERepositoryError",
    "Phase7ERun",
    "PublicationResult",
    "PublicationStatus",
    "RecordingSearch7ERepository",
]
