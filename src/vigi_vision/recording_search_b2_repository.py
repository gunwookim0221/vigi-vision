"""Atomic schema-3 child preparation, publication, and strict loading helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vigi_vision.durable_io import is_safe_contained_path
from vigi_vision.recording_search_a2_models import RecordingSearchManifestV2
from vigi_vision.recording_search_b2_models import (
    RecordingSearchManifestV3,
    build_schema3_successor,
)
from vigi_vision.recording_search_b2_policy import RecordingSearchPolicyV3  # noqa: TC001
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
from vigi_vision.recording_search_b2_validation import (
    read_schema3_children,
    validate_schema3_tree,
)
from vigi_vision.recording_search_models import (
    RecordingSearchArtifactError,
    RecordingSearchManifestCorruptError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from vigi_vision.recording_search_b2_records import (
        ChildRecord,
        ClassificationOperationRecord,
        ConfirmedReferenceBaselineRecord,
        RecordingProbeObservationRecord,
        TargetAliasRecord,
    )
    from vigi_vision.recording_search_models import RecordingSearchManifest


class Schema3RepositoryBoundary(Protocol):
    """Minimal repository surface required by the persistence primitive."""

    @property
    def root(self) -> Path:
        """Return the trusted repository root."""
        ...

    @property
    def now_utc(self) -> Callable[[], datetime]:
        """Return the repository clock."""
        ...

    def run_path(self, investigation_id: str, search_run_id: str) -> Path:
        """Return a confined run path."""
        ...

    def load(
        self, investigation_id: str, search_run_id: str
    ) -> RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchManifestV3:
        """Load the current strict manifest."""
        ...

    def load_manifest_for_commit(
        self, investigation_id: str, search_run_id: str
    ) -> RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchManifestV3:
        """Load only the current manifest within the publication mutex."""
        ...

    def write_schema3_manifest(self, manifest: RecordingSearchManifestV3, directory: Path) -> None:
        """Atomically replace the manifest."""
        ...


def prepare_schema3_successor(  # noqa: PLR0913
    manifest: RecordingSearchManifestV2,
    policy: RecordingSearchPolicyV3,
    baseline: ConfirmedReferenceBaselineRecord,
    operation: ClassificationOperationRecord,
    observation: RecordingProbeObservationRecord,
    aliases: tuple[TargetAliasRecord, ...] = (),
) -> RecordingSearchManifestV3:
    """Validate all immutable values and construct, but do not publish, a successor."""
    return build_schema3_successor(manifest, policy, baseline, operation, observation, aliases)


def publish_schema3_successor(  # noqa: C901, PLR0912, PLR0913, PLR0915
    repository: Schema3RepositoryBoundary,
    expected: RecordingSearchManifestV2,
    policy: RecordingSearchPolicyV3,
    baseline: ConfirmedReferenceBaselineRecord,
    operation: ClassificationOperationRecord,
    observation: RecordingProbeObservationRecord,
    aliases: tuple[TargetAliasRecord, ...] = (),
) -> RecordingSearchManifestV3:
    """Publish children and replace the manifest as one admission commit point."""
    current = repository.load(expected.investigation_id, expected.search_run_id)
    if isinstance(current, RecordingSearchManifestV3):
        _ = validate_schema3_tree(
            repository.root,
            repository.run_path(current.investigation_id, current.search_run_id),
            current,
        )
        _, _, observations, _ = read_schema3_children(
            repository.root,
            repository.run_path(current.investigation_id, current.search_run_id),
            current,
        )
        if (
            current.baseline_observation_id == baseline.observation_id
            and observations.get(observation.observation_id) == observation
        ):
            return current
        raise RecordingSearchArtifactError
    if not isinstance(current, RecordingSearchManifestV2) or current != expected:
        raise RecordingSearchArtifactError
    successor = prepare_schema3_successor(
        current, policy, baseline, operation, observation, aliases
    )
    run_path = repository.run_path(current.investigation_id, current.search_run_id)
    root = repository.root
    created_directories = ensure_schema3_directories(root, run_path)
    staging = run_path / f".phase7b2-{operation.classification_operation_id}"
    created: list[Path] = []
    staging_created = False
    committed = False
    children: tuple[ChildRecord, ...] = (baseline, operation, observation, *aliases)
    try:
        if (
            not is_safe_contained_path(root, staging.parent, require_target=True)
            or staging.exists()
        ):
            _raise_artifact()
        _ = staging.mkdir()
        staging_created = True
        if not is_safe_contained_path(root, staging, require_target=True) or staging.is_symlink():
            _raise_artifact()
        for child in children:
            relative = child_relative_path(child)
            staged = staging / relative
            destination = run_path / relative
            payload = canonical_record_json(child)
            write_new_child(staged, payload)
            if destination.exists() or destination.is_symlink():
                _raise_artifact()
            move_new_child(root, staged, destination)
            created.append(destination)
        _ = validate_schema3_tree(root, run_path, successor)
        latest = repository.load_manifest_for_commit(
            current.investigation_id, current.search_run_id
        )
        if not isinstance(latest, RecordingSearchManifestV2) or latest != current:
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
    loaded = repository.load(current.investigation_id, current.search_run_id)
    if not isinstance(loaded, RecordingSearchManifestV3):
        raise RecordingSearchManifestCorruptError
    _ = validate_schema3_tree(root, run_path, loaded)
    return loaded


def validate_schema3(
    repository: Schema3RepositoryBoundary, manifest: RecordingSearchManifestV3
) -> None:
    """Reopen one already committed schema-3 tree without mutation."""
    _ = validate_schema3_tree(
        repository.root,
        repository.run_path(manifest.investigation_id, manifest.search_run_id),
        manifest,
    )


def _raise_artifact() -> None:
    raise RecordingSearchArtifactError
