"""Optional, non-authoritative diagnostics for successor observations."""

# Closed internal record validation uses literal safe error codes and explicit
# runtime narrowing at its filesystem boundary.
# ruff: noqa: BLE001, EM101, PLR0913, TC003

from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

from vigi_vision.object_presence_retention_diagnostics import (
    FOREGROUND_RETENTION_FACT_FIELDS,
    ForegroundRetentionFacts,
)
from vigi_vision.object_presence_support_change_diagnostics import (
    SUPPORT_CHANGE_FACT_FIELDS,
    SupportChangeFacts,
)

_LOGGER = logging.getLogger("uvicorn.error.vigi_vision.phase7e")
_INVESTIGATION_ID = re.compile(r"object-disappearance-v3-ch[1-9][0-9]*-[0-9]{8}T[0-9]{6}Z\Z")
_RUN_ID = re.compile(r"search-run-[0-9a-f]{32}\Z")
_OBSERVATION_ID = re.compile(r"successor-observation-v1-[0-9a-f]{64}\Z")
_SAFE_ID = re.compile(r"[a-z0-9-]{1,160}\Z")
_RESOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,191}\Z")
_UTC_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 4096
_MAX_ALIGNMENT_TRANSLATION = 16
_ALLOWED_ALIGNMENT_ROTATIONS = frozenset({-10, -5, 0, 5, 10})
_VERSION = "phase7e-b4-failure-diagnostic-v1"
_RETENTION_VERSION = "phase7e-foreground-retention-v1"
_RETENTION_KIND = "foreground_retention"
_SUPPORT_CHANGE_VERSION = "phase7e-support-change-v1"
_SUPPORT_CHANGE_KIND = "support_change"
_PERFORMANCE_VERSION = "phase7e-probe-performance-v1"
_PERFORMANCE_KIND = "probe_performance"
_PERFORMANCE_ROLES = frozenset({"anchor", "coarse", "narrowing", "fallback"})
_MAX_PERFORMANCE_MS = 86_400_000
_MAX_PERFORMANCE_COUNT = 1_000_000
_STAGES = frozenset(
    {
        "startup",
        "reference_prepare",
        "probe_inference",
        "ipc_receive",
        "result_validation",
        "process_exit",
        "cleanup",
        "unknown",
    }
)
_ERROR_CODES = frozenset(
    {
        "worker_start_failed",
        "worker_execution_failed",
        "worker_abnormal_exit",
        "malformed_worker_protocol",
        "invalid_classifier_output",
        "classifier_unavailable",
        "classifier_execution_failed",
        "classifier_timeout",
        "classifier_failed",
    }
)
_CLEANUP = frozenset({"completed", "failed"})
_TIMEOUT = frozenset({"startup", "inference"})
_FIELDS = frozenset(
    {
        "version",
        "investigation_id",
        "run_id",
        "plan_id",
        "target_id",
        "observation_id",
        "requested_time_utc",
        "classifier_stage",
        "classifier_error_code",
        "startup_elapsed_ms",
        "inference_elapsed_ms",
        "ipc_result_elapsed_ms",
        "cleanup_elapsed_ms",
        "total_elapsed_ms",
        "timeout_phase",
        "child_started",
        "child_exit_code",
        "result_received",
        "cleanup_status",
    }
)
_RETENTION_FIELDS = (
    frozenset(
        {
            "version",
            "diagnostic_kind",
            "investigation_id",
            "run_id",
            "plan_id",
            "target_id",
            "observation_id",
            "requested_time_utc",
            "reference_identity",
            "reference_frame_resource_id",
            "roi_identity",
            "classifier_policy_identity",
        }
    )
    | FOREGROUND_RETENTION_FACT_FIELDS
)
_SUPPORT_CHANGE_FIELDS = (
    frozenset(
        {
            "version",
            "diagnostic_kind",
            "investigation_id",
            "run_id",
            "plan_id",
            "target_id",
            "observation_id",
            "requested_time_utc",
            "reference_identity",
            "reference_frame_resource_id",
            "baseline_frame_digest",
            "probe_frame_digest",
            "roi_identity",
            "roi_x",
            "roi_y",
            "roi_width",
            "roi_height",
            "classifier_policy_identity",
        }
    )
    | SUPPORT_CHANGE_FACT_FIELDS
)
_PERFORMANCE_DURATION_FIELDS = (
    "replay_total_ms",
    "replay_first_output_ms",
    "replay_process_exit_ms",
    "replay_cleanup_tail_ms",
    "probe_total_ms",
    "classifier_total_ms",
    "request_decoded_ms",
    "child_startup_ms",
    "preprocessing_ms",
    "inference_ms",
    "child_inference_ms",
    "alignment_elapsed_ms",
    "ipc_result_ms",
    "b4_cleanup_ms",
)
_PERFORMANCE_COUNT_FIELDS = (
    "classifier_invocation_count",
    "decoder_calls",
    "segmentation_calls",
    "baseline_segmentation_calls",
    "candidate_segmentation_calls",
    "alignment_comparisons",
    "alignment_translation_candidates",
    "alignment_rotation_candidates",
    "alignment_scale_candidates",
    "duplicate_processing_count",
)
_PERFORMANCE_FIELDS = frozenset(
    {
        "version",
        "diagnostic_kind",
        "investigation_id",
        "run_id",
        "plan_id",
        "target_id",
        "observation_id",
        "requested_time_utc",
        "selected_frame_time_utc",
        "observation_role",
        "reference_identity",
        "reference_frame_resource_id",
        "roi_identity",
        "classifier_policy_identity",
        *_PERFORMANCE_DURATION_FIELDS,
        *_PERFORMANCE_COUNT_FIELDS,
    }
)
_B4_DURATION_MAP = {
    "request_decoded_ms": "request_decoded_ms",
    "startup_ms": "child_startup_ms",
    "preprocessing_ms": "preprocessing_ms",
    "inference_ms": "inference_ms",
    "child_inference_ms": "child_inference_ms",
    "alignment_elapsed_ms": "alignment_elapsed_ms",
    "ipc_result_ms": "ipc_result_ms",
    "cleanup_ms": "b4_cleanup_ms",
}
_B4_COUNT_FIELDS = frozenset(_PERFORMANCE_COUNT_FIELDS) - {"classifier_invocation_count"}


class SuccessorDiagnosticError(ValueError):
    """Optional diagnostic record is invalid or conflicts with an existing record."""


class SuccessorDiagnosticRepository:
    """Create-once sidecars outside strict evidence and terminal manifests."""

    def __init__(self, root: Path) -> None:
        """Use the existing successor artifact root without owning its lifecycle."""
        self.root: Path = root

    def _path(self, investigation_id: str, run_id: str, observation_id: str) -> Path:
        if (
            _INVESTIGATION_ID.fullmatch(investigation_id) is None
            or _RUN_ID.fullmatch(run_id) is None
            or _OBSERVATION_ID.fullmatch(observation_id) is None
        ):
            raise SuccessorDiagnosticError("invalid_identity")
        path = self.root / investigation_id / run_id / "diagnostics" / f"{observation_id}.json"
        for component in (path.parent.parent.parent, path.parent.parent, path.parent, path):
            if component.exists() and _is_reparse(component):
                raise SuccessorDiagnosticError("diagnostic_unsafe_path")
        return path

    def read(
        self, investigation_id: str, run_id: str, observation_id: str
    ) -> dict[str, object] | None:
        """Strictly reopen one optional sidecar; absence is not corruption."""
        path = self._path(investigation_id, run_id, observation_id)
        return self._read_at(
            path,
            lambda value: _valid_record(value, investigation_id, run_id, observation_id),
        )

    def read_retention(
        self, investigation_id: str, run_id: str, observation_id: str
    ) -> dict[str, object] | None:
        """Strictly reopen one optional foreground-retention sidecar."""
        path = self._retention_path(investigation_id, run_id, observation_id)
        return self._read_at(
            path,
            lambda value: _valid_retention_record(value, investigation_id, run_id, observation_id),
        )

    def read_support_change(
        self, investigation_id: str, run_id: str, observation_id: str
    ) -> dict[str, object] | None:
        """Strictly reopen one optional support-change-v1 sidecar."""
        path = self._support_change_path(investigation_id, run_id, observation_id)
        return self._read_at(
            path,
            lambda value: _valid_support_change_record(
                value, investigation_id, run_id, observation_id
            ),
        )

    def read_probe_performance(
        self, investigation_id: str, run_id: str, observation_id: str
    ) -> dict[str, object] | None:
        """Strictly reopen one optional per-observation performance sidecar."""
        path = self._performance_path(investigation_id, run_id, observation_id)
        return self._read_at(
            path,
            lambda value: _valid_performance_record(
                value, investigation_id, run_id, observation_id
            ),
        )

    @staticmethod
    def _read_at(path: Path, validator: Callable[[object], bool]) -> dict[str, object] | None:
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            raise SuccessorDiagnosticError("diagnostic_corrupt")
        try:
            value = cast("object", json.loads(path.read_bytes()))
        except (OSError, ValueError) as error:
            raise SuccessorDiagnosticError("diagnostic_corrupt") from error
        if not validator(value):
            raise SuccessorDiagnosticError("diagnostic_corrupt")
        return cast("dict[str, object]", value)

    def publish(self, record: dict[str, object]) -> None:
        """Atomically link one immutable, bounded record without replacement."""
        investigation_id = record.get("investigation_id")
        run_id = record.get("run_id")
        observation_id = record.get("observation_id")
        if not all(type(value) is str for value in (investigation_id, run_id, observation_id)):
            raise SuccessorDiagnosticError("invalid_identity")
        investigation_id = cast("str", investigation_id)
        run_id = cast("str", run_id)
        observation_id = cast("str", observation_id)
        path = self._path(investigation_id, run_id, observation_id)
        self._publish_at(
            path,
            record,
            lambda value: _valid_record(value, investigation_id, run_id, observation_id),
            lambda: self.read(investigation_id, run_id, observation_id),
            ".b4-",
        )

    def publish_retention(self, record: dict[str, object]) -> None:
        """Create one immutable, bounded foreground-retention sidecar."""
        investigation_id = record.get("investigation_id")
        run_id = record.get("run_id")
        observation_id = record.get("observation_id")
        if not all(type(value) is str for value in (investigation_id, run_id, observation_id)):
            raise SuccessorDiagnosticError("invalid_identity")
        investigation_id = cast("str", investigation_id)
        run_id = cast("str", run_id)
        observation_id = cast("str", observation_id)
        path = self._retention_path(investigation_id, run_id, observation_id)
        self._publish_at(
            path,
            record,
            lambda value: _valid_retention_record(value, investigation_id, run_id, observation_id),
            lambda: self.read_retention(investigation_id, run_id, observation_id),
            ".retention-",
        )

    def publish_support_change(self, record: dict[str, object]) -> None:
        """Create one immutable, bounded support-change-v1 sidecar."""
        investigation_id = record.get("investigation_id")
        run_id = record.get("run_id")
        observation_id = record.get("observation_id")
        if not all(type(value) is str for value in (investigation_id, run_id, observation_id)):
            raise SuccessorDiagnosticError("invalid_identity")
        investigation_id = cast("str", investigation_id)
        run_id = cast("str", run_id)
        observation_id = cast("str", observation_id)
        path = self._support_change_path(investigation_id, run_id, observation_id)
        self._publish_at(
            path,
            record,
            lambda value: _valid_support_change_record(
                value, investigation_id, run_id, observation_id
            ),
            lambda: self.read_support_change(investigation_id, run_id, observation_id),
            ".support-",
        )

    def publish_probe_performance(self, record: dict[str, object]) -> None:
        """Create one immutable, bounded probe-performance-v1 sidecar."""
        investigation_id = record.get("investigation_id")
        run_id = record.get("run_id")
        observation_id = record.get("observation_id")
        if not all(type(value) is str for value in (investigation_id, run_id, observation_id)):
            raise SuccessorDiagnosticError("invalid_identity")
        investigation_id = cast("str", investigation_id)
        run_id = cast("str", run_id)
        observation_id = cast("str", observation_id)
        path = self._performance_path(investigation_id, run_id, observation_id)
        self._publish_at(
            path,
            record,
            lambda value: _valid_performance_record(
                value, investigation_id, run_id, observation_id
            ),
            lambda: self.read_probe_performance(investigation_id, run_id, observation_id),
            ".performance-",
        )

    def _retention_path(self, investigation_id: str, run_id: str, observation_id: str) -> Path:
        b4_path = self._path(investigation_id, run_id, observation_id)
        path = b4_path.parent / "foreground-retention-v1" / f"{observation_id}.json"
        for component in (path.parent, path):
            if component.exists() and _is_reparse(component):
                raise SuccessorDiagnosticError("diagnostic_unsafe_path")
        return path

    def _support_change_path(self, investigation_id: str, run_id: str, observation_id: str) -> Path:
        b4_path = self._path(investigation_id, run_id, observation_id)
        path = b4_path.parent / "support-change-v1" / f"{observation_id}.json"
        for component in (path.parent, path):
            if component.exists() and _is_reparse(component):
                raise SuccessorDiagnosticError("diagnostic_unsafe_path")
        return path

    def _performance_path(self, investigation_id: str, run_id: str, observation_id: str) -> Path:
        b4_path = self._path(investigation_id, run_id, observation_id)
        path = b4_path.parent / "probe-performance-v1" / f"{observation_id}.json"
        for component in (path.parent, path):
            if component.exists() and _is_reparse(component):
                raise SuccessorDiagnosticError("diagnostic_unsafe_path")
        return path

    @staticmethod
    def _publish_at(
        path: Path,
        record: dict[str, object],
        validator: Callable[[object], bool],
        read_existing: Callable[[], dict[str, object] | None],
        temporary_prefix: str,
    ) -> None:
        if not validator(record):
            raise SuccessorDiagnosticError("diagnostic_corrupt")
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
        if len(encoded) > _MAX_BYTES:
            raise SuccessorDiagnosticError("diagnostic_too_large")
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_existing()
        if existing is not None:
            if existing != record:
                raise SuccessorDiagnosticError("diagnostic_conflict")
            return
        fd, temporary = tempfile.mkstemp(prefix=temporary_prefix, suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                _ = handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                _ = os.link(temporary, path)
            except FileExistsError:
                existing = read_existing()
                if existing != record:
                    raise SuccessorDiagnosticError("diagnostic_conflict") from None
        finally:
            Path(temporary).unlink()


def _valid_record(value: object, investigation_id: str, run_id: str, observation_id: str) -> bool:
    if not isinstance(value, dict):
        return False
    record = cast("dict[str, object]", value)
    if set(record) != set(_FIELDS):
        return False
    if (
        record.get("version") != _VERSION
        or record.get("investigation_id") != investigation_id
        or record.get("run_id") != run_id
        or record.get("observation_id") != observation_id
        or not _safe_text(record.get("plan_id"), _SAFE_ID)
        or not _safe_text(record.get("target_id"), _SAFE_ID)
        or not _safe_text(record.get("requested_time_utc"), _UTC_TIME)
        or not _safe_text(record.get("classifier_stage"), _STAGES)
        or not _safe_text(record.get("classifier_error_code"), _ERROR_CODES)
        or not _safe_nullable_text(record.get("cleanup_status"), _CLEANUP)
        or not _safe_nullable_text(record.get("timeout_phase"), _TIMEOUT)
    ):
        return False
    for key in (
        "startup_elapsed_ms",
        "inference_elapsed_ms",
        "ipc_result_elapsed_ms",
        "cleanup_elapsed_ms",
        "total_elapsed_ms",
    ):
        item = record[key]
        if item is not None and (type(item) is not int or item < 0):
            return False
    for key in ("child_started", "result_received"):
        if record[key] is not None and type(record[key]) is not bool:
            return False
    exit_code = record["child_exit_code"]
    return exit_code is None or type(exit_code) is int


def _valid_retention_record(
    value: object, investigation_id: str, run_id: str, observation_id: str
) -> bool:
    if not isinstance(value, dict):
        return False
    record = cast("dict[str, object]", value)
    if (
        frozenset(record) != _RETENTION_FIELDS
        or record.get("version") != _RETENTION_VERSION
        or record.get("diagnostic_kind") != _RETENTION_KIND
        or record.get("investigation_id") != investigation_id
        or record.get("run_id") != run_id
        or record.get("observation_id") != observation_id
        or not _safe_text(record.get("plan_id"), _SAFE_ID)
        or not _safe_text(record.get("target_id"), _SAFE_ID)
        or not _safe_text(record.get("requested_time_utc"), _UTC_TIME)
        or not _safe_text(record.get("reference_identity"), _SAFE_ID)
        or not _safe_text(record.get("reference_frame_resource_id"), _RESOURCE_ID)
        or not _safe_text(record.get("roi_identity"), _SAFE_ID)
        or not _safe_text(record.get("classifier_policy_identity"), _SAFE_ID)
    ):
        return False
    facts = {key: record[key] for key in FOREGROUND_RETENTION_FACT_FIELDS}
    return ForegroundRetentionFacts.from_payload(facts) is not None


def _valid_support_change_record(
    value: object, investigation_id: str, run_id: str, observation_id: str
) -> bool:
    if not isinstance(value, dict):
        return False
    record = cast("dict[str, object]", value)
    if (
        frozenset(record) != _SUPPORT_CHANGE_FIELDS
        or record.get("version") != _SUPPORT_CHANGE_VERSION
        or record.get("diagnostic_kind") != _SUPPORT_CHANGE_KIND
        or record.get("investigation_id") != investigation_id
        or record.get("run_id") != run_id
        or record.get("observation_id") != observation_id
        or not _safe_text(record.get("plan_id"), _SAFE_ID)
        or not _safe_text(record.get("target_id"), _SAFE_ID)
        or not _safe_text(record.get("requested_time_utc"), _UTC_TIME)
        or not _safe_text(record.get("reference_identity"), _SAFE_ID)
        or not _safe_text(record.get("reference_frame_resource_id"), _RESOURCE_ID)
        or not _safe_text(record.get("baseline_frame_digest"), _SHA256)
        or not _safe_text(record.get("probe_frame_digest"), _SHA256)
        or not _safe_text(record.get("roi_identity"), _SAFE_ID)
        or not _safe_text(record.get("classifier_policy_identity"), _SAFE_ID)
    ):
        return False
    roi = tuple(record[key] for key in ("roi_x", "roi_y", "roi_width", "roi_height"))
    if (
        any(type(item) is not int for item in roi)
        or cast("int", roi[0]) < 0
        or cast("int", roi[1]) < 0
        or cast("int", roi[2]) <= 0
        or cast("int", roi[3]) <= 0
    ):
        return False
    facts = SupportChangeFacts.from_payload(
        {key: record[key] for key in SUPPORT_CHANGE_FACT_FIELDS}
    )
    if facts is None:
        return False
    roi_area = cast("int", roi[2]) * cast("int", roi[3])
    return (
        cast("int", roi[0]) + cast("int", roi[2]) <= facts.baseline_mask_width
        and cast("int", roi[1]) + cast("int", roi[3]) <= facts.baseline_mask_height
        and facts.baseline_background_pixel_count <= roi_area
        and facts.probe_background_pixel_count <= roi_area
        and abs(facts.alignment_dx) <= _MAX_ALIGNMENT_TRANSLATION
        and abs(facts.alignment_dy) <= _MAX_ALIGNMENT_TRANSLATION
        and facts.alignment_rotation_degrees in _ALLOWED_ALIGNMENT_ROTATIONS
    )


def _valid_performance_record(
    value: object, investigation_id: str, run_id: str, observation_id: str
) -> bool:
    if not isinstance(value, dict):
        return False
    record = cast("dict[str, object]", value)
    identity_is_valid = (
        frozenset(record) == _PERFORMANCE_FIELDS
        and record.get("version") == _PERFORMANCE_VERSION
        and record.get("diagnostic_kind") == _PERFORMANCE_KIND
        and record.get("investigation_id") == investigation_id
        and record.get("run_id") == run_id
        and record.get("observation_id") == observation_id
        and _safe_text(record.get("plan_id"), _SAFE_ID)
        and _safe_text(record.get("target_id"), _SAFE_ID)
        and _safe_text(record.get("requested_time_utc"), _UTC_TIME)
        and (
            record.get("selected_frame_time_utc") is None
            or _safe_text(record.get("selected_frame_time_utc"), _UTC_TIME)
        )
        and _safe_text(record.get("observation_role"), _PERFORMANCE_ROLES)
        and _safe_text(record.get("reference_identity"), _SAFE_ID)
        and _safe_text(record.get("reference_frame_resource_id"), _RESOURCE_ID)
        and _safe_text(record.get("roi_identity"), _SAFE_ID)
        and _safe_text(record.get("classifier_policy_identity"), _SAFE_ID)
    )
    if not identity_is_valid:
        return False
    return _valid_performance_timing_values(record)


def _valid_performance_timing_values(record: dict[str, object]) -> bool:
    """Validate bounded integers and replay timing arithmetic."""
    duration_values_valid = all(
        item is None or (type(item) is int and 0 <= item <= _MAX_PERFORMANCE_MS)
        for item in (record[key] for key in _PERFORMANCE_DURATION_FIELDS)
    )
    count_values_valid = all(
        item is None or (type(item) is int and 0 <= item <= _MAX_PERFORMANCE_COUNT)
        for item in (record[key] for key in _PERFORMANCE_COUNT_FIELDS)
    )
    probe_total = record.get("probe_total_ms")
    if (
        type(probe_total) is not int
        or probe_total > _MAX_PERFORMANCE_MS
        or not duration_values_valid
        or not count_values_valid
    ):
        return False
    replay_total = record["replay_total_ms"]
    replay_first = record["replay_first_output_ms"]
    replay_exit = record["replay_process_exit_ms"]
    replay_tail = record["replay_cleanup_tail_ms"]
    return not (
        (type(replay_total) is int and type(replay_first) is int and replay_first > replay_total)
        or (type(replay_total) is int and type(replay_exit) is int and replay_exit > replay_total)
        or ((replay_total is None or replay_exit is None) and replay_tail is not None)
        or (
            type(replay_total) is int
            and type(replay_exit) is int
            and (type(replay_tail) is not int or replay_tail != replay_total - replay_exit)
        )
    )


def _safe_text(value: object, allowed: re.Pattern[str] | frozenset[str]) -> bool:
    if type(value) is not str:
        return False
    return (
        allowed.fullmatch(value) is not None
        if isinstance(allowed, re.Pattern)
        else value in allowed
    )


def _safe_nullable_text(value: object, allowed: frozenset[str]) -> bool:
    return value is None or _safe_text(value, allowed)


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


_ACTIVE: ContextVar[tuple[SuccessorDiagnosticRepository, str, str] | None] = ContextVar(
    "successor_b4_diagnostic_scope", default=None
)


@contextmanager
def diagnostic_scope(
    repository: SuccessorDiagnosticRepository, investigation_id: str, run_id: str
) -> Generator[None, None, None]:
    """Bind one synchronous execution without sharing identity across workers."""
    token = _ACTIVE.set((repository, investigation_id, run_id))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def persist_failure(
    *,
    plan_id: str,
    target_id: str,
    observation_id: str,
    requested_time_utc: str,
    total_elapsed_ms: int | None,
    public_reason: str,
    event: Mapping[str, object] | None,
) -> None:
    """Best-effort, allowlist-only persistence; never changes search authority."""
    scope = _ACTIVE.get()
    if scope is None:
        return
    try:
        repository, investigation_id, run_id = scope
        facts = event or {}
        stage = facts.get("failure_phase")
        code = facts.get("error_code")
        timeout_stage = facts.get("timeout_stage")
        cleanup_status = facts.get("cleanup_status")
        record: dict[str, object] = {
            "version": _VERSION,
            "investigation_id": investigation_id,
            "run_id": run_id,
            "plan_id": plan_id,
            "target_id": target_id,
            "observation_id": observation_id,
            "requested_time_utc": requested_time_utc,
            "classifier_stage": stage if _safe_text(stage, _STAGES) else "unknown",
            "classifier_error_code": code if _safe_text(code, _ERROR_CODES) else public_reason,
            "startup_elapsed_ms": _nonnegative_int(facts.get("startup_ms")),
            "inference_elapsed_ms": _nonnegative_int(facts.get("inference_ms")),
            "ipc_result_elapsed_ms": _nonnegative_int(facts.get("ipc_result_ms")),
            "cleanup_elapsed_ms": _nonnegative_int(facts.get("cleanup_ms")),
            "total_elapsed_ms": _nonnegative_int(total_elapsed_ms),
            "timeout_phase": timeout_stage if _safe_text(timeout_stage, _TIMEOUT) else None,
            "child_started": facts.get("child_started")
            if type(facts.get("child_started")) is bool
            else None,
            "child_exit_code": facts.get("child_exit_code")
            if type(facts.get("child_exit_code")) is int
            else None,
            "result_received": facts.get("result_received")
            if type(facts.get("result_received")) is bool
            else None,
            "cleanup_status": cleanup_status if _safe_text(cleanup_status, _CLEANUP) else None,
        }
        repository.publish(record)
    except Exception:
        with suppress(Exception):
            _LOGGER.warning(
                "%s %s",
                "phase7e.classifier_diagnostic",
                "stage=persist_failed error_code=diagnostic_unavailable",
            )


def persist_foreground_retention(
    *,
    plan_id: str,
    target_id: str,
    observation_id: str,
    requested_time_utc: str,
    reference_identity: str,
    reference_frame_resource_id: str,
    roi_identity: str,
    classifier_policy_identity: str,
    facts: ForegroundRetentionFacts | None,
) -> None:
    """Best-effort sidecar publication; never changes the classified result."""
    scope = _ACTIVE.get()
    if scope is None or facts is None:
        return
    try:
        repository, investigation_id, run_id = scope
        record: dict[str, object] = {
            "version": _RETENTION_VERSION,
            "diagnostic_kind": _RETENTION_KIND,
            "investigation_id": investigation_id,
            "run_id": run_id,
            "plan_id": plan_id,
            "target_id": target_id,
            "observation_id": observation_id,
            "requested_time_utc": requested_time_utc,
            "reference_identity": reference_identity,
            "reference_frame_resource_id": reference_frame_resource_id,
            "roi_identity": roi_identity,
            "classifier_policy_identity": classifier_policy_identity,
            **facts.to_payload(),
        }
        repository.publish_retention(record)
    except Exception:
        with suppress(Exception):
            _LOGGER.warning(
                "%s %s",
                "phase7e.foreground_retention_diagnostic",
                "stage=persist_failed error_code=diagnostic_unavailable",
            )


def persist_support_change(
    *,
    plan_id: str,
    target_id: str,
    observation_id: str,
    requested_time_utc: str,
    reference_identity: str,
    reference_frame_resource_id: str,
    baseline_frame_digest: str,
    probe_frame_digest: str,
    roi_identity: str,
    roi_x: int,
    roi_y: int,
    roi_width: int,
    roi_height: int,
    classifier_policy_identity: str,
    facts: SupportChangeFacts | None,
) -> None:
    """Best-effort support-change publication, isolated from search authority."""
    scope = _ACTIVE.get()
    if scope is None or facts is None:
        return
    try:
        repository, investigation_id, run_id = scope
        record: dict[str, object] = {
            "version": _SUPPORT_CHANGE_VERSION,
            "diagnostic_kind": _SUPPORT_CHANGE_KIND,
            "investigation_id": investigation_id,
            "run_id": run_id,
            "plan_id": plan_id,
            "target_id": target_id,
            "observation_id": observation_id,
            "requested_time_utc": requested_time_utc,
            "reference_identity": reference_identity,
            "reference_frame_resource_id": reference_frame_resource_id,
            "baseline_frame_digest": baseline_frame_digest,
            "probe_frame_digest": probe_frame_digest,
            "roi_identity": roi_identity,
            "roi_x": roi_x,
            "roi_y": roi_y,
            "roi_width": roi_width,
            "roi_height": roi_height,
            "classifier_policy_identity": classifier_policy_identity,
            **facts.to_payload(),
        }
        repository.publish_support_change(record)
    except Exception:
        with suppress(Exception):
            _LOGGER.warning(
                "%s %s",
                "phase7e.support_change_diagnostic",
                "stage=persist_failed error_code=diagnostic_unavailable",
            )


def _nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


@dataclass(slots=True)
class ProbePerformanceCapture:
    """Process-local facts for one probe, never authoritative search state."""

    plan_id: str
    target_id: str
    requested_time_utc: str
    observation_role: str
    started_at: float
    clock: Callable[[], float] = field(repr=False)
    durations_ms: dict[str, int | None] = field(default_factory=dict)
    counts: dict[str, int | None] = field(default_factory=dict)
    invalid: bool = False

    def record_replay_progress(
        self, stage: str, elapsed_ms: object, *, exit_code: object = None
    ) -> None:
        """Observe existing replay lifecycle events without controlling them."""
        if type(elapsed_ms) is not int or not 0 <= elapsed_ms <= _MAX_PERFORMANCE_MS:
            self.invalid = True
            return
        key: str | None = None
        if stage == "first_output":
            key = "replay_first_output_ms"
        elif stage == "process_exited" and type(exit_code) is int:
            key = "replay_process_exit_ms"
        elif stage == "cleanup_completed":
            key = "replay_total_ms"
        if key is None:
            return
        if self.durations_ms.get(key) is not None:
            self.invalid = True
            return
        self.durations_ms[key] = elapsed_ms

    def record_classifier_timing(self, event: object) -> None:
        """Aggregate fixed-shape facts from the existing B4 timing sink."""
        if not isinstance(event, dict):
            self.invalid = True
            return
        facts = cast("dict[str, object]", event)
        if facts.get("event") != "phase7e.classifier_timing":
            self.invalid = True
            return
        for source, destination in _B4_DURATION_MAP.items():
            if source not in facts:
                continue
            self._sum_fact(self.durations_ms, destination, facts[source], _MAX_PERFORMANCE_MS)
        for name in _B4_COUNT_FIELDS:
            if name in facts:
                self._sum_fact(self.counts, name, facts[name], _MAX_PERFORMANCE_COUNT)

    def record_classifier_total(self, elapsed_ms: object) -> None:
        """Accumulate the classifier adapter's measured wall durations."""
        current = self.durations_ms.get("classifier_total_ms")
        if current is None:
            self.durations_ms["classifier_total_ms"] = 0
        self._sum_fact(
            self.durations_ms,
            "classifier_total_ms",
            elapsed_ms,
            _MAX_PERFORMANCE_MS,
        )
        current_count = self.counts.get("classifier_invocation_count")
        if current_count is None:
            self.counts["classifier_invocation_count"] = 0
        self._sum_fact(
            self.counts,
            "classifier_invocation_count",
            1,
            _MAX_PERFORMANCE_COUNT,
        )

    def _sum_fact(
        self, destination: dict[str, int | None], key: str, value: object, maximum: int
    ) -> None:
        if type(value) is not int or not 0 <= value <= maximum:
            destination[key] = None
            self.invalid = True
            return
        previous = destination.get(key)
        total = value if previous is None else previous + value
        if total > maximum:
            self.invalid = True
            destination[key] = None
        else:
            destination[key] = total

    def complete(self) -> None:
        """Finalize durations from the injected monotonic clock."""
        try:
            ended_at = self.clock()
            elapsed = (ended_at - self.started_at) * 1000
            if not math.isfinite(elapsed) or elapsed < 0 or elapsed > _MAX_PERFORMANCE_MS:
                self.invalid = True
                return
            self.durations_ms["probe_total_ms"] = round(elapsed)
            process_exit = self.durations_ms.get("replay_process_exit_ms")
            replay_total = self.durations_ms.get("replay_total_ms")
            if process_exit is not None and replay_total is not None:
                if process_exit > replay_total:
                    self.invalid = True
                else:
                    self.durations_ms["replay_cleanup_tail_ms"] = replay_total - process_exit
        except Exception:
            self.invalid = True

    def matches(self, plan_id: str, target_id: str, requested_time_utc: str) -> bool:
        """Match only stable pre-publication target identity, never a provisional ID."""
        return (
            not self.invalid
            and self.plan_id == plan_id
            and self.target_id == target_id
            and self.requested_time_utc == requested_time_utc
            and type(self.durations_ms.get("probe_total_ms")) is int
        )

    def record(
        self,
        *,
        investigation_id: str,
        run_id: str,
        observation_id: str,
        selected_frame_time_utc: str | None,
        reference_identity: str,
        reference_frame_resource_id: str,
        roi_identity: str,
        classifier_policy_identity: str,
    ) -> dict[str, object]:
        """Build the final-ID record after its authoritative evidence commits."""
        durations = {name: self.durations_ms.get(name) for name in _PERFORMANCE_DURATION_FIELDS}
        counts = {name: self.counts.get(name) for name in _PERFORMANCE_COUNT_FIELDS}
        return {
            "version": _PERFORMANCE_VERSION,
            "diagnostic_kind": _PERFORMANCE_KIND,
            "investigation_id": investigation_id,
            "run_id": run_id,
            "plan_id": self.plan_id,
            "target_id": self.target_id,
            "observation_id": observation_id,
            "requested_time_utc": self.requested_time_utc,
            "selected_frame_time_utc": selected_frame_time_utc,
            "observation_role": self.observation_role,
            "reference_identity": reference_identity,
            "reference_frame_resource_id": reference_frame_resource_id,
            "roi_identity": roi_identity,
            "classifier_policy_identity": classifier_policy_identity,
            **durations,
            **counts,
        }


_PERFORMANCE_RUN: ContextVar[list[ProbePerformanceCapture] | None] = ContextVar(
    "successor_probe_performance_run", default=None
)
_ACTIVE_PROBE: ContextVar[ProbePerformanceCapture | None] = ContextVar(
    "successor_probe_performance_capture", default=None
)


@contextmanager
def performance_run_scope() -> Generator[None, None, None]:
    """Collect optional in-memory facts for one synchronous successor run."""
    token = _PERFORMANCE_RUN.set([])
    try:
        yield
    finally:
        _PERFORMANCE_RUN.reset(token)


def completed_probe_performance_captures() -> tuple[ProbePerformanceCapture, ...]:
    """Return the active run's completed, still-unbound process-local captures."""
    captures = _PERFORMANCE_RUN.get()
    return () if captures is None else tuple(captures)


def current_probe_performance_capture() -> ProbePerformanceCapture | None:
    """Return the current probe capture for existing lifecycle callbacks."""
    return _ACTIVE_PROBE.get()


@contextmanager
def probe_performance_scope(
    *,
    plan_id: str,
    target_id: str,
    requested_time_utc: datetime,
    observation_role: str,
    clock: Callable[[], float] = monotonic,
) -> Generator[ProbePerformanceCapture | None, None, None]:
    """Measure one acquisition/classification interval without owning it."""
    active_run = _PERFORMANCE_RUN.get()
    capture: ProbePerformanceCapture | None = None
    if active_run is not None and observation_role in _PERFORMANCE_ROLES:
        try:
            started_at = clock()
            if math.isfinite(started_at):
                capture = ProbePerformanceCapture(
                    plan_id,
                    target_id,
                    requested_time_utc.astimezone(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                    observation_role,
                    float(started_at),
                    clock,
                )
        except Exception:
            capture = None
    token = _ACTIVE_PROBE.set(capture)
    try:
        yield capture
    finally:
        _ACTIVE_PROBE.reset(token)
        if capture is not None:
            capture.complete()
            if not capture.invalid and active_run is not None:
                active_run.append(capture)


def record_replay_progress(
    stage: str,
    elapsed_ms: int,
    *,
    exit_code: int | None = None,
    capture: ProbePerformanceCapture | None = None,
) -> None:
    """Best-effort observer for existing replay lifecycle events."""
    active = capture if capture is not None else _ACTIVE_PROBE.get()
    if active is None:
        return
    try:
        active.record_replay_progress(stage, elapsed_ms, exit_code=exit_code)
    except Exception:
        active.invalid = True


def record_classifier_timing(event: object) -> None:
    """Best-effort observer for the existing B4 timing callback."""
    capture = _ACTIVE_PROBE.get()
    if capture is None:
        return
    try:
        capture.record_classifier_timing(event)
    except Exception:
        capture.invalid = True


def record_classifier_total(elapsed_ms: int) -> None:
    """Record one existing classifier-adapter elapsed result for the active probe."""
    capture = _ACTIVE_PROBE.get()
    if capture is None:
        return
    try:
        capture.record_classifier_total(elapsed_ms)
    except Exception:
        capture.invalid = True


def persist_probe_performance(record: dict[str, object]) -> None:
    """Best-effort publication under the final observation's diagnostic path."""
    scope = _ACTIVE.get()
    if scope is None:
        return
    try:
        repository, _, _ = scope
        repository.publish_probe_performance(record)
    except Exception:
        with suppress(Exception):
            _LOGGER.warning(
                "%s %s",
                "phase7e.probe_performance_diagnostic",
                "stage=persist_failed error_code=diagnostic_unavailable",
            )
