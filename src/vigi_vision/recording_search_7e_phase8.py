"""Strict Phase 8 source-clip, handoff, and retained-media lifecycle.

This module owns only the approved Phase 7E-2 projection boundary.  It derives
one bounded local source clip from the already retained common-session MP4,
publishes the closed Phase 8 package, and implements the durable two-media
deletion state machine.  It never opens the NVR or performs analysis.
"""

# The state machine deliberately keeps every publication and recovery branch
# explicit.  Complexity exemptions describe the closed persistence contract,
# not an open-ended implementation surface.
# ruff: noqa: B009, C901, D102, D107, EM101, PLC0415, PLR0911, PLR0912, PLR0913, PLR0915, PLR2004, PTH105, SIM117, TC001, TRY300, TRY301
# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportReturnType=false, reportUnannotatedClassAttribute=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnnecessaryIsInstance=false, reportUnusedCallResult=false

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vigi_vision.durable_io import (
    is_safe_contained_path,
    is_safe_path,
    load_durable_json_object,
)
from vigi_vision.recording_search_7e_1c import MediaProbe, MediaProbeFacts
from vigi_vision.recording_search_7e_media_authority import (
    MediaFilesystemAuthorityError,
    RetainedMediaFilesystemAuthority,
    authority_path,
    descriptor_stamp,
    filesystem_identity,
    mark_open_file_for_deletion,
    open_stable_file,
    read_retained_media_authority,
    rename_open_file_no_replace,
)
from vigi_vision.recording_search_7e_media_authority import (
    stable_source_path as stable_descriptor_path,
)
from vigi_vision.recording_search_7e_models import StrictIdentityEnvelope

if TYPE_CHECKING:
    from collections.abc import Callable, Generator


_MANIFEST = "manifest.json"
_SOURCE_CLIP = "source-clip.json"
_REQUEST = "phase8-request.json"
_JOURNAL = "journal.json"
_PACKAGE = "package"
_MAX_CLIP_BYTES = 536_870_912
_MAX_CLIP_SECONDS = 41
_MAX_FRAME_RATE = Fraction(60, 1)
_DELETION_OBJECT_STATES = {
    "not_moved",
    "move_intent",
    "moved",
    "disposition_intent",
    "disposition_observed",
    "disposition_completed",
    "deleted",
}
_DELETION_OBJECT_KEYS = {
    "role",
    "state",
    "live_path",
    "tombstone_path",
    "sha256",
    "size_bytes",
    "stamp",
    "filesystem_identity",
    "authority_binding",
}
_DELETION_JOURNAL_KEYS = {
    "schema_version",
    "kind",
    "investigation_id",
    "run_id",
    "final_root",
    "transition",
    "owner_id",
    "lifecycle_state",
    "lifecycle_generation",
    "ready_manifest_id",
    "deleting_manifest_id",
    "objects",
}


class Phase8LifecycleError(RuntimeError):
    """Safe internal Phase 8 failure with a public category."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SourceClipGenerator(Protocol):
    """Generate one local source clip without reopening the NVR."""

    def generate(
        self,
        source: Path,
        destination: Path,
        *,
        stream_index: int,
        offset_seconds: int,
        duration_seconds: int,
        timeout_seconds: float,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class FfmpegSourceClipGenerator:
    """Produce the approved H.264 review clip from retained local media."""

    executable: Path
    runner: Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]] = field(
        default=lambda args, timeout: subprocess.run(  # noqa: S603
            args,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        ),
        repr=False,
    )

    def generate(
        self,
        source: Path,
        destination: Path,
        *,
        stream_index: int,
        offset_seconds: int,
        duration_seconds: int,
        timeout_seconds: float,
    ) -> str:
        """Re-encode exactly one bounded interval using the approved policy."""
        arguments = (
            str(self.executable),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-ss",
            str(offset_seconds),
            "-i",
            str(source),
            "-map",
            f"0:{stream_index}",
            "-t",
            str(duration_seconds),
            "-an",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-level:v",
            "4.1",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-fps_mode",
            "passthrough",
            "-movflags",
            "+faststart",
            "-y",
            str(destination),
        )
        try:
            completed = self.runner(arguments, timeout_seconds)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Phase8LifecycleError("phase8_clip_failed") from error
        if completed.returncode != 0:
            raise Phase8LifecycleError("phase8_clip_failed")
        return "REENCODED"


@dataclass(frozen=True, slots=True)
class _FileStamp:
    device: int
    inode: int
    size: int
    modified_ns: int
    link_count: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _FileStamp:
        return cls(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_nlink)


@dataclass(frozen=True, slots=True)
class _VerifiedFile:
    path: Path
    descriptor: int
    stamp: _FileStamp
    sha256: str
    size_bytes: int
    facts: MediaProbeFacts
    filesystem_identity: dict[str, object]
    authority: RetainedMediaFilesystemAuthority | None = None


@dataclass(frozen=True, slots=True)
class Phase8Package:
    """Strictly reopened Phase 8 package."""

    root: Path
    manifest: StrictIdentityEnvelope
    source_clip: StrictIdentityEnvelope
    request: StrictIdentityEnvelope | None
    clip_path: Path | None

    @property
    def state(self) -> str:
        return str(self.manifest.payload["state"])


@dataclass(slots=True)
class Phase8HandoffRepository:
    """Closed Phase 8 repository beneath one recording-search root."""

    root: Path
    media_root: Path
    media_probe: MediaProbe
    clip_generator: SourceClipGenerator
    checkpoint: Callable[[str], None] = lambda _name: None

    @property
    def staging_root(self) -> Path:
        return self.root.parent / ".phase8-staging"

    def create_or_reuse(
        self,
        run: object,
        media_policy: StrictIdentityEnvelope,
        *,
        timeout_seconds: float,
    ) -> StrictIdentityEnvelope:
        """Generate and atomically publish one exact READY package."""
        terminal, session, snapshot = _terminal_authority(run)
        investigation_id = str(getattr(run, "investigation_id"))
        run_id = str(getattr(run, "run_id"))
        final = self._directory(investigation_id, run_id, create_parent=True)
        self._recover_staging(investigation_id, run_id, final)
        source_clip = _source_clip_envelope(
            investigation_id,
            run_id,
            terminal,
            session,
            media_policy,
        )
        if final.exists() or final.is_symlink():
            try:
                package = self.reopen(run)
            except Phase8LifecycleError as error:
                if error.code != "phase8_corrupt":
                    raise
                existing_manifest = self._read_envelope(final / _MANIFEST)
                existing_source = self._read_envelope(final / _SOURCE_CLIP)
                if (
                    existing_manifest.family == "phase8-manifest"
                    and existing_source.family == "source-clip"
                    and existing_manifest.payload.get("investigation_id") == investigation_id
                    and existing_manifest.payload.get("run_id") == run_id
                    and (
                        existing_manifest.payload.get("terminal_result_id") != terminal.identity
                        or existing_manifest.payload.get("common_session_id") != session.identity
                        or existing_source != source_clip
                    )
                ):
                    raise Phase8LifecycleError("phase8_conflict") from error
                raise
            if package.state != "READY" or package.source_clip != source_clip:
                raise Phase8LifecycleError("phase8_conflict")
            if package.request is None:
                raise Phase8LifecycleError("phase8_corrupt")
            return package.request

        source_path = self._common_media_path(investigation_id, run_id, session.identity)
        with self._verified_common_media(source_path, session.payload, timeout_seconds):
            pass
        wrapper = self._new_staging(investigation_id, run_id, final, "handoff")
        package_root = wrapper / _PACKAGE
        try:
            (package_root / "manifests").mkdir(parents=True, exist_ok=False)
            (package_root / "clips").mkdir(exist_ok=False)
            with self._verified_common_media(
                source_path, session.payload, timeout_seconds
            ) as source:
                clipped_start, clipped_end = _clip_interval(terminal.payload, session.payload)
                offset = int(
                    (
                        clipped_start - _utc(session.payload["replay_start_requested_time_utc"])
                    ).total_seconds()
                )
                duration = int((clipped_end - clipped_start).total_seconds())
                candidate = package_root / "clips" / ".candidate.mp4"
                outcome = self.clip_generator.generate(
                    _stable_source_path(source),
                    candidate,
                    stream_index=source.facts.selected_video_stream_index,
                    offset_seconds=offset,
                    duration_seconds=duration,
                    timeout_seconds=timeout_seconds,
                )
                self.checkpoint("after_clip_generation")
                self._revalidate_open_file(source, session.payload)
                integrity = self._validate_generated_clip(
                    candidate,
                    outcome,
                    source.facts,
                    duration,
                    timeout_seconds,
                )
                clip_path = package_root / "clips" / f"{integrity['sha256']}.mp4"
                candidate.replace(clip_path)
                _fsync_directory(clip_path.parent)
                self.checkpoint("after_clip_validation")
                self._revalidate_open_file(source, session.payload)

            request = StrictIdentityEnvelope.from_payload(
                "phase8-request",
                {
                    "schema_version": 1,
                    "investigation_id": investigation_id,
                    "run_id": run_id,
                    "terminal_result_id": terminal.identity,
                    "source_clip_id": source_clip.identity,
                    "selected_observation_ids": list(snapshot.payload["selected_observation_ids"]),
                    "selected_support_group_ids": list(
                        snapshot.payload["selected_support_group_ids"]
                    ),
                    "clip_integrity": integrity,
                },
            )
            clip_ready = StrictIdentityEnvelope.from_payload(
                "phase8-manifest",
                {
                    "schema_version": 1,
                    "state": "CLIP_READY",
                    "investigation_id": investigation_id,
                    "run_id": run_id,
                    "terminal_result_id": terminal.identity,
                    "common_session_id": session.identity,
                    "previous_phase8_manifest_id": None,
                    "source_clip_id": source_clip.identity,
                    "clip_integrity": integrity,
                },
            )
            ready = StrictIdentityEnvelope.from_payload(
                "phase8-manifest",
                {
                    "schema_version": 1,
                    "state": "READY",
                    "investigation_id": investigation_id,
                    "run_id": run_id,
                    "terminal_result_id": terminal.identity,
                    "common_session_id": session.identity,
                    "previous_phase8_manifest_id": clip_ready.identity,
                    "source_clip_id": source_clip.identity,
                    "clip_integrity": integrity,
                    "phase8_request_id": request.identity,
                },
            )
            self._write_envelope(package_root / _SOURCE_CLIP, source_clip)
            self._write_envelope(package_root / _REQUEST, request)
            self._write_envelope(
                package_root / "manifests" / f"{clip_ready.identity}.json", clip_ready
            )
            self._write_envelope(package_root / _MANIFEST, ready)
            _fsync_directory(package_root / "manifests")
            _fsync_directory(package_root)
            staged = self._reopen_at(package_root, run, validate_common=True)
            if staged.manifest != ready or staged.request != request:
                raise Phase8LifecycleError("phase8_corrupt")
            self.checkpoint("after_staged_readback")
            try:
                _durable_move(package_root, final, replace=False)
                _fsync_directory(final.parent)
            except OSError:
                if not final.exists():
                    raise
                winner = self.reopen(run)
                if winner.manifest != ready or winner.request != request:
                    raise Phase8LifecycleError("phase8_conflict") from None
            self.checkpoint("after_handoff_publication")
            published = self.reopen(run)
            if published.manifest != ready or published.request != request:
                raise Phase8LifecycleError("phase8_corrupt")
            return request
        except Phase8LifecycleError:
            raise
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise Phase8LifecycleError("phase8_corrupt") from error
        finally:
            self._remove_wrapper_if_unpublished(wrapper, final)

    def reopen(self, run: object) -> Phase8Package:
        """Strictly reopen without recovery or mutation."""
        investigation_id = str(getattr(run, "investigation_id"))
        run_id = str(getattr(run, "run_id"))
        root = self._directory(investigation_id, run_id, create_parent=False)
        if not root.exists() and not root.is_symlink():
            raise Phase8LifecycleError("phase8_not_found")
        return self._reopen_at(root, run, validate_common=True)

    def status(
        self, run: object | None, investigation_id: str, run_id: str
    ) -> tuple[str, str | None]:
        """Return a read-only safe projection of exact package/media state."""
        root = self._directory(investigation_id, run_id, create_parent=False)
        if not root.exists() and not root.is_symlink():
            if run is not None:
                try:
                    _terminal, session, _snapshot = _terminal_authority(run)
                    common = self._common_media_path(investigation_id, run_id, session.identity)
                    with self._verified_common_media(common, session.payload, 20.0):
                        pass
                except Phase8LifecycleError as error:
                    if error.code == "phase8_media_unavailable":
                        return "MEDIA_MISSING", error.code
                    return "MEDIA_CORRUPT", "phase8_media_corrupt"
            return "NOT_REQUESTED", None
        if run is None:
            return "CORRUPT", "phase8_corrupt"
        try:
            package = self._reopen_at(root, run, validate_common=True)
        except Phase8LifecycleError as error:
            if error.code == "phase8_media_unavailable":
                return "MEDIA_MISSING", error.code
            if error.code == "phase8_media_corrupt":
                return "MEDIA_CORRUPT", error.code
            return "CORRUPT", "phase8_corrupt"
        return package.state, None

    def delete(self, run: object) -> str:
        """Execute or resume the durable two-media deletion transition."""
        terminal, session, _snapshot = _terminal_authority(run)
        del terminal
        investigation_id = str(getattr(run, "investigation_id"))
        run_id = str(getattr(run, "run_id"))
        final = self._directory(investigation_id, run_id, create_parent=False)
        self._recover_staging(investigation_id, run_id, final, preserve_deletion=True)
        package = self._reopen_at(final, run, validate_common=True)
        if package.state == "DELETED":
            return "DELETED"
        if package.state == "DELETING":
            return self._resume_deletion(run, package, session)
        if package.state not in {"READY", "CLIP_READY"} or package.clip_path is None:
            raise Phase8LifecycleError("phase8_not_eligible")
        common_path = self._common_media_path(investigation_id, run_id, session.identity)
        integrity = package.manifest.payload["clip_integrity"]
        if not isinstance(integrity, Mapping):
            raise Phase8LifecycleError("phase8_corrupt")
        with self._verified_common_media(common_path, session.payload, 20.0) as common:
            with self._verified_clip(package.clip_path, integrity, 20.0) as clip:
                deleting = StrictIdentityEnvelope.from_payload(
                    "phase8-manifest",
                    {
                        "schema_version": 1,
                        "state": "DELETING",
                        "investigation_id": investigation_id,
                        "run_id": run_id,
                        "terminal_result_id": package.manifest.payload["terminal_result_id"],
                        "common_session_id": session.identity,
                        "previous_phase8_manifest_id": package.manifest.identity,
                        "source_clip_id": package.source_clip.identity,
                        "clip_integrity": dict(integrity),
                        "phase8_request_id": (
                            None if package.request is None else package.request.identity
                        ),
                        "common_media_tombstone_name": f".delete-{session.identity}.mp4",
                        "source_clip_tombstone_name": f".delete-{integrity['sha256']}.mp4",
                    },
                )
                wrapper = self._new_deletion_journal(
                    investigation_id,
                    run_id,
                    final,
                    package,
                    deleting,
                    common,
                    clip,
                )
                self._publish_transition(package, deleting, wrapper)
                self.checkpoint("after_deleting_publication")
        deleting_package = self._reopen_at(final, run, validate_common=True)
        return self._resume_deletion(run, deleting_package, session, wrapper=wrapper)

    def _resume_deletion(
        self,
        run: object,
        package: Phase8Package,
        session: StrictIdentityEnvelope,
        *,
        wrapper: Path | None = None,
    ) -> str:
        if package.state != "DELETING":
            raise Phase8LifecycleError("phase8_corrupt")
        if wrapper is None:
            wrapper = self._find_deletion_wrapper(package)
        journal = self._read_journal(wrapper)
        self._validate_deletion_journal(journal, wrapper, package)
        final = package.root
        common_live = self._common_media_path(
            str(getattr(run, "investigation_id")), str(getattr(run, "run_id")), session.identity
        )
        common_tomb = common_live.parent / str(
            package.manifest.payload["common_media_tombstone_name"]
        )
        integrity = package.manifest.payload["clip_integrity"]
        if not isinstance(integrity, Mapping):
            raise Phase8LifecycleError("phase8_corrupt")
        clip_live = final / "clips" / f"{integrity['sha256']}.mp4"
        clip_tomb = clip_live.parent / str(package.manifest.payload["source_clip_tombstone_name"])
        journal = self._delete_object(
            journal,
            wrapper,
            package,
            "common",
            common_live,
            common_tomb,
            str(session.payload["mp4_sha256"]),
            int(session.payload["mp4_size_bytes"]),
            stop_after_move=True,
        )
        journal = self._delete_object(
            journal,
            wrapper,
            package,
            "clip",
            clip_live,
            clip_tomb,
            str(integrity["sha256"]),
            int(integrity["size_bytes"]),
            stop_after_move=True,
        )
        journal = self._delete_object(
            journal,
            wrapper,
            package,
            "common",
            common_live,
            common_tomb,
            str(session.payload["mp4_sha256"]),
            int(session.payload["mp4_size_bytes"]),
        )
        self.checkpoint("after_common_unlink")
        journal = self._delete_object(
            journal,
            wrapper,
            package,
            "clip",
            clip_live,
            clip_tomb,
            str(integrity["sha256"]),
            int(integrity["size_bytes"]),
        )
        self.checkpoint("after_clip_unlink")
        self.checkpoint("before_terminal_closed_membership")
        self._validate_terminal_deletion(run, package, wrapper)
        deleted = StrictIdentityEnvelope.from_payload(
            "phase8-manifest",
            {
                **package.manifest.payload,
                "state": "DELETED",
                "previous_phase8_manifest_id": package.manifest.identity,
                "deletion_result": "DELETED",
            },
        )

        def before_commit() -> None:
            self.checkpoint("before_deleted_commit")
            self._validate_terminal_deletion(
                run,
                package,
                wrapper,
                expected_successor=deleted,
            )

        def after_commit() -> None:
            self.checkpoint("after_deleted_publication")
            reopened = self._reopen_at(final, run, validate_common=True)
            if reopened.state != "DELETED" or reopened.manifest != deleted:
                raise Phase8LifecycleError("phase8_corrupt")

        self._publish_transition(
            package,
            deleted,
            wrapper,
            before_commit=before_commit,
            after_commit=after_commit,
            rollback_on_after_failure=True,
        )
        _remove_tree(wrapper, self.staging_root)
        return "DELETED"

    def _delete_object(
        self,
        journal: dict[str, object],
        wrapper: Path,
        package: Phase8Package,
        role: str,
        live: Path,
        tomb: Path,
        expected_sha: str,
        expected_size: int,
        *,
        stop_after_move: bool = False,
    ) -> dict[str, object]:
        while True:
            value = self._deletion_object(journal, role, live, tomb, expected_sha, expected_size)
            state = str(value["state"])
            stamp_value = value["stamp"]
            identity = value["filesystem_identity"]
            if state == "not_moved":
                if tomb.exists() or tomb.is_symlink():
                    raise Phase8LifecycleError("phase8_media_corrupt")
                _verify_path_bytes(live, expected_sha, expected_size, stamp_value, identity)
                journal = self._advance_deletion_object(journal, wrapper, role, "move_intent")
                continue
            if state == "move_intent":
                live_present = live.exists() or live.is_symlink()
                tomb_present = tomb.exists() or tomb.is_symlink()
                if live_present and tomb_present:
                    raise Phase8LifecycleError("phase8_media_corrupt")
                if live_present:
                    descriptor = open_stable_file(live, delete_access=True)
                    try:
                        _verify_open_descriptor(
                            descriptor,
                            expected_sha,
                            expected_size,
                            stamp_value,
                            identity,
                        )
                        rename_open_file_no_replace(descriptor, live, tomb)
                        _verify_open_descriptor(
                            descriptor,
                            expected_sha,
                            expected_size,
                            stamp_value,
                            identity,
                        )
                    except (OSError, MediaFilesystemAuthorityError) as error:
                        raise Phase8LifecycleError("phase8_media_corrupt") from error
                    finally:
                        os.close(descriptor)
                elif not tomb_present:
                    raise Phase8LifecycleError("phase8_media_corrupt")
                _verify_path_bytes(tomb, expected_sha, expected_size, stamp_value, identity)
                _fsync_directory(tomb.parent)
                self.checkpoint(f"after_{role}_tombstone")
                journal = self._advance_deletion_object(journal, wrapper, role, "moved")
                continue
            if state == "moved":
                if stop_after_move:
                    return journal
                if live.exists() or live.is_symlink():
                    raise Phase8LifecycleError("phase8_media_corrupt")
                _verify_path_bytes(tomb, expected_sha, expected_size, stamp_value, identity)
                journal = self._advance_deletion_object(
                    journal, wrapper, role, "disposition_intent"
                )
                continue
            if state == "disposition_intent":
                if live.exists() or live.is_symlink():
                    raise Phase8LifecycleError("phase8_media_corrupt")
                if tomb.exists():
                    _delete_verified_tombstone(
                        tomb,
                        expected_sha,
                        expected_size,
                        stamp_value,
                        identity,
                        before_disposition=lambda: self.checkpoint(
                            f"before_{role}_delete_disposition"
                        ),
                    )
                elif tomb.is_symlink():
                    raise Phase8LifecycleError("phase8_media_corrupt")
                self.checkpoint(f"after_{role}_disposition_requested")
                self._validate_disposed_name(package, journal, role)
                self.checkpoint(f"after_{role}_disappearance_observed")
                journal = self._advance_deletion_object(
                    journal, wrapper, role, "disposition_observed"
                )
                continue
            if state == "disposition_observed":
                self._validate_disposed_name(package, journal, role)
                _fsync_directory(tomb.parent)
                self.checkpoint(f"after_{role}_directory_durability")
                journal = self._advance_deletion_object(
                    journal, wrapper, role, "disposition_completed"
                )
                continue
            if state == "disposition_completed":
                self._validate_disposed_name(package, journal, role)
                journal = self._advance_deletion_object(journal, wrapper, role, "deleted")
                continue
            if state == "deleted":
                self._validate_disposed_name(package, journal, role)
                return journal
            raise Phase8LifecycleError("phase8_corrupt")

    def _deletion_object(
        self,
        journal: Mapping[str, object],
        role: str,
        live: Path,
        tomb: Path,
        expected_sha: str,
        expected_size: int,
    ) -> dict[str, object]:
        objects = journal.get("objects")
        if not isinstance(objects, dict) or set(objects) != {"common", "clip"}:
            raise Phase8LifecycleError("phase8_corrupt")
        value = objects.get(role)
        if not isinstance(value, dict) or set(value) != _DELETION_OBJECT_KEYS:
            raise Phase8LifecycleError("phase8_corrupt")
        if (
            value.get("role") != role
            or value.get("state") not in _DELETION_OBJECT_STATES
            or value.get("live_path") != str(live.absolute())
            or value.get("tombstone_path") != str(tomb.absolute())
            or value.get("sha256") != expected_sha
            or value.get("size_bytes") != expected_size
        ):
            raise Phase8LifecycleError("phase8_corrupt")
        stamp_value = _stamp_from_value(value.get("stamp"))
        identity = value.get("filesystem_identity")
        if (
            stamp_value.link_count != 1
            or not isinstance(identity, dict)
            or set(identity) != {"platform", "volume_id", "file_id"}
            or identity.get("platform") not in {"windows", "posix"}
            or any(type(identity.get(key)) is not int for key in ("volume_id", "file_id"))
            or (role == "common") != isinstance(value.get("authority_binding"), dict)
        ):
            raise Phase8LifecycleError("phase8_corrupt")
        return value

    def _advance_deletion_object(
        self,
        journal: dict[str, object],
        wrapper: Path,
        role: str,
        state: str,
    ) -> dict[str, object]:
        objects = journal.get("objects")
        if not isinstance(objects, dict) or not isinstance(objects.get(role), dict):
            raise Phase8LifecycleError("phase8_corrupt")
        objects[role]["state"] = state
        self._write_journal(wrapper, journal)
        reopened = self._read_journal(wrapper)
        reopened_objects = reopened.get("objects")
        if (
            not isinstance(reopened_objects, dict)
            or not isinstance(reopened_objects.get(role), dict)
            or reopened_objects[role].get("state") != state
        ):
            raise Phase8LifecycleError("phase8_corrupt")
        self.checkpoint(f"after_{role}_{state}")
        return reopened

    def _validate_disposed_name(
        self,
        package: Phase8Package,
        journal: Mapping[str, object],
        role: str,
    ) -> None:
        objects = journal.get("objects")
        if not isinstance(objects, dict) or not isinstance(objects.get(role), dict):
            raise Phase8LifecycleError("phase8_corrupt")
        value = objects[role]
        live = Path(str(value.get("live_path")))
        tomb = Path(str(value.get("tombstone_path")))
        if any(path.exists() or path.is_symlink() for path in (live, tomb)):
            raise Phase8LifecycleError("phase8_media_corrupt")
        if role == "common":
            expected = {authority_path(live).name}
            if _entry_names(live.parent) != expected:
                raise Phase8LifecycleError("phase8_media_corrupt")
        elif _entry_names(package.root / "clips"):
            raise Phase8LifecycleError("phase8_media_corrupt")

    def _validate_deletion_journal(
        self,
        journal: Mapping[str, object],
        wrapper: Path,
        package: Phase8Package,
    ) -> None:
        if (
            set(journal) != _DELETION_JOURNAL_KEYS
            or journal.get("schema_version") != 2
            or journal.get("kind") != "deletion"
            or journal.get("owner_id") != wrapper.name
            or journal.get("lifecycle_state") != "DELETING"
            or journal.get("lifecycle_generation") != package.manifest.identity
            or journal.get("deleting_manifest_id") != package.manifest.identity
            or journal.get("ready_manifest_id")
            != package.manifest.payload.get("previous_phase8_manifest_id")
            or journal.get("investigation_id") != package.manifest.payload.get("investigation_id")
            or journal.get("run_id") != package.manifest.payload.get("run_id")
            or journal.get("final_root") != str(package.root.absolute())
        ):
            raise Phase8LifecycleError("phase8_corrupt")
        transition = journal.get("transition")
        if transition is not None and (
            not isinstance(transition, dict)
            or set(transition) != {"previous_manifest_id", "successor_manifest_id"}
            or transition.get("previous_manifest_id") != package.manifest.identity
        ):
            raise Phase8LifecycleError("phase8_corrupt")

    def _validate_terminal_deletion(
        self,
        run: object,
        package: Phase8Package,
        wrapper: Path,
        *,
        expected_successor: StrictIdentityEnvelope | None = None,
    ) -> None:
        current = self._reopen_at(package.root, run, validate_common=False)
        if current.state != "DELETING" or current.manifest != package.manifest:
            raise Phase8LifecycleError("phase8_corrupt")
        journal = self._read_journal(wrapper)
        self._validate_deletion_journal(journal, wrapper, current)
        transition = journal.get("transition")
        if expected_successor is None:
            if transition is not None:
                raise Phase8LifecycleError("phase8_corrupt")
        elif (
            not isinstance(transition, dict)
            or transition.get("successor_manifest_id") != expected_successor.identity
        ):
            raise Phase8LifecycleError("phase8_corrupt")
        objects = journal.get("objects")
        if not isinstance(objects, dict):
            raise Phase8LifecycleError("phase8_corrupt")
        for role in ("common", "clip"):
            value = objects.get(role)
            if not isinstance(value, dict) or value.get("state") != "deleted":
                raise Phase8LifecycleError("phase8_corrupt")
            self._validate_disposed_name(current, journal, role)
        common = objects["common"]
        common_live = Path(str(common["live_path"]))
        binding = common.get("authority_binding")
        if not isinstance(binding, dict) or set(binding) != {
            "record_path",
            "record_sha256",
            "filesystem_identity",
            "file_stamp",
        }:
            raise Phase8LifecycleError("phase8_corrupt")
        terminal, session, _snapshot = _terminal_authority(run)
        del terminal
        try:
            authority = read_retained_media_authority(
                self.media_root,
                common_live,
                {**session.payload, "common_session_id": session.identity},
            )
        except MediaFilesystemAuthorityError as error:
            raise Phase8LifecycleError("phase8_media_corrupt") from error
        if (
            binding.get("record_path") != str(authority_path(common_live).absolute())
            or binding.get("record_sha256")
            != hashlib.sha256(authority.canonical_bytes()).hexdigest()
            or binding.get("filesystem_identity") != authority.filesystem_identity
            or binding.get("file_stamp") != authority.file_stamp
        ):
            raise Phase8LifecycleError("phase8_media_corrupt")
        staging_parent = wrapper.parent
        if _entry_names(staging_parent) != {wrapper.name}:
            raise Phase8LifecycleError("phase8_corrupt")
        wrapper_names = _entry_names(wrapper)
        if not wrapper_names <= {_JOURNAL, "transition"} or _JOURNAL not in wrapper_names:
            raise Phase8LifecycleError("phase8_corrupt")
        transition_root = wrapper / "transition"
        if transition_root.exists():
            if not _safe_directory(self.staging_root, transition_root):
                raise Phase8LifecycleError("phase8_corrupt")
            transition_names = _entry_names(transition_root)
            expected_names = set() if expected_successor is None else {_MANIFEST}
            if transition_names != expected_names:
                raise Phase8LifecycleError("phase8_corrupt")
            if (
                expected_successor is not None
                and self._read_envelope(transition_root / _MANIFEST) != expected_successor
            ):
                raise Phase8LifecycleError("phase8_corrupt")
        elif expected_successor is not None:
            raise Phase8LifecycleError("phase8_corrupt")
        for directory in (
            common_live.parent,
            current.root / "clips",
            current.root / "manifests",
            current.root,
            wrapper,
            staging_parent,
        ):
            _fsync_directory(directory)

    def _reopen_at(self, root: Path, run: object, *, validate_common: bool) -> Phase8Package:
        if not _safe_directory(self.root.parent, root):
            raise Phase8LifecycleError("phase8_corrupt")
        manifest = self._read_envelope(root / _MANIFEST)
        if manifest.family != "phase8-manifest":
            raise Phase8LifecycleError("phase8_corrupt")
        state = str(manifest.payload.get("state"))
        if state not in {"CLIP_READY", "READY", "DELETING", "DELETED"}:
            raise Phase8LifecycleError("phase8_corrupt")
        investigation_id = str(getattr(run, "investigation_id"))
        run_id = str(getattr(run, "run_id"))
        terminal, session, _snapshot = _terminal_authority(run)
        if (
            manifest.payload.get("investigation_id") != investigation_id
            or manifest.payload.get("run_id") != run_id
            or manifest.payload.get("terminal_result_id") != terminal.identity
            or manifest.payload.get("common_session_id") != session.identity
        ):
            raise Phase8LifecycleError("phase8_corrupt")
        source_clip = self._read_envelope(root / _SOURCE_CLIP)
        request = None if state == "CLIP_READY" else self._read_envelope(root / _REQUEST)
        if source_clip.family != "source-clip" or source_clip.identity != manifest.payload.get(
            "source_clip_id"
        ):
            raise Phase8LifecycleError("phase8_corrupt")
        if request is not None and (
            request.family != "phase8-request"
            or request.identity != manifest.payload.get("phase8_request_id")
            or request.payload.get("source_clip_id") != source_clip.identity
            or request.payload.get("clip_integrity") != manifest.payload.get("clip_integrity")
        ):
            raise Phase8LifecycleError("phase8_corrupt")
        expected_root = {_MANIFEST, _SOURCE_CLIP, "manifests", "clips"}
        if state != "CLIP_READY":
            expected_root.add(_REQUEST)
        if _entry_names(root) != expected_root:
            raise Phase8LifecycleError("phase8_corrupt")
        self._validate_archives(root, manifest)
        integrity = manifest.payload.get("clip_integrity")
        if not isinstance(integrity, Mapping):
            raise Phase8LifecycleError("phase8_corrupt")
        live_clip = root / "clips" / f"{integrity['sha256']}.mp4"
        clip_tomb = root / "clips" / str(manifest.payload.get("source_clip_tombstone_name", ""))
        common_live = self._common_media_path(investigation_id, run_id, session.identity)
        common_tomb = common_live.parent / str(
            manifest.payload.get("common_media_tombstone_name", "")
        )
        authority_session = {**session.payload, "common_session_id": session.identity}
        try:
            _ = read_retained_media_authority(
                self.media_root,
                common_live,
                authority_session,
            )
        except MediaFilesystemAuthorityError as error:
            raise Phase8LifecycleError("phase8_media_corrupt") from error
        media_names = _entry_names(common_live.parent)
        authority_name = authority_path(common_live).name
        if state in {"READY", "CLIP_READY"}:
            if media_names != {authority_name, common_live.name}:
                raise Phase8LifecycleError("phase8_media_corrupt")
        elif state == "DELETING":
            if (
                authority_name not in media_names
                or not media_names <= {authority_name, common_live.name, common_tomb.name}
                or len(media_names - {authority_name}) > 1
            ):
                raise Phase8LifecycleError("phase8_media_corrupt")
        elif media_names != {authority_name}:
            raise Phase8LifecycleError("phase8_media_corrupt")
        clip_path: Path | None = None
        if state in {"READY", "CLIP_READY"}:
            if _entry_names(root / "clips") != {live_clip.name}:
                raise Phase8LifecycleError("phase8_media_unavailable")
            with self._verified_clip(live_clip, integrity, 20.0):
                pass
            clip_path = live_clip
            if validate_common:
                with self._verified_common_media(common_live, session.payload, 20.0):
                    pass
        elif state == "DELETING":
            provisional = Phase8Package(root, manifest, source_clip, request, None)
            deletion_wrapper = self._find_deletion_wrapper(provisional)
            deletion_journal = self._read_journal(deletion_wrapper)
            self._validate_deletion_journal(deletion_journal, deletion_wrapper, provisional)
            names = _entry_names(root / "clips")
            allowed = {live_clip.name, clip_tomb.name}
            if not names <= allowed or len(names) > 1:
                raise Phase8LifecycleError("phase8_media_corrupt")
            clip_object = self._deletion_object(
                deletion_journal,
                "clip",
                live_clip,
                clip_tomb,
                str(integrity["sha256"]),
                int(integrity["size_bytes"]),
            )
            clip_state = str(clip_object["state"])
            if live_clip.name in names:
                if clip_state not in {"not_moved", "move_intent"}:
                    raise Phase8LifecycleError("phase8_media_corrupt")
                _verify_path_bytes(
                    live_clip,
                    str(integrity["sha256"]),
                    int(integrity["size_bytes"]),
                    clip_object["stamp"],
                    clip_object["filesystem_identity"],
                )
                with self._verified_clip(live_clip, integrity, 20.0):
                    pass
                clip_path = live_clip
            elif clip_tomb.name in names:
                if clip_state not in {"move_intent", "moved", "disposition_intent"}:
                    raise Phase8LifecycleError("phase8_media_corrupt")
                _verify_path_bytes(
                    clip_tomb,
                    str(integrity["sha256"]),
                    int(integrity["size_bytes"]),
                    clip_object["stamp"],
                    clip_object["filesystem_identity"],
                )
                clip_path = clip_tomb
            elif clip_state not in {
                "disposition_intent",
                "disposition_observed",
                "disposition_completed",
                "deleted",
            }:
                raise Phase8LifecycleError("phase8_media_corrupt")
            common_object = self._deletion_object(
                deletion_journal,
                "common",
                common_live,
                common_tomb,
                str(session.payload["mp4_sha256"]),
                int(session.payload["mp4_size_bytes"]),
            )
            common_state = str(common_object["state"])
            common_present = common_live.exists() or common_live.is_symlink()
            tomb_present = common_tomb.exists() or common_tomb.is_symlink()
            if common_present and tomb_present:
                raise Phase8LifecycleError("phase8_media_corrupt")
            if common_present:
                if common_state not in {"not_moved", "move_intent"}:
                    raise Phase8LifecycleError("phase8_media_corrupt")
                _verify_path_bytes(
                    common_live,
                    str(session.payload["mp4_sha256"]),
                    int(session.payload["mp4_size_bytes"]),
                    common_object["stamp"],
                    common_object["filesystem_identity"],
                )
                if validate_common:
                    with self._verified_common_media(common_live, session.payload, 20.0):
                        pass
            elif tomb_present:
                if common_state not in {"move_intent", "moved", "disposition_intent"}:
                    raise Phase8LifecycleError("phase8_media_corrupt")
                _verify_path_bytes(
                    common_tomb,
                    str(session.payload["mp4_sha256"]),
                    int(session.payload["mp4_size_bytes"]),
                    common_object["stamp"],
                    common_object["filesystem_identity"],
                )
            elif common_state not in {
                "disposition_intent",
                "disposition_observed",
                "disposition_completed",
                "deleted",
            }:
                raise Phase8LifecycleError("phase8_media_corrupt")
        else:
            if _entry_names(root / "clips"):
                raise Phase8LifecycleError("phase8_media_corrupt")
            if any(
                path.exists() or path.is_symlink()
                for path in (common_live, common_tomb, live_clip, clip_tomb)
            ):
                raise Phase8LifecycleError("phase8_media_corrupt")
        return Phase8Package(root, manifest, source_clip, request, clip_path)

    def _validate_archives(self, root: Path, current: StrictIdentityEnvelope) -> None:
        archives = root / "manifests"
        if not _safe_directory(root, archives):
            raise Phase8LifecycleError("phase8_corrupt")
        values: dict[str, StrictIdentityEnvelope] = {}
        for path in archives.iterdir():
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise Phase8LifecycleError("phase8_corrupt")
            value = self._read_envelope(path)
            if value.family != "phase8-manifest" or path.name != f"{value.identity}.json":
                raise Phase8LifecycleError("phase8_corrupt")
            values[value.identity] = value
        expected: set[str] = set()
        cursor = current.payload.get("previous_phase8_manifest_id")
        while cursor is not None:
            if not isinstance(cursor, str) or cursor in expected or cursor not in values:
                raise Phase8LifecycleError("phase8_corrupt")
            expected.add(cursor)
            previous = values[cursor]
            if previous.payload.get("investigation_id") != current.payload.get(
                "investigation_id"
            ) or previous.payload.get("run_id") != current.payload.get("run_id"):
                raise Phase8LifecycleError("phase8_corrupt")
            cursor = previous.payload.get("previous_phase8_manifest_id")
        pending = self._pending_current_archive(current)
        permitted = expected | ({current.identity} if pending else set())
        if set(values) != permitted:
            raise Phase8LifecycleError("phase8_corrupt")

    @contextmanager
    def _verified_common_media(
        self, path: Path, payload: Mapping[str, object], timeout: float
    ) -> Generator[_VerifiedFile, None, None]:
        session = {**payload, "common_session_id": path.stem}
        try:
            authority = read_retained_media_authority(self.media_root, path, session)
        except MediaFilesystemAuthorityError as error:
            raise Phase8LifecycleError("phase8_media_corrupt") from error
        with self._verified_file(
            path,
            str(payload["mp4_sha256"]),
            int(payload["mp4_size_bytes"]),
            timeout,
            authority=authority,
        ) as verified:
            if _entry_names(path.parent) != {path.name, authority_path(path).name}:
                raise Phase8LifecycleError("phase8_media_corrupt")
            facts = verified.facts
            if (
                facts.selected_video_stream_index != payload["selected_video_stream_index"]
                or facts.container_start_pts != payload["container_start_pts"]
                or facts.time_base_num != payload["time_base_num"]
                or facts.time_base_den != payload["time_base_den"]
                or facts.duration_ticks != payload["duration_ticks"]
            ):
                raise Phase8LifecycleError("phase8_media_corrupt")
            yield verified

    @contextmanager
    def _verified_clip(
        self, path: Path, integrity: Mapping[str, object], timeout: float
    ) -> Generator[_VerifiedFile, None, None]:
        with self._verified_file(
            path, str(integrity["sha256"]), int(integrity["size_bytes"]), timeout
        ) as verified:
            if _integrity_from_facts(
                verified.sha256,
                verified.size_bytes,
                verified.facts,
                str(integrity["generation_outcome"]),
            ) != dict(integrity):
                raise Phase8LifecycleError("phase8_media_corrupt")
            yield verified

    @contextmanager
    def _verified_file(
        self,
        path: Path,
        expected_sha: str,
        expected_size: int,
        timeout: float,
        *,
        authority: RetainedMediaFilesystemAuthority | None = None,
    ) -> Generator[_VerifiedFile, None, None]:
        if not path.exists() and not path.is_symlink():
            raise Phase8LifecycleError("phase8_media_unavailable")
        _require_regular_confined(
            path.parent.parent.parent if path.parent.name != "clips" else path.parent.parent,
            path,
        )
        try:
            descriptor = open_stable_file(path)
        except FileNotFoundError as error:
            raise Phase8LifecycleError("phase8_media_unavailable") from error
        except OSError as error:
            raise Phase8LifecycleError("phase8_media_corrupt") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise Phase8LifecycleError("phase8_media_corrupt")
            stamp = _FileStamp.from_stat(opened)
            digest, size = _hash_descriptor(descriptor)
            if digest != expected_sha or size != expected_size:
                raise Phase8LifecycleError("phase8_media_corrupt")
            facts = self.media_probe.probe(
                stable_descriptor_path(descriptor, path),
                timeout,
            )
            facts.validate()
            stable_identity = filesystem_identity(descriptor)
            if authority is not None and (
                stable_identity != authority.filesystem_identity
                or descriptor_stamp(descriptor) != authority.file_stamp
            ):
                raise Phase8LifecycleError("phase8_media_corrupt")
            value = _VerifiedFile(
                path,
                descriptor,
                stamp,
                digest,
                size,
                facts,
                stable_identity,
                authority,
            )
            self._revalidate_open_file(
                value, {"mp4_sha256": expected_sha, "mp4_size_bytes": expected_size}
            )
            yield value
            self._revalidate_open_file(
                value, {"mp4_sha256": expected_sha, "mp4_size_bytes": expected_size}
            )
        except Phase8LifecycleError:
            raise
        except Exception as error:
            raise Phase8LifecycleError("phase8_media_corrupt") from error
        finally:
            os.close(descriptor)

    def _revalidate_open_file(self, value: _VerifiedFile, payload: Mapping[str, object]) -> None:
        try:
            path_stat = value.path.stat(follow_symlinks=False)
            opened = os.fstat(value.descriptor)
        except OSError as error:
            raise Phase8LifecycleError("phase8_media_corrupt") from error
        if (
            _FileStamp.from_stat(opened) != value.stamp
            or _FileStamp.from_stat(path_stat) != value.stamp
        ):
            raise Phase8LifecycleError("phase8_media_corrupt")
        if value.authority is not None and (
            filesystem_identity(value.descriptor) != value.authority.filesystem_identity
            or descriptor_stamp(value.descriptor) != value.authority.file_stamp
        ):
            raise Phase8LifecycleError("phase8_media_corrupt")
        digest, size = _hash_descriptor(value.descriptor)
        if digest != payload["mp4_sha256"] or size != payload["mp4_size_bytes"]:
            raise Phase8LifecycleError("phase8_media_corrupt")

    def _validate_generated_clip(
        self,
        path: Path,
        outcome: str,
        source: MediaProbeFacts,
        requested_duration: int,
        timeout: float,
    ) -> dict[str, object]:
        if outcome not in {"STREAM_COPY", "REENCODED"}:
            raise Phase8LifecycleError("phase8_clip_failed")
        try:
            size = path.stat().st_size
            if size <= 0 or size > _MAX_CLIP_BYTES:
                raise Phase8LifecycleError("phase8_clip_failed")
            digest = _hash_path(path)
            facts = self.media_probe.probe(path, timeout)
            facts.validate()
        except Phase8LifecycleError:
            raise
        except Exception as error:
            raise Phase8LifecycleError("phase8_clip_failed") from error
        rate = Fraction(facts.average_frame_rate_num, facts.average_frame_rate_den)
        duration = Fraction(facts.duration_ticks * facts.time_base_num, facts.time_base_den)
        tolerance = Fraction(facts.average_frame_rate_den, facts.average_frame_rate_num)
        if (
            rate > _MAX_FRAME_RATE
            or duration > Fraction(requested_duration, 1) + tolerance
            or duration > _MAX_CLIP_SECONDS + tolerance
            or facts.audio_stream_count != 0
            or facts.width != source.width
            or facts.height != source.height
        ):
            raise Phase8LifecycleError("phase8_clip_failed")
        if outcome == "REENCODED" and (
            facts.codec != "h264"
            or facts.profile != "High"
            or facts.level != 41
            or facts.pixel_format != "yuv420p"
        ):
            raise Phase8LifecycleError("phase8_clip_failed")
        return _integrity_from_facts(digest, size, facts, outcome)

    def _publish_transition(
        self,
        current: Phase8Package,
        successor: StrictIdentityEnvelope,
        wrapper: Path,
        *,
        before_commit: Callable[[], None] | None = None,
        after_commit: Callable[[], None] | None = None,
        rollback_on_after_failure: bool = False,
    ) -> None:
        transition = wrapper / "transition"
        transition.mkdir(exist_ok=True)
        archive = transition / f"{current.manifest.identity}.json"
        proposed = transition / _MANIFEST
        self._write_envelope(archive, current.manifest, replace=True)
        self._write_envelope(proposed, successor, replace=True)
        journal = self._read_journal(wrapper)
        journal["transition"] = {
            "previous_manifest_id": current.manifest.identity,
            "successor_manifest_id": successor.identity,
        }
        self._write_journal(wrapper, journal)
        final_archive = current.root / "manifests" / archive.name
        if final_archive.exists():
            if self._read_envelope(final_archive) != current.manifest:
                raise Phase8LifecycleError("phase8_conflict")
        else:
            _durable_move(archive, final_archive, replace=False)
            _fsync_directory(final_archive.parent)
        self.checkpoint("after_transition_archive")
        if before_commit is not None:
            before_commit()
        _durable_move(proposed, current.root / _MANIFEST, replace=True)
        _fsync_directory(current.root)
        try:
            if after_commit is not None:
                after_commit()
        except BaseException as primary:
            if rollback_on_after_failure:
                try:
                    self._write_envelope(current.root / _MANIFEST, current.manifest, replace=True)
                    if final_archive.exists():
                        final_archive.unlink()
                        _fsync_directory(final_archive.parent)
                    journal["transition"] = None
                    self._write_journal(wrapper, journal)
                except (OSError, Phase8LifecycleError) as cleanup_error:
                    primary.add_note(f"Phase 8 transition rollback failed: {cleanup_error!r}")
            raise
        journal["transition"] = None
        self._write_journal(wrapper, journal)

    def _new_staging(self, investigation_id: str, run_id: str, final: Path, kind: str) -> Path:
        root = self.staging_root / investigation_id / run_id
        root.mkdir(parents=True, exist_ok=True)
        if not _safe_directory(self.staging_root, root):
            raise Phase8LifecycleError("phase8_corrupt")
        wrapper = Path(tempfile.mkdtemp(prefix="phase8-", dir=root))
        self._write_journal(
            wrapper,
            {
                "schema_version": 1,
                "kind": kind,
                "investigation_id": investigation_id,
                "run_id": run_id,
                "final_root": str(final.absolute()),
                "progress": "staging",
                "transition": None,
            },
        )
        return wrapper

    def _new_deletion_journal(
        self,
        investigation_id: str,
        run_id: str,
        final: Path,
        package: Phase8Package,
        deleting: StrictIdentityEnvelope,
        common: _VerifiedFile,
        clip: _VerifiedFile,
    ) -> Path:
        wrapper = self._new_staging(investigation_id, run_id, final, "deletion")
        if common.authority is None:
            raise Phase8LifecycleError("phase8_media_corrupt")
        common_tomb = common.path.parent / str(deleting.payload["common_media_tombstone_name"])
        clip_tomb = clip.path.parent / str(deleting.payload["source_clip_tombstone_name"])
        journal: dict[str, object] = {
            "schema_version": 2,
            "kind": "deletion",
            "investigation_id": investigation_id,
            "run_id": run_id,
            "final_root": str(final.absolute()),
            "transition": None,
            "owner_id": wrapper.name,
            "lifecycle_state": "DELETING",
            "lifecycle_generation": deleting.identity,
            "ready_manifest_id": package.manifest.identity,
            "deleting_manifest_id": deleting.identity,
            "objects": {
                "common": {
                    "role": "common",
                    "state": "not_moved",
                    "live_path": str(common.path.absolute()),
                    "tombstone_path": str(common_tomb.absolute()),
                    "sha256": common.sha256,
                    "size_bytes": common.size_bytes,
                    "stamp": asdict(common.stamp),
                    "filesystem_identity": common.filesystem_identity,
                    "authority_binding": {
                        "record_path": str(authority_path(common.path).absolute()),
                        "record_sha256": hashlib.sha256(
                            common.authority.canonical_bytes()
                        ).hexdigest(),
                        "filesystem_identity": common.authority.filesystem_identity,
                        "file_stamp": common.authority.file_stamp,
                    },
                },
                "clip": {
                    "role": "clip",
                    "state": "not_moved",
                    "live_path": str(clip.path.absolute()),
                    "tombstone_path": str(clip_tomb.absolute()),
                    "sha256": clip.sha256,
                    "size_bytes": clip.size_bytes,
                    "stamp": asdict(clip.stamp),
                    "filesystem_identity": clip.filesystem_identity,
                    "authority_binding": None,
                },
            },
        }
        self._write_journal(wrapper, journal)
        return wrapper

    def _find_deletion_wrapper(self, package: Phase8Package) -> Path:
        root = (
            self.staging_root
            / str(package.manifest.payload["investigation_id"])
            / str(package.manifest.payload["run_id"])
        )
        if not root.is_dir() or root.is_symlink():
            raise Phase8LifecycleError("phase8_corrupt")
        matches = []
        for wrapper in root.iterdir():
            try:
                journal = self._read_journal(wrapper)
            except Phase8LifecycleError:
                continue
            if (
                journal.get("kind") == "deletion"
                and journal.get("deleting_manifest_id") == package.manifest.identity
            ):
                matches.append(wrapper)
        if len(matches) != 1:
            raise Phase8LifecycleError("phase8_corrupt")
        return matches[0]

    def _recover_staging(
        self,
        investigation_id: str,
        run_id: str,
        final: Path,
        *,
        preserve_deletion: bool = False,
    ) -> None:
        root = self.staging_root / investigation_id / run_id
        if not root.exists():
            return
        if not _safe_directory(self.staging_root, root):
            raise Phase8LifecycleError("phase8_corrupt")
        for wrapper in tuple(root.iterdir()):
            journal = self._read_journal(wrapper)
            if (
                journal.get("investigation_id") != investigation_id
                or journal.get("run_id") != run_id
                or journal.get("final_root") != str(final.absolute())
            ):
                raise Phase8LifecycleError("phase8_corrupt")
            if journal.get("kind") == "deletion":
                self._recover_transition(wrapper, final)
                current = self._read_envelope(final / _MANIFEST)
                if preserve_deletion and current.payload.get("state") == "DELETING":
                    continue
            _remove_tree(wrapper, self.staging_root)

    def _recover_transition(self, wrapper: Path, final: Path) -> None:
        journal = self._read_journal(wrapper)
        transition = journal.get("transition")
        if not isinstance(transition, Mapping):
            return
        current = self._read_envelope(final / _MANIFEST)
        previous = transition.get("previous_manifest_id")
        successor = transition.get("successor_manifest_id")
        archive = final / "manifests" / f"{previous}.json"
        transition_root = wrapper / "transition"
        if not _safe_directory(self.staging_root, transition_root):
            raise Phase8LifecycleError("phase8_corrupt")
        staged_archive = transition_root / f"{previous}.json"
        staged_manifest = transition_root / _MANIFEST
        allowed = {staged_archive.name, staged_manifest.name}
        if not _entry_names(transition_root) <= allowed:
            raise Phase8LifecycleError("phase8_corrupt")
        if current.identity == previous:
            if archive.exists():
                if archive.is_symlink() or self._read_envelope(archive).identity != previous:
                    raise Phase8LifecycleError("phase8_corrupt")
                archive.unlink()
                _fsync_directory(archive.parent)
            if staged_archive.exists():
                if self._read_envelope(staged_archive).identity != previous:
                    raise Phase8LifecycleError("phase8_corrupt")
                staged_archive.unlink()
            if staged_manifest.exists():
                if self._read_envelope(staged_manifest).identity != successor:
                    raise Phase8LifecycleError("phase8_corrupt")
                staged_manifest.unlink()
            _fsync_directory(transition_root)
        elif current.identity != successor or _entry_names(transition_root):
            raise Phase8LifecycleError("phase8_corrupt")
        journal["transition"] = None
        self._write_journal(wrapper, journal)

    def _pending_current_archive(self, current: StrictIdentityEnvelope) -> bool:
        root = (
            self.staging_root
            / str(current.payload.get("investigation_id"))
            / str(current.payload.get("run_id"))
        )
        if not root.exists() or not root.is_dir() or root.is_symlink():
            return False
        matches = 0
        for wrapper in root.iterdir():
            try:
                transition = self._read_journal(wrapper).get("transition")
            except Phase8LifecycleError:
                continue
            if (
                isinstance(transition, Mapping)
                and transition.get("previous_manifest_id") == current.identity
            ):
                matches += 1
        return matches == 1

    def _directory(self, investigation_id: str, run_id: str, *, create_parent: bool) -> Path:
        if not _safe_component(investigation_id) or not _safe_component(run_id):
            raise Phase8LifecycleError("invalid_request")
        if create_parent:
            self.root.mkdir(parents=True, exist_ok=True)
        elif not self.root.exists() and not self.root.is_symlink():
            return self.root / investigation_id / run_id
        if self.root.exists() and not _safe_directory(self.root.parent, self.root):
            raise Phase8LifecycleError("phase8_corrupt")
        target = self.root / investigation_id / run_id
        if create_parent:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not _safe_directory(self.root, target.parent):
                raise Phase8LifecycleError("phase8_corrupt")
        if not is_safe_contained_path(self.root, target):
            raise Phase8LifecycleError("phase8_corrupt")
        return target

    def _common_media_path(self, investigation_id: str, run_id: str, session_id: str) -> Path:
        path = self.media_root / investigation_id / run_id / f"{session_id}.mp4"
        if not is_safe_contained_path(self.media_root, path):
            raise Phase8LifecycleError("phase8_media_corrupt")
        return path

    def _read_envelope(self, path: Path) -> StrictIdentityEnvelope:
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or not is_safe_path(path, require_target=True)
            ):
                raise Phase8LifecycleError("phase8_corrupt")
            raw = path.read_text(encoding="utf-8")
            value = StrictIdentityEnvelope.model_validate(
                load_durable_json_object(raw), strict=True
            )
            if raw != _document(value):
                raise Phase8LifecycleError("phase8_corrupt")
            return value
        except Phase8LifecycleError:
            raise
        except Exception as error:
            raise Phase8LifecycleError("phase8_corrupt") from error

    def _write_envelope(
        self,
        path: Path,
        value: StrictIdentityEnvelope,
        *,
        replace: bool = False,
    ) -> None:
        _write_text(path, _document(value), replace=replace)

    def _read_journal(self, wrapper: Path) -> dict[str, object]:
        try:
            if not _safe_directory(self.staging_root, wrapper):
                raise Phase8LifecycleError("phase8_corrupt")
            raw = (wrapper / _JOURNAL).read_text(encoding="utf-8")
            value = dict(load_durable_json_object(raw))
            if raw != _canonical(value):
                raise Phase8LifecycleError("phase8_corrupt")
            return value
        except Phase8LifecycleError:
            raise
        except Exception as error:
            raise Phase8LifecycleError("phase8_corrupt") from error

    def _write_journal(self, wrapper: Path, value: Mapping[str, object]) -> None:
        wrapper.mkdir(parents=True, exist_ok=True)
        _write_text(wrapper / _JOURNAL, _canonical(value), replace=True)

    def _remove_wrapper_if_unpublished(self, wrapper: Path, final: Path) -> None:
        package = wrapper / _PACKAGE
        if package.exists() and not final.exists():
            return
        if wrapper.exists():
            _remove_tree(wrapper, self.staging_root)


def _terminal_authority(
    run: object,
) -> tuple[StrictIdentityEnvelope, StrictIdentityEnvelope, StrictIdentityEnvelope]:
    if getattr(run, "schema_version", None) != 7 or getattr(run, "result_kind", None) != "FOUND":
        raise Phase8LifecycleError("phase8_not_eligible")
    records = tuple(getattr(run, "records", ()))
    values: list[StrictIdentityEnvelope] = []
    for family in ("terminal-result", "common-session", "evidence-snapshot"):
        matches = [item for item in records if getattr(item, "family", None) == family]
        if len(matches) != 1 or not isinstance(matches[0], StrictIdentityEnvelope):
            raise Phase8LifecycleError("phase8_corrupt")
        values.append(matches[0])
    terminal, session, snapshot = values
    if (
        terminal.payload.get("common_session_id") != session.identity
        or terminal.payload.get("evidence_snapshot_id") != snapshot.identity
        or any(
            item.payload.get("investigation_id") != getattr(run, "investigation_id")
            or item.payload.get("run_id") != getattr(run, "run_id")
            for item in values
        )
    ):
        raise Phase8LifecycleError("phase8_corrupt")
    return terminal, session, snapshot


def _source_clip_envelope(
    investigation_id: str,
    run_id: str,
    terminal: StrictIdentityEnvelope,
    session: StrictIdentityEnvelope,
    media_policy: StrictIdentityEnvelope,
) -> StrictIdentityEnvelope:
    clipped_start, clipped_end = _clip_interval(terminal.payload, session.payload)
    return StrictIdentityEnvelope.from_payload(
        "source-clip",
        {
            "schema_version": 1,
            "investigation_id": investigation_id,
            "run_id": run_id,
            "terminal_result_id": terminal.identity,
            "common_session_id": session.identity,
            "input_stream_index": session.payload["selected_video_stream_index"],
            "media_generation_policy_id": media_policy.identity,
            "requested_interval_start_requested_time_utc": _whole(
                _utc(terminal.payload["interval_start_requested_time_utc"]) - timedelta(seconds=10)
            ),
            "requested_interval_end_requested_time_utc": _whole(
                _utc(terminal.payload["interval_end_requested_time_utc"]) + timedelta(seconds=30)
            ),
            "clipped_interval_start_requested_time_utc": _whole(clipped_start),
            "clipped_interval_end_requested_time_utc": _whole(clipped_end),
        },
    )


def _clip_interval(
    terminal: Mapping[str, object], session: Mapping[str, object]
) -> tuple[datetime, datetime]:
    requested_start = _utc(terminal["interval_start_requested_time_utc"]) - timedelta(seconds=10)
    requested_end = _utc(terminal["interval_end_requested_time_utc"]) + timedelta(seconds=30)
    start = max(_utc(session["replay_start_requested_time_utc"]), requested_start)
    end = min(_utc(session["replay_end_requested_time_utc"]), requested_end)
    if start >= end or (end - start).total_seconds() > _MAX_CLIP_SECONDS:
        raise Phase8LifecycleError("phase8_corrupt")
    return start, end


def _integrity_from_facts(
    digest: str, size: int, facts: MediaProbeFacts, outcome: str
) -> dict[str, object]:
    return {
        "sha256": digest,
        "size_bytes": size,
        "observed_duration_ticks": facts.duration_ticks,
        "observed_time_base_num": facts.time_base_num,
        "observed_time_base_den": facts.time_base_den,
        "video_stream_index": facts.selected_video_stream_index,
        "codec": facts.codec,
        "profile": facts.profile,
        "level": facts.level,
        "pixel_format": facts.pixel_format,
        "width": facts.width,
        "height": facts.height,
        "average_frame_rate_num": facts.average_frame_rate_num,
        "average_frame_rate_den": facts.average_frame_rate_den,
        "audio_stream_count": facts.audio_stream_count,
        "generation_outcome": outcome,
    }


def _verify_path_bytes(
    path: Path,
    expected_sha: str,
    expected_size: int,
    expected_stamp: object | None,
    expected_identity: object | None = None,
) -> None:
    _require_regular_confined(path.parent, path)
    descriptor: int | None = None
    try:
        descriptor = open_stable_file(path)
        current = _FileStamp.from_stat(os.fstat(descriptor))
    except OSError as error:
        raise Phase8LifecycleError("phase8_media_corrupt") from error
    try:
        if expected_stamp is not None and current != _stamp_from_value(expected_stamp):
            raise Phase8LifecycleError("phase8_media_corrupt")
        if expected_identity is not None and filesystem_identity(descriptor) != expected_identity:
            raise Phase8LifecycleError("phase8_media_corrupt")
        digest, size = _hash_descriptor(descriptor)
        if current.size != expected_size or size != expected_size or digest != expected_sha:
            raise Phase8LifecycleError("phase8_media_corrupt")
    finally:
        os.close(descriptor)


def _delete_verified_tombstone(
    path: Path,
    expected_sha: str,
    expected_size: int,
    expected_stamp: object,
    expected_identity: object,
    *,
    before_disposition: Callable[[], None],
) -> None:
    _require_regular_confined(path.parent, path)
    descriptor: int | None = None
    try:
        descriptor = open_stable_file(path, delete_access=True)
        current = _FileStamp.from_stat(os.fstat(descriptor))
        digest, size = _hash_descriptor(descriptor)
        if (
            current != _stamp_from_value(expected_stamp)
            or filesystem_identity(descriptor) != expected_identity
            or digest != expected_sha
            or size != expected_size
        ):
            raise Phase8LifecycleError("phase8_media_corrupt")
        before_disposition()
        if (
            _FileStamp.from_stat(os.fstat(descriptor)) != current
            or filesystem_identity(descriptor) != expected_identity
            or _hash_descriptor(descriptor) != (expected_sha, expected_size)
        ):
            raise Phase8LifecycleError("phase8_media_corrupt")
        mark_open_file_for_deletion(descriptor)
    except Phase8LifecycleError:
        raise
    except (OSError, MediaFilesystemAuthorityError) as error:
        raise Phase8LifecycleError("phase8_media_corrupt") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _stamp_from_value(value: object) -> _FileStamp:
    keys = {"device", "inode", "size", "modified_ns", "link_count"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise Phase8LifecycleError("phase8_corrupt")
    try:
        return _FileStamp(
            *(int(value[key]) for key in ("device", "inode", "size", "modified_ns", "link_count"))
        )
    except (TypeError, ValueError) as error:
        raise Phase8LifecycleError("phase8_corrupt") from error


def _hash_descriptor(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        digest.update(block)
        size += len(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest(), size


def _verify_open_descriptor(
    descriptor: int,
    expected_sha: str,
    expected_size: int,
    expected_stamp: object,
    expected_identity: object,
) -> None:
    if (
        _FileStamp.from_stat(os.fstat(descriptor)) != _stamp_from_value(expected_stamp)
        or filesystem_identity(descriptor) != expected_identity
        or _hash_descriptor(descriptor) != (expected_sha, expected_size)
    ):
        raise Phase8LifecycleError("phase8_media_corrupt")


def _stable_source_path(source: _VerifiedFile) -> Path:
    """Expose the held inode to local generation without reopening its live path."""
    if os.name == "nt":
        return source.path
    descriptor_path = Path(f"/proc/{os.getpid()}/fd/{source.descriptor}")
    return descriptor_path if descriptor_path.exists() else source.path


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_regular_confined(root: Path, path: Path) -> None:
    if (
        path.is_symlink()
        or not path.is_file()
        or not is_safe_path(path, require_target=True)
        or not is_safe_contained_path(root, path, require_target=True)
    ):
        raise Phase8LifecycleError("phase8_media_corrupt")


def _safe_directory(root: Path, path: Path) -> bool:
    return (
        path.exists()
        and path.is_dir()
        and not path.is_symlink()
        and is_safe_path(path, require_target=True)
        and is_safe_contained_path(root, path, require_target=True)
    )


def _entry_names(path: Path) -> set[str]:
    if not path.is_dir() or path.is_symlink() or not is_safe_path(path, require_target=True):
        raise Phase8LifecycleError("phase8_corrupt")
    result: set[str] = set()
    for entry in path.iterdir():
        if entry.is_symlink() or not is_safe_path(entry, require_target=True):
            raise Phase8LifecycleError("phase8_corrupt")
        result.add(entry.name)
    return result


def _write_text(path: Path, value: str, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if not replace and path.exists():
            raise Phase8LifecycleError("phase8_conflict")
        _durable_move(temporary, path, replace=replace)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _document(value: StrictIdentityEnvelope) -> str:
    return _canonical(
        {"family": value.family, "identity": value.identity, "payload": value.payload}
    )


def _durable_move(source: Path, destination: Path, *, replace: bool) -> None:
    if os.name != "nt":
        if not replace and (destination.exists() or destination.is_symlink()):
            raise FileExistsError(destination)
        os.replace(source, destination)
        return
    import ctypes

    flags = 0x00000008 | (0x00000001 if replace else 0)  # WRITE_THROUGH | REPLACE_EXISTING
    if not ctypes.windll.kernel32.MoveFileExW(str(source), str(destination), flags):
        raise OSError(ctypes.get_last_error(), "MoveFileExW failed")


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise Phase8LifecycleError("phase8_corrupt")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise Phase8LifecycleError("phase8_corrupt") from error


def _whole(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\0" not in value
    )


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_tree(path: Path, root: Path) -> None:
    if path.exists() and _safe_directory(root, path):
        shutil.rmtree(path)


__all__ = [
    "FfmpegSourceClipGenerator",
    "Phase8HandoffRepository",
    "Phase8LifecycleError",
    "Phase8Package",
    "SourceClipGenerator",
]
