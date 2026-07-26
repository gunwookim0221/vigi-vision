"""Credential-free artifact and manifest handling for recording samples."""

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from secrets import token_hex
from typing import Literal, TypedDict, final

from typing_extensions import override

from vigi_vision.sampling import RecordingCoverage, SamplePoint, SamplingChunk, SamplingPlan

_MANIFEST_FILENAME = "manifest.json"

FrameStatus = Literal["written", "skipped_gap", "failed_extraction"]
ChunkStatus = Literal["pending", "completed", "failed"]
PackageStatus = Literal["completed", "completed_with_gaps", "cancelled", "failed"]


@final
@dataclass(frozen=True, slots=True)
class SamplingArtifactError(RuntimeError):
    """Raised when the sampling package cannot be created safely."""

    @override
    def __str__(self) -> str:
        return "Sampling artifacts could not be created safely."


@dataclass(frozen=True, slots=True)
class FrameOutcome:
    """One requested timestamp and its credential-free artifact state."""

    point: SamplePoint
    status: FrameStatus
    frame_filename: str | None
    source_coverage: RecordingCoverage | None


@dataclass(frozen=True, slots=True)
class ChunkOutcome:
    """One bounded replay chunk and its processing state."""

    chunk: SamplingChunk
    status: ChunkStatus
    frame_count: int
    failure_category: str | None


@dataclass(frozen=True, slots=True)
class SamplingResult:
    """Safe handoff describing the final or inspectable partial package."""

    artifact_directory: Path
    status: PackageStatus
    written_frame_count: int
    skipped_frame_count: int


class _ManifestFrame(TypedDict):
    requested_timestamp_utc: str
    status: FrameStatus
    frame_filename: str | None
    source_coverage_start_utc: str | None
    source_coverage_end_utc: str | None


class _ManifestInterval(TypedDict):
    start_utc: str
    end_utc: str


class _ManifestChunk(TypedDict):
    start_utc: str
    end_utc: str
    source_coverage_start_utc: str
    source_coverage_end_utc: str
    status: ChunkStatus
    frame_count: int
    failure_category: str | None


class _ManifestDocument(TypedDict):
    schema_version: int
    status: PackageStatus
    channel_id: int
    requested_start: str
    source_timezone: str
    start_utc: str
    end_utc: str
    interval_seconds: int
    chunk_duration_seconds: int
    frames: list[_ManifestFrame]
    chunks: list[_ManifestChunk]
    requested_frame_count: int
    written_frame_count: int
    skipped_frame_count: int
    coverage: list[_ManifestInterval]
    gaps: list[_ManifestInterval]


@final
class SamplingArtifactWriter:
    """Own one staging package and finalize it as complete or inspectable partial output."""

    output_root: Path
    plan: SamplingPlan
    package_id: str
    final_directory: Path
    staging_directory: Path
    frame_outcomes: list[FrameOutcome]
    chunk_outcomes: list[ChunkOutcome]

    def __init__(self, output_root: Path, plan: SamplingPlan) -> None:
        """Prepare mutable outcome storage for one invocation-owned package."""
        self.output_root = output_root
        self.plan = plan
        self.package_id = _package_id(self.plan)
        self.final_directory = self.output_root / self.package_id
        self.staging_directory = self.output_root / f".{self.package_id}-{token_hex(4)}"
        self.frame_outcomes = [
            FrameOutcome(point, "skipped_gap", None, None) for point in self.plan.skipped_points
        ]
        self.chunk_outcomes = []

    def begin(self) -> None:
        """Create an invocation-owned staging directory without replacing existing output."""
        if self.final_directory.exists():
            raise FileExistsError
        try:
            (self.staging_directory / "frames").mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise SamplingArtifactError from error

    def frame_path(self, point: SamplePoint) -> Path:
        """Return the deterministic staging frame path for one scheduled UTC timestamp."""
        return self.staging_directory / "frames" / f"{_utc_token(point.timestamp_utc)}.jpg"

    def record_chunk(self, outcome: ChunkOutcome, frames: tuple[FrameOutcome, ...]) -> None:
        """Retain one finished chunk's safe outcome before the next chunk starts."""
        self.chunk_outcomes.append(outcome)
        self.frame_outcomes.extend(frames)

    def finalize(self, status: PackageStatus) -> SamplingResult:
        """Write the manifest and atomically promote owned staging output when possible."""
        self._write_manifest(status)
        target = (
            self.final_directory
            if status in {"completed", "completed_with_gaps"}
            else self._partial_path()
        )
        try:
            _ = self.staging_directory.replace(target)
        except OSError as error:
            raise SamplingArtifactError from error
        written = sum(outcome.status == "written" for outcome in self.frame_outcomes)
        skipped = sum(outcome.status == "skipped_gap" for outcome in self.frame_outcomes)
        return SamplingResult(target, status, written, skipped)

    def discard(self) -> None:
        """Remove only the active invocation's empty staging output."""
        shutil.rmtree(self.staging_directory, ignore_errors=True)

    def _write_manifest(self, status: PackageStatus) -> None:
        document = _manifest_document(self.plan, status, self.frame_outcomes, self.chunk_outcomes)
        try:
            _ = (self.staging_directory / _MANIFEST_FILENAME).write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as error:
            raise SamplingArtifactError from error

    def _partial_path(self) -> Path:
        return self.output_root / f"{self.package_id}-{token_hex(4)}-partial"


def _manifest_document(
    plan: SamplingPlan,
    status: PackageStatus,
    frames: list[FrameOutcome],
    chunks: list[ChunkOutcome],
) -> _ManifestDocument:
    return {
        "schema_version": 1,
        "status": status,
        "channel_id": plan.request.channel_id,
        "requested_start": plan.request.start_text,
        "source_timezone": plan.request.source_timezone,
        "start_utc": _format_utc(plan.request.start_utc),
        "end_utc": _format_utc(plan.request.end_utc),
        "interval_seconds": plan.request.interval_seconds,
        "chunk_duration_seconds": plan.request.chunk_duration_seconds,
        "frames": [_manifest_frame(item) for item in frames],
        "chunks": [_manifest_chunk(item) for item in chunks],
        "requested_frame_count": len(plan.written_points) + len(plan.skipped_points),
        "written_frame_count": sum(item.status == "written" for item in frames),
        "skipped_frame_count": sum(item.status == "skipped_gap" for item in frames),
        "coverage": [_manifest_interval(item) for item in plan.coverage],
        "gaps": [_manifest_interval(item) for item in _gaps(plan)],
    }


def _manifest_frame(item: FrameOutcome) -> _ManifestFrame:
    return {
        "requested_timestamp_utc": _format_utc(item.point.timestamp_utc),
        "status": item.status,
        "frame_filename": item.frame_filename,
        "source_coverage_start_utc": _format_utc(item.source_coverage.start_utc)
        if item.source_coverage is not None
        else None,
        "source_coverage_end_utc": _format_utc(item.source_coverage.end_utc)
        if item.source_coverage is not None
        else None,
    }


def _manifest_chunk(item: ChunkOutcome) -> _ManifestChunk:
    chunk = item.chunk
    return {
        "start_utc": _format_utc(chunk.start_utc),
        "end_utc": _format_utc(chunk.end_utc),
        "source_coverage_start_utc": _format_utc(chunk.source_coverage.start_utc),
        "source_coverage_end_utc": _format_utc(chunk.source_coverage.end_utc),
        "status": item.status,
        "frame_count": item.frame_count,
        "failure_category": item.failure_category,
    }


def _manifest_interval(item: RecordingCoverage) -> _ManifestInterval:
    return {"start_utc": _format_utc(item.start_utc), "end_utc": _format_utc(item.end_utc)}


def _gaps(plan: SamplingPlan) -> tuple[RecordingCoverage, ...]:
    cursor = plan.request.start_utc
    gaps: list[RecordingCoverage] = []
    for coverage in plan.coverage:
        if coverage.start_utc > cursor:
            gaps.append(RecordingCoverage(cursor, coverage.start_utc))
        cursor = max(cursor, coverage.end_utc)
    if cursor < plan.request.end_utc:
        gaps.append(RecordingCoverage(cursor, plan.request.end_utc))
    return tuple(gaps)


def _package_id(plan: SamplingPlan) -> str:
    return (
        f"channel-{plan.request.channel_id}_"
        f"{_utc_token(plan.request.start_utc)}_{_utc_token(plan.request.end_utc)}"
    )


def _utc_token(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _format_utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
