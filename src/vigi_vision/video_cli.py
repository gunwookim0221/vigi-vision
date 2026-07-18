"""CLI command and report rendering for bounded local-video analysis."""

import textwrap
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console

from vigi_vision.analysis import AnalysisRequestError, OpenAiAnalyzer, TemporalAnalysisRequest
from vigi_vision.config import load_local_analysis_settings
from vigi_vision.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg
from vigi_vision.profiles import UnknownProfileError, resolve_profile_alias
from vigi_vision.temporal_profiles import (
    TemporalProfileAnalysis,
    TemporalProfileResponseError,
    get_temporal_profile,
)
from vigi_vision.video import VideoError, VideoSampler, resolve_ffprobe

_console = Console()


def analyze_video(
    video_path: Path,
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
    """Analyze a short local MP4 using sparse, ordered representative frames."""
    try:
        settings = load_local_analysis_settings(Path.cwd() / ".env")
        selected_profile = get_temporal_profile(resolve_profile_alias(profile))
        ffmpeg = resolve_ffmpeg(settings.ffmpeg_path)
        sampler = VideoSampler(ffmpeg=ffmpeg, ffprobe=resolve_ffprobe(ffmpeg))
        with sampler.sample(video_path) as sample:
            analysis = OpenAiAnalyzer(settings.openai_api_key.get_secret_value()).analyze_temporal(
                TemporalAnalysisRequest(
                    profile=selected_profile,
                    metadata=sample.metadata,
                    frames=sample.frames,
                )
            )
    except (
        AnalysisRequestError,
        FfmpegUnavailableError,
        TemporalProfileResponseError,
        UnknownProfileError,
        ValidationError,
        VideoError,
    ) as error:
        _console.print(f"Error: {error}", style="red")
        raise typer.Exit(code=1) from error
    _print_temporal_report(video_path, analysis)


def _print_temporal_report(video_path: Path, analysis: TemporalProfileAnalysis) -> None:
    _console.print("=" * 60, markup=False)
    _console.print(f"VIGI Vision — Video Analysis ({analysis.profile})", markup=False)
    _console.print("=" * 60, markup=False)
    _console.print()
    _print_section("Video", str(video_path))
    _print_section("Profile", analysis.profile)
    _print_section("Duration", f"{analysis.video_duration_seconds:.1f} seconds")
    _print_section("Frame Count", str(analysis.sampled_frame_count))
    _print_section("Sample Timestamps", _format_timestamps(analysis.sampled_timestamps))
    _print_section("Summary", analysis.summary)
    _print_section("Confidence", analysis.confidence.capitalize())
    _print_observations("Evidence", tuple(item.description for item in analysis.evidence))
    _print_observations("Profile-specific Findings", analysis.profile_findings())
    _print_observations(
        "Observed Changes", tuple(item.description for item in analysis.observed_changes)
    )
    _print_observations(
        "Possible Events",
        tuple(f"{item.description} {item.confidence_note}" for item in analysis.possible_events),
    )
    if analysis.recommendations:
        _print_observations(
            "Recommendations", tuple(item.description for item in analysis.recommendations)
        )
    _print_section("Analysis Limitations", analysis.limitations)
    _console.print("Video analysis completed successfully.", markup=False)


def _format_timestamps(timestamps: tuple[float, ...]) -> str:
    return ", ".join(_format_timestamp(timestamp) for timestamp in timestamps)


def _format_timestamp(timestamp: float) -> str:
    total_tenths = round(timestamp * 10)
    total_seconds, tenths = divmod(total_tenths, 10)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}.{tenths}"


def _print_section(title: str, value: str) -> None:
    _console.print(title, markup=False)
    _console.print("-" * len(title), markup=False)
    for line in _wrapped_lines(value):
        _console.print(line, markup=False)
    _console.print()


def _print_observations(title: str, observations: tuple[str, ...]) -> None:
    _console.print(title, markup=False)
    _console.print("-" * len(title), markup=False)
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
