"""Validated completed-reference-frame lookup beneath the configured artifact root."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, Final, Literal, final

from pydantic import BaseModel, ConfigDict, ValidationError

from vigi_vision.recording import RecordingSegment, RecordingWindow, RecordingWindowError
from vigi_vision.reference_frame_artifacts import reference_frame_resource_id
from vigi_vision.reference_frame_models import (
    MANIFEST_SCHEMA_VERSION,
    FrameSelectionPolicy,
    ReferenceFrameRequest,
    ReferenceFrameResourceCorruptError,
    ReferenceFrameResourceIncompatibleError,
    ReferenceFrameResourceNotFoundError,
    ReferenceFrameResult,
    TimingPrecisionStatus,
    segment_identity,
)

_FRAME_FILENAME: Final = "frame.jpg"
_MANIFEST_FILENAME: Final = "manifest.json"
_MIN_JPEG_SIZE: Final = 4
_RESOURCE_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,191}$")


class _ManifestDocument(BaseModel):
    """The strict persisted manifest shape accepted for completed-resource reuse."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    generation_policy_version: int
    resource_id: str
    status: Literal["completed"]
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
    jpeg_filename: Literal["frame.jpg"]
    width: int
    height: int
    decoded_local_pts_seconds: float | None
    estimated_source_time_utc: str | None
    offset_from_requested_seconds: float | None
    timing_precision_status: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReferenceFrameImageResource:
    """A validated durable JPEG owned by a completed reference-frame resource."""

    resource_id: str
    jpeg_path: Path = field(repr=False)


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameResourceStore:
    """Resolve only complete, compatible reference-frame artifacts under one root."""

    output_root: Path = field(repr=False)

    def resolve_for_request(
        self, request: ReferenceFrameRequest, segment: RecordingSegment
    ) -> ReferenceFrameResult | None:
        """Return a compatible completed result or ``None`` when its identity is absent."""
        resource_id = reference_frame_resource_id(request, segment)
        if not self._resource_path(resource_id).exists():
            return None
        document, image = self._open(resource_id)
        self._validate_compatibility(document, request, segment, resource_id)
        return _result_from_document(document, request, segment, resource_id, image.jpeg_path)

    def resolve_image(self, resource_id: str) -> ReferenceFrameImageResource:
        """Return the fixed JPEG only after validating its completed resource package."""
        _, image = self._open(resource_id)
        return image

    def _open(self, resource_id: str) -> tuple[_ManifestDocument, ReferenceFrameImageResource]:
        path = self._resource_path(resource_id)
        try:
            if not path.exists():
                raise ReferenceFrameResourceNotFoundError
            if path.is_symlink() or not path.is_dir():
                raise ReferenceFrameResourceCorruptError
            manifest_path = _direct_child(path, _MANIFEST_FILENAME)
            document = _read_manifest(manifest_path)
            image_path = _direct_child(path, _FRAME_FILENAME)
            _validate_jpeg(image_path)
        except OSError:
            raise ReferenceFrameResourceCorruptError from None
        if document.resource_id != resource_id:
            raise ReferenceFrameResourceCorruptError
        return document, ReferenceFrameImageResource(resource_id, image_path)

    def _resource_path(self, resource_id: str) -> Path:
        if _RESOURCE_ID_PATTERN.fullmatch(resource_id) is None:
            raise ReferenceFrameResourceNotFoundError
        return _direct_child(self.output_root, resource_id)

    def _validate_compatibility(
        self,
        document: _ManifestDocument,
        request: ReferenceFrameRequest,
        segment: RecordingSegment,
        resource_id: str,
    ) -> None:
        if (
            document.schema_version != MANIFEST_SCHEMA_VERSION
            or document.generation_policy_version != request.generation_policy_version
            or document.resource_id != resource_id
            or document.channel_id != request.channel_id
            or document.requested_time_utc != _utc_text(request.requested_time_utc)
            or document.selected_segment_id != segment_identity(segment)
            or document.selected_segment_start_utc != _utc_text(segment.start_utc)
            or document.selected_segment_end_utc != _utc_text(segment.end_utc)
            or document.frame_selection_policy != request.frame_selection_policy.value
        ):
            raise ReferenceFrameResourceIncompatibleError


def _read_manifest(path: Path) -> _ManifestDocument:
    try:
        return _ManifestDocument.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError):
        raise ReferenceFrameResourceCorruptError from None


def _validate_jpeg(path: Path) -> None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size < _MIN_JPEG_SIZE:
            raise ReferenceFrameResourceCorruptError
        with path.open("rb") as image:
            if image.read(2) != b"\xff\xd8":
                raise ReferenceFrameResourceCorruptError
            _ = image.seek(-2, 2)
            if image.read(2) != b"\xff\xd9":
                raise ReferenceFrameResourceCorruptError
    except OSError:
        raise ReferenceFrameResourceCorruptError from None


def _result_from_document(
    document: _ManifestDocument,
    request: ReferenceFrameRequest,
    segment: RecordingSegment,
    resource_id: str,
    _: Path,
) -> ReferenceFrameResult:
    try:
        extraction_window = RecordingWindow(
            request.channel_id,
            _parse_utc(document.extraction_start_utc),
            _parse_utc(document.extraction_end_utc),
        )
        policy = FrameSelectionPolicy(document.frame_selection_policy)
        timing_status = TimingPrecisionStatus(document.timing_precision_status)
        estimated_time = (
            _parse_utc(document.estimated_source_time_utc)
            if document.estimated_source_time_utc is not None
            else None
        )
    except (RecordingWindowError, ValueError):
        raise ReferenceFrameResourceCorruptError from None
    if document.width <= 0 or document.height <= 0:
        raise ReferenceFrameResourceCorruptError
    return ReferenceFrameResult(
        resource_id=resource_id,
        manifest_schema_version=document.schema_version,
        generation_policy_version=document.generation_policy_version,
        channel_id=request.channel_id,
        requested_time_text=request.requested_time_text,
        source_timezone=request.source_timezone,
        requested_time_utc=request.requested_time_utc,
        selected_segment=segment,
        extraction_window=extraction_window,
        frame_selection_policy=policy,
        jpeg_relative_path=Path(resource_id) / _FRAME_FILENAME,
        manifest_relative_path=Path(resource_id) / _MANIFEST_FILENAME,
        width=document.width,
        height=document.height,
        decoded_local_pts_seconds=document.decoded_local_pts_seconds,
        estimated_source_time_utc=estimated_time,
        offset_from_requested_seconds=document.offset_from_requested_seconds,
        timing_precision_status=timing_status,
        warnings=document.warnings,
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError
    return parsed


def _direct_child(root: Path, name: str) -> Path:
    candidate = root / name
    if candidate.parent != root:
        raise ReferenceFrameResourceCorruptError
    return candidate


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
