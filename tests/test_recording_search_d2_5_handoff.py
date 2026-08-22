from __future__ import annotations

import json
import multiprocessing
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING, Protocol, cast

import pytest
from tests import test_recording_search_d2_2_found as found_fixture
from tests import test_recording_search_d2_2_terminal as terminal_fixture
from tests import test_recording_search_d2_4_reopen as reopen_fixture

from vigi_vision import recording_search_service
from vigi_vision.recording_search_d2_5_handoff import (
    Phase8HandoffArtifactError,
    Phase8HandoffConflictError,
    Phase8HandoffCorruptError,
    Phase8HandoffNotApplicableError,
    Phase8HandoffOutcome,
    Phase8HandoffRequestV1,
    Phase8HandoffResult,
    build_phase8_handoff_request,
    canonical_phase8_handoff_json,
    create_or_reuse_phase8_request,
    phase8_handoff_status,
)
from vigi_vision.recording_search_d2_publication_models import RecordingSearchManifestV4
from vigi_vision.recording_search_d2_terminal import TerminalInputSnapshot, interpret_terminal
from vigi_vision.recording_search_d2_terminal_models import FoundResult, NotFoundResult
from vigi_vision.recording_search_models import (
    Phase8HandoffStatus,
    RecordingSearchManifestCorruptError,
)

UTC = timezone.utc

if TYPE_CHECKING:
    from tests.recording_search_b4_support import Harness

    from vigi_vision.channel_selection import Channel
    from vigi_vision.investigation_confirmation_models import ConfirmedInvestigationInput
    from vigi_vision.recording_search_repository import RecordingSearchRepository


class _Waitable(Protocol):
    def wait(self, timeout: float | None = None) -> bool: ...


class _MessageQueue(Protocol):
    def put(self, value: object) -> None: ...


class _UnusedConfirmationLoader:
    def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
        _ = investigation_id
        raise AssertionError


class _UnusedChannelInventory:
    def channels(self) -> tuple[Channel, ...]:
        return ()


class _HandoffProcessRepository:
    """Minimal process-safe repository adapter for the public service test."""

    def __init__(self, root: Path, run_path: Path, manifest: RecordingSearchManifestV4) -> None:
        self.root: Path = root
        self._run_path: Path = run_path
        self._manifest: RecordingSearchManifestV4 = manifest

    def run_path(self, _investigation_id: str, _search_run_id: str) -> Path:
        return self._run_path

    def lock_path(self, _investigation_id: str) -> Path:
        return self.root / ".locks" / "phase8-service-process.lock"

    def load(
        self, _investigation_id: str, _search_run_id: str, *, include_terminal: bool = False
    ) -> RecordingSearchManifestV4:
        _ = include_terminal
        return self._manifest

    def create_or_reuse_phase8_request(
        self, request: Phase8HandoffRequestV1
    ) -> Phase8HandoffResult:
        return create_or_reuse_phase8_request(self.root, self._run_path, request)


def _public_handoff_process_worker(  # noqa: PLR0913
    root_text: str,
    run_path_text: str,
    manifest_payload: dict[str, object],
    result: FoundResult,
    gate: _Waitable,
    ready_queue: _MessageQueue,
    result_queue: _MessageQueue,
) -> None:
    manifest = RecordingSearchManifestV4.model_validate(manifest_payload, strict=True)
    root = Path(root_text)
    run_path = Path(run_path_text)
    repository = _HandoffProcessRepository(root, run_path, manifest)

    def reopen_found(
        _root: Path, _run_path: Path, _manifest: RecordingSearchManifestV4
    ) -> FoundResult:
        return result

    setattr(recording_search_service, "reopen_terminal_result", reopen_found)  # noqa: B010
    service = recording_search_service.RecordingSearchService(
        confirmation_service=_UnusedConfirmationLoader(),
        repository=cast("RecordingSearchRepository", cast("object", repository)),
        channel_inventory=_UnusedChannelInventory(),
        artifact_root=root,
        now_utc=lambda: datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
        lock_timeout_seconds=10.0,
    )
    ready_queue.put("ready")
    _ = gate.wait(30)
    try:
        handoff = service.create_phase8_handoff(manifest.investigation_id, manifest.search_run_id)
        result_queue.put(("ok", handoff.outcome.value))
    except Exception as error:  # noqa: BLE001
        result_queue.put(("error", type(error).__name__))
    finally:
        service.close()


def _published_harness_for_test(
    tmp_path: Path,
) -> tuple[Harness, RecordingSearchManifestV4]:
    factory = cast(
        "Callable[[Path], tuple[Harness, RecordingSearchManifestV4]]",
        reopen_fixture.__dict__["_published_harness"],
    )
    return factory(tmp_path)


def _found_result() -> FoundResult:
    context_factory = cast(
        "Callable[[], TerminalInputSnapshot]", found_fixture.__dict__["_found_context"]
    )
    outcome = interpret_terminal(context_factory())
    assert isinstance(outcome, FoundResult)
    return outcome


def _aligned_found_result(harness: Harness, manifest: RecordingSearchManifestV4) -> FoundResult:
    """Adapt the pure fixture result to one real published service run."""
    start = manifest.policy.search_start_utc
    return replace(
        _found_result(),
        investigation_id=harness.investigation_id,
        search_run_id=manifest.search_run_id,
        phase6_confirmation_id=harness.handle.phase6_confirmation_id,
        baseline_observation_id=manifest.as_schema3().baseline_observation_id,
        lower_bound_requested_time_utc=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        upper_bound_requested_time_utc=(start + timedelta(seconds=4)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    )


def test_public_service_handoff_reuses_after_clock_advance_restart_and_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, manifest = _published_harness_for_test(tmp_path)
    harness.service.close()
    result = _aligned_found_result(harness, manifest)

    def reopen_found(
        _root: Path, _run_path: Path, _manifest: RecordingSearchManifestV4
    ) -> FoundResult:
        return result

    monkeypatch.setattr(recording_search_service, "reopen_terminal_result", reopen_found)
    service = replace(harness.service, lock_timeout_seconds=10.0)
    barrier = Barrier(2)

    def submit(_unused: int) -> Phase8HandoffResult:
        _ = barrier.wait()
        return service.create_phase8_handoff(harness.investigation_id, manifest.search_run_id)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = tuple(executor.map(submit, (1, 2)))
        assert {first.outcome, second.outcome} == {
            Phase8HandoffOutcome.CREATED,
            Phase8HandoffOutcome.REUSED,
        }
        created = first if first.outcome is Phase8HandoffOutcome.CREATED else second
        service.now_utc = lambda: datetime(2026, 8, 2, 4, 10, 6, tzinfo=UTC)
        delayed = service.create_phase8_handoff(harness.investigation_id, manifest.search_run_id)
        assert delayed.outcome is Phase8HandoffOutcome.REUSED
        assert delayed.request == created.request
        service.close()
        restarted = replace(service)
        try:
            after_restart = restarted.create_phase8_handoff(
                harness.investigation_id, manifest.search_run_id
            )
            assert after_restart.outcome is Phase8HandoffOutcome.REUSED
            assert after_restart.request == created.request
        finally:
            restarted.close()
    finally:
        service.close()


def test_public_service_handoff_reuses_across_processes(tmp_path: Path) -> None:
    harness, manifest = _published_harness_for_test(tmp_path)
    result = _aligned_found_result(harness, manifest)
    root = harness.service.repository.root
    run_path = harness.service.repository.run_path(harness.investigation_id, manifest.search_run_id)
    harness.service.close()
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    ready_queue = context.Queue()
    result_queue = context.Queue()
    manifest_payload = cast("dict[str, object]", manifest.model_dump(mode="python"))
    processes = [
        context.Process(
            target=_public_handoff_process_worker,
            args=(
                str(root),
                str(run_path),
                manifest_payload,
                result,
                gate,
                ready_queue,
                result_queue,
            ),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        assert ready_queue.get(timeout=30) == "ready"
        assert ready_queue.get(timeout=30) == "ready"
        gate.set()
        outcomes = {cast("tuple[str, str]", result_queue.get(timeout=30)) for _ in processes}
        assert outcomes == {("ok", "created"), ("ok", "reused")}
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
    finally:
        gate.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)


def test_public_service_handoff_rejects_non_found_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, manifest = _published_harness_for_test(tmp_path)
    harness.service.close()
    context_factory = cast(
        "Callable[[], TerminalInputSnapshot]", terminal_fixture.__dict__["_context"]
    )
    not_found = interpret_terminal(context_factory())
    assert isinstance(not_found, NotFoundResult)

    def reopen_not_found(
        _root: Path, _run_path: Path, _manifest: RecordingSearchManifestV4
    ) -> NotFoundResult:
        return not_found

    monkeypatch.setattr(
        recording_search_service,
        "reopen_terminal_result",
        reopen_not_found,
    )
    service = replace(harness.service)
    try:
        with pytest.raises(Phase8HandoffNotApplicableError):
            _ = service.create_phase8_handoff(harness.investigation_id, manifest.search_run_id)
    finally:
        service.close()


def test_public_service_handoff_conflict_and_corrupt_request_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, manifest = _published_harness_for_test(tmp_path)
    harness.service.close()
    result = _aligned_found_result(harness, manifest)
    current = [result]

    def reopen_current(
        _root: Path, _run_path: Path, _manifest: RecordingSearchManifestV4
    ) -> FoundResult:
        return current[0]

    monkeypatch.setattr(recording_search_service, "reopen_terminal_result", reopen_current)
    service = replace(harness.service)
    request_path = (
        service.repository.run_path(harness.investigation_id, manifest.search_run_id)
        / "phase8-request.json"
    )
    try:
        first = service.create_phase8_handoff(harness.investigation_id, manifest.search_run_id)
        before = request_path.read_bytes()
        current[0] = replace(
            result,
            upper_bound_requested_time_utc="2026-07-20T03:34:23Z",
        )
        with pytest.raises(Phase8HandoffConflictError):
            _ = service.create_phase8_handoff(harness.investigation_id, manifest.search_run_id)
        assert request_path.read_bytes() == before
        _ = request_path.write_text("{}", encoding="utf-8")
        with pytest.raises(Phase8HandoffCorruptError):
            _ = service.create_phase8_handoff(harness.investigation_id, manifest.search_run_id)
        assert first.request.handoff_request_id
    finally:
        service.close()


def test_public_service_handoff_rejects_terminal_residue_without_mutation(
    tmp_path: Path,
) -> None:
    harness, manifest = _published_harness_for_test(tmp_path)
    run_path = harness.service.repository.run_path(harness.investigation_id, manifest.search_run_id)
    residue = run_path / ".phase7a2-admission-phase8-residue"
    residue.mkdir()
    marker = residue / "operation.json"
    _ = marker.write_bytes(b"residue")
    before = marker.read_bytes()
    manifest_path = run_path / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    harness.service.close()
    service = replace(harness.service)
    try:
        with pytest.raises(RecordingSearchManifestCorruptError):
            _ = service.create_phase8_handoff(harness.investigation_id, manifest.search_run_id)
        assert residue.is_dir()
        assert marker.read_bytes() == before
        assert manifest_path.read_bytes() == manifest_bytes
    finally:
        service.close()


def test_found_handoff_uses_requested_interval_and_clips_nominal_window() -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )

    assert request.lower_bound_requested_time_utc == datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
    assert request.upper_bound_requested_time_utc == datetime(2026, 7, 20, 3, 0, 4, tzinfo=UTC)
    assert request.review_anchor_utc == request.upper_bound_requested_time_utc
    assert request.nominal_review_start_utc == request.lower_bound_requested_time_utc
    assert request.nominal_review_end_utc == datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC)
    assert request.handoff_request_id.startswith("phase8-handoff-v1-")


def test_handoff_identity_is_stable_when_creation_time_changes() -> None:
    result = _found_result()
    first = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    second = first.model_copy(
        update={"created_at_utc": first.created_at_utc + timedelta(seconds=1)}
    )

    assert first.handoff_request_id == second.handoff_request_id
    assert canonical_phase8_handoff_json(first) == canonical_phase8_handoff_json(second)


def test_non_found_result_cannot_create_handoff() -> None:
    result = replace(_found_result(), result_kind="NOT_FOUND")

    with pytest.raises(ValueError, match=r"^$"):
        _ = build_phase8_handoff_request(
            result,
            channel_id=1,
            source_timezone="Asia/Seoul",
            search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
            search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
            created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
        )


def test_request_model_forbids_unknown_fields_and_paths() -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    payload = request.model_dump(mode="python")
    payload["path"] = str(Path("/foreign/secret"))

    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        _ = Phase8HandoffRequestV1.model_validate(payload, strict=True)


def test_request_repository_reuses_exact_duplicate_without_rewrite(tmp_path: Path) -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    run_path = tmp_path / result.investigation_id / result.search_run_id
    run_path.mkdir(parents=True)

    first = create_or_reuse_phase8_request(tmp_path, run_path, request)
    before = (run_path / "phase8-request.json").read_bytes()
    second = create_or_reuse_phase8_request(tmp_path, run_path, request)
    delayed_retry = request.model_copy(
        update={"created_at_utc": request.created_at_utc + timedelta(minutes=5)}
    )
    third = create_or_reuse_phase8_request(tmp_path, run_path, delayed_retry)

    assert first.request == second.request == request
    assert third.request == request
    assert first.outcome.value == "created"
    assert second.outcome.value == "reused"
    assert third.outcome.value == "reused"
    assert (run_path / "phase8-request.json").read_bytes() == before


def test_request_repository_rejects_conflict_without_rewrite(tmp_path: Path) -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    run_path = tmp_path / result.investigation_id / result.search_run_id
    run_path.mkdir(parents=True)
    _ = create_or_reuse_phase8_request(tmp_path, run_path, request)
    before = (run_path / "phase8-request.json").read_bytes()
    conflict = request.model_copy(update={"upper_support_observation_ids": ("observation-other",)})

    with pytest.raises(Phase8HandoffConflictError):
        _ = create_or_reuse_phase8_request(tmp_path, run_path, conflict)

    assert (run_path / "phase8-request.json").read_bytes() == before


def test_handoff_status_and_persisted_schema_are_strict(tmp_path: Path) -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    run_path = tmp_path / result.investigation_id / result.search_run_id
    run_path.mkdir(parents=True)

    assert phase8_handoff_status(tmp_path, run_path, request.terminal_result_id) is (
        Phase8HandoffStatus.PENDING
    )
    _ = create_or_reuse_phase8_request(tmp_path, run_path, request)
    payload = cast(
        "dict[str, object]",
        json.loads((run_path / "phase8-request.json").read_text(encoding="utf-8")),
    )
    assert payload["schema_version"] == 1
    with pytest.raises(Phase8HandoffCorruptError):
        _ = phase8_handoff_status(
            tmp_path,
            run_path,
            request.terminal_result_id,
            expected_handoff_request_id="phase8-handoff-v1-" + "0" * 64,
        )
    assert phase8_handoff_status(tmp_path, run_path, request.terminal_result_id) is (
        Phase8HandoffStatus.READY
    )


def test_corrupt_existing_request_fails_closed_without_rewrite(tmp_path: Path) -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    run_path = tmp_path / result.investigation_id / result.search_run_id
    run_path.mkdir(parents=True)
    destination = run_path / "phase8-request.json"
    _ = destination.write_text("{}", encoding="utf-8")
    before = destination.read_bytes()

    with pytest.raises(Phase8HandoffCorruptError):
        _ = create_or_reuse_phase8_request(tmp_path, run_path, request)

    assert destination.read_bytes() == before


def test_storage_failure_cleans_owned_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    run_path = tmp_path / result.investigation_id / result.search_run_id
    run_path.mkdir(parents=True)

    def fail_rename(_source: Path, _destination: Path) -> Path:
        error = OSError("simulated")
        raise error

    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(Phase8HandoffArtifactError):
        _ = create_or_reuse_phase8_request(tmp_path, run_path, request)

    assert not list(run_path.glob("*.phase8-request-*.tmp"))
    assert not (run_path / "phase8-request.json").exists()
