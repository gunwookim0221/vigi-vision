from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, cast

import pytest
from fastapi.testclient import TestClient
from tests.test_investigation_confirmation import build_context, build_request
from typing_extensions import override

from vigi_vision.channel_selection import Channel
from vigi_vision.recording_search_models import (
    RecordingSearchArtifactError,
    RecordingSearchManifest,
    RecordingSearchManifestCorruptError,
    RecordingSearchRequest,
    RecordingSearchState,
)
from vigi_vision.recording_search_repository import RecordingSearchRepository
from vigi_vision.recording_search_service import RecordingSearchService
from vigi_vision.reference_frame_api import create_reference_frame_app
from vigi_vision.reference_frame_models import ReferenceFrameRequest

_NOW = datetime(2026, 8, 2, 4, 5, 6, tzinfo=timezone.utc)


class Inventory:
    values: tuple[Channel, ...] = (Channel(1, "Counter", "Counter", online=True),)

    def channels(self) -> tuple[Channel, ...]:
        return self.values


class UnusedReferenceFrameService:
    def execute_or_resolve(self, request: ReferenceFrameRequest) -> NoReturn:
        _ = request
        raise AssertionError


class UnusedResources:
    def resolve_image(self, resource_id: str) -> NoReturn:
        _ = resource_id
        raise AssertionError


def request(investigation_id: str) -> RecordingSearchRequest:
    return RecordingSearchRequest(
        investigation_id=investigation_id,
        search_end_time_text="2026-07-20T13:00:00+09:00",
        source_timezone="Asia/Seoul",
    )


class _FailBeforeManifestRepository(RecordingSearchRepository):
    @override
    def _write_manifest_to_directory(
        self, manifest: RecordingSearchManifest, directory: Path
    ) -> NoReturn:
        _ = manifest, directory
        raise RecordingSearchArtifactError


class _FailBeforePublicationRepository(RecordingSearchRepository):
    @override
    def _publish_staging_directory(self, staging: Path, destination: Path) -> NoReturn:
        _ = staging, destination
        raise RecordingSearchArtifactError


def _build_started(
    tmp_path: Path, repository: RecordingSearchRepository | None = None
) -> tuple[RecordingSearchService, str, str]:
    context = build_context(tmp_path)
    _ = context.service.confirm(build_request(context.resource_id))
    selected_repository = (
        RecordingSearchRepository(tmp_path / "searches") if repository is None else repository
    )
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=selected_repository,
        channel_inventory=Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )
    started = service.start(request(context.investigation_id))
    assert started.run_handle is not None
    return service, context.investigation_id, started.manifest.search_run_id


def _manifest_path(
    repository: RecordingSearchRepository, investigation_id: str, run_id: str
) -> Path:
    return repository.run_path(investigation_id, run_id) / "manifest.json"


def _mutate_manifest(
    repository: RecordingSearchRepository,
    investigation_id: str,
    run_id: str,
    **updates: object,
) -> None:
    path = _manifest_path(repository, investigation_id, run_id)
    payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    payload.update(updates)
    _ = path.write_text(json.dumps(payload), encoding="utf-8")


def test_forged_reason_is_rejected_and_not_returned_by_get(tmp_path: Path) -> None:
    service, investigation_id, run_id = _build_started(tmp_path)
    repository = service.repository
    service.close()

    _mutate_manifest(
        repository,
        investigation_id,
        run_id,
        state="FAILED",
        started_at_utc=None,
        completed_at_utc="2026-08-02T04:05:06Z",
        failure_reason="C:/foreign/secret.txt",
        phase8_failure_reason="C:/foreign/phase8-secret.txt",
    )
    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = repository.load(investigation_id, run_id)

    app = create_reference_frame_app(
        UnusedReferenceFrameService(),
        UnusedResources(),
        recording_search_service=service,
    )
    with TestClient(app) as client:
        response = client.get(f"/api/v1/recording-searches/{investigation_id}/{run_id}")
    assert response.status_code == 500
    assert "C:/foreign/secret.txt" not in response.text
    assert "C:/foreign/phase8-secret.txt" not in response.text


@pytest.mark.parametrize(
    ("state", "started_at_utc", "completed_at_utc"),
    [
        ("RUNNING", "2026-08-02T04:05:05Z", None),
        ("FAILED", "2026-08-02T04:05:08Z", "2026-08-02T04:05:07Z"),
        ("FAILED", "2026-08-02T04:05:07Z", None),
        ("PENDING", "2026-08-02T04:05:07Z", None),
    ],
)
def test_invalid_lifecycle_timestamps_are_rejected(
    tmp_path: Path,
    state: str,
    started_at_utc: str | None,
    completed_at_utc: str | None,
) -> None:
    service, investigation_id, run_id = _build_started(tmp_path)
    repository = service.repository
    service.close()
    _mutate_manifest(
        repository,
        investigation_id,
        run_id,
        state=state,
        started_at_utc=started_at_utc,
        completed_at_utc=completed_at_utc,
        failure_reason="unexpected_error" if state == "FAILED" else None,
    )
    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = repository.load(investigation_id, run_id)


def test_equal_boundary_timestamps_round_trip(tmp_path: Path) -> None:
    service, investigation_id, run_id = _build_started(tmp_path)
    repository = service.repository
    service.close()
    _mutate_manifest(
        repository,
        investigation_id,
        run_id,
        state="FAILED",
        started_at_utc="2026-08-02T04:05:06Z",
        completed_at_utc="2026-08-02T04:05:06Z",
        failure_reason="unexpected_error",
    )
    loaded = repository.load(investigation_id, run_id)
    assert loaded.state is RecordingSearchState.FAILED


def test_future_manifest_state_and_result_fields_are_rejected_by_get(tmp_path: Path) -> None:
    service, investigation_id, run_id = _build_started(tmp_path)
    repository = service.repository
    service.close()
    _mutate_manifest(
        repository,
        investigation_id,
        run_id,
        state="FOUND",
        started_at_utc="2026-08-02T04:05:06Z",
        completed_at_utc="2026-08-02T04:05:06Z",
        failure_reason=None,
        canonical_observation_ids=["observation-forged"],
        candidate_interval={
            "last_present_observation_id": "observation-forged",
            "last_present_requested_time_utc": "2026-07-20T11:42:10Z",
            "first_absent_observation_id": "observation-forged-absent",
            "first_absent_requested_time_utc": "2026-07-20T11:42:11Z",
            "absence_support_observation_ids": [
                "observation-forged-absent",
                "observation-forged-absent-1",
                "observation-forged-absent-2",
            ],
        },
        phase8_handoff_status="READY",
    )
    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = repository.load(investigation_id, run_id)

    app = create_reference_frame_app(
        UnusedReferenceFrameService(),
        UnusedResources(),
        recording_search_service=service,
    )
    with TestClient(app) as client:
        response = client.get(f"/api/v1/recording-searches/{investigation_id}/{run_id}")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "search_manifest_corrupt"


def test_stale_staging_directory_does_not_block_a_new_start(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    _ = context.service.confirm(build_request(context.resource_id))
    repository = RecordingSearchRepository(tmp_path / "searches")
    staging = (
        repository.root / context.investigation_id / ".phase7a1-search-run-deadbeef-staging-old"
    )
    (staging / "observations").mkdir(parents=True)
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=repository,
        channel_inventory=Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )
    started = service.start(request(context.investigation_id))
    assert started.manifest.state is RecordingSearchState.RUNNING
    assert started.run_handle is not None
    started.run_handle.release()


@pytest.mark.parametrize(
    "repository_type", [_FailBeforeManifestRepository, _FailBeforePublicationRepository]
)
def test_publication_fault_leaves_no_final_run_and_allows_retry(
    tmp_path: Path,
    repository_type: type[RecordingSearchRepository],
) -> None:
    context = build_context(tmp_path)
    _ = context.service.confirm(build_request(context.resource_id))
    failing_repository = repository_type(tmp_path / "searches")
    failing_service = RecordingSearchService(
        confirmation_service=context.service,
        repository=failing_repository,
        channel_inventory=Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )
    with pytest.raises(RecordingSearchArtifactError):
        _ = failing_service.start(request(context.investigation_id))
    failing_service.close()
    investigation_directory = failing_repository.root / context.investigation_id
    if investigation_directory.exists():
        assert tuple(investigation_directory.iterdir()) == ()

    retry_repository = RecordingSearchRepository(tmp_path / "searches")
    retry_service = RecordingSearchService(
        confirmation_service=context.service,
        repository=retry_repository,
        channel_inventory=Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )
    retry = retry_service.start(request(context.investigation_id))
    assert retry.manifest.state is RecordingSearchState.RUNNING
    assert retry.run_handle is not None
    retry.run_handle.release()


def test_existing_final_destination_is_never_overwritten(tmp_path: Path) -> None:
    service, investigation_id, run_id = _build_started(tmp_path)
    repository = service.repository
    service.close()
    manifest = repository.load(investigation_id, run_id)
    with pytest.raises(RecordingSearchArtifactError):
        _ = repository.create(manifest)
    assert repository.load(investigation_id, run_id) == manifest
