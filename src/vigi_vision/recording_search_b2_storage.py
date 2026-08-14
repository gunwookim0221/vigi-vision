"""Confined no-overwrite storage helpers for schema-3 publication."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from vigi_vision.durable_io import is_safe_contained_path
from vigi_vision.recording_search_b2_models import child_record_id
from vigi_vision.recording_search_b2_records import ChildRecord, ClassificationOperationRecord
from vigi_vision.recording_search_models import RecordingSearchArtifactError

if TYPE_CHECKING:
    from pydantic import BaseModel


def ensure_schema3_directories(root: Path, run_path: Path) -> tuple[Path, ...]:
    """Create only the confined schema-3 child directories that are missing."""
    created: list[Path] = []
    for relative in ("classification-operations", "observations"):
        path = run_path / relative
        try:
            _require_safe(condition=is_safe_contained_path(root, path.parent, require_target=True))
            existed = path.exists()
            _require_safe(condition=not existed or (not path.is_symlink() and path.is_dir()))
            path.mkdir(exist_ok=True)
            _require_safe(condition=is_safe_contained_path(root, path, require_target=True))
            if not existed:
                created.append(path)
        except OSError:
            for directory in reversed(created):
                remove_empty_owned_directory(root, directory)
            raise RecordingSearchArtifactError from None
        except RecordingSearchArtifactError:
            for directory in reversed(created):
                remove_empty_owned_directory(root, directory)
            raise
    return tuple(created)


def _require_safe(*, condition: bool) -> None:
    if not condition:
        raise RecordingSearchArtifactError


def child_relative_path(child: ChildRecord) -> Path:
    """Return the canonical relative path for one schema-3 child record."""
    child_id = child_record_id(child)
    if isinstance(child, ClassificationOperationRecord):
        return Path("classification-operations") / f"{child_id}.json"
    return Path("observations") / f"{child_id}.json"


def canonical_record_json(model: BaseModel) -> str:
    """Serialize one record as deterministic UTF-8 JSON text."""
    return (
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    )


def write_new_child(path: Path, payload: str) -> None:
    """Create and fsync one staging child without overwriting existing data."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            _ = stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise RecordingSearchArtifactError from None


def move_new_child(root: Path, source: Path, destination: Path) -> None:
    """Move one confined staged child into an unused durable path."""
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


def remove_owned_file(root: Path, path: Path) -> None:
    """Best-effort remove one confirmed invocation-owned confined file."""
    try:
        if is_safe_contained_path(root, path, require_target=True) and not path.is_symlink():
            path.unlink()
    except OSError:
        pass


def remove_owned_directory(root: Path, path: Path | None) -> None:
    """Best-effort remove one confirmed invocation-owned confined directory."""
    if path is None:
        return
    try:
        if (
            path.exists()
            and not path.is_symlink()
            and is_safe_contained_path(root, path, require_target=True)
        ):
            shutil.rmtree(path)
    except OSError:
        pass


def remove_empty_owned_directory(root: Path, path: Path) -> None:
    """Best-effort remove an empty confined directory created by publication."""
    try:
        if (
            path.is_dir()
            and not path.is_symlink()
            and is_safe_contained_path(root, path, require_target=True)
            and not any(path.iterdir())
        ):
            path.rmdir()
    except OSError:
        pass
