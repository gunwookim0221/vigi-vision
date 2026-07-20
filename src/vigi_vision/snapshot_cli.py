"""Public command for capturing one current NVR channel frame."""

from dataclasses import replace
from pathlib import Path
from typing import Annotated, final

import typer
from pydantic import ValidationError
from rich.console import Console
from typing_extensions import override

from vigi_vision.channel_selection import ChannelSelectionError, format_channels
from vigi_vision.config import CaptureSettings, NvrConnection, load_capture_settings
from vigi_vision.ffmpeg import (
    FfmpegExtractionError,
    FfmpegExtractor,
    FfmpegUnavailableError,
    resolve_ffmpeg,
)
from vigi_vision.nvr import NvrRequestError, SdkNvrGateway
from vigi_vision.workflow import SnapshotCapture, SnapshotResult

_console = Console()


@final
class SnapshotSourceError(RuntimeError):
    """Report that the snapshot command requires an NVR source."""

    @override
    def __str__(self) -> str:
        return "snapshot is available only when VIGI_SOURCE=nvr."


def snapshot(
    channel: Annotated[int, typer.Option(min=1, help="Positive NVR channel identifier.")],
) -> None:
    """Capture one current JPEG frame from an online NVR channel."""
    try:
        settings = load_capture_settings(Path.cwd() / ".env")
        connection = replace(_require_nvr(settings), channel_id=channel)
        result = SnapshotCapture(
            gateway=SdkNvrGateway(connection),
            extractor=FfmpegExtractor(resolve_ffmpeg(settings.ffmpeg_path)),
            artifact_root=Path("artifacts"),
            artifact_directory="channel-snapshots",
        ).run()
    except ChannelSelectionError as error:
        _console.print(f"Error: {error}", style="red")
        _console.print(format_channels(error.channels))
        raise typer.Exit(code=1) from error
    except (
        FfmpegExtractionError,
        FfmpegUnavailableError,
        NvrRequestError,
        SnapshotSourceError,
        ValidationError,
    ) as error:
        _console.print(f"Error: {error}", style="red")
        raise typer.Exit(code=1) from error
    _print_snapshot_report(result)


def _require_nvr(settings: CaptureSettings) -> NvrConnection:
    if settings.vigi_source != "nvr":
        raise SnapshotSourceError
    return settings.nvr_connection


def _print_snapshot_report(result: SnapshotResult) -> None:
    _console.print("VIGI Vision — Channel Snapshot", markup=False)
    _console.print()
    _console.print("Channel", markup=False)
    _console.print("-------", markup=False)
    _console.print("Not available" if result.channel is None else str(result.channel.channel_id))
    _console.print()
    _console.print("Snapshot", markup=False)
    _console.print("--------", markup=False)
    _console.print(result.snapshot_path.as_posix(), markup=False)
    _console.print()
    _console.print("Snapshot captured successfully.", markup=False)
