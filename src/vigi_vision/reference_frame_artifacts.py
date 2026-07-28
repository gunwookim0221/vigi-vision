"""Staged, credential-free durable artifacts for one reference frame."""

import json
import shutil
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from secrets import token_hex
from typing import TypedDict, final

from vigi_vision.recording import RecordingSegment, RecordingWindow
from vigi_vision.reference_frame_models import (
    MANIFEST_SCHEMA_VERSION,
    DecodedFrameEvidence,
    ReferenceFrameArtifactConflictError,
    ReferenceFrameArtifactError,
    ReferenceFrameRequest,
    segment_identity,
)

_FRAME_FILENAME = "frame.jpg"
_MANIFEST_FILENAME = "manifest.json"


class ReferenceFrameManifestDocument(TypedDict):
    """Credential-safe JSON shape persisted next to a durable reference JPEG."""

    schema_version: int
    generation_policy_version: int
    resource_id: str
    status: str
    channel_id: int
    requested_time: str
    requested_time_utc: str
    source_timezone: str
    selected_segment_id: str
    selected_segment_start_utc: str
    selected_segment_end_utc: str
    extraction_start_utc: str
    extraction_end_utc: str
    frame_selection_policy: str
    jpeg_filename: str
    width: int
    height: int
    decoded_local_pts_seconds: float | None
    estimated_source_time_utc: str | None
    offset_from_requested_seconds: float | None
    timing_precision_status: str
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class ReferenceFrameManifest:
    """Typed source facts used to serialize one completed reference-frame manifest."""

    request: ReferenceFrameRequest
    segment: RecordingSegment
    extraction_window: RecordingWindow
    resource_id: str
    evidence: DecodedFrameEvidence
    estimated_source_time_utc: datetime | None
    offset_from_requested_seconds: float | None

    def document(self) -> ReferenceFrameManifestDocument:
        """Return a secret-free manifest document with artifact-relative references only."""
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "generation_policy_version": self.request.generation_policy_version,
            "resource_id": self.resource_id,
            "status": "completed",
            "channel_id": self.request.channel_id,
            "requested_time": self.request.requested_time_text,
            "requested_time_utc": _utc_text(self.request.requested_time_utc),
            "source_timezone": self.request.source_timezone,
            "selected_segment_id": segment_identity(self.segment),
            "selected_segment_start_utc": _utc_text(self.segment.start_utc),
            "selected_segment_end_utc": _utc_text(self.segment.end_utc),
            "extraction_start_utc": _utc_text(self.extraction_window.start_utc),
            "extraction_end_utc": _utc_text(self.extraction_window.end_utc),
            "frame_selection_policy": self.request.frame_selection_policy.value,
            "jpeg_filename": _FRAME_FILENAME,
            "width": self.evidence.width,
            "height": self.evidence.height,
            "decoded_local_pts_seconds": self.evidence.local_pts_seconds,
            "estimated_source_time_utc": (
                _utc_text(self.estimated_source_time_utc)
                if self.estimated_source_time_utc is not None
                else None
            ),
            "offset_from_requested_seconds": self.offset_from_requested_seconds,
            "timing_precision_status": self.evidence.timing_precision_status.value,
            "warnings": list(self.evidence.warnings),
        }


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameArtifactStore:
    """Create invocation-owned staging packages under the configured output root."""

    output_root: Path = field(repr=False)

    def begin(
        self, request: ReferenceFrameRequest, segment: RecordingSegment
    ) -> "ReferenceFrameArtifactSession":
        """Create staging for a new deterministic resource without overwriting completed output."""
        resource_id = _resource_id(request, segment)
        final_directory = _direct_child(self.output_root, resource_id)
        claim_path = _direct_child(self.output_root, f".{resource_id}.claim")
        staging_directory = _direct_child(self.output_root, f".{resource_id}-{token_hex(4)}")
        try:
            self.output_root.mkdir(parents=True, exist_ok=True)
            claim_path.touch(exist_ok=False)
        except FileExistsError:
            raise ReferenceFrameArtifactConflictError from None
        except OSError:
            raise ReferenceFrameArtifactError from None
        try:
            final_exists = final_directory.is_symlink() or final_directory.exists()
        except OSError:
            _remove_claim(claim_path)
            raise ReferenceFrameArtifactError from None
        except BaseException:
            _remove_claim(claim_path)
            raise
        if final_exists:
            _remove_claim(claim_path)
            raise ReferenceFrameArtifactConflictError
        try:
            staging_directory.mkdir(parents=True, exist_ok=False)
            return ReferenceFrameArtifactSession(
                resource_id, final_directory, staging_directory, claim_path
            )
        except ReferenceFrameArtifactConflictError:
            shutil.rmtree(staging_directory, ignore_errors=True)
            _remove_claim(claim_path)
            raise
        except OSError:
            shutil.rmtree(staging_directory, ignore_errors=True)
            _remove_claim(claim_path)
            raise ReferenceFrameArtifactError from None
        except BaseException:
            shutil.rmtree(staging_directory, ignore_errors=True)
            _remove_claim(claim_path)
            raise


@dataclass(frozen=True, slots=True)
class ReferenceFrameArtifactSession:
    """One invocation-owned reference-frame staging directory."""

    resource_id: str
    final_directory: Path = field(repr=False)
    staging_directory: Path = field(repr=False)
    claim_path: Path = field(repr=False)

    @property
    def jpeg_path(self) -> Path:
        """Return the staging JPEG path owned by this invocation."""
        return self.staging_directory / _FRAME_FILENAME

    def finalize(self, manifest: ReferenceFrameManifest) -> tuple[Path, Path]:
        """Write the manifest then promote the complete package without replacement."""
        if manifest.evidence.jpeg_path != self.jpeg_path or not _is_publishable_frame(
            self.jpeg_path
        ):
            self.discard()
            raise ReferenceFrameArtifactError
        try:
            final_exists = self.final_directory.is_symlink() or self.final_directory.exists()
        except OSError:
            self.discard()
            raise ReferenceFrameArtifactError from None
        if final_exists:
            self.discard()
            raise ReferenceFrameArtifactConflictError
        try:
            _ = (self.staging_directory / _MANIFEST_FILENAME).write_text(
                json.dumps(manifest.document(), indent=2) + "\n", encoding="utf-8"
            )
            _ = self.staging_directory.rename(self.final_directory)
        except FileExistsError:
            self.discard()
            raise ReferenceFrameArtifactConflictError from None
        except OSError:
            self.discard()
            raise ReferenceFrameArtifactError from None
        with suppress(OSError):
            self.claim_path.unlink(missing_ok=True)
        return self.final_directory / _FRAME_FILENAME, self.final_directory / _MANIFEST_FILENAME

    def discard(self) -> None:
        """Remove only incomplete resources owned by this invocation."""
        shutil.rmtree(self.staging_directory, ignore_errors=True)
        _remove_claim(self.claim_path)


def _resource_id(request: ReferenceFrameRequest, segment: RecordingSegment) -> str:
    return (
        f"channel-{request.channel_id}_"
        f"{request.requested_time_utc.strftime('%Y%m%dT%H%M%SZ')}_"
        f"{segment_identity(segment)}_{request.frame_selection_policy.value.replace('_', '-')}"
        f"_gpv-{request.generation_policy_version}"
    )


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_publishable_frame(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _direct_child(root: Path, name: str) -> Path:
    candidate = root / name
    if candidate.parent != root:
        raise ReferenceFrameArtifactError
    return candidate


def _remove_claim(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)
