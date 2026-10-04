"""Bounded, non-authoritative diagnostics for reference-frame NVR acquisition."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager, suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from secrets import token_hex
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, final

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


NvrAcquisitionErrorKind = Literal[
    "authentication",
    "tls_verification",
    "timeout",
    "connection_refused",
    "host_resolution",
    "sdk_request",
    "unexpected",
]
_SAFE_NVR_ERROR_KINDS: tuple[NvrAcquisitionErrorKind, ...] = (
    "authentication",
    "tls_verification",
    "timeout",
    "connection_refused",
    "host_resolution",
    "sdk_request",
    "unexpected",
)

_DIAGNOSTIC_KIND = "phase-reference-frame-nvr-acquisition-v1"
MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORD_BYTES = 2_048
MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORDS = 128
_DIAGNOSTIC_FILENAME = re.compile(r"^[0-9a-f]{32}\.json$")
_SAFE_EXCEPTION_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


class NvrAcquisitionDiagnosticStage(str, Enum):
    """Closed production-stage vocabulary for candidate acquisition failures."""

    CHANNEL_REFRESH = "channel_refresh"
    RECORDING_FREE_PROCESS = "recording_free_process"
    RECORDING_DAYS = "recording_days"
    RECORDING_SEARCH_RESULTS = "recording_search_results"
    RECORDING_RESPONSE_PARSE = "recording_response_parse"
    RECORDING_SEGMENT_SELECTION = "recording_segment_selection"
    REPLAY_URL_BUILD = "replay_url_build"
    UNKNOWN_NVR_REQUEST = "unknown_nvr_request"


class NvrAcquisitionDiagnosticOperation(str, Enum):
    """Closed operation vocabulary; values never contain request data."""

    CHANNELS = "sdk_nvr_gateway.channels"
    CHANNELS_PARSE = "sdk_nvr_gateway.channels.parse"
    GET_FREE_PROCESS = "records.get_free_process"
    LIST_DAYS = "records.list_days"
    PARSE_DAYS = "recording_days.parse"
    LIST_RESULTS = "records.list_results"
    PARSE_RESULTS = "recording_results.parse"
    SELECT_SEGMENT = "recording_segments.select_covering"
    BUILD_REPLAY_URL = "stream.build_replay_url"
    UNKNOWN_NVR_REQUEST = "unknown_nvr_request"


class NvrAcquisitionDiagnosticDocument(TypedDict):
    """The exact persisted sidecar schema; raw exceptions have no field."""

    schema_version: int
    diagnostic_kind: str
    diagnostic_id: str
    channel_id: int
    requested_time_utc: str
    candidate_offset_seconds: int
    candidate_time_utc: str
    timestamp_utc: str
    stage: str
    operation: str
    sanitized_error_kind: NvrAcquisitionErrorKind
    exception_class_name: str


@final
@dataclass(frozen=True, slots=True)
class NvrAcquisitionDiagnostic:
    """One closed, credential-free failure record for a single candidate."""

    diagnostic_id: str
    channel_id: int
    requested_time_utc: datetime
    candidate_offset_seconds: int
    candidate_time_utc: datetime
    timestamp_utc: datetime
    stage: NvrAcquisitionDiagnosticStage
    operation: NvrAcquisitionDiagnosticOperation
    sanitized_error_kind: NvrAcquisitionErrorKind
    exception_class_name: str

    def document(self) -> NvrAcquisitionDiagnosticDocument:
        """Return only the fixed allowlisted fields used by the sidecar writer."""
        return {
            "schema_version": 1,
            "diagnostic_kind": _DIAGNOSTIC_KIND,
            "diagnostic_id": self.diagnostic_id,
            "channel_id": self.channel_id,
            "requested_time_utc": _utc_text(self.requested_time_utc),
            "candidate_offset_seconds": self.candidate_offset_seconds,
            "candidate_time_utc": _utc_text(self.candidate_time_utc),
            "timestamp_utc": _utc_text(self.timestamp_utc),
            "stage": self.stage.value,
            "operation": self.operation.value,
            "sanitized_error_kind": self.sanitized_error_kind,
            "exception_class_name": self.exception_class_name,
        }


@dataclass(slots=True)
class NvrAcquisitionDiagnosticCapture:
    """Candidate identity plus the first precise acquisition failure, if one occurs."""

    diagnostic_id: str
    channel_id: int
    requested_time_utc: datetime
    candidate_offset_seconds: int
    candidate_time_utc: datetime
    diagnostic: NvrAcquisitionDiagnostic | None = None


_ACTIVE_CAPTURE: ContextVar[NvrAcquisitionDiagnosticCapture | None] = ContextVar(
    "reference_frame_nvr_acquisition_capture", default=None
)


class NvrAcquisitionDiagnosticWriter(Protocol):
    """Narrow persistence boundary so write failures can be isolated in tests."""

    def write(self, diagnostic: NvrAcquisitionDiagnostic) -> None:
        """Persist one bounded candidate sidecar without changing its public result."""
        ...


@final
@dataclass(frozen=True, slots=True)
class NvrAcquisitionDiagnosticStore:
    """Keep at most 128 bounded JSON sidecars in a dedicated diagnostics directory."""

    root: Path = field(repr=False)

    def write(self, diagnostic: NvrAcquisitionDiagnostic) -> None:
        """Publish one immutable JSON record and prune only for a new diagnostic ID."""
        if not _DIAGNOSTIC_FILENAME.fullmatch(f"{diagnostic.diagnostic_id}.json"):
            raise ValueError
        payload = json.dumps(diagnostic.document(), ensure_ascii=True, indent=2) + "\n"
        payload_bytes = payload.encode("utf-8")
        if len(payload_bytes) > MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORD_BYTES:
            raise ValueError

        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise OSError
        destination = self.root / f"{diagnostic.diagnostic_id}.json"
        existing_payload = _existing_diagnostic_payload(destination)
        if existing_payload == payload_bytes:
            return
        if existing_payload is not None:
            # Conflicting or corrupt evidence is immutable; skip this publication.
            return

        self._prune_older_records()
        try:
            with destination.open("x", encoding="utf-8", newline="\n") as stream:
                _ = stream.write(payload)
        except FileExistsError:
            # Another writer may have published this ID after the first check.
            # Exclusive creation prevents replacement; an existing record wins.
            if _existing_diagnostic_payload(destination) is not None:
                return
            raise

    def _prune_older_records(self) -> None:
        records = _owned_diagnostic_files(self.root)
        excess = len(records) - MAX_NVR_ACQUISITION_DIAGNOSTIC_RECORDS + 1
        if excess <= 0:
            return
        ordered = sorted(records, key=lambda path: (path.stat().st_mtime_ns, path.name))
        for path in ordered[:excess]:
            path.unlink()


@contextmanager
def candidate_nvr_acquisition_scope(
    *,
    channel_id: int,
    requested_time_utc: datetime,
    candidate_offset_seconds: int,
    candidate_time_utc: datetime,
) -> Generator[NvrAcquisitionDiagnosticCapture | None, None, None]:
    """Set candidate identity for exact inner NVR boundaries, failing open on diagnostics."""
    capture: NvrAcquisitionDiagnosticCapture | None = None
    token: Token[NvrAcquisitionDiagnosticCapture | None] | None = None
    try:
        capture = NvrAcquisitionDiagnosticCapture(
            diagnostic_id=token_hex(16),
            channel_id=channel_id,
            requested_time_utc=requested_time_utc,
            candidate_offset_seconds=candidate_offset_seconds,
            candidate_time_utc=candidate_time_utc,
        )
        token = _ACTIVE_CAPTURE.set(capture)
    except Exception:  # noqa: BLE001 - diagnostic setup is optional.
        capture = None
    try:
        yield capture
    finally:
        if token is not None:
            with suppress(Exception):
                _ACTIVE_CAPTURE.reset(token)


def record_nvr_acquisition_failure(
    *,
    stage: NvrAcquisitionDiagnosticStage,
    operation: NvrAcquisitionDiagnosticOperation,
    error_kind: Enum,
    exception_class_name: str,
) -> None:
    """Capture the first safe failure for the active candidate without raising."""
    try:
        capture = _ACTIVE_CAPTURE.get()
        if capture is None or capture.diagnostic is not None:
            return
        sanitized_error_kind = error_kind.name.lower()
        if sanitized_error_kind not in _SAFE_NVR_ERROR_KINDS:
            return
        if not _SAFE_EXCEPTION_CLASS.fullmatch(exception_class_name):
            exception_class_name = "UnknownException"
        capture.diagnostic = NvrAcquisitionDiagnostic(
            diagnostic_id=capture.diagnostic_id,
            channel_id=capture.channel_id,
            requested_time_utc=capture.requested_time_utc,
            candidate_offset_seconds=capture.candidate_offset_seconds,
            candidate_time_utc=capture.candidate_time_utc,
            timestamp_utc=datetime.now(timezone.utc),
            stage=stage,
            operation=operation,
            sanitized_error_kind=sanitized_error_kind,
            exception_class_name=exception_class_name,
        )
    except Exception:  # noqa: BLE001 - diagnostic construction is non-authoritative.
        return


def _owned_diagnostic_files(root: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in root.iterdir()
        if _DIAGNOSTIC_FILENAME.fullmatch(path.name) and not path.is_symlink() and path.is_file()
    )


def _existing_diagnostic_payload(destination: Path) -> bytes | None:
    if destination.is_symlink():
        return b""
    try:
        return destination.read_bytes()
    except FileNotFoundError:
        if destination.is_symlink():
            return b""
        return None


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
