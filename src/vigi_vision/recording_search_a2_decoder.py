"""Decoder boundary for authoritative Phase 7A-2 source-time provenance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vigi_vision.recording_search_models import RecordingSearchError

if TYPE_CHECKING:
    from datetime import datetime

    from vigi_vision.recording_search_a2_models import BatchDecodeRequest, DecodedTargetResult


class MissingProvenanceError(RecordingSearchError):
    """The decoder did not provide mandatory source-time evidence."""


class RecordingProbeBatchDecoder(Protocol):
    """Decode ordered target times while preserving raw source timing."""

    def decode_targets(
        self,
        acquisition: BatchDecodeRequest,
        ordered_requested_targets: tuple[datetime, ...],
    ) -> tuple[DecodedTargetResult, ...]:
        """Return one credential-free selected result per resolved target."""
        ...


@dataclass(frozen=True, slots=True)
class UnconfiguredBatchDecoder:
    """Safe default when no authoritative A2 decoder is configured."""

    def decode_targets(
        self,
        acquisition: BatchDecodeRequest,
        ordered_requested_targets: tuple[datetime, ...],
    ) -> tuple[DecodedTargetResult, ...]:
        """Reject decoding when authoritative source timing is unavailable."""
        del acquisition, ordered_requested_targets
        raise MissingProvenanceError
