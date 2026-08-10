"""Validate reference JPEG bytes and compute confirmation integrity facts."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol, final

from vigi_vision.investigation_confirmation_models import ConfirmationArtifactError

if TYPE_CHECKING:
    from pathlib import Path

_JPEG_TIMEOUT_SECONDS: Final = 15.0
_JPEG_MIN_BYTES: Final = 4
_JPEG_MARKER_PREFIX: Final = 0xFF
_JPEG_MARKER_START_BYTES: Final = 2
_JPEG_SEGMENT_LENGTH_BYTES: Final = 2
_JPEG_SOF_MIN_SEGMENT_BYTES: Final = 7
_SOF_MARKERS: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
JpegRunner = Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]]


def _run(arguments: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # Fixed executable and arguments; no shell.
        arguments,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
    )


@final
@dataclass(frozen=True, slots=True)
class JpegIntegrity:
    """SHA-256 and byte count for one validated JPEG payload."""

    sha256: str
    size_bytes: int


class JpegDecoder(Protocol):
    """Decode a JPEG path and return the exact bytes that were validated."""

    def decode(self, path: Path) -> bytes:
        """Return decoded-source bytes or raise a safe artifact error."""
        ...


@final
@dataclass(frozen=True, slots=True)
class FfmpegJpegDecoder:
    """Use the configured ffmpeg executable to validate one JPEG resource."""

    executable: Path = field(repr=False)
    runner: JpegRunner = field(default=_run, repr=False)

    def decode(self, path: Path) -> bytes:
        """Validate the image through ffmpeg and return its source bytes."""
        try:
            completed = self.runner(
                (
                    str(self.executable),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-i",
                    str(path),
                    "-f",
                    "null",
                    "-",
                ),
                _JPEG_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ConfirmationArtifactError from None
        if completed.returncode != 0:
            raise ConfirmationArtifactError
        try:
            return path.read_bytes()
        except (OSError, ValueError):
            raise ConfirmationArtifactError from None


def compute_jpeg_integrity(
    path: Path,
    width: int,
    height: int,
    decoder: JpegDecoder | None = None,
) -> JpegIntegrity:
    """Validate JPEG markers and dimensions, then hash the exact source bytes."""
    try:
        raw = path.read_bytes() if decoder is None else decoder.decode(path)
    except ConfirmationArtifactError:
        raise
    except (OSError, ValueError):
        raise ConfirmationArtifactError from None
    _validate_jpeg_bytes(raw, width, height)
    return JpegIntegrity(hashlib.sha256(raw).hexdigest(), len(raw))


def _validate_jpeg_bytes(raw: bytes, width: int, height: int) -> None:
    if (
        len(raw) < _JPEG_MIN_BYTES
        or raw[:_JPEG_MARKER_START_BYTES] != b"\xff\xd8"
        or raw[-_JPEG_MARKER_START_BYTES:] != b"\xff\xd9"
    ):
        raise ConfirmationArtifactError
    dimensions = _find_dimensions(raw)
    if dimensions != (width, height):
        raise ConfirmationArtifactError


def _find_dimensions(raw: bytes) -> tuple[int, int] | None:
    position = _JPEG_MARKER_START_BYTES
    while position < len(raw):
        if raw[position] != _JPEG_MARKER_PREFIX:
            position += 1
            continue
        while position < len(raw) and raw[position] == _JPEG_MARKER_PREFIX:
            position += 1
        if position >= len(raw):
            return None
        marker = raw[position]
        position += 1
        if marker in (0xD8, 0xD9):
            continue
        if position + _JPEG_SEGMENT_LENGTH_BYTES > len(raw):
            return None
        segment_length = int.from_bytes(
            raw[position : position + _JPEG_SEGMENT_LENGTH_BYTES], "big"
        )
        if segment_length < _JPEG_SEGMENT_LENGTH_BYTES or position + segment_length > len(raw):
            return None
        if marker in _SOF_MARKERS and segment_length >= _JPEG_SOF_MIN_SEGMENT_BYTES:
            payload = position + _JPEG_SEGMENT_LENGTH_BYTES
            return (
                int.from_bytes(raw[payload + 3 : payload + 5], "big"),
                int.from_bytes(raw[payload + 1 : payload + 3], "big"),
            )
        position += segment_length
    return None
