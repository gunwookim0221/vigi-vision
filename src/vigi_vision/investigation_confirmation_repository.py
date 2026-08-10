"""Immutable schema 2 confirmation publication and read-only loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Final, final

from pydantic import ValidationError

from vigi_vision._investigation_confirmation_storage import (
    direct_child,
    ensure_root,
    entry_exists,
    publish_directory_no_replace,
    remove_staging,
    resource_matches,
    sync_directory,
    validate_final_directory,
)
from vigi_vision.durable_io import (
    DurableJsonError,
    is_safe_contained_path,
    load_durable_json_object,
)
from vigi_vision.investigation_confirmation_claims import (
    ConfirmationClaim,
    ConfirmationClaimStore,
)
from vigi_vision.investigation_confirmation_integrity import (
    JpegDecoder,
    compute_jpeg_integrity,
)
from vigi_vision.investigation_confirmation_models import (
    CONFIRMATION_SCHEMA_TWO,
    CONFIRMATION_SCHEMA_VERSION,
    ConfirmationArtifactError,
    ConfirmationConflictError,
    ConfirmationCorruptError,
    ConfirmationError,
    ConfirmationInProgressError,
    ConfirmationManifest,
    ConfirmationOutcome,
    ConfirmationResult,
    InvestigationConfirmationNotFoundError,
    LegacyInvestigationError,
    artifact_relative_path,
    canonical_manifest_json,
    is_investigation_id,
)
from vigi_vision.reference_frame_models import ReferenceFrameError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vigi_vision.reference_frame_resources import (
        ReferenceFrameResourceMetadata,
        ReferenceFrameResourceStore,
    )

_MANIFEST_FILENAME: Final = "manifest.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@final
@dataclass(frozen=True, slots=True)
class InvestigationConfirmationRepository:
    """Publish and load immutable confirmations under one artifact root."""

    output_root: Path = field(repr=False)
    resource_store: ReferenceFrameResourceStore = field(repr=False)
    now_utc: Callable[[], datetime] = _utc_now
    jpeg_decoder: JpegDecoder | None = field(default=None, repr=False)

    def publish(self, manifest: ConfirmationManifest) -> ConfirmationResult:
        """Create a confirmation or resolve an identical existing package."""
        if manifest.schema_version != CONFIRMATION_SCHEMA_VERSION:
            raise ConfirmationArtifactError
        final_directory = self._final_directory(manifest.investigation_id)
        ensure_root(self.output_root)
        if entry_exists(self.output_root, final_directory):
            return self._resolve_existing(final_directory, manifest)

        claims = ConfirmationClaimStore(self.output_root, self.now_utc)
        try:
            claim = claims.acquire(manifest.investigation_id, final_directory=final_directory)
        except ConfirmationInProgressError:
            if entry_exists(self.output_root, final_directory):
                return self._resolve_existing(final_directory, manifest)
            raise
        try:
            return self._publish_claimed(manifest, final_directory, claim)
        finally:
            claim.release()

    def _publish_claimed(
        self,
        manifest: ConfirmationManifest,
        final_directory: Path,
        claim: ConfirmationClaim,
    ) -> ConfirmationResult:
        staging_directory = direct_child(
            self.output_root, f".{manifest.investigation_id}-{claim.operation_id}.staging"
        )
        staging_created = False
        try:
            if entry_exists(self.output_root, final_directory):
                return self._resolve_existing(final_directory, manifest)
            staging_directory.mkdir(parents=False, exist_ok=False)
            staging_created = True
            _write_manifest(staging_directory / _MANIFEST_FILENAME, manifest)
            try:
                staged = _read_manifest(
                    staging_directory / _MANIFEST_FILENAME, manifest.investigation_id
                )
            except (ConfirmationCorruptError, LegacyInvestigationError):
                raise ConfirmationArtifactError from None
            if canonical_manifest_json(staged) != canonical_manifest_json(manifest):
                raise ConfirmationArtifactError
            resource = self.resolve_resource_for_manifest(manifest)
            self._validate_jpeg_integrity(manifest, resource)
            if not publish_directory_no_replace(staging_directory, final_directory):
                return self._resolve_existing(final_directory, manifest)
            sync_directory(self.output_root)
            return ConfirmationResult(manifest, ConfirmationOutcome.CREATED, final_directory)
        except ConfirmationError:
            raise
        except (OSError, ReferenceFrameError, ValidationError, ValueError):
            raise ConfirmationArtifactError from None
        finally:
            if staging_created:
                remove_staging(self.output_root, staging_directory)

    def _validate_jpeg_integrity(
        self,
        manifest: ConfirmationManifest,
        resource: ReferenceFrameResourceMetadata,
    ) -> None:
        integrity = compute_jpeg_integrity(
            resource.jpeg_path,
            resource.width,
            resource.height,
            self.jpeg_decoder,
        )
        reference = manifest.confirmation.reference_frame
        if (
            reference.jpeg_sha256 != integrity.sha256
            or reference.jpeg_size_bytes != integrity.size_bytes
        ):
            raise ConfirmationArtifactError

    def load(self, investigation_id: str) -> ConfirmationManifest:
        """Load one strictly parsed schema 2 or 3 confirmation without mutation."""
        final_directory = self._final_directory(investigation_id)
        try:
            if not self.output_root.exists():
                raise InvestigationConfirmationNotFoundError
            if not is_safe_contained_path(self.output_root, final_directory):
                raise ConfirmationCorruptError
            if final_directory.is_symlink():
                raise ConfirmationCorruptError
            if not final_directory.exists():
                raise InvestigationConfirmationNotFoundError
        except OSError:
            raise ConfirmationCorruptError from None
        validate_final_directory(final_directory)
        return _read_manifest(final_directory / _MANIFEST_FILENAME, investigation_id)

    def _resolve_existing(
        self, final_directory: Path, requested: ConfirmationManifest
    ) -> ConfirmationResult:
        if not is_safe_contained_path(self.output_root, final_directory, require_target=True):
            raise ConfirmationCorruptError
        validate_final_directory(final_directory)
        existing = _read_manifest(final_directory / _MANIFEST_FILENAME, requested.investigation_id)
        if existing.material_json() != requested.material_json():
            raise ConfirmationConflictError
        return ConfirmationResult(existing, ConfirmationOutcome.REUSED, final_directory)

    def resolve_resource_for_manifest(
        self, manifest: ConfirmationManifest
    ) -> ReferenceFrameResourceMetadata:
        """Resolve and compare the trusted reference resource for a manifest."""
        resource_id = manifest.confirmation.reference_frame.resource_id
        resource = self.resource_store.resolve_resource(resource_id)
        if not resource_matches(resource, manifest):
            raise ConfirmationArtifactError
        return resource

    def _final_directory(self, investigation_id: str) -> Path:
        if not is_investigation_id(investigation_id):
            raise InvestigationConfirmationNotFoundError
        relative = artifact_relative_path(investigation_id)
        return direct_child(self.output_root, relative.rsplit("/", maxsplit=1)[-1])


def _read_manifest(path: Path, investigation_id: str) -> ConfirmationManifest:
    try:
        is_invalid_file = path.is_symlink() or not path.is_file()
    except OSError:
        raise ConfirmationCorruptError from None
    if is_invalid_file:
        raise ConfirmationCorruptError
    try:
        raw = path.read_text(encoding="utf-8")
        payload = load_durable_json_object(raw)
    except (DurableJsonError, OSError, UnicodeError):
        raise ConfirmationCorruptError from None
    schema = payload.get("schema_version")
    if schema is None or (type(schema) is int and schema == 1):
        raise LegacyInvestigationError
    if type(schema) is not int or schema not in (
        CONFIRMATION_SCHEMA_TWO,
        CONFIRMATION_SCHEMA_VERSION,
    ):
        raise ConfirmationCorruptError
    try:
        manifest = ConfirmationManifest.model_validate_json(raw, strict=True)
    except ValidationError:
        raise ConfirmationCorruptError from None
    if manifest.investigation_id != investigation_id:
        raise ConfirmationCorruptError
    return manifest


def _write_manifest(path: Path, manifest: ConfirmationManifest) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            _ = stream.write(canonical_manifest_json(manifest))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise ConfirmationArtifactError from None
