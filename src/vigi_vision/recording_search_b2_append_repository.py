"""Atomic append publication for an existing schema-3 run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vigi_vision.durable_io import is_safe_contained_path
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b2_records import (
    ChildRecord,
    ClassificationOperationRecord,
    RecordingProbeObservationRecord,
    TargetAliasRecord,
)
from vigi_vision.recording_search_b2_storage import (
    canonical_record_json,
    child_relative_path,
    ensure_schema3_directories,
    move_new_child,
    remove_empty_owned_directory,
    remove_owned_directory,
    remove_owned_file,
    write_new_child,
)
from vigi_vision.recording_search_b2_successors import (
    append_alias_successor,
    append_classification_successor,
)
from vigi_vision.recording_search_b2_validation import validate_schema3_tree
from vigi_vision.recording_search_models import (
    RecordingSearchArtifactError,
    RecordingSearchManifestCorruptError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vigi_vision.recording_search_b2_repository import Schema3RepositoryBoundary


def publish_schema3_classification_append(
    repository: Schema3RepositoryBoundary,
    expected: RecordingSearchManifestV3,
    operation: ClassificationOperationRecord,
    observation: RecordingProbeObservationRecord,
    aliases: tuple[TargetAliasRecord, ...] = (),
) -> RecordingSearchManifestV3:
    """Append one validated classification through manifest replacement."""
    successor = append_classification_successor(expected, operation, observation, aliases)
    return _publish_append(repository, expected, successor, (operation, observation, *aliases))


def publish_schema3_alias_append(
    repository: Schema3RepositoryBoundary,
    expected: RecordingSearchManifestV3,
    alias: TargetAliasRecord,
) -> RecordingSearchManifestV3:
    """Append one alias without creating another visual observation."""
    successor = append_alias_successor(expected, alias)
    return _publish_append(repository, expected, successor, (alias,))


def _publish_append(  # noqa: C901, PLR0912, PLR0915
    repository: Schema3RepositoryBoundary,
    expected: RecordingSearchManifestV3,
    successor: RecordingSearchManifestV3,
    children: tuple[ChildRecord, ...],
) -> RecordingSearchManifestV3:
    current = repository.load(expected.investigation_id, expected.search_run_id)
    if current != expected:
        raise RecordingSearchArtifactError
    run_path = repository.run_path(expected.investigation_id, expected.search_run_id)
    root = repository.root
    created_directories = ensure_schema3_directories(root, run_path)
    marker = children[0]
    if isinstance(marker, ClassificationOperationRecord):
        marker_id = marker.classification_operation_id
    elif isinstance(marker, TargetAliasRecord):
        marker_id = marker.alias_id
    else:
        raise RecordingSearchArtifactError
    staging = run_path / f".phase7b2-{marker_id}"
    created: list[Path] = []
    staging_created = False
    committed = False
    try:
        if (
            not is_safe_contained_path(root, staging.parent, require_target=True)
            or staging.exists()
        ):
            _raise_artifact()
        staging.mkdir()
        staging_created = True
        if not is_safe_contained_path(root, staging, require_target=True) or staging.is_symlink():
            _raise_artifact()
        for child in children:
            relative = child_relative_path(child)
            staged = staging / relative
            destination = run_path / relative
            write_new_child(staged, canonical_record_json(child))
            if destination.exists() or destination.is_symlink():
                _raise_artifact()
            move_new_child(root, staged, destination)
            created.append(destination)
        _ = validate_schema3_tree(root, run_path, successor)
        latest = repository.load_manifest_for_commit(
            expected.investigation_id, expected.search_run_id
        )
        if latest != expected:
            _raise_artifact()
        repository.write_schema3_manifest(successor, run_path)
        committed = True
    except (
        OSError,
        ValueError,
        TypeError,
        RecordingSearchArtifactError,
        RecordingSearchManifestCorruptError,
    ):
        if not committed:
            for path in reversed(created):
                remove_owned_file(root, path)
            remove_owned_directory(root, staging if staging_created else None)
            for directory in reversed(created_directories):
                remove_empty_owned_directory(root, directory)
        raise
    finally:
        if committed:
            remove_owned_directory(root, staging if staging_created else None)
    loaded = repository.load(expected.investigation_id, expected.search_run_id)
    if not isinstance(loaded, RecordingSearchManifestV3):
        raise RecordingSearchManifestCorruptError
    return loaded


def _raise_artifact() -> None:
    raise RecordingSearchArtifactError
