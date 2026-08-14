from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from tests.test_recording_search_a2 import successful_a2_run

from vigi_vision.durable_io import load_durable_json_object
from vigi_vision.object_presence_evidence import RawComparison
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.object_presence_values import ClassificationOutcome, VisualStatus
from vigi_vision.recording_search_a2_models import (
    ProbeFrameRequestRecord,
    ProbeRequestStatus,
    RecordingSearchManifestV2,
)
from vigi_vision.recording_search_a2_repository import read_schema2_children
from vigi_vision.recording_search_b2_identity import (
    alias_id_for,
    baseline_observation_id_for,
    observation_id_for,
)
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b2_policy import RecordingSearchPolicyV3
from vigi_vision.recording_search_b2_records import (
    ClassificationOperationRecord,
    ConfirmedReferenceBaselineRecord,
    RecordingProbeObservationRecord,
    TargetAliasRecord,
)
from vigi_vision.recording_search_b2_repository import publish_schema3_successor
from vigi_vision.recording_search_models import (
    RecordingSearchArtifactError,
    RecordingSearchManifestCorruptError,
)
from vigi_vision.recording_search_repository import RecordingSearchRepository
from vigi_vision.recording_search_service import RecordingSearchRunHandle

_B2Values = tuple[
    RecordingSearchRepository,
    RecordingSearchRunHandle,
    RecordingSearchManifestV2,
    RecordingSearchPolicyV3,
    ConfirmedReferenceBaselineRecord,
    ClassificationOperationRecord,
    RecordingProbeObservationRecord,
]


def _build_values(root: Path) -> _B2Values:
    service, investigation_id, handle, manifest, request = successful_a2_run(root)
    assert request.canonical_frame_id is not None
    confirmation = manifest.confirmation
    baseline_id = baseline_observation_id_for(
        investigation_id=investigation_id,
        search_run_id=manifest.search_run_id,
        channel_id=confirmation.channel_id,
        reference_frame_resource_id=confirmation.reference_frame_resource_id,
        reference_requested_time_utc=confirmation.reference_requested_time_utc,
        source_width=confirmation.source_width,
        source_height=confirmation.source_height,
        roi=confirmation.roi.model_dump(mode="json"),
        jpeg_sha256=confirmation.jpeg_sha256,
        jpeg_size_bytes=confirmation.jpeg_size_bytes,
    )
    baseline = ConfirmedReferenceBaselineRecord(
        record_type="confirmed_reference_baseline",
        observation_id=baseline_id,
        investigation_id=investigation_id,
        search_run_id=manifest.search_run_id,
        channel_id=confirmation.channel_id,
        reference_frame_resource_id=confirmation.reference_frame_resource_id,
        reference_requested_time_utc=confirmation.reference_requested_time_utc,
        source_width=confirmation.source_width,
        source_height=confirmation.source_height,
        roi=confirmation.roi,
        jpeg_sha256=confirmation.jpeg_sha256,
        jpeg_size_bytes=confirmation.jpeg_size_bytes,
        timing_precision_status=confirmation.timing_precision_status,
        warnings=confirmation.warnings,
        state="PRESENT",
        reason_code="user_confirmed_reference",
        published_at_utc=manifest.created_at_utc,
    )
    classifier = ObjectPresenceDecisionPolicy(
        minimum_mask_overlap_for_comparison=0.1,
        minimum_comparison_area=1,
        minimum_clipped_mask_pixels=1,
    )
    policy = RecordingSearchPolicyV3.from_policies(manifest.policy, classifier)
    operation = ClassificationOperationRecord(
        record_type="classification_operation",
        classification_operation_id="classification-op-test",
        investigation_id=investigation_id,
        search_run_id=manifest.search_run_id,
        operation_kind="recording_probe_classification_v1",
        state="ADMITTED",
        probe_request_id=request.probe_request_id,
        canonical_frame_id=request.canonical_frame_id,
        baseline_observation_id=baseline_id,
        classifier_policy_version=classifier.classifier_policy_version,
        admitted_at_utc=manifest.created_at_utc,
    )
    comparison = RawComparison(
        baseline_mask_pixel_count=80,
        probe_mask_pixel_count=80,
        roi_pixel_count=100,
        mask_intersection_pixel_count=80,
        mask_union_pixel_count=80,
        baseline_mask_coverage=0.8,
        probe_mask_coverage=0.8,
        mask_iou=1.0,
        effective_comparison_area=80,
        roi_luma_ncc=1.0,
        visual_status=VisualStatus.COMPARABLE,
        unusable_reason=None,
    )
    observation_id = observation_id_for(
        investigation_id=investigation_id,
        search_run_id=manifest.search_run_id,
        channel_id=confirmation.channel_id,
        baseline_observation_id=baseline_id,
        canonical_frame_id=request.canonical_frame_id,
        classifier_policy_version=classifier.classifier_policy_version,
    )
    observation = RecordingProbeObservationRecord(
        record_type="recording_probe",
        observation_id=observation_id,
        investigation_id=investigation_id,
        search_run_id=manifest.search_run_id,
        channel_id=confirmation.channel_id,
        classification_operation_id=operation.classification_operation_id,
        baseline_observation_id=baseline_id,
        canonical_frame_id=request.canonical_frame_id,
        primary_probe_request_id=request.probe_request_id,
        primary_requested_time_utc=request.requested_time_utc,
        classifier_policy_version=classifier.classifier_policy_version,
        state=ClassificationOutcome.PRESENT,
        reason_code=None,
        classifier_evidence=comparison,
        published_at_utc=manifest.created_at_utc,
    )
    return service.repository, handle, manifest, policy, baseline, operation, observation


def test_valid_schema3_publication_reopens_and_preserves_schema2_until_commit() -> None:
    with TemporaryDirectory() as directory:
        repository, handle, manifest, policy, baseline, operation, observation = _build_values(
            Path(directory)
        )
        try:
            result = publish_schema3_successor(
                repository, manifest, policy, baseline, operation, observation
            )
            assert isinstance(result, RecordingSearchManifestV3)
            assert repository.load(manifest.investigation_id, manifest.search_run_id) == result
            assert result.baseline_observation_id == baseline.observation_id
            assert result.canonical_observation_ids == (observation.observation_id,)
        finally:
            handle.release()


def test_failed_manifest_replacement_leaves_schema2_authoritative() -> None:
    with TemporaryDirectory() as directory:
        repository, handle, manifest, policy, baseline, operation, observation = _build_values(
            Path(directory)
        )
        try:
            with (
                patch.object(
                    type(repository),
                    "write_schema3_manifest",
                    side_effect=RecordingSearchArtifactError,
                ),
                pytest.raises(RecordingSearchArtifactError),
            ):
                _ = publish_schema3_successor(
                    repository, manifest, policy, baseline, operation, observation
                )
            assert (
                repository.load(manifest.investigation_id, manifest.search_run_id).schema_version
                == 2
            )
        finally:
            handle.release()


def test_strict_reopen_rejects_unknown_observation_key() -> None:
    with TemporaryDirectory() as directory:
        repository, handle, manifest, policy, baseline, operation, observation = _build_values(
            Path(directory)
        )
        try:
            _ = publish_schema3_successor(
                repository, manifest, policy, baseline, operation, observation
            )
            path = repository.run_path(manifest.investigation_id, manifest.search_run_id)
            observation_path = path / "observations" / f"{observation.observation_id}.json"
            payload = load_durable_json_object(observation_path.read_text(encoding="utf-8"))
            payload["unexpected"] = True
            _ = observation_path.write_text(json.dumps(payload), encoding="utf-8")
            with pytest.raises(RecordingSearchManifestCorruptError):
                _ = repository.load(manifest.investigation_id, manifest.search_run_id)
        finally:
            handle.release()


def test_precommit_failure_removes_only_new_schema3_namespaces() -> None:
    with TemporaryDirectory() as directory:
        repository, handle, manifest, policy, baseline, operation, observation = _build_values(
            Path(directory)
        )
        try:
            with (
                patch(
                    "vigi_vision.recording_search_b2_repository.validate_schema3_tree",
                    side_effect=RecordingSearchManifestCorruptError,
                ),
                pytest.raises(RecordingSearchManifestCorruptError),
            ):
                _ = publish_schema3_successor(
                    repository, manifest, policy, baseline, operation, observation
                )
            assert isinstance(
                repository.load(manifest.investigation_id, manifest.search_run_id),
                RecordingSearchManifestV2,
            )
            run_path = repository.run_path(manifest.investigation_id, manifest.search_run_id)
            assert not (run_path / "classification-operations").exists()
            assert (run_path / "observations").is_dir()
            assert not list((run_path / "observations").iterdir())
            assert not list(run_path.glob(".phase7b2-*"))
        finally:
            handle.release()


def test_strict_reopen_rejects_unindexed_observation_residue() -> None:
    with TemporaryDirectory() as directory:
        repository, handle, manifest, policy, baseline, operation, observation = _build_values(
            Path(directory)
        )
        try:
            _ = publish_schema3_successor(
                repository, manifest, policy, baseline, operation, observation
            )
            residue = (
                repository.run_path(manifest.investigation_id, manifest.search_run_id)
                / "observations"
                / f"observation-{'f' * 64}.json"
            )
            _ = residue.write_text("{}", encoding="utf-8")
            with pytest.raises(RecordingSearchManifestCorruptError):
                _ = repository.load(manifest.investigation_id, manifest.search_run_id)
        finally:
            handle.release()


def test_strict_reopen_rejects_measurements_contradicting_area_policy() -> None:
    with TemporaryDirectory() as directory:
        repository, handle, manifest, policy, baseline, operation, observation = _build_values(
            Path(directory)
        )
        try:
            _ = publish_schema3_successor(
                repository, manifest, policy, baseline, operation, observation
            )
            path = repository.run_path(manifest.investigation_id, manifest.search_run_id)
            observation_path = path / "observations" / f"{observation.observation_id}.json"
            payload = load_durable_json_object(observation_path.read_text(encoding="utf-8"))
            evidence = payload["classifier_evidence"]
            assert isinstance(evidence, dict)
            evidence.update(
                {
                    "effective_comparison_area": 80,
                    "visual_status": "unusable",
                    "unusable_reason": "insufficient_comparison_area",
                    "roi_luma_ncc": None,
                }
            )
            payload["state"] = "INDETERMINATE"
            payload["reason_code"] = "insufficient_comparison_area"
            _ = observation_path.write_text(json.dumps(payload), encoding="utf-8")
            with pytest.raises(RecordingSearchManifestCorruptError):
                _ = repository.load(manifest.investigation_id, manifest.search_run_id)
        finally:
            handle.release()


def test_schema3_alias_is_indexed_without_creating_observation_evidence() -> None:
    with TemporaryDirectory() as directory:
        repository, handle, manifest, policy, baseline, operation, observation = _build_values(
            Path(directory)
        )
        try:
            run_path = repository.run_path(manifest.investigation_id, manifest.search_run_id)
            _, _, requests = read_schema2_children(run_path.parent.parent, run_path, manifest)
            primary = requests[operation.probe_request_id]
            alias_request = ProbeFrameRequestRecord(
                record_type="probe_frame_request",
                probe_request_id="probe-request-alias",
                investigation_id=primary.investigation_id,
                search_run_id=primary.search_run_id,
                operation_id=primary.operation_id,
                channel_id=primary.channel_id,
                requested_time_utc=primary.requested_time_utc + timedelta(seconds=1),
                status=ProbeRequestStatus.SUCCEEDED,
                canonical_frame_id=primary.canonical_frame_id,
                alias_of_probe_request_id=primary.probe_request_id,
                failure_reason=None,
                created_at_utc=primary.created_at_utc,
                completed_at_utc=primary.completed_at_utc,
            )
            promoted = repository.publish_a2_bundle(manifest, (alias_request,), ())
            assert isinstance(promoted, RecordingSearchManifestV2)
            alias = TargetAliasRecord(
                record_type="target_alias",
                alias_id=alias_id_for(
                    promoted.search_run_id,
                    alias_request.probe_request_id,
                    observation.observation_id,
                ),
                investigation_id=promoted.investigation_id,
                search_run_id=promoted.search_run_id,
                channel_id=alias_request.channel_id,
                probe_request_id=alias_request.probe_request_id,
                requested_time_utc=alias_request.requested_time_utc,
                canonical_observation_id=observation.observation_id,
                reason_code="same_decoded_frame",
                published_at_utc=promoted.created_at_utc,
            )
            result = publish_schema3_successor(
                repository, promoted, policy, baseline, operation, observation, (alias,)
            )
            assert result.target_alias_ids == (alias.alias_id,)
            assert result.canonical_observation_ids == (observation.observation_id,)
        finally:
            handle.release()


def test_observation_identity_excludes_administrative_time() -> None:
    assert observation_id_for(
        investigation_id="object-disappearance-v3-ch1-20260720T033418Z",
        search_run_id="search-run-abcdef12",
        channel_id=1,
        baseline_observation_id="baseline-" + "a" * 64,
        canonical_frame_id="frame-" + "b" * 64,
        classifier_policy_version="efficient-sam-ti-roi-ncc-v1",
    ) == observation_id_for(
        investigation_id="object-disappearance-v3-ch1-20260720T033418Z",
        search_run_id="search-run-abcdef12",
        channel_id=1,
        baseline_observation_id="baseline-" + "a" * 64,
        canonical_frame_id="frame-" + "b" * 64,
        classifier_policy_version="efficient-sam-ti-roi-ncc-v1",
    )


def test_identical_schema3_retry_reuses_without_rewriting() -> None:
    with TemporaryDirectory() as directory:
        repository, handle, manifest, policy, baseline, operation, observation = _build_values(
            Path(directory)
        )
        try:
            first = publish_schema3_successor(
                repository, manifest, policy, baseline, operation, observation
            )
            observation_path = (
                repository.run_path(manifest.investigation_id, manifest.search_run_id)
                / "observations"
                / f"{observation.observation_id}.json"
            )
            before = observation_path.read_bytes()
            second = publish_schema3_successor(
                repository, manifest, policy, baseline, operation, observation
            )
            assert second == first
            assert observation_path.read_bytes() == before
        finally:
            handle.release()
