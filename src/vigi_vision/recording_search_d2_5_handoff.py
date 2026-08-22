"""Strict, immutable Phase 8 request handoff for a reopened FOUND result."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import ClassVar, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from vigi_vision.durable_io import (
    DurableJsonError,
    is_safe_contained_path,
    load_durable_json_object,
    parse_canonical_utc,
)
from vigi_vision.recording_search_d2_terminal_models import FoundResult, TerminalResultKind
from vigi_vision.recording_search_models import (
    Phase8HandoffStatus,
    RecordingSearchError,
)

_REQUEST_FILE: Final = "phase8-request.json"
_ID_PREFIX: Final = "phase8-handoff-v1-"


class Phase8HandoffConflictError(RecordingSearchError):
    """A different immutable request already exists for the run."""


class Phase8HandoffCorruptError(RecordingSearchError):
    """An existing Phase 8 request failed strict readback."""


class Phase8HandoffArtifactError(RecordingSearchError):
    """The request could not be stored within the owned run directory."""


class Phase8HandoffNotApplicableError(RecordingSearchError):
    """The reopened terminal result is not eligible for Phase 8."""


class Phase8HandoffOutcome(str, Enum):
    """Safe disposition of a Phase 8 request attempt."""

    CREATED = "created"
    REUSED = "reused"


class Phase8HandoffRequestV1(BaseModel):
    """Closed durable request contract consumed by the future Phase 8 boundary."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    handoff_request_id: StrictStr = Field(pattern=r"^phase8-handoff-v1-[0-9a-f]{64}$")
    terminal_result_id: StrictStr = Field(pattern=r"^recording-search-result-v1-[0-9a-f]{64}$")
    investigation_id: StrictStr = Field(min_length=1, max_length=128)
    search_run_id: StrictStr = Field(min_length=1, max_length=128)
    channel_id: StrictInt = Field(gt=0)
    source_timezone: StrictStr = Field(min_length=1, max_length=64)
    lower_bound_requested_time_utc: datetime
    upper_bound_requested_time_utc: datetime
    review_anchor_utc: datetime
    nominal_review_start_utc: datetime
    nominal_review_end_utc: datetime
    lower_bound_observation_id: StrictStr = Field(min_length=1, max_length=192)
    upper_support_observation_ids: tuple[StrictStr, ...] = Field(min_length=1, max_length=16)
    phase6_confirmation_id: StrictStr = Field(min_length=1, max_length=192)
    baseline_observation_id: StrictStr = Field(min_length=1, max_length=192)
    created_at_utc: datetime

    @model_validator(mode="after")
    def validate_contract(self) -> Phase8HandoffRequestV1:
        """Require whole-second UTC timestamps and a non-empty requested interval."""
        timestamps = (
            self.lower_bound_requested_time_utc,
            self.upper_bound_requested_time_utc,
            self.review_anchor_utc,
            self.nominal_review_start_utc,
            self.nominal_review_end_utc,
            self.created_at_utc,
        )
        if any(not _is_whole_utc(value) for value in timestamps):
            raise ValueError
        if self.lower_bound_requested_time_utc >= self.upper_bound_requested_time_utc:
            raise ValueError
        if self.review_anchor_utc != self.upper_bound_requested_time_utc:
            raise ValueError
        if self.nominal_review_start_utc >= self.nominal_review_end_utc:
            raise ValueError
        if len(set(self.upper_support_observation_ids)) != len(self.upper_support_observation_ids):
            raise ValueError
        return self


@dataclass(frozen=True, slots=True)
class Phase8HandoffResult:
    """Immutable request and whether it was created or exactly reused."""

    request: Phase8HandoffRequestV1
    outcome: Phase8HandoffOutcome


def build_phase8_handoff_request(  # noqa: PLR0913 - contract fields are explicit.
    result: FoundResult,
    *,
    channel_id: int,
    source_timezone: str,
    search_start_utc: datetime,
    search_end_utc: datetime,
    created_at_utc: datetime,
) -> Phase8HandoffRequestV1:
    """Construct the deterministic request from a strict FOUND result."""
    if type(result) is not FoundResult or result.result_kind is not TerminalResultKind.FOUND:
        raise ValueError
    lower = _parse_whole(result.lower_bound_requested_time_utc)
    upper = _parse_whole(result.upper_bound_requested_time_utc)
    start = _parse_whole(search_start_utc)
    end = _parse_whole(search_end_utc)
    created = _parse_whole(created_at_utc)
    if not start <= lower < upper <= end:
        raise ValueError
    nominal_start = max(start, upper - timedelta(seconds=10))
    nominal_end = min(end, upper + timedelta(seconds=30))
    lower_observation_id = result.lower_reference.observation_id
    support_ids_raw = tuple(item.observation_id for item in result.upper_support)
    if (
        not isinstance(lower_observation_id, str)
        or not lower_observation_id
        or len(support_ids_raw) == 0
        or not all(isinstance(value, str) and value for value in support_ids_raw)
    ):
        raise ValueError
    support_ids = tuple(cast("str", value) for value in support_ids_raw)
    draft = Phase8HandoffRequestV1(
        handoff_request_id=f"{_ID_PREFIX}{'0' * 64}",
        terminal_result_id=result.result_id,
        investigation_id=result.investigation_id,
        search_run_id=result.search_run_id,
        channel_id=channel_id,
        source_timezone=source_timezone,
        lower_bound_requested_time_utc=lower,
        upper_bound_requested_time_utc=upper,
        review_anchor_utc=upper,
        nominal_review_start_utc=nominal_start,
        nominal_review_end_utc=nominal_end,
        lower_bound_observation_id=lower_observation_id,
        upper_support_observation_ids=support_ids,
        phase6_confirmation_id=result.phase6_confirmation_id,
        baseline_observation_id=result.baseline_observation_id,
        created_at_utc=created,
    )
    return _with_request_id(draft)


def canonical_phase8_handoff_json(request: Phase8HandoffRequestV1) -> str:
    """Serialize the allowlisted identity payload with compact canonical JSON."""
    payload = cast(
        "dict[str, object]",
        request.model_dump(mode="python", exclude={"handoff_request_id", "created_at_utc"}),
    )
    payload = {
        key: _timestamp_text(value) if isinstance(value, datetime) else value
        for key, value in payload.items()
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def create_or_reuse_phase8_request(  # noqa: C901 - atomic retry branches are contract cases.
    root: Path, run_path: Path, request: Phase8HandoffRequestV1
) -> Phase8HandoffResult:
    """Atomically create or strictly reuse the run-owned request artifact."""
    _validate_run_path(root, run_path)
    _validate_request_ownership(run_path, request)
    destination = run_path / _REQUEST_FILE
    if not is_safe_contained_path(root, destination):
        raise Phase8HandoffArtifactError
    if destination.exists() or destination.is_symlink():
        existing = _read_request(root, destination)
        if _same_deterministic_request(existing, request):
            return Phase8HandoffResult(existing, Phase8HandoffOutcome.REUSED)
        raise Phase8HandoffConflictError
    temporary: Path | None = None
    try:
        fd, raw_temporary = tempfile.mkstemp(
            prefix=f".{run_path.name}.phase8-request-", suffix=".tmp", dir=run_path
        )
        temporary = Path(raw_temporary)
        if not is_safe_contained_path(root, temporary, require_target=True):
            raise Phase8HandoffArtifactError  # noqa: TRY301
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            _ = handle.write(_durable_json(request))
            _ = handle.flush()
            os.fsync(handle.fileno())
        if destination.exists() or destination.is_symlink():
            existing = _read_request(root, destination)
            if _same_deterministic_request(existing, request):
                return Phase8HandoffResult(existing, Phase8HandoffOutcome.REUSED)
            raise Phase8HandoffConflictError  # noqa: TRY301
        _ = temporary.rename(destination)
        committed = _read_request(root, destination)
        if not _same_deterministic_request(committed, request):
            raise Phase8HandoffCorruptError  # noqa: TRY301
        return Phase8HandoffResult(committed, Phase8HandoffOutcome.CREATED)
    except (Phase8HandoffConflictError, Phase8HandoffCorruptError, Phase8HandoffArtifactError):
        raise
    except (OSError, DurableJsonError, TypeError, ValueError):
        raise Phase8HandoffArtifactError from None
    finally:
        if temporary is not None and temporary.exists() and not temporary.is_symlink():
            with suppress(OSError):
                temporary.unlink()


def phase8_handoff_status(
    root: Path,
    run_path: Path,
    terminal_result_id: str,
    *,
    expected_handoff_request_id: str | None = None,
) -> Phase8HandoffStatus:
    """Return READY only for a valid request owned by the reopened FOUND result."""
    _validate_run_path(root, run_path)
    destination = run_path / _REQUEST_FILE
    if not destination.exists() and not destination.is_symlink():
        return Phase8HandoffStatus.PENDING
    request = _read_request(root, destination)
    if (
        request.investigation_id != run_path.parent.name
        or request.search_run_id != run_path.name
        or request.terminal_result_id != terminal_result_id
        or (
            expected_handoff_request_id is not None
            and request.handoff_request_id != expected_handoff_request_id
        )
    ):
        raise Phase8HandoffCorruptError
    return Phase8HandoffStatus.READY


def _with_request_id(request: Phase8HandoffRequestV1) -> Phase8HandoffRequestV1:
    digest = hashlib.sha256(canonical_phase8_handoff_json(request).encode("utf-8")).hexdigest()
    return request.model_copy(update={"handoff_request_id": f"{_ID_PREFIX}{digest}"})


def _durable_json(request: Phase8HandoffRequestV1) -> str:
    payload = cast("dict[str, object]", request.model_dump(mode="python"))
    payload = {
        key: _timestamp_text(value) if isinstance(value, datetime) else value
        for key, value in payload.items()
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _read_request(root: Path, path: Path) -> Phase8HandoffRequestV1:
    if (
        path.is_symlink()
        or not path.is_file()
        or not is_safe_contained_path(root, path, require_target=True)
    ):
        raise Phase8HandoffCorruptError
    try:
        raw = path.read_text(encoding="utf-8")
        _ = load_durable_json_object(raw)
        request = Phase8HandoffRequestV1.model_validate_json(raw, strict=True)
    except (OSError, UnicodeError, DurableJsonError, TypeError, ValueError):
        raise Phase8HandoffCorruptError from None
    if _expected_request_id(request) != request.handoff_request_id:
        raise Phase8HandoffCorruptError
    return request


def _expected_request_id(request: Phase8HandoffRequestV1) -> str:
    digest = hashlib.sha256(canonical_phase8_handoff_json(request).encode("utf-8")).hexdigest()
    return f"{_ID_PREFIX}{digest}"


def _same_deterministic_request(
    existing: Phase8HandoffRequestV1, requested: Phase8HandoffRequestV1
) -> bool:
    """Compare only the immutable deterministic identity of two requests.

    ``created_at_utc`` is publication metadata.  It is intentionally excluded
    from the request identity and must not turn a delayed or restarted retry
    into a conflict.  Strict readback still validates the persisted timestamp;
    this helper only decides whether the existing immutable request is the
    same request that was asked for.
    """
    return (
        existing.handoff_request_id == requested.handoff_request_id
        and canonical_phase8_handoff_json(existing) == canonical_phase8_handoff_json(requested)
    )


def _validate_run_path(root: Path, run_path: Path) -> None:
    if (
        run_path.is_symlink()
        or not run_path.is_dir()
        or run_path.parent.parent != root
        or not is_safe_contained_path(root, run_path, require_target=True)
    ):
        raise Phase8HandoffArtifactError


def _validate_request_ownership(run_path: Path, request: Phase8HandoffRequestV1) -> None:
    if request.investigation_id != run_path.parent.name or request.search_run_id != run_path.name:
        raise Phase8HandoffArtifactError


def _parse_whole(value: datetime | str) -> datetime:
    parsed = parse_canonical_utc(value)
    if not _is_whole_utc(parsed):
        raise ValueError
    return parsed


def _is_whole_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0) and value.microsecond == 0


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
