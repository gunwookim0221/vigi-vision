from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from tests import test_recording_search_d2_3_publication as publication_fixture
from tests.recording_search_b4_support import (
    ControlledExecutor,
    Harness,
    build_harness,
    completed_future,
    install_executor,
)

from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_d2_publication import build_schema4_successor
from vigi_vision.recording_search_d2_publication_models import RecordingSearchManifestV4
from vigi_vision.recording_search_d2_reopen_validation import reopen_terminal
from vigi_vision.recording_search_d2_status import terminal_status
from vigi_vision.recording_search_d2_terminal import TerminalInputSnapshot, TerminalResult
from vigi_vision.recording_search_models import (
    RecordingSearchTerminalReopenCategory,
    RecordingSearchTerminalReopenError,
)


def _published_harness(tmp_path: Path) -> tuple[Harness, RecordingSearchManifestV4]:
    harness = build_harness(tmp_path)
    executor = ControlledExecutor(lambda snapshot, _attempt: completed_future(snapshot, "PRESENT"))
    _ = install_executor(harness, executor)
    _ = harness.service.classify(harness.handle, harness.command)
    context_factory = cast(
        "Callable[[Harness], tuple[TerminalInputSnapshot, RecordingSearchManifestV3]]",
        publication_fixture.__dict__["_publishable_context"],
    )
    result_factory = cast(
        "Callable[[TerminalInputSnapshot], TerminalResult]",
        publication_fixture.__dict__["_terminal_result"],
    )
    context, _ = context_factory(harness)
    result = result_factory(context)
    predecessor = harness.service.repository.load(
        harness.investigation_id, harness.manifest.search_run_id
    )
    assert isinstance(predecessor, RecordingSearchManifestV3)
    published = build_schema4_successor(
        predecessor,
        context,
        result,
        harness.service.repository.now_utc(),
    )
    harness.service.repository.write_schema4_manifest(
        published,
        harness.service.repository.run_path(harness.investigation_id, predecessor.search_run_id),
    )
    return harness, published


def test_terminal_status_is_schema4_allowlisted_projection(tmp_path: Path) -> None:
    harness, manifest = _published_harness(tmp_path)
    try:
        status = terminal_status(manifest)
        payload = cast("dict[str, object]", status.model_dump(mode="json"))
        result_payload = cast("dict[str, object]", payload["result"])
        assert status.schema_version == 4
        assert result_payload["kind"] == "NOT_FOUND"
        assert "terminal_result" not in payload
        assert set(payload) == {
            "schema_version",
            "investigation_id",
            "search_run_id",
            "state",
            "created_at_utc",
            "started_at_utc",
            "completed_at_utc",
            "failure_reason",
            "result",
            "phase8_handoff_status",
        }
        assert set(result_payload) == {
            "result_id",
            "kind",
            "terminal_reason",
            "interval",
            "achieved_precision_seconds",
            "limitations",
            "review_anchor_utc",
        }
        serialized = str(payload)
        for forbidden in (
            "confirmation",
            "probe_request",
            "canonical_frame",
            "observation",
            "alias",
            "classification_operation",
            "operation_id",
            "decoded_pts",
            "decoded_ordinal",
            "decode_session",
            "jpeg_sha256",
            "jpeg_relative_path",
        ):
            assert forbidden not in serialized
    finally:
        harness.service.close()


def test_service_reopen_rejects_terminal_evidence_not_in_index(tmp_path: Path) -> None:
    harness, manifest = _published_harness(tmp_path)
    try:
        harness.service.close()
        with pytest.raises(RecordingSearchTerminalReopenError) as raised:
            _ = harness.service.status(harness.investigation_id, manifest.search_run_id)
        assert raised.value.category is RecordingSearchTerminalReopenCategory.MISSING_RECORD
        assert str(raised.value) == ""
    finally:
        harness.service.close()


def test_reopen_uses_read_only_path_after_service_restart(tmp_path: Path) -> None:
    harness, manifest = _published_harness(tmp_path)
    replacement = replace(harness.service)
    harness.service.close()
    try:
        with pytest.raises(RecordingSearchTerminalReopenError) as raised:
            _ = replacement.status(harness.investigation_id, manifest.search_run_id)
        assert raised.value.category is RecordingSearchTerminalReopenCategory.MISSING_RECORD
        assert str(raised.value) == ""
    finally:
        replacement.close()


def test_reopen_rejects_run_path_outside_repository_root(tmp_path: Path) -> None:
    harness, manifest = _published_harness(tmp_path)
    try:
        with pytest.raises(RecordingSearchTerminalReopenError) as raised:
            _ = reopen_terminal(tmp_path, tmp_path / "foreign-run", manifest)
        assert raised.value.category is RecordingSearchTerminalReopenCategory.FOREIGN_OWNERSHIP
        assert str(raised.value) == ""
    finally:
        harness.service.close()
