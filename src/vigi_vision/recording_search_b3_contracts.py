"""Typed Phase 7B capture capabilities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from typing_extensions import override

from vigi_vision.recording_search_a2_support import (
    A2HandleBoundary,
    A2RepositoryBoundary,
    A2ServiceBoundary,
)
from vigi_vision.recording_search_b2_repository import Schema3RepositoryBoundary

if TYPE_CHECKING:
    from vigi_vision.recording_search_a2_models import RecordingSearchManifestV2
    from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
    from vigi_vision.recording_search_b3_media import DecodedMedia
    from vigi_vision.recording_search_models import RecordingSearchManifest


class MediaDecoder(Protocol):
    """Decode one exact admitted byte sequence into immutable RGB media."""

    def decode(self, payload: bytes, width: int, height: int) -> DecodedMedia:
        """Validate and decode the supplied bytes."""
        ...


class ClassificationRepository(A2RepositoryBoundary, Schema3RepositoryBoundary, Protocol):
    """Combine acquisition reads with schema-3 atomic publication."""

    @override
    def load(
        self, investigation_id: str, search_run_id: str
    ) -> RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchManifestV3:
        """Strictly load the current supported recording-search schema."""
        ...


class ClassificationHost(A2ServiceBoundary, Protocol):
    """Expose only the active run repository and existing mutation boundary."""

    @property
    @override
    def repository(self) -> ClassificationRepository:
        """Return the combined acquisition and classification repository."""
        ...


class ClassificationHandle(A2HandleBoundary, Protocol):
    """Active handle extension exposing the invocation-owned baseline bytes."""

    @property
    def baseline_bytes(self) -> bytes:
        """Return the immutable baseline bytes captured for this handle."""
        ...
