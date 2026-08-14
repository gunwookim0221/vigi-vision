from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Lock

import pytest
from tests.recording_search_b4_support import (
    ControlledExecutor,
    Harness,
    build_harness,
    completed_future,
    completed_result,
    install_executor,
)

from vigi_vision.investigation_confirmation_models import ConfirmedInvestigationInput
from vigi_vision.recording_search_a2_models import RecordingSearchManifestV2
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b3_models import (
    ClassificationSnapshot,
    NonAuthoritativeClassificationResult,
)
from vigi_vision.recording_search_b4_executor import ThreadedSnapshotClassificationExecutor
from vigi_vision.recording_search_b4_models import (
    ClassificationOperationalError,
    ClassificationOperationalReason,
)
from vigi_vision.recording_search_b4_service import ObservationClassificationService
from vigi_vision.recording_search_models import (
    RecordingSearchManifestCorruptError,
    RecordingSearchState,
    RecordingSearchTransitionError,
)
from vigi_vision.recording_search_repository import RecordingSearchRepository
from vigi_vision.recording_search_service import ConfirmationLoader


def _classify_once(tmp_path: Path) -> Harness:
    harness = build_harness(tmp_path)
    executor = ControlledExecutor(lambda snapshot, _attempt: completed_future(snapshot, "PRESENT"))
    _ = install_executor(harness, executor)
    _ = harness.service.classify(harness.handle, harness.command)
    return harness


def _run_children(run_path: Path) -> dict[str, bytes]:
    return {
        path.relative_to(run_path).as_posix(): path.read_bytes()
        for path in run_path.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }


def test_schema3_status_and_interruption_preserve_committed_evidence(tmp_path: Path) -> None:
    harness = _classify_once(tmp_path)
    run_path = harness.service.repository.run_path(
        harness.investigation_id, harness.manifest.search_run_id
    )
    committed = harness.service.repository.load(
        harness.investigation_id, harness.manifest.search_run_id
    )
    assert isinstance(committed, RecordingSearchManifestV3)
    children_before = _run_children(run_path)

    active_status = harness.service.status(harness.investigation_id, harness.manifest.search_run_id)
    assert isinstance(active_status, RecordingSearchManifestV2)
    assert active_status.state is RecordingSearchState.RUNNING
    assert "canonical_observation_ids" not in type(active_status).model_fields

    harness.handle.release()
    interrupted_status = harness.service.status(
        harness.investigation_id, harness.manifest.search_run_id
    )
    interrupted = harness.service.repository.load(
        harness.investigation_id, harness.manifest.search_run_id
    )

    assert isinstance(interrupted_status, RecordingSearchManifestV2)
    assert interrupted_status.state is RecordingSearchState.INTERRUPTED
    assert isinstance(interrupted, RecordingSearchManifestV3)
    assert interrupted.state == "INTERRUPTED"
    assert interrupted.completed_at_utc is not None
    assert interrupted.failure_reason == "process_lock_released"
    assert interrupted.policy == committed.policy
    assert interrupted.acquisition_operation_ids == committed.acquisition_operation_ids
    assert interrupted.probe_request_ids == committed.probe_request_ids
    assert interrupted.canonical_frame_ids == committed.canonical_frame_ids
    assert interrupted.baseline_observation_id == committed.baseline_observation_id
    assert interrupted.classification_operation_ids == committed.classification_operation_ids
    assert interrupted.canonical_observation_ids == committed.canonical_observation_ids
    assert interrupted.target_alias_ids == committed.target_alias_ids
    assert _run_children(run_path) == children_before
    with pytest.raises(RecordingSearchTransitionError):
        _ = harness.service.repository.transition(
            harness.investigation_id,
            harness.manifest.search_run_id,
            RecordingSearchState.FAILED,
        )


@pytest.mark.parametrize("corruption", ["missing", "same_size", "changed_size"])
def test_schema3_reopen_revalidates_phase6_jpeg(tmp_path: Path, corruption: str) -> None:
    harness = _classify_once(tmp_path)
    confirmed = harness.service.confirmation_service.load_confirmed(harness.investigation_id)
    jpeg_path = confirmed.jpeg_path
    original = jpeg_path.read_bytes()
    manifest_path = (
        harness.service.repository.run_path(
            harness.investigation_id, harness.manifest.search_run_id
        )
        / "manifest.json"
    )
    manifest_before = manifest_path.read_bytes()
    try:
        if corruption == "missing":
            jpeg_path.unlink()
        elif corruption == "same_size":
            _ = jpeg_path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        else:
            _ = jpeg_path.write_bytes(original[:-1])
        with pytest.raises(RecordingSearchManifestCorruptError):
            _ = harness.service.repository.load(
                harness.investigation_id, harness.manifest.search_run_id
            )
        assert manifest_path.read_bytes() == manifest_before
    finally:
        _ = jpeg_path.write_bytes(original)
    assert isinstance(
        harness.service.repository.load(harness.investigation_id, harness.manifest.search_run_id),
        RecordingSearchManifestV3,
    )
    harness.handle.release()


@dataclass(frozen=True, slots=True)
class _DimensionMismatchLoader:
    delegate: ConfirmationLoader

    def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
        loaded = self.delegate.load_confirmed(investigation_id)
        return replace(loaded, source_width=loaded.source_width + 1)


def test_schema3_reopen_rejects_phase6_dimension_mismatch(tmp_path: Path) -> None:
    harness = _classify_once(tmp_path)
    repository = RecordingSearchRepository(
        harness.service.repository.root,
        harness.service.repository.now_utc,
        confirmation_loader=_DimensionMismatchLoader(harness.service.confirmation_service),
    )
    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = repository.load(harness.investigation_id, harness.manifest.search_run_id)
    harness.handle.release()


def _snapshot(tmp_path: Path) -> tuple[ClassificationSnapshot, Harness]:
    harness = build_harness(tmp_path)
    with harness.service.a2_mutation(harness.handle):
        captured = harness.preparer.capture_locked(harness.handle, harness.command)
    assert isinstance(captured, ClassificationSnapshot)
    return captured, harness


def test_production_executor_bounds_running_and_queued_work(tmp_path: Path) -> None:
    snapshot, harness = _snapshot(tmp_path)
    release = Event()
    all_started = Event()
    guard = Lock()
    started = 0

    def worker(
        value: ClassificationSnapshot,
    ) -> NonAuthoritativeClassificationResult:
        nonlocal started
        with guard:
            started += 1
            if started == 2:
                all_started.set()
        _ = release.wait()
        return completed_result(value, "PRESENT")

    executor = ThreadedSnapshotClassificationExecutor(worker, max_workers=2, max_queue_size=2)
    try:
        running = (executor.submit(snapshot), executor.submit(snapshot))
        assert all_started.wait(5)
        queued = (executor.submit(snapshot), executor.submit(snapshot))
        assert executor.admitted_work == 4
        with pytest.raises(RuntimeError):
            _ = executor.submit(snapshot)
        assert queued[0].cancel()
        assert queued[1].cancel()
        assert started == 2
        assert executor.admitted_work == 2
        release.set()
        assert all(future.result(timeout=5).snapshot == snapshot for future in running)
        assert executor.admitted_work == 0
    finally:
        release.set()
        try:
            _ = executor.wait_until_idle(5)
        finally:
            executor.close()
            harness.handle.release()


def test_repeated_timeouts_cannot_grow_production_executor_backlog(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    release = Event()
    all_started = Event()
    guard = Lock()
    started = 0

    def worker(
        snapshot: ClassificationSnapshot,
    ) -> NonAuthoritativeClassificationResult:
        nonlocal started
        with guard:
            started += 1
            if started == 2:
                all_started.set()
        _ = release.wait()
        return completed_result(snapshot, "PRESENT")

    executor = ThreadedSnapshotClassificationExecutor(worker, max_workers=2, max_queue_size=1)
    harness.service.classification_service = ObservationClassificationService(
        host=harness.service,
        preparer=harness.preparer,
        executor=executor,
        timeout_seconds=0.001,
        now_utc=harness.service.repository.now_utc,
        attempt_id_factory=iter(
            tuple(f"classification-attempt-{index}" for index in range(20))
        ).__next__,
        operation_id_factory=iter(
            tuple(f"classification-op-{index}" for index in range(20))
        ).__next__,
    )
    manifest_path = (
        harness.service.repository.run_path(
            harness.investigation_id, harness.manifest.search_run_id
        )
        / "manifest.json"
    )
    before = manifest_path.read_bytes()

    try:
        for _ in range(10):
            with pytest.raises(ClassificationOperationalError) as caught:
                _ = harness.service.classify(harness.handle, harness.command)
            assert caught.value.reason is ClassificationOperationalReason.CLASSIFIER_TIMEOUT

        assert all_started.wait(5)
        assert started == 2
        assert executor.admitted_work == 2
        assert executor.maximum_admitted_work == 3
        assert manifest_path.read_bytes() == before
        release.set()
        assert executor.wait_until_idle(5)
        assert executor.admitted_work == 0
        assert manifest_path.read_bytes() == before
    finally:
        release.set()
        try:
            _ = executor.wait_until_idle(5)
        finally:
            executor.close()
            harness.handle.release()


def test_executor_capacity_exhaustion_is_safe_and_does_not_mutate(tmp_path: Path) -> None:
    snapshot, harness = _snapshot(tmp_path)
    release = Event()
    started = Event()

    def worker(
        value: ClassificationSnapshot,
    ) -> NonAuthoritativeClassificationResult:
        started.set()
        _ = release.wait()
        return completed_result(value, "PRESENT")

    executor = ThreadedSnapshotClassificationExecutor(worker, max_workers=1, max_queue_size=1)
    try:
        running = executor.submit(snapshot)
        assert started.wait(5)
        queued = executor.submit(snapshot)
        harness.service.classification_service = ObservationClassificationService(
            host=harness.service,
            preparer=harness.preparer,
            executor=executor,
            timeout_seconds=1,
            now_utc=harness.service.repository.now_utc,
            attempt_id_factory=lambda: "classification-attempt-busy",
            operation_id_factory=lambda: "classification-op-busy",
        )
        manifest_path = (
            harness.service.repository.run_path(
                harness.investigation_id, harness.manifest.search_run_id
            )
            / "manifest.json"
        )
        before = manifest_path.read_bytes()

        with pytest.raises(ClassificationOperationalError) as caught:
            _ = harness.service.classify(harness.handle, harness.command)

        assert caught.value.reason is ClassificationOperationalReason.CLASSIFICATION_IN_PROGRESS
        assert manifest_path.read_bytes() == before
        assert queued.cancel()
        release.set()
        assert running.result(timeout=5).snapshot == snapshot
        assert executor.wait_until_idle(5)
        assert executor.admitted_work == 0
    finally:
        release.set()
        try:
            _ = executor.wait_until_idle(5)
        finally:
            executor.close()
            harness.handle.release()


def test_production_executor_releases_permit_after_worker_exception(tmp_path: Path) -> None:
    snapshot, harness = _snapshot(tmp_path)
    calls = 0

    def worker(
        value: ClassificationSnapshot,
    ) -> NonAuthoritativeClassificationResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError
        return completed_result(value, "PRESENT")

    executor = ThreadedSnapshotClassificationExecutor(worker, max_workers=1, max_queue_size=1)
    try:
        failed = executor.submit(snapshot)
        with pytest.raises(RuntimeError):
            _ = failed.result(timeout=5)
        assert executor.wait_until_idle(5)
        assert executor.admitted_work == 0

        recovered = executor.submit(snapshot)
        assert recovered.result(timeout=5).snapshot == snapshot
        assert executor.wait_until_idle(5)
        assert executor.admitted_work == 0
    finally:
        executor.close()
        harness.handle.release()


def test_running_cancellation_attempt_and_completion_release_permit_once(
    tmp_path: Path,
) -> None:
    snapshot, harness = _snapshot(tmp_path)
    started = Event()
    allow_completion = Event()

    def worker(
        value: ClassificationSnapshot,
    ) -> NonAuthoritativeClassificationResult:
        started.set()
        _ = allow_completion.wait()
        return completed_result(value, "PRESENT")

    executor = ThreadedSnapshotClassificationExecutor(worker, max_workers=1, max_queue_size=1)
    try:
        future = executor.submit(snapshot)
        assert started.wait(5)
        assert not future.cancel()
        assert executor.admitted_work == 1
        allow_completion.set()
        assert future.result(timeout=5).snapshot == snapshot
        assert executor.wait_until_idle(5)
        assert executor.admitted_work == 0
    finally:
        allow_completion.set()
        try:
            _ = executor.wait_until_idle(5)
        finally:
            executor.close()
            harness.handle.release()
