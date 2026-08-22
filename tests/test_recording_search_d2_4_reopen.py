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
from vigi_vision.recording_search_d2_publication import TerminalPublicationOutcome
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
    published = harness.service.publish_terminal(harness.handle, result, context)
    assert published.outcome is TerminalPublicationOutcome.CREATED
    return harness, published.manifest


def test_terminal_status_is_schema4_allowlisted_projection(tmp_path: Path) -> None:
    harness, manifest = _published_harness(tmp_path)
    try:
        status = terminal_status(manifest)
        payload = status.model_dump(mode="json")
        assert status.schema_version == 4
        assert payload["result"]["kind"] == "NOT_FOUND"
        assert "terminal_result" not in payload
        assert all("path" not in key.lower() for key in payload)
    finally:
        harness.service.close()


def test_service_reopen_rejects_terminal_evidence_not_in_index(tmp_path: Path) -> None:
    harness, manifest = _published_harness(tmp_path)
    try:
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
