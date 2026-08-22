"""Local immutable recording-search run repository."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, NoReturn, overload

from pydantic import ValidationError

from vigi_vision.durable_io import (
    DurableJsonError,
    is_safe_contained_path,
    is_safe_path,
    load_durable_json_object,
)
from vigi_vision.recording_search_a2_models import (
    AcquisitionOperationRecord,
    CanonicalProbeFrameRecord,
    ProbeFrameRequestRecord,
    RecordingSearchManifestV2,
)
from vigi_vision.recording_search_a2_repository import (
    admit_operation,
    parse_schema2_manifest,
    promote_schema2,
    publish_a2_bundle,
    transition_schema2,
    validate_schema2_tree,
    validate_schema2_tree_structure,
)
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b2_successors import (
    Schema3LifecycleUpdate,
    lifecycle_successor,
)
from vigi_vision.recording_search_b2_validation import (
    ConfirmedBaselineLoader,
    parse_schema3_manifest,
    validate_authoritative_baseline,
    validate_schema3_tree,
    validate_schema3_tree_read_only,
)
from vigi_vision.recording_search_d2_5_handoff import (
    Phase8HandoffRequestV1,
    Phase8HandoffResult,
    create_or_reuse_phase8_request,
)
from vigi_vision.recording_search_d2_publication_models import RecordingSearchManifestV4
from vigi_vision.recording_search_models import (
    RecordingSearchArtifactError,
    RecordingSearchManifest,
    RecordingSearchManifestCorruptError,
    RecordingSearchNotFoundError,
    RecordingSearchState,
    RecordingSearchTransitionError,
    is_recording_search_investigation_id,
    is_recording_search_run_id,
    validate_phase7a1_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Callable


_STAGING_PREFIX: Final = ".phase7a1-"
_SCHEMA2: Final = 2
_SCHEMA3: Final = 3
_SCHEMA4: Final = 4
SearchManifest = (
    RecordingSearchManifest
    | RecordingSearchManifestV2
    | RecordingSearchManifestV3
    | RecordingSearchManifestV4
)
LegacySearchManifest = (
    RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchManifestV3
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@dataclass(frozen=True, slots=True)
class RecordingSearchRepository:
    """Persist one isolated run tree below the ignored Phase 7 artifact root."""

    root: Path = field(repr=False)
    now_utc: Callable[[], datetime] = _utc_now
    confirmation_loader: ConfirmedBaselineLoader | None = field(default=None, repr=False)

    def ensure_root(self) -> None:
        """Create and validate the repository and lock directories."""
        try:
            if not is_safe_path(self.root):
                _raise_artifact()
            if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
                _raise_artifact()
            self.root.mkdir(parents=True, exist_ok=True)
            locks = self.root / ".locks"
            if not is_safe_contained_path(self.root, locks):
                _raise_artifact()
            if locks.exists() and (locks.is_symlink() or not locks.is_dir()):
                _raise_artifact()
            locks.mkdir(exist_ok=True)
        except OSError:
            raise RecordingSearchArtifactError from None

    def create(self, manifest: RecordingSearchManifest) -> RecordingSearchManifest:
        """Publish one new run directory without replacing another run."""
        self.ensure_root()
        run_directory = self.run_path(manifest.investigation_id, manifest.search_run_id)
        investigation_directory = run_directory.parent
        staging_directory: Path | None = None
        published = False
        successful = False
        destination_preexisting = False
        investigation_created = False
        try:
            investigation_created = not investigation_directory.exists()
            investigation_directory.mkdir(exist_ok=True)
            if (
                not is_safe_contained_path(self.root, investigation_directory, require_target=True)
                or investigation_directory.is_symlink()
            ):
                _raise_artifact()
            destination_preexisting = run_directory.exists() or run_directory.is_symlink()
            staging_directory = Path(
                tempfile.mkdtemp(
                    prefix=f"{_STAGING_PREFIX}{manifest.search_run_id}-",
                    dir=str(investigation_directory),
                )
            )
            if not is_safe_contained_path(self.root, staging_directory, require_target=True):
                _raise_artifact()
            (staging_directory / "observations").mkdir()
            (staging_directory / "evidence").mkdir()
            self._write_manifest_to_directory(manifest, staging_directory)
            self._publish_staging_directory(staging_directory, run_directory)
            published = True
            result = self.load(manifest.investigation_id, manifest.search_run_id)
            if not isinstance(result, RecordingSearchManifest):
                raise RecordingSearchManifestCorruptError
            successful = True
        except RecordingSearchArtifactError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise RecordingSearchArtifactError from None
        else:
            return result
        finally:
            if not successful:
                _remove_directory(self.root, staging_directory)
                if published or not destination_preexisting:
                    _remove_directory(self.root, run_directory)
            if investigation_created and investigation_directory.exists():
                with suppress(OSError):
                    investigation_directory.rmdir()

    @overload
    def load(self, investigation_id: str, search_run_id: str) -> LegacySearchManifest: ...

    @overload
    def load(
        self, investigation_id: str, search_run_id: str, *, include_terminal: Literal[True]
    ) -> SearchManifest: ...

    def load(  # noqa: C901
        self, investigation_id: str, search_run_id: str, *, include_terminal: bool = False
    ) -> SearchManifest:
        """Strictly load one persisted manifest."""
        _ = include_terminal
        path = self.run_path(investigation_id, search_run_id)
        try:
            if path.is_symlink():
                _raise_corrupt()
            if not path.exists():
                raise RecordingSearchNotFoundError
            if not is_safe_contained_path(self.root, path, require_target=True):
                _raise_corrupt()
            if not path.is_dir():
                _raise_corrupt()
            manifest_path = path / "manifest.json"
            if manifest_path.is_symlink() or not manifest_path.is_file():
                _raise_corrupt()
            raw = manifest_path.read_text(encoding="utf-8")
            manifest = _parse_manifest(raw)
            if isinstance(manifest, RecordingSearchManifestV2):
                validate_schema2_tree(self.root, path, manifest)
            elif isinstance(manifest, RecordingSearchManifestV3):
                baseline = validate_schema3_tree(self.root, path, manifest)
                validate_authoritative_baseline(self.confirmation_loader, manifest, baseline)
            elif isinstance(manifest, RecordingSearchManifestV4):
                predecessor = manifest.as_schema3()
                baseline = validate_schema3_tree_read_only(self.root, path, predecessor)
                validate_authoritative_baseline(self.confirmation_loader, predecessor, baseline)
        except RecordingSearchManifestCorruptError:
            raise
        except (DurableJsonError, OSError, UnicodeError, ValidationError, ValueError):
            raise RecordingSearchManifestCorruptError from None
        if manifest.investigation_id != investigation_id or manifest.search_run_id != search_run_id:
            raise RecordingSearchManifestCorruptError
        return manifest

    def load_for_probe_admission(self, investigation_id: str, search_run_id: str) -> SearchManifest:
        """Load one manifest while deferring indexed JPEG byte validation."""
        path = self.run_path(investigation_id, search_run_id)
        try:
            if (
                path.is_symlink()
                or not path.exists()
                or not is_safe_contained_path(self.root, path, require_target=True)
                or not path.is_dir()
            ):
                _raise_corrupt()
            manifest_path = path / "manifest.json"
            if manifest_path.is_symlink() or not manifest_path.is_file():
                _raise_corrupt()
            raw = manifest_path.read_text(encoding="utf-8")
            manifest = _parse_manifest(raw)
            if isinstance(manifest, RecordingSearchManifestV2):
                validate_schema2_tree_structure(self.root, path, manifest)
            elif isinstance(manifest, RecordingSearchManifestV3):
                baseline = validate_schema3_tree(self.root, path, manifest)
                validate_authoritative_baseline(self.confirmation_loader, manifest, baseline)
            elif isinstance(manifest, RecordingSearchManifestV4):
                _raise_corrupt()
        except RecordingSearchManifestCorruptError:
            raise
        except (DurableJsonError, OSError, UnicodeError, ValidationError, ValueError):
            raise RecordingSearchManifestCorruptError from None
        if manifest.investigation_id != investigation_id or manifest.search_run_id != search_run_id:
            raise RecordingSearchManifestCorruptError
        return manifest

    def load_manifest_for_commit(
        self, investigation_id: str, search_run_id: str
    ) -> LegacySearchManifest:
        """Read only the confined manifest for a mutex-protected compare-and-swap."""
        path = self.run_path(investigation_id, search_run_id)
        try:
            manifest_path = path / "manifest.json"
            if (
                path.is_symlink()
                or not is_safe_contained_path(self.root, path, require_target=True)
                or not path.is_dir()
                or manifest_path.is_symlink()
                or not manifest_path.is_file()
            ):
                _raise_corrupt()
            manifest = _parse_manifest(manifest_path.read_text(encoding="utf-8"))
        except RecordingSearchManifestCorruptError:
            raise
        except (DurableJsonError, OSError, UnicodeError, ValidationError, ValueError):
            raise RecordingSearchManifestCorruptError from None
        if manifest.investigation_id != investigation_id or manifest.search_run_id != search_run_id:
            raise RecordingSearchManifestCorruptError
        if isinstance(manifest, RecordingSearchManifestV4):
            raise RecordingSearchManifestCorruptError
        return manifest

    def latest_nonterminal(
        self, investigation_id: str
    ) -> RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchManifestV3 | None:
        """Return the newest strict pending or running run."""
        if not is_recording_search_investigation_id(investigation_id):
            raise RecordingSearchNotFoundError
        directory = self.root / investigation_id
        try:
            if not directory.exists():
                return None
            if not is_safe_contained_path(self.root, directory, require_target=True):
                _raise_corrupt()
            entries = tuple(directory.iterdir())
        except (OSError, ValueError):
            raise RecordingSearchManifestCorruptError from None
        manifests: list[
            RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchManifestV3
        ] = []
        for entry in entries:
            if entry.name.startswith(_STAGING_PREFIX):
                continue
            if entry.name in {"observations", "evidence"}:
                raise RecordingSearchManifestCorruptError
            if (
                not is_recording_search_run_id(entry.name)
                or entry.is_symlink()
                or not entry.is_dir()
            ):
                _raise_corrupt()
            manifest = self.load(investigation_id, entry.name)
            candidate = _legacy_nonterminal(manifest)
            if candidate is not None:
                manifests.append(candidate)
        return max(manifests, key=lambda value: value.created_at_utc) if manifests else None

    def transition(
        self,
        investigation_id: str,
        search_run_id: str,
        target: RecordingSearchState,
        failure_reason: str | None = None,
    ) -> RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchManifestV3:
        """Atomically apply one Phase 7A-1 lifecycle transition."""
        current = _legacy_transition_manifest(self.load(investigation_id, search_run_id))
        if isinstance(current, RecordingSearchManifestV2):
            return transition_schema2(self, current, target, failure_reason)
        if isinstance(current, RecordingSearchManifestV3):
            reason = failure_reason or (
                "process_lock_released"
                if target is RecordingSearchState.INTERRUPTED
                else "unexpected_error"
            )
            updated = lifecycle_successor(
                current,
                Schema3LifecycleUpdate(target, _canonical_now(self.now_utc()), reason),
            )
            self.write_schema3_manifest(updated, self.run_path(investigation_id, search_run_id))
            loaded = self.load(investigation_id, search_run_id)
            if not isinstance(loaded, RecordingSearchManifestV3):
                raise RecordingSearchManifestCorruptError
            return loaded
        if current.state not in (RecordingSearchState.PENDING, RecordingSearchState.RUNNING):
            raise RecordingSearchTransitionError
        if target not in (
            RecordingSearchState.RUNNING,
            RecordingSearchState.FAILED,
            RecordingSearchState.INTERRUPTED,
        ):
            raise RecordingSearchTransitionError
        if current.state is RecordingSearchState.RUNNING and target is RecordingSearchState.RUNNING:
            raise RecordingSearchTransitionError
        now = _canonical_now(self.now_utc())
        if target is RecordingSearchState.RUNNING:
            updates = {"state": target, "started_at_utc": now}
        else:
            reason = failure_reason or (
                "process_lock_released"
                if target is RecordingSearchState.INTERRUPTED
                else "unexpected_error"
            )
            if reason not in {
                "baseline_validation_failed",
                "process_lock_released",
                "unexpected_error",
            }:
                raise RecordingSearchTransitionError
            updates = {
                "state": target,
                "completed_at_utc": now,
                "failure_reason": reason,
            }
        updated = RecordingSearchManifest.model_validate(
            {**current.model_dump(mode="python"), **updates}, strict=True
        )
        self._write_manifest(updated)
        loaded = self.load(investigation_id, search_run_id)
        if isinstance(loaded, RecordingSearchManifestV4):
            raise RecordingSearchManifestCorruptError
        return loaded

    def promote_schema2(self, manifest: RecordingSearchManifestV2) -> RecordingSearchManifestV2:
        """Publish a schema-2 successor for an active schema-1 run."""
        return promote_schema2(self, manifest)

    def admit_operation(
        self,
        manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
        operation: AcquisitionOperationRecord,
    ) -> RecordingSearchManifestV2 | RecordingSearchManifestV3:
        """Publish one admitted A2 operation and its manifest index successor."""
        return admit_operation(self, manifest, operation)

    def publish_a2_bundle(
        self,
        manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
        request_records: tuple[ProbeFrameRequestRecord, ...],
        frame_records: tuple[tuple[CanonicalProbeFrameRecord, bytes], ...],
    ) -> RecordingSearchManifestV2 | RecordingSearchManifestV3:
        """Publish immutable A2 children and one manifest index successor."""
        return publish_a2_bundle(self, manifest, request_records, frame_records)

    def run_path(self, investigation_id: str, search_run_id: str) -> Path:
        """Return a validated run path without creating it."""
        if not is_recording_search_investigation_id(investigation_id):
            raise RecordingSearchNotFoundError
        if not is_recording_search_run_id(search_run_id):
            raise RecordingSearchNotFoundError
        path = self.root / investigation_id / search_run_id
        if path.parent.parent != self.root or not is_safe_contained_path(self.root, path):
            raise RecordingSearchArtifactError
        return path

    def lock_path(self, investigation_id: str) -> Path:
        """Return the validated per-investigation lock path."""
        if not is_recording_search_investigation_id(investigation_id):
            raise RecordingSearchNotFoundError
        self.ensure_root()
        path = self.root / ".locks" / f"{investigation_id}.lock"
        if path.parent.parent != self.root or not is_safe_contained_path(self.root, path):
            raise RecordingSearchArtifactError
        return path

    def write_schema2_manifest(self, manifest: RecordingSearchManifestV2, directory: Path) -> None:
        """Write one schema-2 manifest through the repository's atomic writer."""
        self._write_manifest_to_directory(manifest, directory)

    def write_schema3_manifest(self, manifest: RecordingSearchManifestV3, directory: Path) -> None:
        """Write one schema-3 manifest through the existing atomic writer."""
        self._write_manifest_to_directory(manifest, directory)

    def write_schema4_manifest(self, manifest: RecordingSearchManifestV4, directory: Path) -> None:
        """Write one schema-4 manifest through the existing atomic writer."""
        self._write_any_manifest_to_directory(manifest, directory)

    def create_or_reuse_phase8_request(
        self, request: Phase8HandoffRequestV1
    ) -> Phase8HandoffResult:
        """Create or reuse the request artifact within this repository's run."""
        run_path = self.run_path(request.investigation_id, request.search_run_id)
        return create_or_reuse_phase8_request(self.root, run_path, request)

    def _write_manifest(self, manifest: SearchManifest) -> None:
        directory = self.run_path(manifest.investigation_id, manifest.search_run_id)
        self._write_any_manifest_to_directory(manifest, directory)

    def _write_manifest_to_directory(self, manifest: LegacySearchManifest, directory: Path) -> None:
        self._write_any_manifest_to_directory(manifest, directory)

    def _write_any_manifest_to_directory(self, manifest: SearchManifest, directory: Path) -> None:
        if not is_safe_contained_path(self.root, directory, require_target=True):
            raise RecordingSearchArtifactError
        if isinstance(
            manifest,
            RecordingSearchManifestV2 | RecordingSearchManifestV3 | RecordingSearchManifestV4,
        ):
            _ = manifest
        else:
            validate_phase7a1_manifest(manifest)
        canonical = manifest.canonical_json()
        _ = _parse_manifest(canonical)
        path = directory / "manifest.json"
        temporary: Path | None = None
        try:
            if path.exists() and path.is_symlink():
                _raise_artifact()
            temporary = path.with_name(f".{manifest.search_run_id}.manifest.tmp")
            if temporary.exists() or temporary.is_symlink():
                _raise_artifact()
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                _ = handle.write(canonical)
                _ = handle.flush()
                _ = os.fsync(handle.fileno())
            _ = temporary.replace(path)
        except RecordingSearchArtifactError:
            raise
        except OSError:
            raise RecordingSearchArtifactError from None
        finally:
            _remove_file(self.root, temporary)

    def _publish_staging_directory(self, staging: Path, destination: Path) -> None:
        if (
            destination.exists()
            or destination.is_symlink()
            or not is_safe_contained_path(self.root, destination)
            or not is_safe_contained_path(self.root, staging, require_target=True)
        ):
            _raise_artifact()
        try:
            _ = staging.rename(destination)
        except OSError:
            raise RecordingSearchArtifactError from None


def _canonical_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise RecordingSearchArtifactError
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _raise_artifact() -> NoReturn:
    raise RecordingSearchArtifactError


def _raise_corrupt() -> NoReturn:
    raise RecordingSearchManifestCorruptError


def _legacy_nonterminal(
    manifest: SearchManifest,
) -> RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchManifestV3 | None:
    if isinstance(manifest, RecordingSearchManifestV4):
        return None
    if manifest.state in (RecordingSearchState.PENDING, RecordingSearchState.RUNNING):
        return manifest
    return None


def _legacy_transition_manifest(
    manifest: SearchManifest,
) -> RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchManifestV3:
    if isinstance(manifest, RecordingSearchManifestV4):
        raise RecordingSearchTransitionError
    return manifest


def _parse_manifest(raw: str) -> SearchManifest:
    _ = load_durable_json_object(raw)
    try:
        schema = _.get("schema_version")
    except AttributeError:
        schema = None
    if schema == _SCHEMA2:
        return parse_schema2_manifest(raw)
    if schema == _SCHEMA3:
        return parse_schema3_manifest(raw)
    if schema == _SCHEMA4:
        try:
            return RecordingSearchManifestV4.model_validate_json(raw, strict=True)
        except (DurableJsonError, ValidationError, ValueError, TypeError):
            raise RecordingSearchManifestCorruptError from None
    manifest = RecordingSearchManifest.model_validate_json(raw, strict=True)
    validate_phase7a1_manifest(manifest)
    return manifest


def _remove_directory(root: Path, path: Path | None) -> None:
    if path is None:
        return
    with suppress(OSError):
        if (
            path.exists()
            and not path.is_symlink()
            and is_safe_contained_path(root, path, require_target=True)
        ):
            shutil.rmtree(path)


def _remove_file(root: Path, path: Path | None) -> None:
    if path is None:
        return
    with suppress(OSError):
        if (
            path.exists()
            and not path.is_symlink()
            and is_safe_contained_path(root, path, require_target=True)
        ):
            path.unlink()
