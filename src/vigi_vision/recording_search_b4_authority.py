"""Thread-safe invocation-local Phase 7B publication authority."""

from __future__ import annotations

from enum import Enum
from threading import RLock
from typing import final


@final
class InvocationAuthorityState(str, Enum):
    """Monotonic lifecycle for one bounded classifier invocation."""

    ACTIVE = "active"
    COMPLETED_IN_TIME = "completed_in_time"
    TIMED_OUT = "timed_out"
    ABANDONED = "abandoned"
    AUTHORITY_REVOKED = "authority_revoked"
    PUBLICATION_COMPLETED = "publication_completed"
    RESULT_DISCARDED = "result_discarded"


@final
class InvocationAuthority:
    """Own one non-reconstructable attempt's publication permission."""

    __slots__ = ("_lock", "_state", "attempt_id")

    def __init__(self, attempt_id: str) -> None:
        """Create active authority for one opaque attempt identifier."""
        self.attempt_id = attempt_id
        self._state = InvocationAuthorityState.ACTIVE
        self._lock = RLock()

    @property
    def state(self) -> InvocationAuthorityState:
        """Return the current monotonic authority state."""
        with self._lock:
            return self._state

    @property
    def can_publish(self) -> bool:
        """Return whether this timely result still owns publication authority."""
        with self._lock:
            return self._state is InvocationAuthorityState.COMPLETED_IN_TIME

    def complete_in_time(self) -> bool:
        """Mark an active invocation as completed before its deadline."""
        with self._lock:
            if self._state is not InvocationAuthorityState.ACTIVE:
                return False
            self._state = InvocationAuthorityState.COMPLETED_IN_TIME
            return True

    def revoke_timeout(self) -> bool:
        """Permanently revoke authority because the deadline elapsed."""
        return self._transition_from_active(InvocationAuthorityState.TIMED_OUT)

    def revoke_abandoned(self) -> bool:
        """Permanently revoke authority because the caller abandoned the wait."""
        return self._transition_from_active(InvocationAuthorityState.ABANDONED)

    def revoke(self) -> bool:
        """Permanently revoke active or timely-completed authority."""
        with self._lock:
            if self._state not in {
                InvocationAuthorityState.ACTIVE,
                InvocationAuthorityState.COMPLETED_IN_TIME,
            }:
                return False
            self._state = InvocationAuthorityState.AUTHORITY_REVOKED
            return True

    def mark_published(self) -> bool:
        """Record publication only for a timely result that still owns authority."""
        with self._lock:
            if self._state is not InvocationAuthorityState.COMPLETED_IN_TIME:
                return False
            self._state = InvocationAuthorityState.PUBLICATION_COMPLETED
            return True

    def discard(self) -> bool:
        """Make an unpublished result permanently unusable."""
        with self._lock:
            if self._state in {
                InvocationAuthorityState.PUBLICATION_COMPLETED,
                InvocationAuthorityState.RESULT_DISCARDED,
            }:
                return False
            self._state = InvocationAuthorityState.RESULT_DISCARDED
            return True

    def _transition_from_active(self, target: InvocationAuthorityState) -> bool:
        with self._lock:
            if self._state is not InvocationAuthorityState.ACTIVE:
                return False
            self._state = target
            return True


@final
class ClassificationAttemptSlot:
    """Handle-owned active-attempt marker guarded by the run mutex."""

    __slots__ = ("_active", "_lock")

    def __init__(self) -> None:
        """Create an empty handle-local attempt slot."""
        self._active: InvocationAuthority | None = None
        self._lock = RLock()

    def claim(self, authority: InvocationAuthority) -> bool:
        """Claim the slot when no invocation is active."""
        with self._lock:
            if self._active is not None:
                return False
            self._active = authority
            return True

    def owns(self, authority: InvocationAuthority) -> bool:
        """Return whether the supplied invocation owns the slot."""
        with self._lock:
            return self._active is authority

    def release(self, authority: InvocationAuthority) -> bool:
        """Release only the invocation that currently owns the slot."""
        with self._lock:
            if self._active is not authority:
                return False
            self._active = None
            return True

    @property
    def active(self) -> bool:
        """Return whether the handle has an active classification invocation."""
        with self._lock:
            return self._active is not None
