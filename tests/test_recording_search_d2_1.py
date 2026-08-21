from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_d1_history import (
    D1BracketState,
    HistoryEntryKind,
    HistoryEvidence,
    NarrowingHistoryEntry,
    history_digest,
    narrowed_bracket_id,
    reconstruct_history,
)
from vigi_vision.recording_search_d1_identity import (
    D1InputBracket,
    D1LowerBoundReference,
    D1SourceRevision,
    D1SupportGroup,
    d1_input_bracket_id,
    support_group_id,
)

UTC = timezone.utc


def _input_fixture() -> D1InputBracket:
    return D1InputBracket(
        investigation_id="investigation-demo",
        search_run_id="search-run-demo",
        phase6_confirmation_id="confirmation-demo",
        baseline_identity="baseline-demo",
        plan_id="coarse-plan-demo",
        policy_identity="policy-demo",
        source_revision=D1SourceRevision(
            c2_bracket_id="coarse-bracket-demo",
            c2_manifest_digest="a" * 56,
        ),
        lower_bound=D1LowerBoundReference(
            kind="PRESENT_PROBE",
            target_id="target-present",
            requested_time_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
            observation_id="obs-present",
            probe_request_id="probe-present",
            canonical_frame_id="frame-present",
        ),
        upper_support=D1SupportGroup(
            support_group_id="coarse-confirmation-demo",
            origin_target_id="target-absent-0",
            support_count=3,
            cadence_seconds=1,
            requested_support_times=tuple(
                datetime(2026, 7, 20, 3, 0, second, tzinfo=UTC) for second in (4, 5, 6)
            ),
            probe_request_ids=("probe-absent-0", "probe-absent-1", "probe-absent-2"),
            observation_ids=("obs-absent-0", "obs-absent-1", "obs-absent-2"),
            canonical_frame_ids=("frame-absent-0", "frame-absent-1", "frame-absent-2"),
            decode_session_id="decode-demo",
            decoded_frame_utc=tuple(
                datetime(2026, 7, 20, 3, 0, second, tzinfo=UTC) for second in (4, 5, 6)
            ),
            decoded_pts=(4000, 5000, 6000),
            decoded_ordinals=(100, 101, 102),
        ),
    )


def _present_entry(value: D1InputBracket) -> NarrowingHistoryEntry:
    before = D1BracketState(
        lower_requested_time_utc=value.lower_bound.requested_time_utc,
        upper_requested_time_utc=value.upper_support.requested_support_times[0],
        lower_reference=value.lower_bound,
        upper_support_group_id=value.upper_support.support_group_id,
    )
    midpoint = HistoryEvidence(
        role="MIDPOINT",
        target_id="midpoint-target-0",
        probe_request_id="probe-mid-0",
        observation_id="obs-mid-0",
        canonical_frame_id="frame-mid-0",
        acquisition_operation_id="acq-mid-0",
        classification_operation_id="class-mid-0",
        decode_session_id="decode-mid-0",
        decoded_frame_utc=datetime(2026, 7, 20, 3, 0, 2, tzinfo=UTC),
        decoded_pts=2000,
        decoded_ordinal=50,
        classification=ClassificationOutcome.PRESENT,
        requested_time_utc=datetime(2026, 7, 20, 3, 0, 2, tzinfo=UTC),
    )
    after = replace(
        before,
        lower_requested_time_utc=midpoint.requested_time_utc,
        lower_reference=D1LowerBoundReference(
            kind="PRESENT_PROBE",
            target_id=midpoint.target_id,
            requested_time_utc=midpoint.requested_time_utc,
            observation_id=midpoint.observation_id,
            probe_request_id=midpoint.probe_request_id,
            canonical_frame_id=midpoint.canonical_frame_id,
        ),
    )
    return NarrowingHistoryEntry(
        iteration=0,
        entry_kind=HistoryEntryKind.PRESENT_TRANSITION,
        target_id=midpoint.target_id,
        midpoint_requested_time_utc=midpoint.requested_time_utc,
        bracket_before=before,
        evidence=(midpoint,),
        classification=ClassificationOutcome.PRESENT,
        support_group_id=None,
        support_indexes=(),
        bracket_after=after,
        visual_stop_reason=None,
        operational_stop_reason=None,
    )


def test_d1_input_fixture_matches_approved_hash() -> None:
    assert d1_input_bracket_id(_input_fixture()) == (
        "d1-input-bracket-v1-4a1c216e94837b8077d6e3190a4554b31293dc58050abba7a0c11656f9f4e08d"
    )


def test_support_fixture_matches_approved_hash() -> None:
    value = _input_fixture().upper_support
    assert (
        support_group_id(
            investigation_id="investigation-demo",
            search_run_id="search-run-demo",
            phase6_confirmation_id="investigation-demo",
            baseline_identity="baseline-demo",
            plan_id="coarse-plan-demo",
            policy_identity="policy-demo",
            source_revision=_input_fixture().source_revision,
            d1_input_bracket_id="d1-input-bracket-demo",
            iteration=0,
            group=value,
        )
        == "d1-support-group-v1-a8fc88a9f5c5c0aa49fd106795d572233ef65ff0109a98e0268c51823e03b5c6"
    )


def test_history_digest_fixture_matches_approved_hash() -> None:
    entry = _present_entry(_input_fixture())
    digest = history_digest(
        _input_fixture(),
        "d1-input-bracket-v1-4a1c216e94837b8077d6e3190a4554b31293dc58050abba7a0c11656f9f4e08d",
        (entry,),
    )
    assert digest == (
        "d1-history-v1-07494a60a99955734727fce60996e40abf7ad10b546b23f4c65b24ee0781bdb5"
    )


def test_history_reconstruction_rejects_reordered_entries() -> None:
    entry = _present_entry(_input_fixture())
    with pytest.raises(ValueError):  # noqa: PT011
        _ = reconstruct_history(_input_fixture(), (entry, entry), None, None)


def test_narrowed_bracket_identity_changes_when_history_changes() -> None:
    entry = _present_entry(_input_fixture())
    altered_midpoint = replace(entry.evidence[0], target_id="different-target")
    altered_after = replace(
        entry.bracket_after,
        lower_reference=replace(entry.bracket_after.lower_reference, target_id="different-target"),
    )
    altered = replace(
        entry,
        target_id="different-target",
        evidence=(altered_midpoint,),
        bracket_after=altered_after,
    )
    altered_digest = history_digest(
        _input_fixture(),
        d1_input_bracket_id(_input_fixture()),
        (altered,),
    )
    first = narrowed_bracket_id(
        _input_fixture(),
        (entry,),
        entry.bracket_after,
        "d1-history-v1-07494a60a99955734727fce60996e40abf7ad10b546b23f4c65b24ee0781bdb5",
        1,
        2,
        "target_precision_reached",
        "a" * 64,
    )
    second = narrowed_bracket_id(
        _input_fixture(),
        (altered,),
        altered.bracket_after,
        altered_digest,
        1,
        2,
        "target_precision_reached",
        "a" * 64,
    )
    assert first != second
