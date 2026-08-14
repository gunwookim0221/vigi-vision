"""Mutex-scoped atomic publication and canonical duplicate reuse."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vigi_vision.recording_search_a2_models import RecordingSearchManifestV2
from vigi_vision.recording_search_a2_repository import (
    read_schema2_children_for_probe_admission,
)
from vigi_vision.recording_search_b2_append_repository import (
    publish_schema3_alias_append,
    publish_schema3_classification_append,
)
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b2_policy import RecordingSearchPolicyV3
from vigi_vision.recording_search_b2_repository import publish_schema3_successor
from vigi_vision.recording_search_b4_models import (
    ClassificationOperationalError,
    ClassificationOperationalReason,
    ClassificationPublicationOutcome,
    PublishedClassificationResult,
)
from vigi_vision.recording_search_b4_records import (
    build_alias_record,
    build_baseline_record,
    build_observation_record,
    build_operation_record,
    result_from_duplicate,
    result_from_observation,
)
from vigi_vision.recording_search_b4_support import fail, fractional_utc_now
from vigi_vision.recording_search_models import (
    RecordingSearchArtifactError,
    RecordingSearchError,
    RecordingSearchManifestCorruptError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from vigi_vision.recording_search_a2_models import ProbeFrameRequestRecord
    from vigi_vision.recording_search_b3_contracts import (
        ClassificationHost,
    )
    from vigi_vision.recording_search_b3_models import (
        CanonicalDuplicateResult,
        ClassificationSnapshot,
        ClassifyRecordingProbeRequest,
        NonAuthoritativeClassificationResult,
    )
    from vigi_vision.recording_search_b3_service import RecordingSearchClassificationService


@dataclass(frozen=True, slots=True)
class ClassificationPublisher:
    """Publish only mutex-revalidated classification records."""

    host: ClassificationHost
    preparer: RecordingSearchClassificationService
    now_utc: Callable[[], datetime]
    operation_id_factory: Callable[[], str]

    def current(
        self, request: ClassifyRecordingProbeRequest
    ) -> RecordingSearchManifestV2 | RecordingSearchManifestV3:
        """Strictly load the current authoritative manifest."""
        try:
            current = self.host.repository.load_for_probe_admission(
                request.investigation_id, request.search_run_id
            )
        except RecordingSearchError:
            fail(ClassificationOperationalReason.STALE_MANIFEST)
        if not isinstance(current, RecordingSearchManifestV2 | RecordingSearchManifestV3):
            fail(ClassificationOperationalReason.STALE_MANIFEST)
        return current

    def publish(
        self,
        current: RecordingSearchManifestV2 | RecordingSearchManifestV3,
        snapshot: ClassificationSnapshot,
        prepared: NonAuthoritativeClassificationResult,
        request: ClassifyRecordingProbeRequest,
    ) -> PublishedClassificationResult:
        """Commit one observation or recover a committed duplicate after response loss."""
        published_at = fractional_utc_now(self.now_utc)
        operation = build_operation_record(snapshot, self.operation_id_factory(), published_at)
        observation = build_observation_record(
            snapshot, operation, prepared.classification, published_at
        )
        baseline = build_baseline_record(snapshot, current, published_at)
        try:
            if isinstance(current, RecordingSearchManifestV2):
                policy = RecordingSearchPolicyV3.from_policies(current.policy, snapshot.policy)
                _ = publish_schema3_successor(
                    self.host.repository,
                    current,
                    policy,
                    baseline,
                    operation,
                    observation,
                )
            else:
                _ = publish_schema3_classification_append(
                    self.host.repository, current, operation, observation
                )
        except (
            RecordingSearchArtifactError,
            RecordingSearchManifestCorruptError,
            ValidationError,
            ValueError,
            TypeError,
        ) as error:
            recovered = self._recover_committed_duplicate(request, observation.observation_id)
            if recovered is not None:
                return recovered
            if isinstance(error, RecordingSearchManifestCorruptError):
                fail(ClassificationOperationalReason.PUBLICATION_CONFLICT)
            fail(ClassificationOperationalReason.PERSISTENCE_FAILURE)
        return result_from_observation(observation, ClassificationPublicationOutcome.CREATED)

    def reuse(
        self,
        duplicate: CanonicalDuplicateResult,
        request: ClassifyRecordingProbeRequest,
    ) -> PublishedClassificationResult:
        """Return or atomically append the canonical alias for a duplicate frame."""
        if not duplicate.alias_required:
            return result_from_duplicate(duplicate)
        current = self.current(request)
        if not isinstance(current, RecordingSearchManifestV3):
            fail(ClassificationOperationalReason.AUTHORITATIVE_STATE_CHANGED)
        request_record = _request_record(self.host, current, request.probe_request_id)
        alias = build_alias_record(duplicate, request_record, fractional_utc_now(self.now_utc))
        try:
            _ = publish_schema3_alias_append(self.host.repository, current, alias)
        except (
            RecordingSearchArtifactError,
            RecordingSearchManifestCorruptError,
            ValueError,
        ):
            recovered = self._recover_committed_duplicate(request, duplicate.observation_id)
            if recovered is not None:
                return recovered
            fail(ClassificationOperationalReason.PERSISTENCE_FAILURE)
        return result_from_duplicate(duplicate, alias.alias_id)

    def _recover_committed_duplicate(
        self,
        request: ClassifyRecordingProbeRequest,
        expected_observation_id: str,
    ) -> PublishedClassificationResult | None:
        try:
            refreshed = self.current(request)
        except ClassificationOperationalError:
            return None
        found = self.preparer.find_duplicate_locked(refreshed, request)
        if found is None or found.observation_id != expected_observation_id:
            return None
        return result_from_duplicate(found)


def _request_record(
    host: ClassificationHost,
    manifest: RecordingSearchManifestV3,
    probe_request_id: str,
) -> ProbeFrameRequestRecord:
    _, _, requests = read_schema2_children_for_probe_admission(
        host.repository.root,
        host.repository.run_path(manifest.investigation_id, manifest.search_run_id),
        manifest.as_schema2(),
    )
    record = requests.get(probe_request_id)
    if record is None:
        fail(ClassificationOperationalReason.ACQUISITION_STATE_CORRUPT)
    return record
