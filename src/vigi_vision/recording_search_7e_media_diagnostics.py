"""Closed, credential-free diagnostics for the Phase 7E media boundary."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Final

# Uvicorn's documented default configuration owns the ``uvicorn`` handler and
# does not configure the process root logger. A child of ``uvicorn.error``
# therefore reaches the established stderr formatter instead of depending on
# Python's environment-sensitive ``lastResort`` fallback.
_LOGGER = logging.getLogger("uvicorn.error.vigi_vision.phase7e")

MEDIA_PROBE_DIAGNOSTIC_VERSION: Final = 1
MEDIA_PROBE_STAGES: Final = frozenset(
    {
        "extracted_file_missing",
        "extracted_file_not_regular",
        "extracted_file_outside_confinement",
        "extracted_file_empty",
        "extracted_file_oversized",
        "extracted_file_unstable",
        "ffprobe_timeout",
        "ffprobe_nonzero_exit",
        "ffprobe_unavailable",
        "ffprobe_invalid_json",
        "ffprobe_invalid_shape",
        "video_stream_missing",
        "unexpected_video_stream_count",
        "unsupported_video_codec",
        "invalid_dimensions",
        "invalid_time_base",
        "missing_duration",
        "invalid_duration",
        "duration_too_short",
        "duration_too_long",
        "probe_facts_mismatch",
        "publication_post_copy_validation",
        "publication_post_rename_validation",
        "publication_final_probe",
        "publication_fact_mismatch",
        "publication_authority",
        "cleanup_failed",
        "unexpected_probe_failure",
    }
)
_FFPROBE_EXIT: Final = frozenset({"not_run", "zero", "nonzero", "unavailable"})
_JSON_STATUS: Final = frozenset({"not_run", "valid", "invalid", "shape_invalid"})
_CLEANUP_OUTCOME: Final = frozenset({"not_required", "succeeded", "failed"})
_CODEC_CLASSES: Final = frozenset({"not_observed", "h264", "hevc", "mpeg4", "mjpeg", "unknown"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class Phase7EMediaProbeDiagnostic:
    """Versioned process-local facts for one failed retained-media probe."""

    stage: str
    retained_bytes: int | None = None
    ffprobe_exit: str = "not_run"
    json_status: str = "not_run"
    video_stream_count: int | None = None
    audio_stream_count: int | None = None
    codec: str = "not_observed"
    width: int | None = None
    height: int | None = None
    requested_duration_ms: int | None = None
    observed_duration_ms: int | None = None
    duration_tolerance_ms: int | None = None
    cleanup_outcome: str = "not_required"
    secondary_stage: str | None = None

    def __post_init__(self) -> None:
        """Reject every value outside the closed safe vocabulary."""
        if self.stage not in MEDIA_PROBE_STAGES:
            raise ValueError
        if self.ffprobe_exit not in _FFPROBE_EXIT:
            raise ValueError
        if self.json_status not in _JSON_STATUS:
            raise ValueError
        if self.codec not in _CODEC_CLASSES:
            raise ValueError
        if self.cleanup_outcome not in _CLEANUP_OUTCOME:
            raise ValueError
        if self.secondary_stage not in {None, "cleanup_failed"}:
            raise ValueError
        for value in (
            self.retained_bytes,
            self.video_stream_count,
            self.audio_stream_count,
            self.width,
            self.height,
            self.requested_duration_ms,
            self.observed_duration_ms,
            self.duration_tolerance_ms,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError

    def with_cleanup(self, *, failed: bool) -> Phase7EMediaProbeDiagnostic:
        """Return the same captured facts with the post-probe cleanup outcome."""
        return replace(
            self,
            cleanup_outcome="failed" if failed else "succeeded",
            secondary_stage="cleanup_failed" if failed else self.secondary_stage,
        )

    def as_dict(self) -> dict[str, object]:
        """Return only bounded facts approved for local diagnostics."""
        return {
            "version": MEDIA_PROBE_DIAGNOSTIC_VERSION,
            "stage": self.stage,
            "retained_bytes": self.retained_bytes,
            "ffprobe_exit": self.ffprobe_exit,
            "json_status": self.json_status,
            "video_stream_count": self.video_stream_count,
            "audio_stream_count": self.audio_stream_count,
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "requested_duration_ms": self.requested_duration_ms,
            "observed_duration_ms": self.observed_duration_ms,
            "duration_tolerance_ms": self.duration_tolerance_ms,
            "cleanup_outcome": self.cleanup_outcome,
            "secondary_stage": self.secondary_stage,
        }


def safe_probe_codec(value: object) -> str:
    """Map an untrusted codec name to a closed diagnostic class."""
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower()
    if normalized in {"h264", "avc1"}:
        return "h264"
    if normalized in {"hevc", "h265", "hev1", "hvc1"}:
        return "hevc"
    if normalized == "mpeg4":
        return "mpeg4"
    if normalized in {"mjpeg", "jpeg"}:
        return "mjpeg"
    return "unknown"


def safe_probe_identity(value: str) -> str:
    """Keep only contract-shaped IDs in the process-local correlation record."""
    return value if _SAFE_ID.fullmatch(value) else "redacted"


def log_media_probe_diagnostic(
    investigation_id: str,
    run_id: str,
    diagnostic: Phase7EMediaProbeDiagnostic,
) -> None:
    """Emit one structured safe record without paths or native subprocess data."""
    payload = {
        "event": "phase7e.media_probe_failure",
        "investigation_id": safe_probe_identity(investigation_id),
        "run_id": safe_probe_identity(run_id),
        "diagnostic": diagnostic.as_dict(),
    }
    _LOGGER.warning("%s", json.dumps(payload, sort_keys=True, separators=(",", ":")))


__all__ = [
    "MEDIA_PROBE_DIAGNOSTIC_VERSION",
    "MEDIA_PROBE_STAGES",
    "Phase7EMediaProbeDiagnostic",
    "log_media_probe_diagnostic",
    "safe_probe_codec",
]
