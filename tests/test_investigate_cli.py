from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, TypeAlias, final

import pytest
from typer.testing import CliRunner

from vigi_vision import cli
from vigi_vision.config import CaptureSettings
from vigi_vision.investigation import (
    AnchorTime,
    CameraRole,
    InvestigationItem,
    InvestigationPlan,
    MissingRequiredCameraRoleError,
)
from vigi_vision.investigation_artifacts import InvestigationResult
from vigi_vision.investigation_collection import (
    CollectionContractError,
    CollectionItemResult,
    CollectionResult,
    CollectionStatus,
)
from vigi_vision.investigation_manifest import InvestigationManifest
from vigi_vision.investigation_progress import InvestigationStage
from vigi_vision.investigation_service import InvestigationRequest
from vigi_vision.investigation_snapshot import AnchorSnapshotError
from vigi_vision.recording import RecordingWindow
from vigi_vision.replay import ReplayClip

ServiceOutcome: TypeAlias = InvestigationResult | Exception
_UNSAFE_ERROR_DETAIL: Final = "rtsp://operator:test-password@nvr.example.invalid/replay"


@final
class StubService:
    outcome: ServiceOutcome
    requests: list[InvestigationRequest]
    progress: Callable[[InvestigationStage], None] | None

    def __init__(self, outcome: ServiceOutcome) -> None:
        self.outcome = outcome
        self.requests = []
        self.progress = None

    def execute(self, request: InvestigationRequest) -> InvestigationResult:
        self.requests.append(request)
        if self.progress is not None:
            self.progress(InvestigationStage.PLANNING)
        match self.outcome:  # noqa: RUF100  # noqa: MATCH_OK — test outcome is closed.
            case InvestigationResult() as result:
                if self.progress is not None:
                    self.progress(InvestigationStage.COLLECTION)
                    self.progress(InvestigationStage.ARTIFACT_PACKAGE)
                    self.progress(InvestigationStage.MANIFEST_WRITING)
                return result
            case Exception() as error:
                if self.progress is not None:
                    stage = _stub_failure_stage(error)
                    if stage is not None:
                        self.progress(stage)
                raise error


def _stub_failure_stage(error: Exception) -> InvestigationStage | None:
    match error:  # noqa: RUF100  # noqa: MATCH_OK — test failures cover only selected stages.
        case CollectionContractError():
            return InvestigationStage.COLLECTION
        case FileExistsError():
            return InvestigationStage.ARTIFACT_PACKAGE
        case AnchorSnapshotError():
            return InvestigationStage.ANCHOR_SNAPSHOT
        case _:
            return None


def _settings(_: Path) -> CaptureSettings:
    return CaptureSettings.model_validate(
        {
            "VIGI_SOURCE": "nvr",
            "VIGI_HOST": "nvr.example.invalid",
            "VIGI_USERNAME": "operator",
            "VIGI_PASSWORD": "test-password",
        }
    )


def _result(statuses: tuple[CollectionStatus, ...]) -> InvestigationResult:
    anchor = AnchorTime(datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc), "Asia/Seoul")
    role = CameraRole("counter")
    items = tuple(
        InvestigationItem(
            f"item-{index}",
            index,
            role,
            "counter",
            RecordingWindow(
                index,
                anchor.anchor_utc - timedelta(seconds=60),
                anchor.anchor_utc + timedelta(seconds=60),
            ),
        )
        for index in range(1, len(statuses) + 1)
    )
    plan = InvestigationPlan("restaurant-checkout", anchor, items)
    collection_items = tuple(
        _collection_item(item, status) for item, status in zip(items, statuses, strict=True)
    )
    collection = CollectionResult(plan, collection_items)
    manifest = InvestigationManifest(
        "restaurant-checkout-20260720T033418Z",
        plan.scenario_id,
        anchor.anchor_utc,
        anchor.source_timezone,
        (),
    )
    return InvestigationResult(plan, collection, manifest, Path("artifacts/investigations/example"))


def _collection_item(item: InvestigationItem, status: CollectionStatus) -> CollectionItemResult:
    match status:  # noqa: RUF100  # noqa: MATCH_OK — CollectionStatus is closed.
        case CollectionStatus.SUCCESS:
            window = item.recording_window
            return CollectionItemResult.success(
                item,
                ReplayClip(
                    item.channel_id,
                    window.start_utc,
                    window.end_utc,
                    "rtsp://operator:test-password@nvr.example.invalid/replay",
                    Path(f"temporary-{item.channel_id}.mp4"),
                    window.duration_seconds,
                ),
            )
        case (
            CollectionStatus.RECORDING_UNAVAILABLE
            | CollectionStatus.AUTHENTICATION_FAILED
            | CollectionStatus.EXTRACTION_FAILED
            | CollectionStatus.TIMEOUT
            | CollectionStatus.UNEXPECTED_ERROR
        ):
            return CollectionItemResult.failure(item, status, "safe collection failure")


def _mock_service(monkeypatch: pytest.MonkeyPatch, service: StubService) -> None:
    def _build_service(
        _: CaptureSettings, progress: Callable[[InvestigationStage], None] | None = None
    ) -> StubService:
        service.progress = progress
        return service

    monkeypatch.setattr("vigi_vision.investigate_cli.load_capture_settings", _settings)
    monkeypatch.setattr("vigi_vision.investigate_cli._build_service", _build_service)


def test_cli_investigate_builds_the_current_kst_request_and_prints_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    service = StubService(_result((CollectionStatus.SUCCESS, CollectionStatus.SUCCESS)))

    # When
    _mock_service(monkeypatch, service)
    result = CliRunner().invoke(
        cli.app,
        ["investigate", "--scenario", "restaurant-checkout", "--time", "2026-07-20 12:34:18"],
    )

    # Then
    assert result.exit_code == 0
    assert "Planning investigation..." in result.stdout
    assert "Collecting recordings..." in result.stdout
    assert "Building artifact package..." in result.stdout
    assert "Writing manifest..." in result.stdout
    assert len(service.requests) == 1
    request = service.requests[0]
    assert request.anchor_time == AnchorTime(
        datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc), "Asia/Seoul"
    )
    assert request.scenario.scenario_id == "restaurant-checkout"
    assert tuple((item.channel_id, item.role.value) for item in request.camera_assignments) == (
        (1, "counter"),
        (2, "entrance"),
        (3, "dining"),
    )
    assert "Scenario\n--------\nrestaurant-checkout" in result.stdout
    assert "Anchor Time (KST)\n-----------------\n2026-07-20 12:34:18" in result.stdout
    assert "Items Collected\n---------------\n2" in result.stdout
    assert "Items Failed\n------------\n0" in result.stdout
    assert "artifacts/investigations/example" in result.stdout
    assert "artifacts/investigations/example/manifest.json" in result.stdout


def test_cli_investigate_reports_planner_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    service = StubService(MissingRequiredCameraRoleError(CameraRole("counter")))

    # When
    _mock_service(monkeypatch, service)
    result = CliRunner().invoke(
        cli.app,
        ["investigate", "--scenario", "restaurant-checkout", "--time", "2026-07-20 12:34:18"],
    )

    # Then
    assert result.exit_code == 1
    assert "stage=planning" in result.stdout
    assert "exception=MissingRequiredCameraRoleError" in result.stdout
    assert "category=planning_failed" in result.stdout
    assert "required camera role 'counter'" in result.stdout


def test_cli_investigate_reports_setup_failures_without_raw_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def _build_service(
        _: CaptureSettings, _progress: Callable[[InvestigationStage], None] | None = None
    ) -> StubService:
        raise RuntimeError(_UNSAFE_ERROR_DETAIL)

    monkeypatch.setattr("vigi_vision.investigate_cli.load_capture_settings", _settings)
    monkeypatch.setattr("vigi_vision.investigate_cli._build_service", _build_service)

    # When
    result = CliRunner().invoke(
        cli.app,
        ["investigate", "--scenario", "restaurant-checkout", "--time", "2026-07-20 12:34:18"],
    )

    # Then
    assert result.exit_code == 1
    assert "Investigation setup failed." in result.stdout
    assert "test-password" not in result.stdout
    assert "rtsp://" not in result.stdout
    assert "nvr.example.invalid" not in result.stdout


def test_cli_investigate_reports_collection_failures_without_raw_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    service = StubService(
        CollectionContractError("ffmpeg -i rtsp://operator:test-password@nvr.example.invalid")
    )

    # When
    _mock_service(monkeypatch, service)
    result = CliRunner().invoke(
        cli.app,
        ["investigate", "--scenario", "restaurant-checkout", "--time", "2026-07-20 12:34:18"],
    )

    # Then
    assert result.exit_code == 1
    assert "stage=recording collection" in result.stdout
    assert "exception=CollectionContractError" in result.stdout
    assert "category=recording_collection_failed" in result.stdout
    assert "Recording collection failed." in result.stdout
    assert "test-password" not in result.stdout
    assert "rtsp://" not in result.stdout
    assert "ffmpeg -i" not in result.stdout


def test_cli_investigate_reports_existing_artifact_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    service = StubService(FileExistsError("artifacts/investigations/restaurant-checkout"))

    # When
    _mock_service(monkeypatch, service)
    result = CliRunner().invoke(
        cli.app,
        ["investigate", "--scenario", "restaurant-checkout", "--time", "2026-07-20 12:34:18"],
    )

    # Then
    assert result.exit_code == 1
    assert "stage=artifact package" in result.stdout
    assert "exception=FileExistsError" in result.stdout
    assert "category=artifact_package_failed" in result.stdout
    assert "Artifact directory already exists." in result.stdout
    assert "artifacts/investigations/restaurant-checkout" not in result.stdout


def test_cli_investigate_reports_snapshot_failures_without_raw_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    service = StubService(AnchorSnapshotError())

    # When
    _mock_service(monkeypatch, service)
    result = CliRunner().invoke(
        cli.app,
        ["investigate", "--scenario", "restaurant-checkout", "--time", "2026-07-20 12:34:18"],
    )

    # Then
    assert result.exit_code == 1
    assert "stage=anchor snapshot extraction" in result.stdout
    assert "exception=AnchorSnapshotError" in result.stdout
    assert "category=anchor_snapshot_failed" in result.stdout
    assert "Snapshot generation failed." in result.stdout


def test_cli_investigate_warns_for_partial_success_and_keeps_artifact_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    service = StubService(
        _result((CollectionStatus.SUCCESS, CollectionStatus.RECORDING_UNAVAILABLE))
    )

    # When
    _mock_service(monkeypatch, service)
    result = CliRunner().invoke(
        cli.app,
        ["investigate", "--scenario", "restaurant-checkout", "--time", "2026-07-20 12:34:18"],
    )

    # Then
    assert result.exit_code == 0
    assert "Items Collected\n---------------\n1" in result.stdout
    assert "Items Failed\n------------\n1" in result.stdout
    assert "Warning: 1 item(s) could not be collected." in result.stdout
    assert "artifacts/investigations/example" in result.stdout


def test_cli_investigate_redacts_unexpected_service_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    service = StubService(RuntimeError(_UNSAFE_ERROR_DETAIL))

    # When
    _mock_service(monkeypatch, service)
    result = CliRunner().invoke(
        cli.app,
        ["investigate", "--scenario", "restaurant-checkout", "--time", "2026-07-20 12:34:18"],
    )

    # Then
    assert result.exit_code == 1
    assert "stage=planning" in result.stdout
    assert "exception=RuntimeError" in result.stdout
    assert "category=planning_failed" in result.stdout
    assert "Investigation execution failed safely." in result.stdout
    assert "test-password" not in result.stdout
    assert "rtsp://" not in result.stdout
    assert "nvr.example.invalid" not in result.stdout
