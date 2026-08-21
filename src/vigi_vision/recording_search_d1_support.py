"""Protocols and pure helpers shared by the Phase 7D-1 service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeVar

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from datetime import datetime

    from vigi_vision.recording_search_a2_models import ProbeFrameRequestRecord
    from vigi_vision.recording_search_b3_models import ClassifyRecordingProbeRequest
    from vigi_vision.recording_search_b4_models import PublishedClassificationResult
    from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
    from vigi_vision.recording_search_models import RecordingSearchPolicy

from vigi_vision.recording_search_d1_models import (
    NarrowingBoundEvidence,
    NarrowingProbeEvidence,
    NarrowingState,
)


class NarrowingHandle(Protocol):
    """Active run identity and lock ownership used by narrowing."""

    @property
    def investigation_id(self) -> str:
        """Return the active investigation identity."""
        ...

    @property
    def search_run_id(self) -> str:
        """Return the active recording-search run identity."""
        ...

    @property
    def phase6_confirmation_id(self) -> str:
        """Return the Phase 6 confirmation identity bound to this run."""
        ...

    @property
    def baseline_identity(self) -> str:
        """Return the immutable baseline identity bound to this run."""
        ...

    @property
    def closed(self) -> bool:
        """Return whether the active handle has released its run lock."""
        ...


HandleT_contra = TypeVar("HandleT_contra", contravariant=True)


class NarrowingHost(Protocol[HandleT_contra]):
    """Existing A2/B4 composition surface."""

    def acquire_targets(
        self,
        handle: HandleT_contra,
        requested_times: tuple[datetime, ...],
    ) -> tuple[ProbeFrameRequestRecord, ...]:
        """Acquire canonical A2 probe requests for the requested times."""
        ...

    def classify(
        self,
        handle: HandleT_contra,
        request: ClassifyRecordingProbeRequest,
    ) -> PublishedClassificationResult:
        """Classify one existing canonical probe through B4."""
        ...

    def a2_mutation(self, handle: HandleT_contra) -> AbstractContextManager[None]:
        """Return the existing A2 mutation exclusion boundary."""
        ...


class NarrowingEvidenceStore(Protocol[HandleT_contra]):
    """Strict evidence adapter for one active run repository."""

    def validate_bracket(
        self,
        handle: HandleT_contra,
        bracket: CoarseCandidateBracket,
        policy: RecordingSearchPolicy,
    ) -> None:
        """Validate that the bracket still matches authoritative run evidence."""
        ...

    def load_state(
        self,
        handle: HandleT_contra,
        bracket: CoarseCandidateBracket,
    ) -> NarrowingState:
        """Load an in-memory narrowing state from persisted evidence."""
        ...

    def find_existing(
        self,
        handle: HandleT_contra,
        requested_time_utc: datetime,
        target_id: str,
    ) -> NarrowingProbeEvidence | None:
        """Find strict existing evidence for one requested target."""
        ...

    def resolve_request(
        self,
        handle: HandleT_contra,
        request: ProbeFrameRequestRecord,
        target_id: str,
    ) -> NarrowingProbeEvidence:
        """Resolve one acquired request into strict admitted evidence."""
        ...

    def current_manifest_digest(self, handle: HandleT_contra) -> str:
        """Return the current authoritative manifest digest."""
        ...


def bound_from_evidence(evidence: NarrowingProbeEvidence) -> NarrowingBoundEvidence:
    """Convert admitted visual evidence into a retained bound."""
    if evidence.state is None or evidence.observation_id is None:
        raise ValueError
    return NarrowingBoundEvidence(
        target_id=evidence.target_id,
        requested_time_utc=evidence.requested_time_utc,
        state=evidence.state,
        observation_id=evidence.observation_id,
        probe_request_id=evidence.probe_request_id,
        canonical_frame_id=evidence.canonical_frame_id,
        operation_id=evidence.operation_id,
        decode_session_id=evidence.decode_session_id,
        decoded_frame_utc=evidence.decoded_frame_utc,
        decoded_pts=evidence.decoded_pts,
        decoded_ordinal=evidence.decoded_ordinal,
    )


def require_successful_result(
    result: PublishedClassificationResult,
    request: ProbeFrameRequestRecord,
) -> None:
    """Reject a classifier result that does not preserve A2 identity."""
    if (
        result.probe_request_id != request.probe_request_id
        or result.canonical_frame_id != request.canonical_frame_id
        or request.canonical_frame_id is None
    ):
        raise ValueError
