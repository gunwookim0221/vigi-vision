from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from tests import test_recording_search_d2_2_terminal as terminal_fixture
from tests.recording_search_b4_support import (
    ControlledExecutor,
    Harness,
    build_harness,
    completed_future,
    install_executor,
)

from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_d2_identity import evidence_snapshot_digest
from vigi_vision.recording_search_d2_publication import TerminalPublicationOutcome
from vigi_vision.recording_search_d2_terminal import (
    NotFoundResult,
    TerminalInputSnapshot,
    TerminalResult,
    interpret_terminal,
)
from vigi_vision.recording_search_models import (
    RecordingSearchManifestCorruptError,
    RecordingSearchTerminalConflictError,
)


def _publishable_context(
    harness: Harness,
) -> tuple[TerminalInputSnapshot, RecordingSearchManifestV3]:
    service = harness.service
    manifest = service.repository.load(harness.investigation_id, harness.manifest.search_run_id)
    assert isinstance(manifest, RecordingSearchManifestV3)
    context_factory = cast(
        "Callable[[], TerminalInputSnapshot]", terminal_fixture.__dict__["_context"]
    )
    context = context_factory()
    evidence = context.evidence_snapshot
    baseline_refs = tuple(
        replace(reference, observation_id=manifest.baseline_observation_id)
        if reference.is_phase6_baseline
        else reference
        for reference in evidence.references
    )
    updated = replace(
        evidence,
        investigation_id=harness.investigation_id,
        search_run_id=manifest.search_run_id,
        phase6_confirmation_id=harness.handle.phase6_confirmation_id,
        baseline_observation_id=manifest.baseline_observation_id,
        source_revision=replace(
            evidence.source_revision,
            manifest_digest=sha256(manifest.canonical_json().encode("utf-8")).hexdigest(),
        ),
        references=baseline_refs,
    )
    return replace(
        context,
        evidence_snapshot=updated,
        c2_result=replace(
            context.c2_result,
            evidence_snapshot_digest=evidence_snapshot_digest(updated),
        ),
    ), manifest


def _terminal_result(context: TerminalInputSnapshot) -> TerminalResult:
    result = interpret_terminal(context)
    assert isinstance(result, NotFoundResult)
    return result


def _harness_with_schema3(tmp_path: Path) -> Harness:
    harness = build_harness(tmp_path)
    executor = ControlledExecutor(lambda snapshot, _attempt: completed_future(snapshot, "PRESENT"))
    _ = install_executor(harness, executor)
    _ = harness.service.classify(harness.handle, harness.command)
    return harness


def test_service_publishes_schema4_and_identical_retry_reuses_without_rewrite(
    tmp_path: Path,
) -> None:
    harness = _harness_with_schema3(tmp_path)
    context, predecessor = _publishable_context(harness)
    result = _terminal_result(context)

    published = harness.service.publish_terminal(harness.handle, result, context)

    assert published.outcome is TerminalPublicationOutcome.CREATED
    assert published.manifest.state == "NOT_FOUND"
    assert harness.handle.closed
    assert (
        harness.service.repository.load(
            harness.investigation_id, predecessor.search_run_id
        ).schema_version
        == 4
    )
    assert (
        harness.service.status(harness.investigation_id, predecessor.search_run_id).schema_version
        == 2
    )
    manifest_path = (
        harness.service.repository.run_path(harness.investigation_id, predecessor.search_run_id)
        / "manifest.json"
    )
    before = manifest_path.read_bytes()
    reused = harness.service.publish_terminal(harness.handle, result, context)

    assert reused.outcome is TerminalPublicationOutcome.REUSED
    assert manifest_path.read_bytes() == before
    harness.service.close()


def test_different_terminal_result_conflicts_after_schema4_commit(tmp_path: Path) -> None:
    harness = _harness_with_schema3(tmp_path)
    context, _ = _publishable_context(harness)
    result = _terminal_result(context)
    _ = harness.service.publish_terminal(harness.handle, result, context)

    different = replace(result, result_id="recording-search-result-v1-" + "f" * 64)
    with pytest.raises(RecordingSearchTerminalConflictError):
        _ = harness.service.publish_terminal(harness.handle, different, context)
    harness.service.close()


def test_invalid_terminal_proposal_preserves_schema3_and_active_handle(tmp_path: Path) -> None:
    harness = _harness_with_schema3(tmp_path)
    context, predecessor = _publishable_context(harness)
    result = _terminal_result(context)
    invalid = replace(result, phase6_confirmation_id="foreign-confirmation")

    with pytest.raises(ValueError, match=r"^$"):
        _ = harness.service.publish_terminal(harness.handle, invalid, context)

    persisted = harness.service.repository.load(harness.investigation_id, predecessor.search_run_id)
    assert isinstance(persisted, RecordingSearchManifestV3)
    assert not harness.handle.closed
    harness.service.close()


def test_schema4_manifest_rejects_unknown_fields(tmp_path: Path) -> None:
    harness = _harness_with_schema3(tmp_path)
    context, _ = _publishable_context(harness)
    result = _terminal_result(context)
    published = harness.service.publish_terminal(harness.handle, result, context)
    payload = published.manifest.model_dump(mode="json")
    payload["unexpected"] = True

    manifest_path = (
        harness.service.repository.run_path(
            harness.investigation_id, harness.manifest.search_run_id
        )
        / "manifest.json"
    )
    _ = manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = harness.service.repository.load(
            harness.investigation_id, harness.manifest.search_run_id
        )
    harness.service.close()
