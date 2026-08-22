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

import vigi_vision.recording_search_a2_repository as a2_repository
from vigi_vision.recording_search_a2_models import AcquisitionOperationRecord
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_d2_publication import build_schema4_successor
from vigi_vision.recording_search_d2_publication_models import RecordingSearchManifestV4
from vigi_vision.recording_search_d2_reopen_validation import reopen_terminal
from vigi_vision.recording_search_d2_status import terminal_status
from vigi_vision.recording_search_d2_terminal import TerminalInputSnapshot, TerminalResult
from vigi_vision.recording_search_models import (
    RecordingSearchManifestCorruptError,
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


def test_schema4_reopen_rejects_residue_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError

    harness, manifest = _published_harness(tmp_path)
    run_path = harness.service.repository.run_path(harness.investigation_id, manifest.search_run_id)
    operation_id = manifest.as_schema3().acquisition_operation_ids[0]
    operation_path = run_path / "operations" / f"{operation_id}.json"
    operation = AcquisitionOperationRecord.model_validate_json(
        operation_path.read_text(encoding="utf-8"), strict=True
    )
    residue = run_path / ".phase7a2-admission-terminal-residue"
    residue.mkdir()
    marker = residue / "operation.json"
    _ = marker.write_text(
        operation.model_copy(
            update={"operation_id": "acquisition-op-terminal-residue"}
        ).model_dump_json(),
        encoding="utf-8",
    )
    manifest_path = run_path / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    marker_bytes = marker.read_bytes()
    harness.service.close()
    monkeypatch.setattr(a2_repository, "_recover_admission_residue", fail_if_called)
    replacement = replace(harness.service)
    try:
        with pytest.raises(RecordingSearchManifestCorruptError):
            _ = replacement.status(harness.investigation_id, manifest.search_run_id)
        assert residue.is_dir()
        assert marker.read_bytes() == marker_bytes
        assert manifest_path.read_bytes() == manifest_bytes
    finally:
        replacement.close()


@pytest.mark.parametrize(
    "entry_kind",
    [
        "classification-operation",
        "observation",
        "alias",
        "nested-directory",
        "unsupported-extension",
    ],
)
def test_schema4_reopen_rejects_unindexed_schema3_entries_without_mutation(
    tmp_path: Path, entry_kind: str
) -> None:
    harness, manifest = _published_harness(tmp_path)
    run_path = harness.service.repository.run_path(harness.investigation_id, manifest.search_run_id)
    if entry_kind == "classification-operation":
        source = next((run_path / "classification-operations").iterdir())
        entry = run_path / "classification-operations" / "foreign-operation.json"
        _ = entry.write_bytes(source.read_bytes())
    elif entry_kind in {"observation", "alias"}:
        source = next((run_path / "observations").iterdir())
        entry = run_path / "observations" / f"foreign-{entry_kind}.json"
        _ = entry.write_bytes(source.read_bytes())
    elif entry_kind == "nested-directory":
        entry = run_path / "observations" / "foreign-nested"
        entry.mkdir()
        _ = (entry / "record.json").write_bytes(b"foreign")
    else:
        entry = run_path / "observations" / "foreign.txt"
        _ = entry.write_bytes(b"foreign")
    before = {
        path.relative_to(run_path): (None if path.is_dir() else path.read_bytes())
        for path in run_path.rglob("*")
    }
    manifest_bytes = (run_path / "manifest.json").read_bytes()
    harness.service.close()
    replacement = replace(harness.service)
    try:
        with pytest.raises(RecordingSearchManifestCorruptError):
            _ = replacement.status(harness.investigation_id, manifest.search_run_id)
        after = {
            path.relative_to(run_path): (None if path.is_dir() else path.read_bytes())
            for path in run_path.rglob("*")
        }
        assert entry.exists()
        assert after == before
        assert (run_path / "manifest.json").read_bytes() == manifest_bytes
    finally:
        replacement.close()


def test_schema4_reopen_rejects_symlink_entry_without_mutation(tmp_path: Path) -> None:
    harness, manifest = _published_harness(tmp_path)
    run_path = harness.service.repository.run_path(harness.investigation_id, manifest.search_run_id)
    outside = tmp_path / "foreign.txt"
    _ = outside.write_bytes(b"foreign")
    link = run_path / "observations" / "foreign-link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        harness.service.close()
        pytest.skip("symlink creation is unavailable")
    manifest_path = run_path / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    harness.service.close()
    replacement = replace(harness.service)
    try:
        with pytest.raises(RecordingSearchManifestCorruptError):
            _ = replacement.status(harness.investigation_id, manifest.search_run_id)
        assert link.is_symlink()
        assert manifest_path.read_bytes() == manifest_bytes
        assert outside.read_bytes() == b"foreign"
    finally:
        replacement.close()
