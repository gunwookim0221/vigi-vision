from datetime import datetime, timezone
from pathlib import Path

import pytest

from vigi_vision.investigation_confirmation_claims import ConfirmationClaimStore
from vigi_vision.investigation_confirmation_models import ConfirmationInProgressError

_INVESTIGATION_ID = "object-disappearance-ch1-20260720T033428Z"


def test_claim_heartbeat_and_owner_checked_release(tmp_path: Path) -> None:
    # Given
    now = datetime(2026, 8, 2, 4, 5, 6, tzinfo=timezone.utc)

    def clock() -> datetime:
        return now

    store = ConfirmationClaimStore(tmp_path, clock)
    claim = store.acquire(_INVESTIGATION_ID)

    # When
    claim.heartbeat()
    document = claim.claim_path.read_text(encoding="utf-8")
    claim.release()

    # Then
    assert f'"operation_id":"{claim.operation_id}"' in document
    assert not claim.claim_path.exists()


def test_second_owner_cannot_acquire_a_live_claim(tmp_path: Path) -> None:
    # Given
    now = datetime(2026, 8, 2, 4, 5, 6, tzinfo=timezone.utc)

    def clock() -> datetime:
        return now

    first = ConfirmationClaimStore(tmp_path, clock).acquire(_INVESTIGATION_ID)

    # When / Then
    with pytest.raises(ConfirmationInProgressError):
        _ = ConfirmationClaimStore(tmp_path, clock).acquire(_INVESTIGATION_ID)
    first.release()


def test_duplicate_heartbeat_claim_is_unverifiable_and_preserved(tmp_path: Path) -> None:
    now = datetime(2026, 8, 2, 4, 5, 6, tzinfo=timezone.utc)
    claim_path = tmp_path / f".{_INVESTIGATION_ID}.claim"
    _ = claim_path.write_text(
        "{}{}{}{}".format(
            '{"operation_id":"1234567890abcdef1234567890abcdef",',
            '"created_at_utc":"2026-08-01T03:00:00Z",',
            '"heartbeat_at_utc":"2026-08-02T04:00:00Z",',
            '"heartbeat_at_utc":"2026-08-01T03:00:00Z"}',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfirmationInProgressError):
        _ = ConfirmationClaimStore(tmp_path, lambda: now).acquire(_INVESTIGATION_ID)

    assert claim_path.read_text(encoding="utf-8").count("heartbeat_at_utc") == 2
