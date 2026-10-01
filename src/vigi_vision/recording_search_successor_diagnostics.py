"""Optional, non-authoritative B4 failure facts for successor observations."""

# Closed internal record validation uses literal safe error codes and explicit
# runtime narrowing at its filesystem boundary.
# ruff: noqa: BLE001, EM101, PLR0913, TC003

from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
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
