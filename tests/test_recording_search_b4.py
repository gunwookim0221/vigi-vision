from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn

import pytest
from tests.recording_search_b4_support import (
    ControlledExecutor,
    build_harness,
    completed_future,
    completed_result,
    install_executor,
    timed_out_future,
    unsafe_future,
)
from tests.test_recording_search_a2 import successful_a2_run

from vigi_vision import recording_search_b4_publication as publication_module
from vigi_vision.assisted_roi_geometry import ImageSize, Point
from vigi_vision.investigation_confirmation_integrity import compute_jpeg_integrity_from_bytes
from vigi_vision.object_presence_models import BinaryMask, DecodedRgbImage
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.recording_search_a2_models import (
    BatchDecodeRequest,
    DecodedTargetResult,
    RecordingSearchManifestV2,
    SourceTimeBase,
)
from vigi_vision.recording_search_a2_service import admit_probe_frame_bytes
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b2_policy import RecordingSearchPolicyV3
from vigi_vision.recording_search_b2_records import (
    ClassificationOperationRecord,
    ConfirmedReferenceBaselineRecord,
    RecordingProbeObservationRecord,
    TargetAliasRecord,
)
from vigi_vision.recording_search_b2_repository import Schema3RepositoryBoundary
from vigi_vision.recording_search_b2_repository import (
    publish_schema3_successor as publish_schema3,
)
from vigi_vision.recording_search_b3_media import DecodedMedia
from vigi_vision.recording_search_b3_models import (
    ClassificationSnapshot,
    ClassifyRecordingProbeRequest,
    NonAuthoritativeClassificationResult,
)
from vigi_vision.recording_search_b3_service import RecordingSearchClassificationService
from vigi_vision.recording_search_b4_executor import ThreadedSnapshotClassificationExecutor
from vigi_vision.recording_search_b4_models import (
    ClassificationOperationalError,
    ClassificationOperationalReason,
    ClassificationPublicationOutcome,
)
from vigi_vision.recording_search_b4_publication import ClassificationPublisher
from vigi_vision.recording_search_b4_service import ObservationClassificationService
from vigi_vision.recording_search_models import RecordingSearchArtifactError
from vigi_vision.recording_search_repository import RecordingSearchRepository


def test_successful_classification_publishes_schema3_through_the_service(tmp_path: Path) -> None:
    service, investigation_id, handle, manifest, request = successful_a2_run(tmp_path)
    image = DecodedRgbImage.from_rows(
        tuple(tuple((x % 256, y % 256, (x + y) % 256) for x in range(1280)) for y in range(720))
    )

    class Decoder:
        def decode(self, payload: bytes, width: int, height: int) -> DecodedMedia:
            return DecodedMedia(compute_jpeg_integrity_from_bytes(payload, width, height), image)

    class Predictor:
        def predict_from_rgb(
            self, image: DecodedRgbImage, point: Point, size: ImageSize
        ) -> BinaryMask:
            _ = (image, point)
            return BinaryMask.from_rows(
                tuple(
                    tuple(20 <= x < 100 and 30 <= y < 110 for x in range(size.width))
                    for y in range(size.height)
                )
            )

    preparer = RecordingSearchClassificationService(
        host=service,
        media_decoder=Decoder(),
        mask_predictor=Predictor(),
        policy=ObjectPresenceDecisionPolicy(
            minimum_mask_overlap_for_comparison=0.1,
            minimum_comparison_area=1,
            minimum_clipped_mask_pixels=1,
        ),
    )
    service.classification_service = ObservationClassificationService(
        host=service,
        preparer=preparer,
        executor=ThreadedSnapshotClassificationExecutor(preparer.classify_snapshot),
        timeout_seconds=5.0,
        now_utc=service.repository.now_utc,
        attempt_id_factory=lambda: "classification-attempt-test",
        operation_id_factory=lambda: "classification-op-test",
    )

    _ = service.classify(
        handle,
        ClassifyRecordingProbeRequest(
            investigation_id=investigation_id,
            search_run_id=manifest.search_run_id,
            probe_request_id=request.probe_request_id,
        ),
    )

    committed = service.repository.load(investigation_id, manifest.search_run_id)
    assert isinstance(committed, RecordingSearchManifestV3)
    assert len(committed.classification_operation_ids) == 1
    assert len(committed.canonical_observation_ids) == 1
    handle.release()


@pytest.mark.parametrize(
    ("visual_case", "expected_state"),
    [
        ("PRESENT", "PRESENT"),
        ("ABSENT", "ABSENT"),
        ("INDETERMINATE", "INDETERMINATE"),
        ("INDETERMINATE_UNUSABLE", "INDETERMINATE"),
    ],
)
def test_closed_visual_results_publish_and_strictly_reopen(
    tmp_path: Path, visual_case: str, expected_state: str
) -> None:
    harness = build_harness(tmp_path)
    executor = ControlledExecutor(
        lambda snapshot, _attempt: completed_future(snapshot, visual_case)
    )
    _ = install_executor(harness, executor)

    result = harness.service.classify(harness.handle, harness.command)
    committed = harness.service.repository.load(
        harness.investigation_id, harness.manifest.search_run_id
    )

    assert result.outcome is ClassificationPublicationOutcome.CREATED
    assert result.state.value == expected_state
    assert isinstance(committed, RecordingSearchManifestV3)
    assert len(committed.classification_operation_ids) == 1
    assert len(committed.canonical_observation_ids) == 1
    harness.handle.release()


def test_schema2_remains_authoritative_until_worker_result_revalidates(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    pending: Future[NonAuthoritativeClassificationResult] = Future()
    executor = ControlledExecutor(lambda _snapshot, _attempt: pending)
    _ = install_executor(harness, executor)

    with ThreadPoolExecutor(max_workers=1) as callers:
        call = callers.submit(harness.service.classify, harness.handle, harness.command)
        assert executor.submitted.wait(5)
        manifest_path = (
            harness.service.repository.run_path(
                harness.investigation_id, harness.manifest.search_run_id
            )
            / "manifest.json"
        )
        assert manifest_path.read_bytes() == harness.manifest.canonical_json().encode("utf-8")
        assert not (manifest_path.parent / "classification-operations").exists()
        pending.set_result(completed_result(executor.snapshots[0], "PRESENT"))
        assert call.result(timeout=5).state.value == "PRESENT"
    harness.handle.release()


def test_timeout_revokes_authority_and_late_result_cannot_mutate_or_block_retry(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    late = timed_out_future()

    def sequence(
        snapshot: ClassificationSnapshot, attempt: int
    ) -> Future[NonAuthoritativeClassificationResult]:
        if attempt == 1:
            return late
        return completed_future(snapshot, "PRESENT")

    executor = ControlledExecutor(sequence)
    _ = install_executor(harness, executor)
    manifest_path = (
        harness.service.repository.run_path(
            harness.investigation_id, harness.manifest.search_run_id
        )
        / "manifest.json"
    )
    before = manifest_path.read_bytes()

    with pytest.raises(ClassificationOperationalError) as caught:
        _ = harness.service.classify(harness.handle, harness.command)
    assert caught.value.reason is ClassificationOperationalReason.CLASSIFIER_TIMEOUT
    assert manifest_path.read_bytes() == before
    assert (
        harness.service.repository.load(harness.investigation_id, harness.manifest.search_run_id)
        == harness.manifest
    )

    retry = harness.service.classify(harness.handle, harness.command)
    committed_before_late = manifest_path.read_bytes()
    late.timeout_immediately = False
    late.set_result(completed_result(executor.snapshots[0], "ABSENT"))

    assert retry.state.value == "PRESENT"
    assert manifest_path.read_bytes() == committed_before_late
    committed = harness.service.repository.load(
        harness.investigation_id, harness.manifest.search_run_id
    )
    assert isinstance(committed, RecordingSearchManifestV3)
    assert len(committed.canonical_observation_ids) == 1
    harness.handle.release()


@pytest.mark.parametrize("failure", [RuntimeError("native detail"), object()])
def test_operational_or_invalid_worker_result_publishes_no_visual_evidence(
    tmp_path: Path, failure: object
) -> None:
    harness = build_harness(tmp_path)

    def result(
        _snapshot: ClassificationSnapshot, _attempt: int
    ) -> Future[NonAuthoritativeClassificationResult]:
        if isinstance(failure, BaseException):
            future: Future[NonAuthoritativeClassificationResult] = Future()
            future.set_exception(failure)
            return future
        return unsafe_future(failure)

    executor = ControlledExecutor(result)
    _ = install_executor(harness, executor)
    manifest_path = (
        harness.service.repository.run_path(
            harness.investigation_id, harness.manifest.search_run_id
        )
        / "manifest.json"
    )
    before = manifest_path.read_bytes()

    with pytest.raises(ClassificationOperationalError) as caught:
        _ = harness.service.classify(harness.handle, harness.command)

    assert caught.value.reason in {
        ClassificationOperationalReason.CLASSIFIER_EXECUTION_FAILED,
        ClassificationOperationalReason.INVALID_CLASSIFIER_OUTPUT,
    }
    assert manifest_path.read_bytes() == before
    assert not (manifest_path.parent / "classification-operations").exists()
    harness.handle.release()


def test_authoritative_mutation_while_worker_runs_discards_stale_result(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    pending: Future[NonAuthoritativeClassificationResult] = Future()
    executor = ControlledExecutor(lambda _snapshot, _attempt: pending)
    _ = install_executor(harness, executor)

    with ThreadPoolExecutor(max_workers=1) as callers:
        call = callers.submit(harness.service.classify, harness.handle, harness.command)
        assert executor.submitted.wait(5)
        changed_policy = harness.manifest.policy.model_copy(
            update={"coarse_interval_seconds": harness.manifest.policy.coarse_interval_seconds + 1}
        )
        changed = RecordingSearchManifestV2.model_validate(
            harness.manifest.model_copy(update={"policy": changed_policy}).model_dump(
                mode="python"
            ),
            strict=True,
        )
        with harness.service.a2_mutation(harness.handle):
            harness.service.repository.write_schema2_manifest(
                changed,
                harness.service.repository.run_path(
                    harness.investigation_id, harness.manifest.search_run_id
                ),
            )
        pending.set_result(completed_result(executor.snapshots[0], "ABSENT"))
        with pytest.raises(ClassificationOperationalError) as caught:
            _ = call.result(timeout=5)

    assert caught.value.reason is ClassificationOperationalReason.AUTHORITATIVE_STATE_CHANGED
    current = harness.service.repository.load(
        harness.investigation_id, harness.manifest.search_run_id
    )
    assert not isinstance(current, RecordingSearchManifestV3)
    harness.handle.release()


def test_duplicate_before_execution_reuses_without_model_capacity(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    executor = ControlledExecutor(lambda snapshot, _attempt: completed_future(snapshot, "PRESENT"))
    _ = install_executor(harness, executor)

    first = harness.service.classify(harness.handle, harness.command)
    second = harness.service.classify(harness.handle, harness.command)

    assert first.outcome is ClassificationPublicationOutcome.CREATED
    assert second.outcome is ClassificationPublicationOutcome.REUSED
    assert second.observation_id == first.observation_id
    assert executor.submissions == 1
    harness.handle.release()


def test_second_probe_acquisition_and_classification_append_preserve_schema3(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    admitted = admit_probe_frame_bytes(
        harness.service,
        harness.investigation_id,
        harness.manifest.search_run_id,
        harness.request_record.probe_request_id,
    )

    class SecondDecoder:
        def decode_targets(
            self,
            acquisition: BatchDecodeRequest,
            ordered_requested_targets: tuple[datetime, ...],
        ) -> tuple[DecodedTargetResult, ...]:
            results: list[DecodedTargetResult] = []
            for target in ordered_requested_targets:
                source_pts = int((target - acquisition.segment.start_utc).total_seconds())
                results.append(
                    DecodedTargetResult(
                        requested_time_utc=target,
                        physical_replay_origin_utc=acquisition.segment.start_utc,
                        source_pts=source_pts,
                        source_time_base=SourceTimeBase(numerator=1, denominator=1),
                        decoded_pts=source_pts,
                        replay_time_base=SourceTimeBase(numerator=1, denominator=1),
                        decoded_ordinal=len(results),
                        source_width=1280,
                        source_height=720,
                        jpeg_bytes=admitted.jpeg_bytes,
                        decode_session_id="decode-session-second",
                    )
                )
            return tuple(results)

    executor = ControlledExecutor(lambda snapshot, _attempt: completed_future(snapshot, "PRESENT"))
    _ = install_executor(harness, executor)
    first = harness.service.classify(harness.handle, harness.command)
    harness.service.operation_id_factory = lambda: "acquisition-op-second"
    harness.service.batch_decoder = SecondDecoder()
    second_request = harness.service.acquire_targets(
        harness.handle,
        (harness.request_record.requested_time_utc + timedelta(seconds=1),),
    )[0]
    second = harness.service.classify(
        harness.handle,
        ClassifyRecordingProbeRequest(
            harness.investigation_id,
            harness.manifest.search_run_id,
            second_request.probe_request_id,
        ),
    )

    committed = harness.service.repository.load(
        harness.investigation_id, harness.manifest.search_run_id
    )
    assert first.outcome is ClassificationPublicationOutcome.CREATED
    assert second.outcome is ClassificationPublicationOutcome.CREATED
    assert isinstance(committed, RecordingSearchManifestV3)
    assert len(committed.acquisition_operation_ids) == 2
    assert len(committed.classification_operation_ids) == 2
    assert len(committed.canonical_observation_ids) == 2
    harness.handle.release()


def test_duplicate_appearing_during_execution_wins_without_second_observation(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    pending: Future[NonAuthoritativeClassificationResult] = Future()
    executor = ControlledExecutor(lambda _snapshot, _attempt: pending)
    authority = install_executor(harness, executor)

    with ThreadPoolExecutor(max_workers=1) as callers:
        call = callers.submit(harness.service.classify, harness.handle, harness.command)
        assert executor.submitted.wait(5)
        prepared = completed_result(executor.snapshots[0], "PRESENT")
        publisher = ClassificationPublisher(
            harness.service,
            harness.preparer,
            harness.service.repository.now_utc,
            lambda: "classification-op-competing",
        )
        with harness.service.a2_mutation(harness.handle):
            committed = publisher.publish(
                publisher.current(harness.command),
                prepared.snapshot,
                prepared,
                harness.command,
            )
        pending.set_result(prepared)
        local = call.result(timeout=5)

    assert local.outcome is ClassificationPublicationOutcome.REUSED
    assert local.observation_id == committed.observation_id
    manifest = harness.service.repository.load(
        harness.investigation_id, harness.manifest.search_run_id
    )
    assert isinstance(manifest, RecordingSearchManifestV3)
    assert len(manifest.classification_operation_ids) == 1
    assert len(manifest.canonical_observation_ids) == 1
    authority.close()
    harness.handle.release()


def test_precommit_persistence_failure_keeps_schema2_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_harness(tmp_path)
    executor = ControlledExecutor(lambda snapshot, _attempt: completed_future(snapshot, "PRESENT"))
    _ = install_executor(harness, executor)

    def fail_manifest_write(
        _self: RecordingSearchRepository,
        _manifest: RecordingSearchManifestV3,
        _directory: Path,
    ) -> NoReturn:
        raise RecordingSearchArtifactError

    monkeypatch.setattr(
        type(harness.service.repository), "write_schema3_manifest", fail_manifest_write
    )
    with pytest.raises(ClassificationOperationalError) as caught:
        _ = harness.service.classify(harness.handle, harness.command)

    assert caught.value.reason is ClassificationOperationalReason.PERSISTENCE_FAILURE
    assert (
        harness.service.repository.load(harness.investigation_id, harness.manifest.search_run_id)
        == harness.manifest
    )
    run_path = harness.service.repository.run_path(
        harness.investigation_id, harness.manifest.search_run_id
    )
    assert not list(run_path.glob(".phase7b2-*"))
    harness.handle.release()


def test_cancelled_future_revokes_authority_without_visual_evidence(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)

    def cancelled(
        _snapshot: ClassificationSnapshot, _attempt: int
    ) -> Future[NonAuthoritativeClassificationResult]:
        future: Future[NonAuthoritativeClassificationResult] = Future()
        assert future.cancel()
        return future

    _ = install_executor(harness, ControlledExecutor(cancelled))
    manifest_path = (
        harness.service.repository.run_path(
            harness.investigation_id, harness.manifest.search_run_id
        )
        / "manifest.json"
    )
    before = manifest_path.read_bytes()

    with pytest.raises(ClassificationOperationalError) as caught:
        _ = harness.service.classify(harness.handle, harness.command)

    assert caught.value.reason is ClassificationOperationalReason.CALLER_ABANDONED
    assert manifest_path.read_bytes() == before
    assert not (manifest_path.parent / "classification-operations").exists()
    harness.handle.release()


def test_handle_close_while_worker_runs_prevents_publication(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    pending: Future[NonAuthoritativeClassificationResult] = Future()
    executor = ControlledExecutor(lambda _snapshot, _attempt: pending)
    _ = install_executor(harness, executor)
    manifest_path = (
        harness.service.repository.run_path(
            harness.investigation_id, harness.manifest.search_run_id
        )
        / "manifest.json"
    )
    before = manifest_path.read_bytes()

    with ThreadPoolExecutor(max_workers=1) as callers:
        call = callers.submit(harness.service.classify, harness.handle, harness.command)
        assert executor.submitted.wait(5)
        harness.handle.release()
        pending.set_result(completed_result(executor.snapshots[0], "ABSENT"))
        with pytest.raises(ClassificationOperationalError) as caught:
            _ = call.result(timeout=5)

    assert caught.value.reason is ClassificationOperationalReason.STALE_RUN_OWNER
    assert manifest_path.read_bytes() == before


def test_same_handle_rejects_competing_attempt_until_owner_settles(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    pending: Future[NonAuthoritativeClassificationResult] = Future()
    executor = ControlledExecutor(lambda _snapshot, _attempt: pending)
    _ = install_executor(harness, executor)

    with ThreadPoolExecutor(max_workers=1) as callers:
        owner = callers.submit(harness.service.classify, harness.handle, harness.command)
        assert executor.submitted.wait(5)
        with pytest.raises(ClassificationOperationalError) as caught:
            _ = harness.service.classify(harness.handle, harness.command)
        pending.set_result(completed_result(executor.snapshots[0], "PRESENT"))
        assert owner.result(timeout=5).state.value == "PRESENT"

    assert caught.value.reason is ClassificationOperationalReason.CLASSIFICATION_IN_PROGRESS
    assert executor.submissions == 1
    harness.handle.release()


def test_worker_receives_only_frozen_pathless_snapshot_values(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)

    def inspect_snapshot(
        snapshot: ClassificationSnapshot, _attempt: int
    ) -> Future[NonAuthoritativeClassificationResult]:
        assert all("path" not in name for name in snapshot.__dataclass_fields__)
        return completed_future(snapshot, "PRESENT")

    executor = ControlledExecutor(inspect_snapshot)
    _ = install_executor(harness, executor)
    assert harness.service.classify(harness.handle, harness.command).state.value == "PRESENT"
    harness.handle.release()


def test_postcommit_response_loss_recovers_committed_canonical_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_harness(tmp_path)
    executor = ControlledExecutor(lambda snapshot, _attempt: completed_future(snapshot, "PRESENT"))
    _ = install_executor(harness, executor)

    def commit_then_lose_response(  # noqa: PLR0913
        repository: Schema3RepositoryBoundary,
        expected: RecordingSearchManifestV2,
        policy: RecordingSearchPolicyV3,
        baseline: ConfirmedReferenceBaselineRecord,
        operation: ClassificationOperationRecord,
        observation: RecordingProbeObservationRecord,
        aliases: tuple[TargetAliasRecord, ...] = (),
    ) -> NoReturn:
        _ = publish_schema3(
            repository,
            expected,
            policy,
            baseline,
            operation,
            observation,
            aliases,
        )
        raise RecordingSearchArtifactError

    monkeypatch.setattr(publication_module, "publish_schema3_successor", commit_then_lose_response)

    result = harness.service.classify(harness.handle, harness.command)
    committed = harness.service.repository.load(
        harness.investigation_id, harness.manifest.search_run_id
    )

    assert result.outcome is ClassificationPublicationOutcome.REUSED
    assert isinstance(committed, RecordingSearchManifestV3)
    assert result.observation_id == committed.canonical_observation_ids[0]
    harness.handle.release()
