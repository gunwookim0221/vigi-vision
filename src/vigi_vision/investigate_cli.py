"""Public CLI adapter for the completed investigation service."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Final, final

import typer
from pydantic import ValidationError
from rich.console import Console
from typing_extensions import override

from vigi_vision.cli_output import print_section
from vigi_vision.config import CaptureSettings, NvrConnection, load_capture_settings
from vigi_vision.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg
from vigi_vision.investigation import (
    CameraAssignment,
    CameraRole,
    InvalidAnchorTimeError,
    InvalidInvestigationDefinitionError,
    InvestigationPlanner,
    MissingRequiredCameraRoleError,
    RelativeWindow,
    Scenario,
    ScenarioCameraRule,
    parse_kst_anchor,
    validate_scenario_profiles,
)
from vigi_vision.investigation_artifacts import (
    InvestigationArtifactBuilder,
    InvestigationArtifactError,
    InvestigationResult,
)
from vigi_vision.investigation_collection import (
    CollectionContractError,
    CollectionStatus,
    InvestigationCollector,
)
from vigi_vision.investigation_progress import InvestigationStage, ProgressReporter
from vigi_vision.investigation_service import InvestigationRequest, InvestigationService
from vigi_vision.investigation_snapshot import AnchorSnapshotError, FfmpegAnchorSnapshotExtractor
from vigi_vision.nvr import NvrRequestError
from vigi_vision.profiles import UnknownProfileError
from vigi_vision.recording import RecordingPlanner
from vigi_vision.replay import ReplayExtractor

_console = Console(soft_wrap=True)
_RESTAURANT_CHECKOUT: Final = "restaurant-checkout"
_ARTIFACT_ROOT: Final = Path("artifacts/investigations")
_ARTIFACT_DIRECTORY_EXISTS: Final = "Artifact directory already exists."
_COLLECTION_FAILED: Final = "Recording collection failed."
_INVALID_SCENARIO: Final = "Scenario must be restaurant-checkout."
_SETUP_FAILED: Final = "Investigation setup failed."
_SNAPSHOT_FAILED: Final = "Snapshot generation failed."
_UNEXPECTED_EXECUTION_ERROR: Final = "Investigation execution failed safely."
_PROGRESS_MESSAGES: Final = {
    InvestigationStage.PLANNING: "Planning investigation...",
    InvestigationStage.COLLECTION: "Collecting recordings...",
    InvestigationStage.ARTIFACT_PACKAGE: "Building artifact package...",
    InvestigationStage.MANIFEST_WRITING: "Writing manifest...",
    InvestigationStage.SETUP: "",
    InvestigationStage.MP4_PRESERVATION: "",
    InvestigationStage.ANCHOR_SNAPSHOT: "",
}
_STAGE_CATEGORIES: Final = {
    InvestigationStage.SETUP: "setup_failed",
    InvestigationStage.PLANNING: "planning_failed",
    InvestigationStage.COLLECTION: "recording_collection_failed",
    InvestigationStage.ARTIFACT_PACKAGE: "artifact_package_failed",
    InvestigationStage.MP4_PRESERVATION: "mp4_preservation_failed",
    InvestigationStage.ANCHOR_SNAPSHOT: "anchor_snapshot_failed",
    InvestigationStage.MANIFEST_WRITING: "manifest_write_failed",
}


@final
class InvestigationSourceError(RuntimeError):
    """Report that the public investigation command requires an NVR source."""

    @override
    def __str__(self) -> str:
        return "investigate is available only when VIGI_SOURCE=nvr."


def investigate(
    scenario: Annotated[
        str,
        typer.Option(help="Current investigation scenario: restaurant-checkout."),
    ],
    anchor_time: Annotated[
        str,
        typer.Option("--time", help="Asia/Seoul time: YYYY-MM-DD HH:MM:SS."),
    ],
) -> None:
    """Collect the current deployment's scenario into a durable investigation package."""
    try:
        settings = load_capture_settings(Path.cwd() / ".env")
        request = _request(scenario, anchor_time)
        progress, current_stage = _progress_reporter()
        service = _build_service(settings, progress)
    except (
        FfmpegUnavailableError,
        InvalidAnchorTimeError,
        InvalidInvestigationDefinitionError,
        InvestigationArtifactError,
        InvestigationSourceError,
        MissingRequiredCameraRoleError,
        NvrRequestError,
        UnknownProfileError,
        ValidationError,
        typer.BadParameter,
    ) as error:
        _print_stage_error(InvestigationStage.SETUP, error, str(error))
        raise typer.Exit(code=1) from error
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — public CLI must redact unexpected service failures.
        _print_stage_error(InvestigationStage.SETUP, error, _SETUP_FAILED)
        raise typer.Exit(code=1) from None

    try:
        result = service.execute(request)
    except CollectionContractError as error:
        _print_stage_error(current_stage(), error, _COLLECTION_FAILED)
        raise typer.Exit(code=1) from None
    except FileExistsError as error:
        _print_stage_error(current_stage(), error, _ARTIFACT_DIRECTORY_EXISTS)
        raise typer.Exit(code=1) from None
    except AnchorSnapshotError as error:
        _print_stage_error(current_stage(), error, _SNAPSHOT_FAILED)
        raise typer.Exit(code=1) from None
    except (
        InvalidInvestigationDefinitionError,
        InvestigationArtifactError,
        MissingRequiredCameraRoleError,
        NvrRequestError,
        UnknownProfileError,
    ) as error:
        _print_stage_error(current_stage(), error, str(error))
        raise typer.Exit(code=1) from error
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — public CLI must redact unexpected service failures.
        _print_stage_error(current_stage(), error, _UNEXPECTED_EXECUTION_ERROR)
        raise typer.Exit(code=1) from None
    _print_investigation_report(result, anchor_time)


def _request(scenario_id: str, anchor_time: str) -> InvestigationRequest:
    return InvestigationRequest(
        parse_kst_anchor(anchor_time),
        _scenario(scenario_id),
        (
            CameraAssignment(1, CameraRole("counter")),
            CameraAssignment(2, CameraRole("entrance")),
            CameraAssignment(3, CameraRole("dining")),
        ),
    )


def _scenario(scenario_id: str) -> Scenario:
    match scenario_id:  # noqa: RUF100  # noqa: MATCH_OK — CLI scenario input is intentionally open.
        case "restaurant-checkout":
            return validate_scenario_profiles(
                Scenario(
                    _RESTAURANT_CHECKOUT,
                    (
                        ScenarioCameraRule(
                            CameraRole("counter"),
                            "counter",
                            RelativeWindow(-15, 15),
                            required=True,
                        ),
                        ScenarioCameraRule(
                            CameraRole("entrance"),
                            "entrance",
                            RelativeWindow(-15, 15),
                            required=True,
                        ),
                        ScenarioCameraRule(
                            CameraRole("dining"),
                            "dining",
                            RelativeWindow(-15, 15),
                            required=False,
                        ),
                    ),
                )
            )
        case _:
            raise typer.BadParameter(_INVALID_SCENARIO)


def _build_service(
    settings: CaptureSettings, progress: ProgressReporter | None = None
) -> InvestigationService:
    connection = _nvr_connection(settings)
    ffmpeg = resolve_ffmpeg(settings.ffmpeg_path)
    return InvestigationService(
        InvestigationPlanner(),
        InvestigationCollector(
            RecordingPlanner.connect(connection),
            ReplayExtractor(
                executable=ffmpeg,
                username=connection.username.get_secret_value(),
                password=connection.password,
            ),
        ),
        InvestigationArtifactBuilder(
            _ARTIFACT_ROOT,
            FfmpegAnchorSnapshotExtractor(ffmpeg),
            progress,
        ),
        progress,
    )


def _nvr_connection(settings: CaptureSettings) -> NvrConnection:
    match settings.vigi_source:  # noqa: RUF100  # noqa: MATCH_OK — CaptureSettings validates the source union.
        case "nvr":
            return settings.nvr_connection
        case "ipc":
            raise InvestigationSourceError


def _print_investigation_report(result: InvestigationResult, anchor_time: str) -> None:
    collection_items = result.collection_result.items
    collected = sum(item.collection_status is CollectionStatus.SUCCESS for item in collection_items)
    failed = len(collection_items) - collected
    _console.print("VIGI Vision — Investigation", markup=False)
    _console.print()
    print_section(_console, "Scenario", result.investigation_plan.scenario_id)
    print_section(_console, "Anchor Time (KST)", anchor_time)
    print_section(_console, "Artifact Directory", result.artifact_directory.as_posix())
    print_section(_console, "Items Collected", str(collected))
    print_section(_console, "Items Failed", str(failed))
    print_section(_console, "Manifest", (result.artifact_directory / "manifest.json").as_posix())
    if failed:
        _console.print(f"Warning: {failed} item(s) could not be collected.", style="yellow")
    _console.print("Investigation completed.", markup=False)


def _progress_reporter() -> tuple[ProgressReporter, Callable[[], InvestigationStage]]:
    current_stage = InvestigationStage.SETUP

    def report(stage: InvestigationStage) -> None:
        nonlocal current_stage
        current_stage = stage
        message = _progress_message(stage)
        if message:
            _console.print(message, markup=False)

    def current() -> InvestigationStage:
        return current_stage

    return report, current


def _progress_message(stage: InvestigationStage) -> str:
    return _PROGRESS_MESSAGES[stage]


def _print_stage_error(stage: InvestigationStage, error: Exception, explanation: str) -> None:
    message = (
        f"Error: stage={stage.value}; exception={type(error).__name__}; "
        f"category={_stage_category(stage)}; {explanation}"
    )
    _console.print(
        message,
        style="red",
    )


def _stage_category(stage: InvestigationStage) -> str:
    return _STAGE_CATEGORIES[stage]
