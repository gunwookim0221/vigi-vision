"""Local-MP4 ffmpeg adapter for one investigation anchor snapshot."""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, final

from typing_extensions import override

_SNAPSHOT_TIMEOUT_SECONDS: Final = 15.0
_SNAPSHOT_FAILURE: Final = "ffmpeg could not extract the investigation anchor snapshot."

AnchorSnapshotRunner = Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]]


@final
@dataclass(frozen=True, slots=True)
class AnchorSnapshotError(RuntimeError):
    """A redacted failure from the local anchor-snapshot ffmpeg boundary."""

    @override
    def __str__(self) -> str:
        return _SNAPSHOT_FAILURE


class AnchorSnapshotBoundary(Protocol):
    """Extract one JPEG at an anchor offset from a local MP4."""

    def extract(self, video_path: Path, anchor_offset_seconds: int, output_path: Path) -> Path:
        """Write one anchor JPEG at the requested durable artifact path."""
        ...


def _run_anchor_snapshot(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # Fixed executable and tuple arguments; never a shell command.
        arguments,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=_SNAPSHOT_TIMEOUT_SECONDS,
    )


@final
@dataclass(frozen=True, slots=True)
class FfmpegAnchorSnapshotExtractor:
    """Use ffmpeg to extract exactly one local-MP4 anchor frame."""

    executable: Path
    runner: AnchorSnapshotRunner = _run_anchor_snapshot

    def extract(self, video_path: Path, anchor_offset_seconds: int, output_path: Path) -> Path:
        """Extract one JPEG without URLs, credentials, or frame sampling."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            completed = self.runner(self._arguments(video_path, anchor_offset_seconds, output_path))
        except (OSError, subprocess.TimeoutExpired) as error:
            output_path.unlink(missing_ok=True)
            raise AnchorSnapshotError from error
        if completed.returncode != 0 or not output_path.is_file():
            output_path.unlink(missing_ok=True)
            raise AnchorSnapshotError
        return output_path

    def _arguments(
        self, video_path: Path, anchor_offset_seconds: int, output_path: Path
    ) -> tuple[str, ...]:
        return (
            str(self.executable),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{anchor_offset_seconds}.000",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-q:v",
            "5",
            "-an",
            str(output_path),
        )
