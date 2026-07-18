"""Terminal interface for the first VIGI Vision inspection slice."""

from dataclasses import dataclass
from pathlib import Path
from typing import final

import typer
from pydantic import ValidationError
from rich.console import Console
from typing_extensions import override

from vigi_vision.analysis import AnalysisRequestError, AnalysisResponseError, OpenAiAnalyzer
from vigi_vision.channel_selection import Channel, ChannelSelectionError, format_channels
from vigi_vision.config import Settings, load_settings
from vigi_vision.ffmpeg import (
    FfmpegExtractionError,
    FfmpegExtractor,
    FfmpegUnavailableError,
    resolve_ffmpeg,
)
from vigi_vision.gateway import select_source_gateway
from vigi_vision.nvr import NvrRequestError, SdkNvrGateway
from vigi_vision.workflow import InspectionWorkflow

app = typer.Typer(add_completion=False, no_args_is_help=True)
_console = Console()


@final
@dataclass(frozen=True, slots=True)
class ChannelsUnavailableError(RuntimeError):
    """Report that safe inventory discovery is limited to NVR sources."""

    @override
    def __str__(self) -> str:
        """Return the stable NVR-only command guidance."""
        return "channels is available only when VIGI_SOURCE=nvr."


@app.callback()
def main() -> None:
    """Retain inspect as the explicit public CLI subcommand."""


@app.command()
def inspect() -> None:
    """Acquire and analyze one current frame from the configured VIGI source."""
    try:
        settings = load_settings(Path.cwd() / ".env")
        workflow = InspectionWorkflow(
            gateway=select_source_gateway(settings),
            extractor=FfmpegExtractor(resolve_ffmpeg(settings.ffmpeg_path)),
            analyzer=OpenAiAnalyzer(settings.openai_api_key.get_secret_value()),
            artifact_root=Path("artifacts"),
        )
        result = workflow.run()
    except ChannelSelectionError as error:
        _console.print(f"Error: {error}", style="red")
        _console.print(format_channels(error.channels))
        raise typer.Exit(code=1) from error
    except (
        AnalysisRequestError,
        AnalysisResponseError,
        FfmpegExtractionError,
        FfmpegUnavailableError,
        NvrRequestError,
        ValidationError,
    ) as error:
        _console.print(f"Error: {error}", style="red")
        raise typer.Exit(code=1) from error
    if result.channel is None:
        _console.print(f"Source: {result.label}")
    else:
        _console.print(f"Channel: {result.channel.channel_id}")
    _console.print(f"Snapshot: {result.snapshot_path}")
    _console.print(f"Summary: {result.analysis.summary}")
    _console.print(f"Person visible: {'yes' if result.analysis.person_visible else 'no'}")
    _console.print(f"Notable: {', '.join(result.analysis.notable_observations)}")
    _console.print(f"Limitations: {result.analysis.limitations}")


@app.command()
def channels() -> None:
    """List safe metadata for NVR channels discovered through the public SDK."""
    try:
        settings = load_settings(Path.cwd() / ".env")
        available_channels = _nvr_channels(settings)
    except (ChannelsUnavailableError, NvrRequestError, ValidationError) as error:
        _console.print(f"Error: {error}", style="red")
        raise typer.Exit(code=1) from error
    _console.print(format_channels(available_channels))


def _nvr_channels(settings: Settings) -> tuple[Channel, ...]:
    match settings.vigi_source:
        case "nvr":
            return SdkNvrGateway(settings.nvr_connection).channels()
        case "ipc":
            raise ChannelsUnavailableError
