"""Terminal interface for the first VIGI Vision inspection slice."""

import textwrap
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
from vigi_vision.workflow import InspectionResult, InspectionWorkflow

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
    _print_inspection_report(result)


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


def _print_inspection_report(result: InspectionResult) -> None:
    _console.print("=" * 60, markup=False)
    _console.print("VIGI Vision — Live Scene Inspection", markup=False)
    _console.print("=" * 60, markup=False)
    _console.print()
    _print_section("Source", result.label)
    _print_section(
        "Channel",
        "Not available" if result.channel is None else str(result.channel.channel_id),
    )
    _print_section("Snapshot", str(result.snapshot_path))
    _print_section("Scene Summary", result.analysis.summary)
    _print_section("People", "Yes" if result.analysis.person_visible else "No")
    _print_observations(result.analysis.notable_observations)
    _print_section("Analysis Limitations", result.analysis.limitations)
    _console.print("Inspection completed successfully.", markup=False)


def _print_section(title: str, value: str) -> None:
    _console.print(title, markup=False)
    _console.print("-" * len(title), markup=False)
    for line in _wrapped_lines(value):
        _console.print(line, markup=False)
    _console.print()


def _print_observations(observations: tuple[str, ...]) -> None:
    _console.print("Key Observations", markup=False)
    _console.print("-" * len("Key Observations"), markup=False)
    if observations:
        for observation in observations:
            lines = _wrapped_lines(observation)
            _console.print(f"• {lines[0]}", markup=False)
            for line in lines[1:]:
                _console.print(f"  {line}", markup=False)
    else:
        _console.print("Not available", markup=False)
    _console.print()


def _wrapped_lines(value: str) -> tuple[str, ...]:
    cleaned = value.strip() or "Not available"
    return tuple(
        textwrap.wrap(
            cleaned,
            width=96,
            break_long_words=False,
            break_on_hyphens=False,
        )
    )
