"""Local immutable recording-search run repository."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

from pydantic import ValidationError

from vigi_vision.durable_io import (
    DurableJsonError,
    is_safe_contained_path,
    is_safe_path,
    load_durable_json_object,
)
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@dataclass(frozen=True, slots=True)
class RecordingSearchRepository:
    """Persist one isolated run tree below the ignored Phase 7 artifact root."""

    root: Path = field(repr=False)
    now_utc: Callable[[], datetime] = _utc_now

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

    def load(self, investigation_id: str, search_run_id: str) -> RecordingSearchManifest:
        """Strictly load one persisted manifest."""
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
        except RecordingSearchManifestCorruptError:
            raise
        except (DurableJsonError, OSError, UnicodeError, ValidationError, ValueError):
            raise RecordingSearchManifestCorruptError from None
        if manifest.investigation_id != investigation_id or manifest.search_run_id != search_run_id:
            raise RecordingSearchManifestCorruptError
        return manifest

    def latest_nonterminal(self, investigation_id: str) -> RecordingSearchManifest | None:
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
        manifests: list[RecordingSearchManifest] = []
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
            if manifest.state in (RecordingSearchState.PENDING, RecordingSearchState.RUNNING):
                manifests.append(manifest)
        return max(manifests, key=lambda value: value.created_at_utc) if manifests else None

    def transition(
        self,
        investigation_id: str,
        search_run_id: str,
        target: RecordingSearchState,
        failure_reason: str | None = None,
    ) -> RecordingSearchManifest:
        """Atomically apply one Phase 7A-1 lifecycle transition."""
        current = self.load(investigation_id, search_run_id)
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
        return self.load(investigation_id, search_run_id)

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

    def _write_manifest(self, manifest: RecordingSearchManifest) -> None:
        directory = self.run_path(manifest.investigation_id, manifest.search_run_id)
        self._write_manifest_to_directory(manifest, directory)

    def _write_manifest_to_directory(
        self, manifest: RecordingSearchManifest, directory: Path
    ) -> None:
        if not is_safe_contained_path(self.root, directory, require_target=True):
            raise RecordingSearchArtifactError
        validate_phase7a1_manifest(manifest)
        canonical = manifest.canonical_json()
        _ = _parse_manifest(canonical)
        path = directory / "manifest.json"
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


def _parse_manifest(raw: str) -> RecordingSearchManifest:
    _ = load_durable_json_object(raw)
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
