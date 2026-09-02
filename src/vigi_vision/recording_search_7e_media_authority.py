# pyright: reportAny=false, reportExplicitAny=false, reportUnannotatedClassAttribute=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnusedCallResult=false

"""Platform-bound retained-media filesystem authority for Phase 7E.

This operational record is deliberately outside every Phase 7E semantic
identity family.  It binds the already-authoritative retained MP4 bytes to the
exact filesystem object that carried them after final publication.
"""

# The platform capability wrapper intentionally keeps Windows imports local and
# every validation branch explicit.
# ruff: noqa: D102, D103, EM101, PLC0415, PLR2004, TRY003, TRY300, TRY301

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vigi_vision.durable_io import is_safe_contained_path, is_safe_path, load_durable_json_object

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping


class MediaFilesystemAuthorityError(RuntimeError):
    """The operational filesystem authority is absent, corrupt, or unsupported."""


_AUTHORITY_KEYS = {
    "schema_version",
    "investigation_id",
    "run_id",
    "common_session_id",
    "replay_operation_id",
    "relative_media_path",
    "sha256",
    "size_bytes",
    "selected_video_stream_index",
    "container_start_pts",
    "time_base_num",
    "time_base_den",
    "duration_ticks",
    "filesystem_identity",
    "file_stamp",
}
_IDENTITY_KEYS = {"platform", "volume_id", "file_id"}
_STAMP_KEYS = {"size_bytes", "modified_ns", "link_count"}
_MAX_AUTHORITY_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class RetainedMediaFilesystemAuthority:
    """Strict versioned operational binding for one retained common-session MP4."""

    payload: dict[str, Any]

    @classmethod
    def parse(cls, value: object) -> RetainedMediaFilesystemAuthority:
        if not isinstance(value, dict) or set(value) != _AUTHORITY_KEYS:
            raise MediaFilesystemAuthorityError
        identity = value.get("filesystem_identity")
        stamp_value = value.get("file_stamp")
        if not isinstance(identity, dict) or set(identity) != _IDENTITY_KEYS:
            raise MediaFilesystemAuthorityError
        if not isinstance(stamp_value, dict) or set(stamp_value) != _STAMP_KEYS:
            raise MediaFilesystemAuthorityError
        string_keys = (
            "investigation_id",
            "run_id",
            "common_session_id",
            "replay_operation_id",
            "relative_media_path",
            "sha256",
        )
        integer_keys = (
            "size_bytes",
            "selected_video_stream_index",
            "container_start_pts",
            "time_base_num",
            "time_base_den",
            "duration_ticks",
        )
        if value.get("schema_version") != 1:
            raise MediaFilesystemAuthorityError
        if any(not isinstance(value.get(key), str) or not value[key] for key in string_keys):
            raise MediaFilesystemAuthorityError
        if any(
            isinstance(value.get(key), bool) or not isinstance(value.get(key), int)
            for key in integer_keys
        ):
            raise MediaFilesystemAuthorityError
        if (
            identity.get("platform") not in {"windows", "posix"}
            or isinstance(identity.get("volume_id"), bool)
            or not isinstance(identity.get("volume_id"), int)
            or isinstance(identity.get("file_id"), bool)
            or not isinstance(identity.get("file_id"), int)
            or any(
                isinstance(stamp_value.get(key), bool) or not isinstance(stamp_value.get(key), int)
                for key in _STAMP_KEYS
            )
            or value["size_bytes"] <= 0
            or value["time_base_num"] <= 0
            or value["time_base_den"] <= 0
            or len(value["sha256"]) != 64
        ):
            raise MediaFilesystemAuthorityError
        return cls(dict(value))

    @property
    def filesystem_identity(self) -> dict[str, object]:
        return dict(self.payload["filesystem_identity"])

    @property
    def file_stamp(self) -> dict[str, int]:
        return dict(self.payload["file_stamp"])

    def canonical_bytes(self) -> bytes:
        return _canonical(self.payload).encode("utf-8")


def authority_path(media_path: Path) -> Path:
    """Return the only admitted authority-record path for a retained MP4."""
    return media_path.with_suffix(".authority.json")


def open_stable_file(path: Path, *, delete_access: bool = False) -> int:
    """Open a regular file while denying replacement; optionally request DELETE."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if os.name != "nt":
        if delete_access:
            raise MediaFilesystemAuthorityError("exact deletion unsupported")
        return os.open(path, flags)
    import ctypes
    import msvcrt

    desired_access = 0x80000000 | (0x00010000 if delete_access else 0)
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        desired_access,
        0x00000001,  # FILE_SHARE_READ only: deny write/delete/rename while verified.
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    try:
        return msvcrt.open_osfhandle(int(handle), flags)
    except BaseException:
        ctypes.windll.kernel32.CloseHandle(handle)
        raise


def create_publication_file(path: Path) -> int:
    """Create one replacement-denying file whose handle can publish itself."""
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if os.name != "nt":
        return os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    import ctypes
    import msvcrt

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80000000 | 0x40000000 | 0x00010000,  # GENERIC_READ | WRITE | DELETE
        0x00000001,  # FILE_SHARE_READ: deny write/delete/rename by every other open.
        None,
        1,  # CREATE_NEW
        0x00200000 | 0x00000080,  # OPEN_REPARSE_POINT | FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    try:
        return msvcrt.open_osfhandle(int(handle), flags)
    except BaseException:
        ctypes.windll.kernel32.CloseHandle(handle)
        raise


def rename_open_file_no_replace(descriptor: int, source: Path, destination: Path) -> None:
    """Bind the exact open object to ``destination`` without replacing an occupant."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    _verify_path_names_descriptor(descriptor, source)
    if os.name != "nt":
        os.link(source, destination, follow_symlinks=False)
        try:
            _verify_path_names_descriptor(descriptor, destination)
            source.unlink()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileRenameInformation(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", wintypes.BOOL),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
            ("file_name", wintypes.WCHAR * 1),
        ]

    target = str(destination.absolute())
    target_bytes = target.encode("utf-16-le")
    buffer_size = (
        FileRenameInformation.file_name.offset + len(target_bytes) + ctypes.sizeof(wintypes.WCHAR)
    )
    buffer = ctypes.create_string_buffer(buffer_size)
    information = FileRenameInformation.from_buffer(buffer)
    information.replace_if_exists = 0
    information.root_directory = None
    information.file_name_length = len(target_bytes)
    ctypes.memmove(
        ctypes.addressof(buffer) + FileRenameInformation.file_name.offset,
        target_bytes,
        len(target_bytes),
    )
    handle = msvcrt.get_osfhandle(descriptor)
    if not ctypes.windll.kernel32.SetFileInformationByHandle(
        handle,
        3,  # FileRenameInfo
        ctypes.byref(buffer),
        buffer_size,
    ):
        raise OSError(ctypes.get_last_error(), "SetFileInformationByHandle(FileRenameInfo) failed")
    if source.exists() or source.is_symlink():
        raise MediaFilesystemAuthorityError


def filesystem_identity(descriptor: int) -> dict[str, object]:
    """Capture the strongest supported stable identity from an open handle."""
    if os.name != "nt":
        value = os.fstat(descriptor)
        return {"platform": "posix", "volume_id": value.st_dev, "file_id": value.st_ino}
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time_low", wintypes.DWORD),
            ("creation_time_high", wintypes.DWORD),
            ("last_access_time_low", wintypes.DWORD),
            ("last_access_time_high", wintypes.DWORD),
            ("last_write_time_low", wintypes.DWORD),
            ("last_write_time_high", wintypes.DWORD),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    info = ByHandleFileInformation()
    handle = msvcrt.get_osfhandle(descriptor)
    if not ctypes.windll.kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
    if info.file_attributes & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
        raise MediaFilesystemAuthorityError
    return {
        "platform": "windows",
        "volume_id": int(info.volume_serial_number),
        "file_id": (int(info.file_index_high) << 32) | int(info.file_index_low),
    }


def descriptor_stamp(descriptor: int) -> dict[str, int]:
    value = os.fstat(descriptor)
    if not stat.S_ISREG(value.st_mode):
        raise MediaFilesystemAuthorityError
    return {
        "size_bytes": value.st_size,
        "modified_ns": value.st_mtime_ns,
        "link_count": value.st_nlink,
    }


def hash_descriptor(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while block := os.read(descriptor, 1024 * 1024):
        digest.update(block)
        size += len(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest(), size


def verify_open_file(
    descriptor: int,
    path: Path,
    authority: RetainedMediaFilesystemAuthority,
) -> None:
    """Compare an open current object with publication-time authority exactly."""
    if filesystem_identity(descriptor) != authority.filesystem_identity:
        raise MediaFilesystemAuthorityError
    if descriptor_stamp(descriptor) != authority.file_stamp:
        raise MediaFilesystemAuthorityError
    digest, size = hash_descriptor(descriptor)
    if digest != authority.payload["sha256"] or size != authority.payload["size_bytes"]:
        raise MediaFilesystemAuthorityError
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as error:
        raise MediaFilesystemAuthorityError from error
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise MediaFilesystemAuthorityError
    _verify_path_names_descriptor(descriptor, path)
    if filesystem_identity(descriptor) != authority.filesystem_identity:
        raise MediaFilesystemAuthorityError


def read_retained_media_authority(
    media_root: Path,
    media_path: Path,
    session: Mapping[str, object],
) -> RetainedMediaFilesystemAuthority:
    """Strictly read the immutable authority record without repair or migration."""
    target = authority_path(media_path)
    try:
        if (
            media_root.is_symlink()
            or not media_root.is_dir()
            or not is_safe_path(media_root, require_target=True)
            or target.is_symlink()
            or not target.is_file()
            or not is_safe_path(target, require_target=True)
            or not is_safe_contained_path(media_root, target, require_target=True)
        ):
            raise MediaFilesystemAuthorityError
        raw = _read_bound_regular_file(target).decode("utf-8")
        record = RetainedMediaFilesystemAuthority.parse(load_durable_json_object(raw))
        if raw.encode("utf-8") != record.canonical_bytes():
            raise MediaFilesystemAuthorityError
        expected_relative = media_path.relative_to(media_root.parent).as_posix()
        expected = {
            "investigation_id": session["investigation_id"],
            "run_id": session["run_id"],
            "common_session_id": session["common_session_id"],
            "replay_operation_id": session["replay_operation_id"],
            "relative_media_path": expected_relative,
            "sha256": session["mp4_sha256"],
            "size_bytes": session["mp4_size_bytes"],
            "selected_video_stream_index": session["selected_video_stream_index"],
            "container_start_pts": session["container_start_pts"],
            "time_base_num": session["time_base_num"],
            "time_base_den": session["time_base_den"],
            "duration_ticks": session["duration_ticks"],
        }
        if any(record.payload.get(key) != value for key, value in expected.items()):
            raise MediaFilesystemAuthorityError
        return record
    except MediaFilesystemAuthorityError:
        raise
    except Exception as error:
        raise MediaFilesystemAuthorityError from error


def publish_retained_media_authority(
    media_root: Path,
    media_path: Path,
    session: Mapping[str, object],
    *,
    descriptor: int | None = None,
) -> RetainedMediaFilesystemAuthority:
    """Capture final-object authority and atomically admit its strict record."""
    if (
        media_path.is_symlink()
        or not media_path.is_file()
        or not is_safe_path(media_path, require_target=True)
        or not is_safe_contained_path(media_root, media_path, require_target=True)
    ):
        raise MediaFilesystemAuthorityError
    owned_descriptor = descriptor is None
    active_descriptor = open_stable_file(media_path) if descriptor is None else descriptor
    try:
        digest, size = hash_descriptor(active_descriptor)
        if digest != session["mp4_sha256"] or size != session["mp4_size_bytes"]:
            raise MediaFilesystemAuthorityError
        identity = filesystem_identity(active_descriptor)
        stamp_value = descriptor_stamp(active_descriptor)
        if stamp_value["link_count"] != 1:
            raise MediaFilesystemAuthorityError
        payload = {
            "schema_version": 1,
            "investigation_id": session["investigation_id"],
            "run_id": session["run_id"],
            "common_session_id": session["common_session_id"],
            "replay_operation_id": session["replay_operation_id"],
            "relative_media_path": media_path.relative_to(media_root.parent).as_posix(),
            "sha256": digest,
            "size_bytes": size,
            "selected_video_stream_index": session["selected_video_stream_index"],
            "container_start_pts": session["container_start_pts"],
            "time_base_num": session["time_base_num"],
            "time_base_den": session["time_base_den"],
            "duration_ticks": session["duration_ticks"],
            "filesystem_identity": identity,
            "file_stamp": stamp_value,
        }
        record = RetainedMediaFilesystemAuthority.parse(payload)
        verify_open_file(active_descriptor, media_path, record)
        target = authority_path(media_path)
        if target.exists() or target.is_symlink():
            existing = read_retained_media_authority(media_root, media_path, session)
            if existing != record:
                raise MediaFilesystemAuthorityError
        else:
            _atomic_write_no_replace(target, record.canonical_bytes())
        reopened = read_retained_media_authority(media_root, media_path, session)
        if reopened != record:
            raise MediaFilesystemAuthorityError
        verify_open_file(active_descriptor, media_path, reopened)
        return reopened
    finally:
        if owned_descriptor:
            os.close(active_descriptor)


def mark_open_file_for_deletion(descriptor: int) -> None:
    """Mark the exact opened Windows object for deletion; never path-fallback."""
    if os.name != "nt":
        raise MediaFilesystemAuthorityError("exact deletion unsupported")
    import ctypes
    import msvcrt

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", ctypes.c_ubyte)]

    info = FileDispositionInfo(1)
    handle = msvcrt.get_osfhandle(descriptor)
    if not ctypes.windll.kernel32.SetFileInformationByHandle(
        handle, 4, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise OSError(ctypes.get_last_error(), "SetFileInformationByHandle failed")


def stable_source_path(descriptor: int, live_path: Path) -> Path:
    """Return a subprocess-readable path for the exact held file object."""
    if os.name == "nt":
        return live_path
    descriptor_path = Path(f"/proc/{os.getpid()}/fd/{descriptor}")
    if not descriptor_path.exists():
        raise MediaFilesystemAuthorityError
    return descriptor_path


@contextmanager
def verified_retained_media(
    media_root: Path,
    media_path: Path,
    session: Mapping[str, object],
) -> Generator[tuple[int, RetainedMediaFilesystemAuthority], None, None]:
    record = read_retained_media_authority(media_root, media_path, session)
    descriptor = open_stable_file(media_path)
    try:
        verify_open_file(descriptor, media_path, record)
        yield descriptor, record
        verify_open_file(descriptor, media_path, record)
    finally:
        os.close(descriptor)


def _atomic_write_no_replace(path: Path, value: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            _windows_move_write_through(temporary, path, replace=False)
        else:
            os.link(temporary, path)
            temporary.unlink()
        _fsync_directory(path.parent)
    except FileExistsError as error:
        raise MediaFilesystemAuthorityError from error
    finally:
        temporary.unlink(missing_ok=True)


def _read_bound_regular_file(path: Path) -> bytes:
    """Read one exact confined object without a path-replacement window."""
    descriptor = open_stable_file(path)
    try:
        before = descriptor_stamp(descriptor)
        if before["link_count"] != 1 or before["size_bytes"] > _MAX_AUTHORITY_BYTES:
            raise MediaFilesystemAuthorityError
        os.lseek(descriptor, 0, os.SEEK_SET)
        blocks: list[bytes] = []
        size = 0
        while block := os.read(descriptor, 8192):
            size += len(block)
            if size > _MAX_AUTHORITY_BYTES:
                raise MediaFilesystemAuthorityError
            blocks.append(block)
        after = descriptor_stamp(descriptor)
        if after != before or size != before["size_bytes"]:
            raise MediaFilesystemAuthorityError
        identity = filesystem_identity(descriptor)
        confirmation = open_stable_file(path)
        try:
            if filesystem_identity(confirmation) != identity:
                raise MediaFilesystemAuthorityError
        finally:
            os.close(confirmation)
        return b"".join(blocks)
    finally:
        os.close(descriptor)


def _verify_path_names_descriptor(descriptor: int, path: Path) -> None:
    confirmation = _open_identity_confirmation(path)
    try:
        if filesystem_identity(confirmation) != filesystem_identity(descriptor):
            raise MediaFilesystemAuthorityError
    finally:
        os.close(confirmation)


def _open_identity_confirmation(path: Path) -> int:
    """Open a read handle compatible with the already-held publication/delete handle."""
    if os.name != "nt":
        return open_stable_file(path)
    import ctypes
    import msvcrt

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete with held handle
        None,
        3,
        0x00200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        raise OSError(ctypes.get_last_error(), "CreateFileW confirmation failed")
    try:
        return msvcrt.open_osfhandle(int(handle), flags)
    except BaseException:
        ctypes.windll.kernel32.CloseHandle(handle)
        raise


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _windows_move_write_through(source: Path, destination: Path, *, replace: bool) -> None:
    import ctypes

    move_file = ctypes.windll.kernel32.MoveFileExW
    flags = 0x00000008 | (0x00000001 if replace else 0)  # WRITE_THROUGH | REPLACE_EXISTING
    if not move_file(str(source), str(destination), flags):
        raise OSError(ctypes.get_last_error(), "MoveFileExW failed")


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "MediaFilesystemAuthorityError",
    "RetainedMediaFilesystemAuthority",
    "authority_path",
    "create_publication_file",
    "descriptor_stamp",
    "filesystem_identity",
    "hash_descriptor",
    "mark_open_file_for_deletion",
    "open_stable_file",
    "publish_retained_media_authority",
    "read_retained_media_authority",
    "rename_open_file_no_replace",
    "stable_source_path",
    "verified_retained_media",
    "verify_open_file",
]
