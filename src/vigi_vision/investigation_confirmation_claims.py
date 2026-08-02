"""Exclusive confirmation claims with conservative local recovery."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from typing import TYPE_CHECKING, ClassVar, Final, final

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, model_validator

from vigi_vision._investigation_confirmation_storage import (
    direct_child,
    ensure_root,
    entry_exists,
    remove_file,
)
from vigi_vision.durable_io import (
    CanonicalUtc,
    DurableJsonError,
    is_safe_contained_path,
    load_durable_json_object,
)
from vigi_vision.investigation_confirmation_models import (
    ConfirmationArtifactError,
    ConfirmationInProgressError,
    is_investigation_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_CLAIM_SUFFIX: Final = ".claim"
_RECOVERY_SUFFIX: Final = ".recovery"
_STALE_AFTER: Final = timedelta(minutes=30)


class _ClaimDocument(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation_id: StrictStr = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    created_at_utc: CanonicalUtc
    heartbeat_at_utc: CanonicalUtc

    @model_validator(mode="after")
    def require_canonical_times(self) -> _ClaimDocument:
        for value in (self.created_at_utc, self.heartbeat_at_utc):
            if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
                raise ValueError
        if self.created_at_utc > self.heartbeat_at_utc:
            raise ValueError
        return self


@final
@dataclass(frozen=True, slots=True)
class ConfirmationClaim:
    """One invocation's ownership token."""

    investigation_id: str
    operation_id: str
    claim_path: Path = field(repr=False)
    _store: ConfirmationClaimStore = field(repr=False, compare=False)

    def heartbeat(self) -> None:
        """Refresh ownership only when the claim still names this operation."""
        self._store.heartbeat(self)

    def release(self) -> None:
        """Remove this operation's claim only after ownership is rechecked."""
        self._store.release(self)


@final
@dataclass(frozen=True, slots=True)
class ConfirmationClaimStore:
    """Own direct-child claim files beneath one investigation artifact root."""

    output_root: Path = field(repr=False)
    now_utc: Callable[[], datetime]
    stale_after: timedelta = _STALE_AFTER

    def acquire(
        self, investigation_id: str, *, final_directory: Path | None = None
    ) -> ConfirmationClaim:
        """Create or conservatively recover an exclusive claim."""
        if not is_investigation_id(investigation_id):
            raise ConfirmationArtifactError
        ensure_root(self.output_root)
        claim_path = direct_child(self.output_root, f".{investigation_id}{_CLAIM_SUFFIX}")
        for _ in range(2):
            if not entry_exists(self.output_root, claim_path):
                record = _new_claim(self.now_utc)
                if _try_create_claim(claim_path, record):
                    claim = ConfirmationClaim(
                        investigation_id, record.operation_id, claim_path, self
                    )
                    if final_directory is not None and entry_exists(
                        self.output_root, final_directory
                    ):
                        claim.release()
                        raise ConfirmationInProgressError
                    return claim
            record = self._recover_or_refuse(claim_path, final_directory)
            if record is not None:
                return ConfirmationClaim(investigation_id, record.operation_id, claim_path, self)
        raise ConfirmationInProgressError

    def heartbeat(self, claim: ConfirmationClaim) -> None:
        """Refresh a claim under the recovery lock without taking ownership away."""
        lock_path = self._try_lock(claim.investigation_id)
        if lock_path is None:
            raise ConfirmationInProgressError
        try:
            snapshot = _read_claim_snapshot(claim.claim_path)
            if snapshot is None or snapshot[1].operation_id != claim.operation_id:
                raise ConfirmationInProgressError
            refreshed = _ClaimDocument(
                operation_id=claim.operation_id,
                created_at_utc=snapshot[1].created_at_utc,
                heartbeat_at_utc=_canonical_now(self.now_utc),
            )
            _replace_claim(claim.claim_path, refreshed)
        finally:
            remove_file(self.output_root, lock_path)

    def release(self, claim: ConfirmationClaim) -> None:
        """Release only an unchanged claim owned by the supplied operation."""
        lock_path = self._try_lock(claim.investigation_id)
        if lock_path is None:
            return
        try:
            snapshot = _read_claim_snapshot(claim.claim_path)
            if snapshot is not None and snapshot[1].operation_id == claim.operation_id:
                remove_file(self.output_root, claim.claim_path)
        finally:
            remove_file(self.output_root, lock_path)

    def _recover_or_refuse(
        self, claim_path: Path, final_directory: Path | None
    ) -> _ClaimDocument | None:
        investigation_id = claim_path.name.removeprefix(".").removesuffix(_CLAIM_SUFFIX)
        lock_path = self._try_lock(investigation_id)
        if lock_path is None:
            raise ConfirmationInProgressError
        result: _ClaimDocument | None = None
        try:
            if final_directory is not None and entry_exists(self.output_root, final_directory):
                raise ConfirmationInProgressError
            if not entry_exists(self.output_root, claim_path):
                record = _new_claim(self.now_utc)
                if _try_create_claim(claim_path, record):
                    if final_directory is not None and entry_exists(
                        self.output_root, final_directory
                    ):
                        remove_file(self.output_root, claim_path)
                        raise ConfirmationInProgressError
                    result = record
            else:
                snapshot = _read_claim_snapshot(claim_path)
                if snapshot is not None:
                    raw, record = snapshot
                    stale = _is_stale(record, _canonical_now(self.now_utc), self.stale_after)
                    current = _read_claim_snapshot(claim_path)
                    if (
                        stale
                        and current is not None
                        and current[0] == raw
                        and (
                            final_directory is None
                            or not entry_exists(self.output_root, final_directory)
                        )
                    ):
                        remove_file(self.output_root, claim_path)
                        replacement = _new_claim(self.now_utc)
                        if _try_create_claim(claim_path, replacement):
                            if final_directory is not None and entry_exists(
                                self.output_root, final_directory
                            ):
                                remove_file(self.output_root, claim_path)
                                raise ConfirmationInProgressError
                            result = replacement
        finally:
            remove_file(self.output_root, lock_path)
        return result

    def _try_lock(self, investigation_id: str) -> Path | None:
        lock_path = direct_child(self.output_root, f".{investigation_id}{_RECOVERY_SUFFIX}")
        try:
            if not is_safe_contained_path(self.output_root, lock_path):
                return None
            with lock_path.open("x", encoding="ascii"):
                pass
        except FileExistsError:
            return None
        except OSError:
            raise ConfirmationArtifactError from None
        return lock_path


def is_claim_stale(
    claim_path: Path, *, now_utc: datetime, stale_after: timedelta = _STALE_AFTER
) -> bool:
    """Return whether a valid claim is demonstrably older than the threshold."""
    snapshot = _read_claim_snapshot(claim_path)
    return snapshot is not None and _is_stale(
        snapshot[1], _canonical_now(lambda: now_utc), stale_after
    )


def _new_claim(now_utc: Callable[[], datetime]) -> _ClaimDocument:
    current = _canonical_now(now_utc)
    return _ClaimDocument(
        operation_id=token_hex(16), created_at_utc=current, heartbeat_at_utc=current
    )


def _try_create_claim(path: Path, record: _ClaimDocument) -> bool:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            _ = stream.write(record.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return False
    except OSError:
        raise ConfirmationArtifactError from None
    return True


def _replace_claim(path: Path, record: _ClaimDocument) -> None:
    temporary = path.with_name(f".{path.name}.{record.operation_id}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            _ = stream.write(record.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
        _ = temporary.replace(path)
    except OSError:
        remove_file(path.parent, temporary)
        raise ConfirmationArtifactError from None


def _read_claim_snapshot(path: Path) -> tuple[str, _ClaimDocument] | None:
    try:
        if not is_safe_contained_path(path.parent, path, require_target=True):
            return None
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
        _ = load_durable_json_object(raw)
        return raw, _ClaimDocument.model_validate_json(raw, strict=True)
    except (DurableJsonError, OSError, ValidationError, ValueError):
        return None


def _is_stale(record: _ClaimDocument, now: datetime, threshold: timedelta) -> bool:
    if record.created_at_utc.tzinfo is None or record.heartbeat_at_utc.tzinfo is None:
        return False
    if record.created_at_utc > record.heartbeat_at_utc or record.heartbeat_at_utc > now:
        return False
    return now - record.heartbeat_at_utc >= threshold


def _canonical_now(now_utc: Callable[[], datetime]) -> datetime:
    value = now_utc()
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ConfirmationArtifactError
    return value.astimezone(timezone.utc).replace(microsecond=0)
