"""Public CLI composition from NVR replay retrieval to temporal analysis."""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Final

import typer
from pydantic import ValidationError
from rich.console import Console
from typing_extensions import override

from vigi_vision.analysis import AnalysisRequestError
from vigi_vision.config import NvrConnection, Settings, load_settings
from vigi_vision.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg
from vigi_vision.nvr import NvrRequestError
from vigi_vision.recording import RecordingPlanner, RecordingUnavailableError, RecordingWindow
from vigi_vision.replay import (
    ReplayAuthenticationError,
    ReplayExtractionError,
    ReplayExtractor,
    ReplayTimeoutError,
    ReplayUnavailableError,
)
from vigi_vision.temporal_profiles import TemporalProfileResponseError
from vigi_vision.video import VideoError
from vigi_vision.video_cli import analyze_video_file, print_temporal_report

_console = Console()
_START_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
_DURATION_PATTERN: Final = re.compile(r"([1-9][0-9]*)s")
_INVALID_START_MESSAGE: Final = "Start must use UTC format YYYY-MM-DD HH:MM:SS."
_INVALID_DURATION_MESSAGE: Final = "Duration must be a positive whole-second value such as 30s."


class RecordingSourceError(RuntimeError):
    """Report that recording analysis requires the configured NVR source."""

    @override
    def __str__(self) -> str:
        """Return safe guidance for the source-specific command."""
        return "analyze-recording is available only when VIGI_SOURCE=nvr."


def analyze_recording(
    channel: Annotated[int, typer.Option(min=1, help="NVR channel identifier.")],
    start: Annotated[str, typer.Option(help="UTC start time: YYYY-MM-DD HH:MM:SS.")],
    duration: Annotated[str, typer.Option(help="Positive whole-second duration, for example 30s.")],
    profile: Annotated[
        str,
        typer.Option(
            help=(
                "Analysis profile: counter (카운터), dining (홀 or 식사공간), "
                "or entrance (입구 or 신발장)."
            )
        ),
    ],
) -> None:
    """Analyze one bounded NVR recording with the local-video analysis workflow."""
    try:
        settings = load_settings(Path.cwd() / ".env")
        connection = _nvr_connection(settings)
        window = _recording_window(channel, start, duration)
        planner = RecordingPlanner.connect(connection)
        extractor = ReplayExtractor(
            executable=resolve_ffmpeg(settings.ffmpeg_path),
            username=connection.username.get_secret_value(),
            password=connection.password,
        )
        clip = extractor.extract(planner.plan(window))
        try:
            analysis = analyze_video_file(clip.temporary_mp4_path, profile, settings)
            print_temporal_report(clip.temporary_mp4_path, analysis)
        finally:
            clip.remove()
    except (
        AnalysisRequestError,
        FfmpegUnavailableError,
        NvrRequestError,
        RecordingSourceError,
        RecordingUnavailableError,
        ReplayAuthenticationError,
        ReplayExtractionError,
        ReplayTimeoutError,
        ReplayUnavailableError,
        TemporalProfileResponseError,
        typer.BadParameter,
        ValidationError,
        VideoError,
    ) as error:
        _console.print(f"Error: {error}", style="red")
        raise typer.Exit(code=1) from error


def _nvr_connection(settings: Settings) -> NvrConnection:
    match settings.vigi_source:  # noqa: RUF100  # noqa: MATCH_OK — Settings validates this closed source union before this command runs.
        case "nvr":
            return settings.nvr_connection
        case "ipc":
            raise RecordingSourceError


def _recording_window(channel: int, start: str, duration: str) -> RecordingWindow:
    try:
        start_utc = datetime.strptime(start, _START_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise typer.BadParameter(_INVALID_START_MESSAGE) from error
    duration_match = _DURATION_PATTERN.fullmatch(duration)
    if duration_match is None:
        raise typer.BadParameter(_INVALID_DURATION_MESSAGE)
    duration_seconds = int(duration_match.group(1))
    return RecordingWindow(
        channel_id=channel,
        start_utc=start_utc,
        end_utc=start_utc + timedelta(seconds=duration_seconds),
    )
