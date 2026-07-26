"""Public CLI adapter for generic bounded NVR recording-frame sampling."""

from pathlib import Path
from typing import Annotated, Final, final

import typer
from pydantic import ValidationError
from rich.console import Console
from typing_extensions import override

from vigi_vision.cli_output import print_section
from vigi_vision.config import CaptureSettings, NvrConnection, load_capture_settings
from vigi_vision.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg
from vigi_vision.investigation_snapshot import FfmpegAnchorSnapshotExtractor
from vigi_vision.nvr import NvrRequestError
from vigi_vision.recording import RecordingDataError, RecordingPlanner, RecordingUnavailableError
from vigi_vision.replay import ReplayError, ReplayExtractor
from vigi_vision.sampling import RawSamplingInput, SamplingInputError, parse_sampling_request
from vigi_vision.sampling_artifacts import SamplingArtifactError, SamplingResult
from vigi_vision.sampling_service import (
    SamplingCancelledError,
    SamplingCoverageResolver,
    SamplingExecutionError,
    SamplingService,
)

_DEFAULT_TIMEZONE: Final = "Asia/Seoul"
_DEFAULT_CHUNK_DURATION: Final = "10m"
_DEFAULT_OUTPUT_ROOT: Final = Path("artifacts/recording-samples")
_console = Console(soft_wrap=True)


@final
class SamplingSourceError(RuntimeError):
    """Report that sampling requires the configured NVR source."""

    @override
    def __str__(self) -> str:
        return "sample-recording is available only when VIGI_SOURCE=nvr."


def sample_recording(  # noqa: PLR0913 — Typer maps each documented CLI option to one parameter.
    channel: Annotated[int, typer.Option(min=1, help="NVR channel identifier.")],
    start: Annotated[str, typer.Option(help="Source time: YYYY-MM-DD HH:MM:SS.")],
    duration: Annotated[str, typer.Option(help="Positive duration, for example 2h.")],
    interval: Annotated[str, typer.Option(help="Positive sampling interval, for example 5s.")],
    source_timezone: Annotated[
        str,
        typer.Option("--timezone", help="Source timezone: UTC or Asia/Seoul."),
    ] = _DEFAULT_TIMEZONE,
    chunk_duration: Annotated[
        str,
        typer.Option("--chunk-duration", help="Bounded replay duration; default: 10m."),
    ] = _DEFAULT_CHUNK_DURATION,
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Artifact parent; default: artifacts/recording-samples."),
    ] = _DEFAULT_OUTPUT_ROOT,
) -> None:
    """Write generic timestamped frames from bounded covered NVR replay chunks."""
    try:
        settings = load_capture_settings(Path.cwd() / ".env")
        request = parse_sampling_request(
            RawSamplingInput(channel, start, source_timezone, duration, interval, chunk_duration)
        )
        service = _build_service(settings, output_dir)
        result = service.execute(request)
    except SamplingCancelledError as error:
        _console.print(f"Cancelled: partial artifacts at {error.artifact_directory.as_posix()}")
        raise typer.Exit(code=130) from None
    except SamplingExecutionError as error:
        _console.print(f"Error: {error} {error.artifact_directory.as_posix()}", style="red")
        raise typer.Exit(code=1) from None
    except FileExistsError:
        _console.print("Error: Sampling artifact directory already exists.", style="red")
        raise typer.Exit(code=1) from None
    except (
        FfmpegUnavailableError,
        NvrRequestError,
        RecordingDataError,
        RecordingUnavailableError,
        ReplayError,
        SamplingArtifactError,
        SamplingInputError,
        SamplingSourceError,
        ValidationError,
    ) as error:
        _console.print(f"Error: {error}", style="red")
        raise typer.Exit(code=1) from error
    _print_result(result)


def _build_service(settings: CaptureSettings, output_dir: Path) -> SamplingService:
    connection = _nvr_connection(settings)
    ffmpeg = resolve_ffmpeg(settings.ffmpeg_path)
    planner = RecordingPlanner.connect(connection)
    return SamplingService(
        SamplingCoverageResolver(planner),
        ReplayExtractor(
            executable=ffmpeg,
            username=connection.username.get_secret_value(),
            password=connection.password,
        ),
        FfmpegAnchorSnapshotExtractor(ffmpeg),
        output_dir,
        _progress,
    )


def _nvr_connection(settings: CaptureSettings) -> NvrConnection:
    match settings.vigi_source:  # noqa: RUF100  # noqa: MATCH_OK — validated closed source union.
        case "nvr":
            return settings.nvr_connection
        case "ipc":
            raise SamplingSourceError


def _progress(message: str) -> None:
    _console.print(message, markup=False)


def _print_result(result: SamplingResult) -> None:
    _console.print("VIGI Vision — Recording Samples", markup=False)
    _console.print()
    print_section(_console, "Artifact Directory", result.artifact_directory.as_posix())
    print_section(_console, "Status", result.status)
    print_section(_console, "Frames Written", str(result.written_frame_count))
    print_section(_console, "Frames Skipped", str(result.skipped_frame_count))
    if result.status == "completed_with_gaps":
        _console.print(
            "Warning: recording gaps left some scheduled frames unavailable.", style="yellow"
        )
    _console.print("Recording sampling completed.", markup=False)
