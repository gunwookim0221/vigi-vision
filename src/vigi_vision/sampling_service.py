"""Public-SDK orchestration for bounded recording-frame sampling."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, Protocol, final

from typing_extensions import override
from vigi import VigiError

from vigi_vision.investigation_snapshot import AnchorSnapshotBoundary, AnchorSnapshotError
from vigi_vision.nvr import NvrRequestError, diagnose_nvr_error
from vigi_vision.recording import (
    RecordingDataError,
    RecordingPlanner,
    RecordingSegment,
    RecordingUnavailableError,
    RecordingWindow,
    ReplayRequest,
)
from vigi_vision.replay import ReplayClip, ReplayError
from vigi_vision.sampling import (
    RecordingCoverage,
    SamplePoint,
    SamplingChunk,
    SamplingRequest,
    build_sampling_plan,
)
from vigi_vision.sampling_artifacts import (
    ChunkOutcome,
    FrameOutcome,
    SamplingArtifactWriter,
    SamplingResult,
)

_PAGE_SIZE: Final = 100
_REPLAY_TIME_FORMAT: Final = "%Y%m%dt%H%M%Sz"
ProgressReporter = Callable[[str], None]


class SamplingCoverageBoundary(Protocol):
    """Resolve public recording coverage and bounded replay requests for sampling."""

    def coverage(self, request: SamplingRequest) -> tuple[RecordingCoverage, ...]:
        """Return all coverage ranges intersecting a request."""
        ...

    def replay_request(self, chunk: SamplingChunk, channel_id: int) -> ReplayRequest:
        """Return a credential-free replay request for one bounded chunk."""
        ...


class ReplayExtractionBoundary(Protocol):
    """Extract one removable temporary replay clip."""

    def extract(self, request: ReplayRequest) -> ReplayClip:
        """Return a caller-owned temporary replay clip."""
        ...


@final
@dataclass(frozen=True, slots=True)
class SamplingExecutionError(RuntimeError):
    """Raised after a credential-safe partial sampling package is finalized."""

    artifact_directory: Path

    @override
    def __str__(self) -> str:
        return "Recording sampling failed; inspect the partial artifact package."


@final
@dataclass(frozen=True, slots=True)
class SamplingCancelledError(RuntimeError):
    """Raised after user cancellation has produced an inspectable partial package."""

    artifact_directory: Path

    @override
    def __str__(self) -> str:
        return "Recording sampling was cancelled; inspect the partial artifact package."


@dataclass(frozen=True, slots=True)
class SamplingCoverageResolver:
    """Enumerate public-SDK recording segments and create credential-free replay requests."""

    planner: RecordingPlanner

    def coverage(self, request: SamplingRequest) -> tuple[RecordingCoverage, ...]:
        """Return all known recording coverage intersecting one sampling request."""
        window = RecordingWindow(request.channel_id, request.start_utc, request.end_utc)
        try:
            process_id = self.planner.client.records.get_free_process().process_id
            segments = tuple(
                segment
                for day in self._matching_days(window)
                for segment in self._segments(request.channel_id, process_id, day)
                if _overlaps(window, segment)
            )
        except VigiError as error:
            raise diagnose_nvr_error(error) from error
        return tuple(
            RecordingCoverage(
                max(segment.start_utc, window.start_utc),
                min(segment.end_utc, window.end_utc),
            )
            for segment in sorted(segments, key=lambda item: item.start_utc)
        )

    def replay_request(self, chunk: SamplingChunk, channel_id: int) -> ReplayRequest:
        """Build one bounded credential-free replay request through the public SDK."""
        window = RecordingWindow(channel_id, chunk.start_utc, chunk.end_utc)
        try:
            replay_url = self.planner.client.stream.build_replay_url(
                self.planner.host,
                channel_id,
                chunk.start_utc.strftime(_REPLAY_TIME_FORMAT),
                chunk.end_utc.strftime(_REPLAY_TIME_FORMAT),
            )
        except VigiError as error:
            raise diagnose_nvr_error(error) from error
        return ReplayRequest(window, replay_url)

    def _matching_days(self, window: RecordingWindow) -> tuple[date, ...]:
        local_start = window.start_utc.astimezone(self.planner.recording_timezone).date()
        local_end = window.end_utc.astimezone(self.planner.recording_timezone).date()
        response = self.planner.client.records.list_days(
            window.channel_id, local_start.strftime("%Y%m"), local_end.strftime("%Y%m")
        )
        days = tuple(_parse_day(item.day) for item in response.days)
        return tuple(day for day in days if local_start <= day <= local_end)

    def _segments(
        self, channel_id: int, process_id: int, recording_day: date
    ) -> tuple[RecordingSegment, ...]:
        segments: list[RecordingSegment] = []
        start_index = 0
        while True:
            response = self.planner.client.records.list_results(
                channel_id,
                process_id,
                recording_day.strftime("%Y%m%d"),
                start_index,
                start_index + _PAGE_SIZE - 1,
            )
            segments.extend(
                RecordingSegment.from_sdk(channel_id, recording_day, item)
                for item in response.results
            )
            if len(response.results) < _PAGE_SIZE:
                return tuple(segments)
            start_index += _PAGE_SIZE


@dataclass(frozen=True, slots=True)
class SamplingService:
    """Collect bounded replay chunks, extract frames, and finalize safe artifacts."""

    resolver: SamplingCoverageBoundary
    replay_extractor: ReplayExtractionBoundary
    frame_extractor: AnchorSnapshotBoundary
    output_root: Path
    progress: ProgressReporter | None = None

    def execute(self, request: SamplingRequest) -> SamplingResult:
        """Run one sampling request without semantic analysis or OpenAI calls."""
        plan = build_sampling_plan(request, self.resolver.coverage(request))
        if not plan.chunks:
            raise RecordingUnavailableError
        artifacts = SamplingArtifactWriter(self.output_root, plan)
        artifacts.begin()
        try:
            for index, chunk in enumerate(plan.chunks, start=1):
                self._report(f"Sampling chunk {index}/{len(plan.chunks)}...")
                self._extract_chunk(artifacts, request, chunk)
        except KeyboardInterrupt:
            result = artifacts.finalize("cancelled")
            raise SamplingCancelledError(result.artifact_directory) from None
        except (AnchorSnapshotError, NvrRequestError, ReplayError):
            result = artifacts.finalize("failed")
            raise SamplingExecutionError(result.artifact_directory) from None
        except OSError:
            result = artifacts.finalize("failed")
            raise SamplingExecutionError(result.artifact_directory) from None
        status = "completed_with_gaps" if plan.skipped_points else "completed"
        return artifacts.finalize(status)

    def _extract_chunk(
        self, artifacts: SamplingArtifactWriter, request: SamplingRequest, chunk: SamplingChunk
    ) -> None:
        outcomes: list[FrameOutcome] = []
        replay_request = self.resolver.replay_request(chunk, request.channel_id)
        try:
            clip = self.replay_extractor.extract(replay_request)
        except ReplayError:
            artifacts.record_chunk(ChunkOutcome(chunk, "failed", 0, "replay_extraction_failed"), ())
            raise
        try:
            for point in chunk.points:
                output_path = artifacts.frame_path(point)
                try:
                    _ = self.frame_extractor.extract(
                        clip.temporary_mp4_path,
                        _offset_seconds(chunk, point),
                        output_path,
                    )
                except AnchorSnapshotError:
                    output_path.unlink(missing_ok=True)
                    outcomes.append(
                        FrameOutcome(point, "failed_extraction", None, chunk.source_coverage)
                    )
                    artifacts.record_chunk(
                        ChunkOutcome(chunk, "failed", len(outcomes) - 1, "frame_extraction_failed"),
                        tuple(outcomes),
                    )
                    raise
                outcomes.append(
                    FrameOutcome(
                        point,
                        "written",
                        f"frames/{output_path.name}",
                        chunk.source_coverage,
                    )
                )
        finally:
            clip.remove()
        artifacts.record_chunk(
            ChunkOutcome(chunk, "completed", len(outcomes), None), tuple(outcomes)
        )

    def _report(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")
    except ValueError as error:
        raise RecordingDataError from error


def _overlaps(window: RecordingWindow, segment: RecordingSegment) -> bool:
    return segment.start_utc < window.end_utc and window.start_utc < segment.end_utc


def _offset_seconds(chunk: SamplingChunk, point: SamplePoint) -> int:
    return int((point.timestamp_utc - chunk.start_utc).total_seconds())
