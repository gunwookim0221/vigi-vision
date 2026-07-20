"""The small NVR-to-frame-to-analysis orchestration flow."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from vigi_vision.analysis import SceneAnalysis
from vigi_vision.channel_selection import Channel
from vigi_vision.ffmpeg import FfmpegExtractionError


@dataclass(frozen=True, slots=True)
class LiveStream:
    """A selected credential-free RTSP stream and its separate credentials."""

    label: str
    channel: Channel | None
    live_url: str
    username: str
    password: str
    artifact_stem: str


class SourceGateway(Protocol):
    """Resolve either configured camera source to one RTSP stream."""

    def stream(self) -> LiveStream:
        """Return one current RTSP stream without exposing it externally."""
        ...


class FrameExtractor(Protocol):
    """The one-frame media boundary consumed by the workflow."""

    def extract(self, live_url: str, *, username: str, password: str, output_path: Path) -> Path:
        """Extract exactly one frame at the requested artifact path."""
        ...


class ImageAnalyzer(Protocol):
    """The one-image analysis boundary consumed by the workflow."""

    def analyze(self, image_path: Path) -> SceneAnalysis:
        """Return validated analysis of one image."""
        ...


@dataclass(frozen=True, slots=True)
class InspectionResult:
    """The structured terminal result of a completed inspection."""

    label: str
    channel: Channel | None
    snapshot_path: Path
    analysis: SceneAnalysis


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """The structured result of capturing one current frame."""

    label: str
    channel: Channel | None
    snapshot_path: Path


@dataclass(frozen=True, slots=True)
class SnapshotCapture:
    """Coordinate source selection and one-frame extraction."""

    gateway: SourceGateway
    extractor: FrameExtractor
    artifact_root: Path
    artifact_directory: str = "snapshots"

    def run(self) -> SnapshotResult:
        """Capture one frame and remove a partial artifact if extraction fails."""
        stream = self.gateway.stream()
        snapshot_path = (
            self.artifact_root / self.artifact_directory / _snapshot_name(stream.artifact_stem)
        )
        try:
            extracted_path = self.extractor.extract(
                stream.live_url,
                username=stream.username,
                password=stream.password,
                output_path=snapshot_path,
            )
        except FfmpegExtractionError:
            snapshot_path.unlink(missing_ok=True)
            raise
        return SnapshotResult(stream.label, stream.channel, extracted_path)


@dataclass(frozen=True, slots=True)
class InspectionWorkflow:
    """Coordinate the minimum safe vertical slice without logging secrets."""

    gateway: SourceGateway
    extractor: FrameExtractor
    analyzer: ImageAnalyzer
    artifact_root: Path

    def run(self) -> InspectionResult:
        """Acquire and analyze exactly one current frame."""
        snapshot = SnapshotCapture(self.gateway, self.extractor, self.artifact_root).run()
        analysis = self.analyzer.analyze(snapshot.snapshot_path)
        return InspectionResult(
            label=snapshot.label,
            channel=snapshot.channel,
            snapshot_path=snapshot.snapshot_path,
            analysis=analysis,
        )


def _snapshot_name(artifact_stem: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{artifact_stem}-{timestamp}.jpg"
