from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
from typing import TYPE_CHECKING, Final, Protocol

from vigi_vision.durable_io import is_safe_contained_path, is_safe_path
from vigi_vision.investigation_confirmation_models import (
    ConfirmationArtifactError,
    ConfirmationCorruptError,
    ConfirmationManifest,
    ConfirmationTiming,
)

_ERROR_ACCESS_DENIED: Final = 5
_ERROR_FILE_EXISTS: Final = 80
_ERROR_ALREADY_EXISTS: Final = 183
_MOVEFILE_WRITE_THROUGH: Final = 0x00000008
_RENAME_NOREPLACE: Final = 1


class _RenameAt2(Protocol):
    argtypes: list[type[ctypes.c_int | ctypes.c_char_p | ctypes.c_uint]]
    restype: type[ctypes.c_int]

    def __call__(
        self, left: int, left_path: bytes, right: int, right_path: bytes, flags: int
    ) -> int: ...


if TYPE_CHECKING:
    from pathlib import Path

    from vigi_vision.reference_frame_resources import ReferenceFrameResourceMetadata


def resource_matches(
    resource: ReferenceFrameResourceMetadata, manifest: ConfirmationManifest
) -> bool:
    reference = manifest.confirmation.reference_frame
    timing = manifest.confirmation.timing
    request = resource.request
    return (
        resource.resource_id == reference.resource_id
        and resource.manifest_schema_version == reference.schema_version
        and request.generation_policy_version == reference.generation_policy_version
        and request.channel_id == manifest.confirmation.channel_id
        and request.requested_time_text == reference.requested_time
        and request.requested_time_utc == reference.requested_time_utc
        and request.source_timezone == reference.source_timezone == manifest.source_timezone
        and request.frame_selection_policy.value == reference.frame_selection_policy
        and candidate_offset_matches(resource, manifest)
        and resource.width == reference.width
        and resource.height == reference.height
        and manifest.confirmation.roi.x + manifest.confirmation.roi.width <= resource.width
        and manifest.confirmation.roi.y + manifest.confirmation.roi.height <= resource.height
        and resource.jpeg_path.is_file()
        and timing_matches(resource, timing)
    )


def timing_matches(resource: ReferenceFrameResourceMetadata, timing: ConfirmationTiming) -> bool:
    return (
        resource.decoded_local_pts_seconds == timing.decoded_local_pts_seconds
        and resource.estimated_source_time_utc == timing.estimated_source_time_utc
        and resource.offset_from_requested_seconds == timing.offset_from_requested_seconds
        and resource.timing_precision_status.value == timing.timing_precision_status
        and resource.warnings == timing.warnings
    )


def candidate_offset_matches(
    resource: ReferenceFrameResourceMetadata, manifest: ConfirmationManifest
) -> bool:
    difference = resource.request.requested_time_utc - manifest.anchor_time_utc
    seconds = difference.total_seconds()
    return seconds == int(seconds) and int(seconds) == (
        manifest.confirmation.candidate_offset_seconds
    )


def ensure_root(root: Path) -> None:
    try:
        if not is_safe_path(root):
            raise ConfirmationArtifactError
        if root.exists() and not is_safe_contained_path(root.parent, root, require_target=True):
            raise ConfirmationArtifactError
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ConfirmationArtifactError
        root.mkdir(parents=True, exist_ok=True)
        if not is_safe_contained_path(root, root, require_target=True):
            raise ConfirmationArtifactError
    except OSError:
        raise ConfirmationArtifactError from None


def entry_exists(root: Path, path: Path) -> bool:
    try:
        if not is_safe_contained_path(root, path):
            raise ConfirmationArtifactError
        return path.is_symlink() or path.exists()
    except OSError:
        raise ConfirmationArtifactError from None


def direct_child(root: Path, name: str) -> Path:
    candidate = root / name
    if candidate.parent != root:
        raise ConfirmationArtifactError
    return candidate


def validate_final_directory(path: Path) -> None:
    try:
        invalid = path.is_symlink() or not path.is_dir()
    except OSError:
        raise ConfirmationCorruptError from None
    if invalid:
        raise ConfirmationCorruptError


def remove_file(root: Path, path: Path) -> None:
    try:
        if is_safe_contained_path(root, path, require_target=True) and (
            path.is_symlink() or path.is_file()
        ):
            path.unlink(missing_ok=True)
    except OSError:
        return


def remove_staging(root: Path, path: Path) -> None:
    try:
        if is_safe_contained_path(root, path, require_target=True) and path.is_dir():
            shutil.rmtree(path)
    except OSError:
        return


def publish_directory_no_replace(source: Path, destination: Path) -> bool:
    """Atomically move one directory without replacing an existing destination.

    Windows uses ``MoveFileExW`` without replace-existing, and Linux uses
    ``renameat2`` with ``RENAME_NOREPLACE``; unsupported platforms fail closed.
    """
    if not is_safe_contained_path(source.parent, source, require_target=True):
        raise ConfirmationArtifactError
    if not is_safe_contained_path(source.parent, destination):
        raise ConfirmationArtifactError
    if os.name == "nt":
        return _windows_move_no_replace(source, destination)
    if sys.platform.startswith("linux"):
        return _linux_rename_no_replace(source, destination)
    raise ConfirmationArtifactError


def _windows_move_no_replace(source: Path, destination: Path) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move_file.restype = ctypes.c_int
    if move_file(str(source), str(destination), _MOVEFILE_WRITE_THROUGH):
        return True
    error = ctypes.get_last_error()
    if error in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS) or (
        error == _ERROR_ACCESS_DENIED and destination.exists()
    ):
        return False
    raise ConfirmationArtifactError


def _linux_rename_no_replace(source: Path, destination: Path) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    native: _RenameAt2 | None = getattr(libc, "renameat2", None)
    if native is None:
        raise ConfirmationArtifactError
    native.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    native.restype = ctypes.c_int
    renameat2 = native
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        return False
    raise ConfirmationArtifactError


def sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        os.close(descriptor)
