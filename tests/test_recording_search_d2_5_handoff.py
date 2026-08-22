from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest
from tests.test_recording_search_d2_2_found import (
    _found_context,  # pyright: ignore[reportPrivateUsage]
)

from vigi_vision.recording_search_d2_5_handoff import (
    Phase8HandoffArtifactError,
    Phase8HandoffConflictError,
    Phase8HandoffCorruptError,
    Phase8HandoffRequestV1,
    build_phase8_handoff_request,
    canonical_phase8_handoff_json,
    create_or_reuse_phase8_request,
    phase8_handoff_status,
)
from vigi_vision.recording_search_d2_terminal import interpret_terminal
from vigi_vision.recording_search_d2_terminal_models import FoundResult
from vigi_vision.recording_search_models import Phase8HandoffStatus

UTC = timezone.utc


def _found_result() -> FoundResult:
    outcome = interpret_terminal(_found_context())
    assert isinstance(outcome, FoundResult)
    return outcome


def test_found_handoff_uses_requested_interval_and_clips_nominal_window() -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )

    assert request.lower_bound_requested_time_utc == datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
    assert request.upper_bound_requested_time_utc == datetime(2026, 7, 20, 3, 0, 4, tzinfo=UTC)
    assert request.review_anchor_utc == request.upper_bound_requested_time_utc
    assert request.nominal_review_start_utc == request.lower_bound_requested_time_utc
    assert request.nominal_review_end_utc == datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC)
    assert request.handoff_request_id.startswith("phase8-handoff-v1-")


def test_handoff_identity_is_stable_when_creation_time_changes() -> None:
    result = _found_result()
    first = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    second = first.model_copy(
        update={"created_at_utc": first.created_at_utc + timedelta(seconds=1)}
    )

    assert first.handoff_request_id == second.handoff_request_id
    assert canonical_phase8_handoff_json(first) == canonical_phase8_handoff_json(second)


def test_non_found_result_cannot_create_handoff() -> None:
    result = replace(_found_result(), result_kind="NOT_FOUND")

    with pytest.raises(ValueError, match=r"^$"):
        _ = build_phase8_handoff_request(
            result,
            channel_id=1,
            source_timezone="Asia/Seoul",
            search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
            search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
            created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
        )


def test_request_model_forbids_unknown_fields_and_paths() -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    payload = request.model_dump(mode="python")
    payload["path"] = str(Path("/foreign/secret"))

    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        _ = Phase8HandoffRequestV1.model_validate(payload, strict=True)


def test_request_repository_reuses_exact_duplicate_without_rewrite(tmp_path: Path) -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    run_path = tmp_path / result.investigation_id / result.search_run_id
    run_path.mkdir(parents=True)

    first = create_or_reuse_phase8_request(tmp_path, run_path, request)
    before = (run_path / "phase8-request.json").read_bytes()
    second = create_or_reuse_phase8_request(tmp_path, run_path, request)

    assert first.request == second.request == request
    assert first.outcome.value == "created"
    assert second.outcome.value == "reused"
    assert (run_path / "phase8-request.json").read_bytes() == before


def test_request_repository_rejects_conflict_without_rewrite(tmp_path: Path) -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    run_path = tmp_path / result.investigation_id / result.search_run_id
    run_path.mkdir(parents=True)
    _ = create_or_reuse_phase8_request(tmp_path, run_path, request)
    before = (run_path / "phase8-request.json").read_bytes()
    conflict = request.model_copy(update={"upper_support_observation_ids": ("observation-other",)})

    with pytest.raises(Phase8HandoffConflictError):
        _ = create_or_reuse_phase8_request(tmp_path, run_path, conflict)

    assert (run_path / "phase8-request.json").read_bytes() == before


def test_handoff_status_and_persisted_schema_are_strict(tmp_path: Path) -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    run_path = tmp_path / result.investigation_id / result.search_run_id
    run_path.mkdir(parents=True)

    assert phase8_handoff_status(tmp_path, run_path, request.terminal_result_id) is (
        Phase8HandoffStatus.PENDING
    )
    _ = create_or_reuse_phase8_request(tmp_path, run_path, request)
    payload = cast(
        "dict[str, object]",
        json.loads((run_path / "phase8-request.json").read_text(encoding="utf-8")),
    )
    assert payload["schema_version"] == 1
    with pytest.raises(Phase8HandoffCorruptError):
        _ = phase8_handoff_status(
            tmp_path,
            run_path,
            request.terminal_result_id,
            expected_handoff_request_id="phase8-handoff-v1-" + "0" * 64,
        )
    assert phase8_handoff_status(tmp_path, run_path, request.terminal_result_id) is (
        Phase8HandoffStatus.READY
    )


def test_corrupt_existing_request_fails_closed_without_rewrite(tmp_path: Path) -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    run_path = tmp_path / result.investigation_id / result.search_run_id
    run_path.mkdir(parents=True)
    destination = run_path / "phase8-request.json"
    _ = destination.write_text("{}", encoding="utf-8")
    before = destination.read_bytes()

    with pytest.raises(Phase8HandoffCorruptError):
        _ = create_or_reuse_phase8_request(tmp_path, run_path, request)

    assert destination.read_bytes() == before


def test_storage_failure_cleans_owned_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _found_result()
    request = build_phase8_handoff_request(
        result,
        channel_id=1,
        source_timezone="Asia/Seoul",
        search_start_utc=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        search_end_utc=datetime(2026, 7, 20, 3, 0, 20, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 2, 4, 5, 6, tzinfo=UTC),
    )
    run_path = tmp_path / result.investigation_id / result.search_run_id
    run_path.mkdir(parents=True)

    def fail_rename(_source: Path, _destination: Path) -> Path:
        error = OSError("simulated")
        raise error

    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(Phase8HandoffArtifactError):
        _ = create_or_reuse_phase8_request(tmp_path, run_path, request)

    assert not list(run_path.glob("*.phase8-request-*.tmp"))
    assert not (run_path / "phase8-request.json").exists()
