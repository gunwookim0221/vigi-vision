"""Thin CLI adapters for the approved Phase 7E public inventory."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from vigi_vision.assisted_roi_composition import build_assisted_roi_service
from vigi_vision.assisted_roi_predictor import LazyEfficientSamPredictor
from vigi_vision.assisted_roi_service import AssistedRoiSuggestionService
from vigi_vision.config import (
    AssistedRoiSettings,
    load_assisted_roi_settings,
    load_capture_settings,
)
from vigi_vision.ffmpeg import resolve_ffmpeg
from vigi_vision.investigation_confirmation_integrity import FfmpegJpegDecoder
from vigi_vision.investigation_confirmation_repository import InvestigationConfirmationRepository
from vigi_vision.investigation_confirmation_service import InvestigationConfirmationService
from vigi_vision.recording import RecordingPlanner
from vigi_vision.recording_search_7e_public import (
    Phase7EPublicError,
    Phase7EPublicRequest,
    Phase7EPublicService,
    Phase7EPublicStatus,
    build_phase7e_service,
)
from vigi_vision.recording_search_b3_masks import LimitedRgbMaskPredictor
from vigi_vision.reference_frame_resources import ReferenceFrameResourceStore
from vigi_vision.replay import ReplayExtractor
from vigi_vision.video import resolve_ffprobe

_console = Console()
_CONFIRMATION_ROOT = Path("artifacts/investigations")
_FRAME_ROOT = Path("artifacts/reference-frames")
_SEARCH_ROOT = Path("artifacts/investigation-searches")


def search_recordings(
    investigation_id: Annotated[str, typer.Option("--investigation-id")],
    end: Annotated[str, typer.Option("--end")],
    timezone: Annotated[str, typer.Option("--timezone")],
    *,
    create_phase8_handoff: Annotated[bool, typer.Option("--create-phase8-handoff")] = False,
) -> None:
    """Execute one bounded synchronous request-relative recording search."""
    try:
        result = _build_service().execute(
            Phase7EPublicRequest(
                investigation_id=investigation_id,
                search_end_time_text=end,
                source_timezone=timezone,
            ),
            create_phase8_handoff=create_phase8_handoff,
        )
    except Phase7EPublicError as error:
        _safe_error(error.code)
        raise typer.Exit(code=_exit_for(error.code)) from error
    except Exception as error:
        _safe_error("recording_search_execution_unavailable")
        raise typer.Exit(code=4) from error
    _print_status(result, title="Recording search")


def recording_search_status(
    investigation_id: Annotated[str, typer.Option("--investigation-id")],
    run_id: Annotated[str, typer.Option("--run-id")],
) -> None:
    """Read one Phase 7E/Phase 8 status projection."""
    try:
        result = _build_service().status(investigation_id, run_id)
    except Exception as error:
        _safe_error("recording_search_status_unavailable")
        raise typer.Exit(code=1) from error
    _print_status(result, title="Recording search status")


def create_phase8_handoff(
    investigation_id: Annotated[str, typer.Option("--investigation-id")],
    run_id: Annotated[str, typer.Option("--run-id")],
) -> None:
    """Create or reuse the minimal Phase 8 handoff request."""
    try:
        request = _build_service().create_phase8_handoff(investigation_id, run_id)
    except Phase7EPublicError as error:
        _safe_error(error.code)
        raise typer.Exit(code=_exit_for(error.code)) from error
    except Exception as error:
        _safe_error("recording_search_execution_unavailable")
        raise typer.Exit(code=4) from error
    _console.print("Phase 8 handoff request created.")
    _console.print(f"Request ID: {request.identity}")


def delete_recording_search_media(
    investigation_id: Annotated[str, typer.Option("--investigation-id")],
    run_id: Annotated[str, typer.Option("--run-id")],
    *,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Delete only retained media explicitly owned by a completed search."""
    if not yes:
        _safe_error("confirmation_required")
        raise typer.Exit(code=2)
    try:
        outcome = _build_service().delete_recording_search_media(investigation_id, run_id)
    except Phase7EPublicError as error:
        _safe_error(error.code)
        raise typer.Exit(code=_exit_for(error.code)) from error
    except Exception as error:
        _safe_error("recording_search_execution_unavailable")
        raise typer.Exit(code=4) from error
    _console.print(f"Recording-search media: {outcome}")


def _build_service() -> Phase7EPublicService:
    """Compose Phase 7E from capture settings only; OpenAI is not required."""
    settings = load_capture_settings(Path.cwd() / ".env")
    if settings.vigi_source != "nvr":
        code = "unsupported_source"
        raise Phase7EPublicError(code)
    connection = settings.nvr_connection
    ffmpeg = resolve_ffmpeg(settings.ffmpeg_path)
    ffprobe = resolve_ffprobe(ffmpeg)
    resources = ReferenceFrameResourceStore(_FRAME_ROOT)
    jpeg_decoder = FfmpegJpegDecoder(ffmpeg)
    confirmations = InvestigationConfirmationService(
        resources,
        InvestigationConfirmationRepository(
            _CONFIRMATION_ROOT,
            resources,
            jpeg_decoder=jpeg_decoder,
        ),
        jpeg_decoder=jpeg_decoder,
    )
    try:
        assisted_settings = load_assisted_roi_settings(Path.cwd() / ".env")
    except ValueError:
        assisted_settings = AssistedRoiSettings()
    suggestion = build_assisted_roi_service(assisted_settings, resources)
    predictor = (
        LimitedRgbMaskPredictor(suggestion)
        if isinstance(suggestion, AssistedRoiSuggestionService)
        and isinstance(suggestion.predictor, LazyEfficientSamPredictor)
        else None
    )
    replay = ReplayExtractor(
        executable=ffmpeg,
        username=connection.username.get_secret_value(),
        password=connection.password,
        timeout_diagnostic_directory=settings.replay_timeout_diagnostic_directory,
        progress_diagnostics=settings.replay_progress_diagnostics,
    )
    return build_phase7e_service(
        root=_SEARCH_ROOT,
        confirmation_service=confirmations,
        recording_planner=RecordingPlanner.connect(connection),
        replay_extractor=replay,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        mask_predictor=predictor,
    )


def _print_status(result: Phase7EPublicStatus, *, title: str) -> None:
    data = result.as_dict()
    _console.print(f"VIGI Vision — {title}")
    _console.print(f"Investigation: {data['investigation_id']}")
    _console.print(f"Run: {data['run_id']}")
    _console.print(f"Status: {data['status']}")
    if data["reason_code"]:
        _console.print(f"Reason: {data['reason_code']}")
    if data["phase8_status"]:
        _console.print(f"Phase 8: {data['phase8_status']}")


def _safe_error(code: str) -> None:
    messages = {
        "unsupported_source": "Recording search is available only for NVR sources.",
        "recording_search_execution_unavailable": "Recording search execution is unavailable.",
        "recording_search_status_unavailable": "Recording search status is unavailable.",
        "baseline_unavailable": "The confirmed investigation baseline is unavailable.",
        "invalid_request": "The recording-search request is invalid.",
        "confirmation_required": "Pass --yes to confirm media deletion.",
        "already_running": "A recording-search run is already active.",
        "run_not_found": "The recording-search run was not found.",
        "phase8_not_eligible": "The run is not eligible for a Phase 8 handoff.",
        "phase8_conflict": "A different Phase 8 handoff already exists.",
        "phase8_corrupt": "The Phase 8 handoff is corrupt or unavailable.",
        "phase8_media_unavailable": "The retained recording media is unavailable.",
        "phase8_media_corrupt": "The retained recording media failed validation.",
    }
    message = messages.get(code, "The recording-search operation failed safely.")
    _console.print(f"Error: {message}", style="red")


def _exit_for(code: str) -> int:
    if code in {"invalid_request", "confirmation_required"}:
        return 2
    if code in {"phase8_conflict", "already_running"}:
        return 3
    if code == "phase8_corrupt":
        return 5
    if code in {"phase8_media_unavailable", "phase8_media_corrupt"}:
        return 6
    return 4


__all__ = [
    "create_phase8_handoff",
    "delete_recording_search_media",
    "recording_search_status",
    "search_recordings",
]
