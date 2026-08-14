"""Bounded in-memory JPEG validation and RGB decoding for Phase 7B-3."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from typing_extensions import override

from vigi_vision.investigation_confirmation_integrity import (
    JpegIntegrity,
    compute_jpeg_integrity_from_bytes,
)
from vigi_vision.investigation_confirmation_models import ConfirmationArtifactError
from vigi_vision.object_presence_models import DecodedRgbImage
from vigi_vision.recording_search_models import RecordingSearchError

if TYPE_CHECKING:
    from pathlib import Path

_MAX_JPEG_BYTES: Final = 256 * 1024 * 1024
_MAX_DECODED_RGB_BYTES: Final = 256 * 1024 * 1024
_DECODE_TIMEOUT_SECONDS: Final = 15.0


class InvalidMediaInputError(RecordingSearchError):
    """The exact admitted bytes are not a supported decodable RGB JPEG."""

    @override
    def __str__(self) -> str:
        """Return the stable operational category."""
        return "invalid_media_input"


RgbDecoderRunner = Callable[[tuple[str, ...], bytes, float], subprocess.CompletedProcess[bytes]]


def _run_decoder(
    arguments: tuple[str, ...], payload: bytes, timeout: float
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - fixed executable and pipe arguments
        arguments,
        input=payload,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


@dataclass(frozen=True, slots=True)
class DecodedMedia:
    """The integrity facts and immutable RGB value for one exact byte sequence."""

    integrity: JpegIntegrity
    image: DecodedRgbImage = field(repr=False)


@dataclass(frozen=True, slots=True)
class InMemoryRgbDecoder:
    """Decode bounded JPEG bytes through one configured ffmpeg process."""

    executable: Path
    runner: RgbDecoderRunner = field(default=_run_decoder, repr=False)

    def decode(self, payload: bytes, width: int, height: int) -> DecodedMedia:
        """Validate and decode the exact supplied bytes without filesystem access."""
        if (
            type(payload) is not bytes
            or type(width) is not int
            or type(height) is not int
            or width <= 0
            or height <= 0
            or width * height * 3 > _MAX_DECODED_RGB_BYTES
        ):
            raise InvalidMediaInputError
        if len(payload) > _MAX_JPEG_BYTES:
            raise InvalidMediaInputError
        try:
            integrity = compute_jpeg_integrity_from_bytes(
                payload,
                width,
                height,
                _MAX_JPEG_BYTES,
            )
        except ConfirmationArtifactError:
            raise InvalidMediaInputError from None
        expected = width * height * 3
        try:
            completed = self.runner(
                (
                    str(self.executable),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "mjpeg",
                    "-i",
                    "pipe:0",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "pipe:1",
                ),
                payload,
                _DECODE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise InvalidMediaInputError from None
        except Exception:  # noqa: BLE001 - decoder failures are one safe category.
            raise InvalidMediaInputError from None
        if (
            type(completed.returncode) is not int
            or completed.returncode != 0
            or type(completed.stdout) is not bytes
            or len(completed.stdout) != expected
        ):
            raise InvalidMediaInputError
        try:
            rows: list[tuple[tuple[int, int, int], ...]] = []
            for y in range(height):
                row = tuple(
                    (
                        completed.stdout[offset],
                        completed.stdout[offset + 1],
                        completed.stdout[offset + 2],
                    )
                    for x in range(width)
                    for offset in ((y * width + x) * 3,)
                )
                rows.append(row)
            image = DecodedRgbImage.from_rows(tuple(rows))
        except (IndexError, TypeError, ValueError):
            raise InvalidMediaInputError from None
        return DecodedMedia(integrity=integrity, image=image)
