"""Strict schema-2 parsing and owned child publication helpers."""

from __future__ import annotations

import json
import os
import shutil
from contextlib import suppress
from datetime import timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from vigi_vision.durable_io import (
    DurableJsonError,
    is_safe_contained_path,
    load_durable_json_object,
)
from vigi_vision.investigation_confirmation_integrity import compute_jpeg_integrity
from vigi_vision.investigation_confirmation_models import ConfirmationArtifactError
from vigi_vision.recording_search_a2_models import (
    AcquisitionOperationRecord,
    CanonicalProbeFrameRecord,
    ProbeFrameRequestRecord,
    ProbeRequestStatus,
    RecordingSearchManifestV2,
    acquisition_id_for,
)
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_models import (
    RecordingSearchArtifactError,
    RecordingSearchManifestCorruptError,
    RecordingSearchState,
    RecordingSearchTransitionError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime


_MANIFEST_NAME: Final = "manifest.json"
_OPERATION_DIR: Final = "operations"
_FRAME_DIR: Final = "frames"
_REQUEST_DIR: Final = "requests"
_EVIDENCE_DIR: Final = "evidence"
_FRAME_EVIDENCE_DIR: Final = "frames"
_ADMISSION_STAGING_PREFIX: Final = ".phase7a2-admission-"
_READ_ONLY_ROOT_FILES: Final = frozenset({_MANIFEST_NAME, "phase8-request.json"})
_READ_ONLY_SCHEMA2_DIRECTORIES: Final = frozenset(
    {_OPERATION_DIR, _FRAME_DIR, _REQUEST_DIR, _EVIDENCE_DIR, "observations"}
)
_READ_ONLY_SCHEMA3_DIRECTORIES: Final = frozenset({"observations", "classification-operations"})
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class A2RepositoryBoundary(Protocol):
    """Repository surface used by schema-2 publication helpers."""

    @property
    def root(self) -> Path:
        """Return the confined repository root."""
        ...

    @property
    def now_utc(self) -> Callable[[], datetime]:
        """Return the repository clock."""
        ...

    def run_path(self, investigation_id: str, search_run_id: str) -> Path:
        """Return a confined run path."""
        ...

    def write_schema2_manifest(self, manifest: RecordingSearchManifestV2, directory: Path) -> None:
        """Atomically replace a schema-2 manifest."""
        ...

    def write_schema3_manifest(self, manifest: RecordingSearchManifestV3, directory: Path) -> None:
        """Atomically replace a schema-3 manifest."""
        ...

    def load(self, investigation_id: str, search_run_id: str) -> object:
        """Strictly reload a persisted manifest."""
        ...

    def load_for_probe_admission(self, investigation_id: str, search_run_id: str) -> object:
        """Strictly load a manifest without opening indexed JPEG payloads."""
        ...

    def promote_schema2(self, manifest: RecordingSearchManifestV2) -> RecordingSearchManifestV2:
        """Promote an active schema-1 run."""
        ...

    def admit_operation(
        self,
        manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
        operation: AcquisitionOperationRecord,
    ) -> RecordingSearchManifestV2 | RecordingSearchManifestV3:
        """Admit one acquisition operation."""
        ...

    def publish_a2_bundle(
        self,
        manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
        request_records: tuple[ProbeFrameRequestRecord, ...],
        frame_records: tuple[tuple[CanonicalProbeFrameRecord, bytes], ...],
    ) -> RecordingSearchManifestV2 | RecordingSearchManifestV3:
        """Publish one immutable acquisition bundle."""
        ...


def _raise_corrupt() -> NoReturn:
    raise RecordingSearchManifestCorruptError


def _raise_publication_conflict() -> NoReturn:
    raise RecordingSearchArtifactError


def parse_schema2_manifest(raw: str) -> RecordingSearchManifestV2:
    """Parse one schema-2 manifest with duplicate and unknown-key rejection."""
    try:
        _ = load_durable_json_object(raw)
        return RecordingSearchManifestV2.model_validate_json(raw, strict=True)
    except (DurableJsonError, ValidationError, ValueError):
        raise RecordingSearchManifestCorruptError from None


def ensure_a2_directories(root: Path, run_path: Path) -> None:
    """Create the closed schema-2 child directories under one run."""
    for relative in (
        _OPERATION_DIR,
        _FRAME_DIR,
        _REQUEST_DIR,
        f"{_EVIDENCE_DIR}/{_FRAME_EVIDENCE_DIR}",
    ):
        path = run_path / relative
        if not is_safe_contained_path(root, path.parent) or (path.exists() and path.is_symlink()):
            raise RecordingSearchArtifactError
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise RecordingSearchArtifactError from None
        if not is_safe_contained_path(root, path, require_target=True) or not path.is_dir():
            raise RecordingSearchArtifactError


def validate_schema2_tree(root: Path, run_path: Path, manifest: RecordingSearchManifestV2) -> None:
    """Strictly reopen every indexed operation, request, frame, and JPEG."""
    if not is_safe_contained_path(root, run_path, require_target=True) or run_path.is_symlink():
        raise RecordingSearchManifestCorruptError
    expected_directories = (
        run_path / _OPERATION_DIR,
        run_path / _FRAME_DIR,
        run_path / _REQUEST_DIR,
        run_path / _EVIDENCE_DIR,
        run_path / _EVIDENCE_DIR / _FRAME_EVIDENCE_DIR,
    )
    try:
        if any(
            not is_safe_contained_path(root, path, require_target=True)
            or path.is_symlink()
            or not path.is_dir()
            for path in expected_directories
        ):
            _raise_corrupt()
        operations = _read_operations(root, run_path, manifest)
        _recover_admission_residue(root, run_path, manifest, operations)
        frames = _read_frames(root, run_path, manifest, operations, validate_media=True)
        requests = _read_requests(root, run_path, manifest, operations, frames)
        _reject_orphan_files(run_path, manifest, operations, frames, requests)
    except RecordingSearchManifestCorruptError:
        raise
    except (ConfirmationArtifactError, OSError, ValueError, ValidationError, DurableJsonError):
        raise RecordingSearchManifestCorruptError from None


def read_schema2_children(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV2
) -> tuple[
    dict[str, AcquisitionOperationRecord],
    dict[str, CanonicalProbeFrameRecord],
    dict[str, ProbeFrameRequestRecord],
]:
    """Return strictly validated indexed child records."""
    validate_schema2_tree(root, run_path, manifest)
    operations = _read_operations(root, run_path, manifest)
    frames = _read_frames(root, run_path, manifest, operations, validate_media=True)
    requests = _read_requests(root, run_path, manifest, operations, frames)
    return operations, frames, requests


def validate_schema2_tree_read_only(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV2
) -> None:
    """Validate one schema-2 tree without recovery or filesystem mutation.

    This boundary is reserved for terminal schema-4 consumers.  Active schema
    2/3 admission retains ``validate_schema2_tree`` and its narrowly scoped
    crash-recovery behavior; terminal reopen must treat every entry as input.
    """
    _validate_schema2_tree_read_only(root, run_path, manifest, _READ_ONLY_SCHEMA2_DIRECTORIES)


def validate_schema2_tree_read_only_for_schema3(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV2
) -> None:
    """Validate schema-2 children while allowing the enclosing schema-3 directories."""
    _validate_schema2_tree_read_only(
        root,
        run_path,
        manifest,
        _READ_ONLY_SCHEMA2_DIRECTORIES | _READ_ONLY_SCHEMA3_DIRECTORIES,
    )


def _validate_schema2_tree_read_only(
    root: Path,
    run_path: Path,
    manifest: RecordingSearchManifestV2,
    allowed_root_directories: frozenset[str],
) -> None:
    if not is_safe_contained_path(root, run_path, require_target=True) or run_path.is_symlink():
        raise RecordingSearchManifestCorruptError
    expected_directories = (
        run_path / _OPERATION_DIR,
        run_path / _FRAME_DIR,
        run_path / _REQUEST_DIR,
        run_path / _EVIDENCE_DIR,
        run_path / _EVIDENCE_DIR / _FRAME_EVIDENCE_DIR,
    )
    try:
        if any(
            not is_safe_contained_path(root, path, require_target=True)
            or path.is_symlink()
            or not path.is_dir()
            for path in expected_directories
        ):
            _raise_corrupt()
        _validate_read_only_root_entries(
            root, run_path, expected_directories, allowed_root_directories
        )
        operations = _read_operations(root, run_path, manifest)
        frames = _read_frames(root, run_path, manifest, operations, validate_media=True)
        requests = _read_requests(root, run_path, manifest, operations, frames)
        _reject_orphan_files(run_path, manifest, operations, frames, requests)
    except RecordingSearchManifestCorruptError:
        raise
    except (ConfirmationArtifactError, OSError, ValueError, ValidationError, DurableJsonError):
        raise RecordingSearchManifestCorruptError from None


def read_schema2_children_read_only(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV2
) -> tuple[
    dict[str, AcquisitionOperationRecord],
    dict[str, CanonicalProbeFrameRecord],
    dict[str, ProbeFrameRequestRecord],
]:
    """Read indexed schema-2 children without invoking admission recovery."""
    validate_schema2_tree_read_only(root, run_path, manifest)
    operations = _read_operations(root, run_path, manifest)
    frames = _read_frames(root, run_path, manifest, operations, validate_media=True)
    requests = _read_requests(root, run_path, manifest, operations, frames)
    return operations, frames, requests


def read_schema2_children_read_only_for_schema3(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV2
) -> tuple[
    dict[str, AcquisitionOperationRecord],
    dict[str, CanonicalProbeFrameRecord],
    dict[str, ProbeFrameRequestRecord],
]:
    """Read schema-2 children under the explicit schema-3 root contract."""
    validate_schema2_tree_read_only_for_schema3(root, run_path, manifest)
    operations = _read_operations(root, run_path, manifest)
    frames = _read_frames(root, run_path, manifest, operations, validate_media=True)
    requests = _read_requests(root, run_path, manifest, operations, frames)
    return operations, frames, requests


def _validate_read_only_root_entries(
    root: Path,
    run_path: Path,
    expected_directories: tuple[Path, ...],
    allowed_root_directories: frozenset[str],
) -> None:
    expected = set(allowed_root_directories) | set(_READ_ONLY_ROOT_FILES)
    actual = {path.name for path in run_path.iterdir()}
    if (
        not actual.issubset(expected)
        or not {path.name for path in expected_directories} <= actual
        or _MANIFEST_NAME not in actual
    ):
        _raise_corrupt()
    for name in actual & allowed_root_directories:
        path = run_path / name
        if (
            path.is_symlink()
            or not path.is_dir()
            or not is_safe_contained_path(root, path, require_target=True)
        ):
            _raise_corrupt()
    for name in _READ_ONLY_ROOT_FILES:
        path = run_path / name
        if path.exists() and (
            path.is_symlink()
            or not path.is_file()
            or not is_safe_contained_path(root, path, require_target=True)
        ):
            _raise_corrupt()


def read_schema2_children_for_probe_admission(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV2
) -> tuple[
    dict[str, AcquisitionOperationRecord],
    dict[str, CanonicalProbeFrameRecord],
    dict[str, ProbeFrameRequestRecord],
]:
    """Read indexed A2 records while deferring the selected JPEG byte read."""
    validate_schema2_tree_structure(root, run_path, manifest)
    operations = _read_operations(root, run_path, manifest)
    frames = _read_frames(root, run_path, manifest, operations, validate_media=False)
    requests = _read_requests(root, run_path, manifest, operations, frames)
    _reject_orphan_files(run_path, manifest, operations, frames, requests)
    return operations, frames, requests


def validate_schema2_tree_structure(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV2
) -> None:
    """Validate the A2 tree and child identities without opening JPEG payloads."""
    if not is_safe_contained_path(root, run_path, require_target=True) or run_path.is_symlink():
        raise RecordingSearchManifestCorruptError
    expected_directories = (
        run_path / _OPERATION_DIR,
        run_path / _FRAME_DIR,
        run_path / _REQUEST_DIR,
        run_path / _EVIDENCE_DIR,
        run_path / _EVIDENCE_DIR / _FRAME_EVIDENCE_DIR,
    )
    try:
        if any(
            not is_safe_contained_path(root, path, require_target=True)
            or path.is_symlink()
            or not path.is_dir()
            for path in expected_directories
        ):
            _raise_corrupt()
        _ = _read_operations(root, run_path, manifest)
        if any(path.name.startswith(_ADMISSION_STAGING_PREFIX) for path in run_path.iterdir()):
            _raise_corrupt()
    except RecordingSearchManifestCorruptError:
        raise
    except (OSError, ValueError, ValidationError, DurableJsonError):
        raise RecordingSearchManifestCorruptError from None


def promote_schema2(
    repository: A2RepositoryBoundary, manifest: RecordingSearchManifestV2
) -> RecordingSearchManifestV2:
    """Publish a schema-2 successor through the A1 repository writer."""
    root = repository.root
    run_path = repository.run_path(manifest.investigation_id, manifest.search_run_id)
    ensure_a2_directories(root, run_path)
    repository.write_schema2_manifest(manifest, run_path)
    loaded = repository.load(manifest.investigation_id, manifest.search_run_id)
    if not isinstance(loaded, RecordingSearchManifestV2):
        raise RecordingSearchManifestCorruptError
    return loaded


def admit_operation(
    repository: A2RepositoryBoundary,
    manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
    operation: AcquisitionOperationRecord,
) -> RecordingSearchManifestV2 | RecordingSearchManifestV3:
    """Atomically admit one operation as the next manifest successor."""
    if (
        operation.investigation_id != manifest.investigation_id
        or operation.search_run_id != manifest.search_run_id
        or operation.operation_id in manifest.acquisition_operation_ids
    ):
        raise RecordingSearchArtifactError
    root = repository.root
    run_path = repository.run_path(manifest.investigation_id, manifest.search_run_id)
    ensure_a2_directories(root, run_path)
    operation_path = run_path / _OPERATION_DIR / f"{operation.operation_id}.json"
    staging = run_path / f"{_ADMISSION_STAGING_PREFIX}{operation.operation_id}"
    staging_marker = staging / "operation.json"
    if not is_safe_contained_path(root, staging.parent) or staging.exists():
        raise RecordingSearchArtifactError
    staging_owned = False
    try:
        _ = staging.mkdir()
        staging_owned = True
        if not is_safe_contained_path(root, staging, require_target=True) or staging.is_symlink():
            _raise_publication_conflict()
        payload = _canonical_json(operation.model_dump(mode="json"))
        _write_no_replace(staging_marker, payload)
        _write_no_replace(operation_path, payload)
        updated = manifest.model_copy(
            update={
                "acquisition_operation_ids": (
                    *manifest.acquisition_operation_ids,
                    operation.operation_id,
                )
            }
        )
        _write_acquisition_manifest(repository, updated, run_path)
    except (RecordingSearchArtifactError, OSError, ValueError, ValidationError):
        _remove_owned_file(root, operation_path)
        _remove_owned_file(root, staging_marker)
        _remove_owned_directory(root, staging if staging_owned else None)
        raise
    _remove_owned_directory(root, staging)
    loaded = repository.load(manifest.investigation_id, manifest.search_run_id)
    if not isinstance(loaded, type(manifest)):
        raise RecordingSearchManifestCorruptError
    return loaded


def publish_a2_bundle(
    repository: A2RepositoryBoundary,
    manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
    request_records: tuple[ProbeFrameRequestRecord, ...],
    frame_records: tuple[tuple[CanonicalProbeFrameRecord, bytes], ...],
) -> RecordingSearchManifestV2 | RecordingSearchManifestV3:
    """Publish immutable child records and one indexed manifest successor."""
    root = repository.root
    run_path = repository.run_path(manifest.investigation_id, manifest.search_run_id)
    ensure_a2_directories(root, run_path)
    created_files: list[Path] = []
    staging: Path | None = None
    staging_owned = False
    committed = False
    try:
        _validate_bundle_ownership(manifest, request_records, frame_records)
        operation_ids = {item.operation_id for item in request_records} | {
            item.operation_id for item, _ in frame_records
        }
        if len(operation_ids) != 1:
            _raise_publication_conflict()
        operation_id = next(iter(operation_ids))
        staging = run_path / f".phase7a2-{operation_id}"
        if not is_safe_contained_path(root, staging.parent) or staging.exists():
            _raise_publication_conflict()
        _ = staging.mkdir()
        staging_owned = True
        for frame, jpeg_bytes in frame_records:
            staged_frame = staging / _FRAME_DIR / f"{frame.canonical_frame_id}.json"
            staged_jpeg = staging / Path(frame.jpeg_relative_path)
            _write_no_replace(staged_frame, _canonical_json(frame.model_dump(mode="json")))
            _write_no_replace(staged_jpeg, jpeg_bytes)
            final_frame = run_path / _FRAME_DIR / f"{frame.canonical_frame_id}.json"
            final_jpeg = run_path / Path(frame.jpeg_relative_path)
            _move_no_replace(root, staged_frame, final_frame)
            created_files.append(final_frame)
            _move_no_replace(root, staged_jpeg, final_jpeg)
            created_files.append(final_jpeg)
        for request in request_records:
            staged_request = staging / _REQUEST_DIR / f"{request.probe_request_id}.json"
            _write_no_replace(staged_request, _canonical_json(request.model_dump(mode="json")))
            request_path = run_path / _REQUEST_DIR / f"{request.probe_request_id}.json"
            _move_no_replace(root, staged_request, request_path)
            created_files.append(request_path)
        next_manifest = manifest.model_copy(
            update={
                "probe_request_ids": (
                    *manifest.probe_request_ids,
                    *(item.probe_request_id for item in request_records),
                ),
                "canonical_frame_ids": (
                    *manifest.canonical_frame_ids,
                    *(
                        frame.canonical_frame_id
                        for frame, _ in frame_records
                        if frame.canonical_frame_id not in manifest.canonical_frame_ids
                    ),
                ),
            }
        )
        _write_acquisition_manifest(repository, next_manifest, run_path)
        committed = True
    except (RecordingSearchArtifactError, OSError, ValueError, ValidationError):
        if not committed:
            for path in reversed(created_files):
                _remove_owned_file(root, path)
            _remove_owned_directory(root, staging if staging_owned else None)
        raise
    finally:
        if committed and staging_owned:
            _remove_owned_directory(root, staging)
    loaded = repository.load(manifest.investigation_id, manifest.search_run_id)
    if not isinstance(loaded, type(manifest)):
        raise RecordingSearchManifestCorruptError
    return loaded


def transition_schema2(
    repository: A2RepositoryBoundary,
    current: RecordingSearchManifestV2,
    target: RecordingSearchState,
    failure_reason: str | None,
) -> RecordingSearchManifestV2:
    """Apply the only supported schema-2 terminal transitions."""
    if current.state not in {RecordingSearchState.PENDING, RecordingSearchState.RUNNING}:
        raise RecordingSearchTransitionError
    if target not in {RecordingSearchState.FAILED, RecordingSearchState.INTERRUPTED}:
        raise RecordingSearchTransitionError
    reason = failure_reason or (
        "process_lock_released"
        if target is RecordingSearchState.INTERRUPTED
        else "unexpected_error"
    )
    if reason not in {"process_lock_released", "unexpected_error", "acquisition_failed"}:
        raise RecordingSearchTransitionError
    value = repository.now_utc()
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise RecordingSearchTransitionError
    now = value.astimezone(timezone.utc).replace(microsecond=0)
    updated = current.model_copy(
        update={"state": target, "completed_at_utc": now, "failure_reason": reason}
    )
    repository.write_schema2_manifest(
        updated, repository.run_path(current.investigation_id, current.search_run_id)
    )
    loaded = repository.load(current.investigation_id, current.search_run_id)
    if not isinstance(loaded, RecordingSearchManifestV2):
        raise RecordingSearchManifestCorruptError
    return loaded


def _validate_bundle_ownership(
    manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
    request_records: tuple[ProbeFrameRequestRecord, ...],
    frame_records: tuple[tuple[CanonicalProbeFrameRecord, bytes], ...],
) -> None:
    operation_ids = set(manifest.acquisition_operation_ids)
    frame_ids = set(manifest.canonical_frame_ids)
    for frame, jpeg_bytes in frame_records:
        if (
            frame.investigation_id != manifest.investigation_id
            or frame.search_run_id != manifest.search_run_id
            or frame.channel_id != manifest.confirmation.channel_id
            or frame.operation_id not in operation_ids
            or frame.canonical_frame_id in frame_ids
            or not jpeg_bytes
        ):
            raise RecordingSearchArtifactError
    request_ids = set(manifest.probe_request_ids)
    for request in request_records:
        if (
            request.status is ProbeRequestStatus.PENDING
            or request.probe_request_id in request_ids
            or request.investigation_id != manifest.investigation_id
            or request.search_run_id != manifest.search_run_id
            or request.channel_id != manifest.confirmation.channel_id
            or request.operation_id not in operation_ids
            or (
                request.status is ProbeRequestStatus.SUCCEEDED
                and request.canonical_frame_id not in frame_ids
                and request.canonical_frame_id
                not in {frame.canonical_frame_id for frame, _ in frame_records}
            )
        ):
            raise RecordingSearchArtifactError


def _write_acquisition_manifest(
    repository: A2RepositoryBoundary,
    manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
    run_path: Path,
) -> None:
    if isinstance(manifest, RecordingSearchManifestV3):
        repository.write_schema3_manifest(manifest, run_path)
    else:
        repository.write_schema2_manifest(manifest, run_path)


def _read_operations(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV2
) -> dict[str, AcquisitionOperationRecord]:
    records: dict[str, AcquisitionOperationRecord] = {}
    for operation_id in manifest.acquisition_operation_ids:
        path = run_path / _OPERATION_DIR / f"{operation_id}.json"
        record = _read_json(path, AcquisitionOperationRecord)
        if (
            record.operation_id != operation_id
            or record.investigation_id != manifest.investigation_id
            or record.search_run_id != manifest.search_run_id
            or not is_safe_contained_path(root, path, require_target=True)
        ):
            raise RecordingSearchManifestCorruptError
        records[operation_id] = record
    return records


def _read_frames(
    root: Path,
    run_path: Path,
    manifest: RecordingSearchManifestV2,
    operations: dict[str, AcquisitionOperationRecord],
    *,
    validate_media: bool,
) -> dict[str, CanonicalProbeFrameRecord]:
    records: dict[str, CanonicalProbeFrameRecord] = {}
    decoded_positions: set[tuple[str, int]] = set()
    for frame_id in manifest.canonical_frame_ids:
        path = run_path / _FRAME_DIR / f"{frame_id}.json"
        record = _read_json(path, CanonicalProbeFrameRecord)
        if (
            record.canonical_frame_id != frame_id
            or record.investigation_id != manifest.investigation_id
            or record.search_run_id != manifest.search_run_id
            or record.channel_id != manifest.confirmation.channel_id
            or record.operation_id not in operations
            or record.source_width != manifest.confirmation.source_width
            or record.source_height != manifest.confirmation.source_height
            or record.jpeg_relative_path != f"evidence/frames/{frame_id}.jpg"
            or not is_safe_contained_path(root, path, require_target=True)
            or record.acquisition_id
            != acquisition_id_for(
                record.source_segment_id,
                record.extraction_start_utc,
                record.extraction_end_utc,
                manifest.policy.acquisition_policy_version,
            )
            or record.acquired_at_utc < operations[record.operation_id].admitted_at_utc
        ):
            raise RecordingSearchManifestCorruptError
        position = (record.decode_session_id, record.decoded_ordinal)
        if position in decoded_positions:
            raise RecordingSearchManifestCorruptError
        decoded_positions.add(position)
        jpeg_path = run_path / Path(record.jpeg_relative_path)
        if (
            not is_safe_contained_path(root, jpeg_path, require_target=True)
            or jpeg_path.is_symlink()
        ):
            raise RecordingSearchManifestCorruptError
        if validate_media:
            integrity = compute_jpeg_integrity(jpeg_path, record.source_width, record.source_height)
            if (
                integrity.sha256 != record.jpeg_sha256
                or integrity.size_bytes != record.jpeg_size_bytes
            ):
                raise RecordingSearchManifestCorruptError
        records[frame_id] = record
    return records


def _read_requests(
    root: Path,
    run_path: Path,
    manifest: RecordingSearchManifestV2,
    operations: dict[str, AcquisitionOperationRecord],
    frames: dict[str, CanonicalProbeFrameRecord],
) -> dict[str, ProbeFrameRequestRecord]:
    records: dict[str, ProbeFrameRequestRecord] = {}
    for request_id in manifest.probe_request_ids:
        path = run_path / _REQUEST_DIR / f"{request_id}.json"
        record = _read_json(path, ProbeFrameRequestRecord)
        if (
            record.probe_request_id != request_id
            or record.investigation_id != manifest.investigation_id
            or record.search_run_id != manifest.search_run_id
            or record.channel_id != manifest.confirmation.channel_id
            or record.operation_id not in operations
            or not is_safe_contained_path(root, path, require_target=True)
            or record.requested_time_utc < manifest.policy.search_start_utc
            or record.requested_time_utc > manifest.policy.search_end_utc
        ):
            raise RecordingSearchManifestCorruptError
        if record.status is ProbeRequestStatus.PENDING:
            raise RecordingSearchManifestCorruptError
        if record.status is ProbeRequestStatus.SUCCEEDED:
            if record.canonical_frame_id not in frames:
                raise RecordingSearchManifestCorruptError
            frame = frames[record.canonical_frame_id]
            if frame.channel_id != record.channel_id:
                raise RecordingSearchManifestCorruptError
            if record.alias_of_probe_request_id is not None:
                prior = records.get(record.alias_of_probe_request_id)
                if (
                    prior is None
                    or prior.status is not ProbeRequestStatus.SUCCEEDED
                    or prior.canonical_frame_id != record.canonical_frame_id
                ):
                    raise RecordingSearchManifestCorruptError
        records[request_id] = record
    return records


def _reject_orphan_files(
    run_path: Path,
    manifest: RecordingSearchManifestV2,
    operations: dict[str, AcquisitionOperationRecord],
    frames: dict[str, CanonicalProbeFrameRecord],
    requests: dict[str, ProbeFrameRequestRecord],
) -> None:
    operations_directory = run_path / _OPERATION_DIR
    if {path.name for path in operations_directory.iterdir()} != {
        f"{value}.json" for value in operations
    }:
        raise RecordingSearchManifestCorruptError
    for directory_name, names in (
        (_FRAME_DIR, {f"{value}.json" for value in frames}),
        (_REQUEST_DIR, {f"{value}.json" for value in requests}),
        (
            f"{_EVIDENCE_DIR}/{_FRAME_EVIDENCE_DIR}",
            {f"{frame_id}.jpg" for frame_id in manifest.canonical_frame_ids},
        ),
    ):
        directory = run_path / directory_name
        if {path.name for path in directory.iterdir()} != names:
            raise RecordingSearchManifestCorruptError


def _recover_admission_residue(
    root: Path,
    run_path: Path,
    manifest: RecordingSearchManifestV2,
    operations: dict[str, AcquisitionOperationRecord],
) -> None:
    operations_directory = run_path / _OPERATION_DIR
    final_paths = {
        path.name: path
        for path in operations_directory.iterdir()
        if path.name not in {f"{value}.json" for value in operations}
    }
    staging_paths = {
        path.name.removeprefix(_ADMISSION_STAGING_PREFIX): path
        for path in run_path.iterdir()
        if path.name.startswith(_ADMISSION_STAGING_PREFIX)
    }
    processed_final_ids: set[str] = set()
    for staging in staging_paths.values():
        operation_id = staging.name.removeprefix(_ADMISSION_STAGING_PREFIX)
        if _recover_admission_staging(
            root,
            run_path,
            manifest,
            operations,
            staging,
        ):
            processed_final_ids.add(operation_id)
    for filename in final_paths:
        parsed_id = Path(filename).stem
        if Path(filename).suffix != ".json" or parsed_id not in processed_final_ids:
            _raise_corrupt()


def _recover_admission_staging(
    root: Path,
    run_path: Path,
    manifest: RecordingSearchManifestV2,
    operations: dict[str, AcquisitionOperationRecord],
    staging: Path,
) -> bool:
    operation_id = staging.name.removeprefix(_ADMISSION_STAGING_PREFIX)
    if not operation_id:
        _raise_corrupt()
    if (
        not is_safe_contained_path(root, staging, require_target=True)
        or staging.is_symlink()
        or not staging.is_dir()
    ):
        _raise_corrupt()
    marker = staging / "operation.json"
    if not is_safe_contained_path(root, marker, require_target=True) or marker.is_symlink():
        _raise_corrupt()
    if {path.name for path in staging.iterdir()} != {"operation.json"}:
        _raise_corrupt()
    staged = _read_json(marker, AcquisitionOperationRecord)
    if (
        staged.operation_id != operation_id
        or staged.investigation_id != manifest.investigation_id
        or staged.search_run_id != manifest.search_run_id
    ):
        _raise_corrupt()
    final = run_path / _OPERATION_DIR / f"{operation_id}.json"
    removed_final = False
    if operation_id in operations:
        if staged != operations[operation_id]:
            _raise_corrupt()
    elif final.exists():
        published = _read_json(final, AcquisitionOperationRecord)
        if published != staged:
            _raise_corrupt()
        _remove_recovered_file(root, final)
        removed_final = True
    _remove_recovered_directory(root, staging)
    return removed_final


def _remove_recovered_file(root: Path, path: Path) -> None:
    if not is_safe_contained_path(root, path, require_target=True) or path.is_symlink():
        _raise_corrupt()
    try:
        path.unlink()
    except OSError:
        _raise_corrupt()
    if path.exists():
        _raise_corrupt()


def _remove_recovered_directory(root: Path, path: Path) -> None:
    if not is_safe_contained_path(root, path, require_target=True) or path.is_symlink():
        _raise_corrupt()
    try:
        shutil.rmtree(path)
    except OSError:
        _raise_corrupt()
    if path.exists():
        _raise_corrupt()


def _read_json(path: Path, model_type: type[_ModelT]) -> _ModelT:
    try:
        raw = path.read_text(encoding="utf-8")
        _ = load_durable_json_object(raw)
        return model_type.model_validate_json(raw, strict=True)
    except (DurableJsonError, OSError, UnicodeError, ValidationError, ValueError):
        raise RecordingSearchManifestCorruptError from None


def _write_no_replace(path: Path, payload: str | bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            _ = stream.write(payload.encode("utf-8") if isinstance(payload, str) else payload)
            _ = stream.flush()
            _ = os.fsync(stream.fileno())
    except OSError:
        raise RecordingSearchArtifactError from None


def _move_no_replace(root: Path, source: Path, destination: Path) -> None:
    if (
        not is_safe_contained_path(root, source, require_target=True)
        or not is_safe_contained_path(root, destination.parent, require_target=True)
        or destination.exists()
    ):
        raise RecordingSearchArtifactError
    try:
        _ = source.rename(destination)
    except OSError:
        raise RecordingSearchArtifactError from None


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _remove_owned_file(root: Path, path: Path) -> None:
    with suppress(OSError):
        if is_safe_contained_path(root, path, require_target=True) and not path.is_symlink():
            path.unlink()


def _remove_owned_directory(root: Path, path: Path | None) -> None:
    if path is None:
        return
    with suppress(OSError):
        if (
            path.exists()
            and not path.is_symlink()
            and is_safe_contained_path(root, path, require_target=True)
        ):
            shutil.rmtree(path)
