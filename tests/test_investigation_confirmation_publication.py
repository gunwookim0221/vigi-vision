import shutil
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from tests.test_investigation_confirmation import build_context, build_request

from vigi_vision import investigation_confirmation_repository as repository_module
from vigi_vision._investigation_confirmation_storage import publish_directory_no_replace
from vigi_vision.investigation_confirmation_claims import (
    ConfirmationClaim,
    ConfirmationClaimStore,
)
from vigi_vision.investigation_confirmation_models import (
    ConfirmationArtifactError,
    ConfirmationConflictError,
    ConfirmationCorruptError,
    ConfirmationManifest,
    ConfirmationOutcome,
    ConfirmationResult,
    canonical_manifest_json,
)
from vigi_vision.investigation_confirmation_repository import (
    InvestigationConfirmationRepository,
)
from vigi_vision.reference_frame_resources import ReferenceFrameResourceMetadata


def _remove_final(result: ConfirmationResult) -> None:
    shutil.rmtree(result.artifact_directory)


def _write_final(result: ConfirmationResult) -> None:
    _ = result.artifact_directory.mkdir()
    _ = (result.artifact_directory / "manifest.json").write_text(
        canonical_manifest_json(result.manifest), encoding="utf-8"
    )


def test_identical_retry_does_not_call_manifest_writer_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    write_manifest_name = "_write_manifest"
    original = cast(
        "Callable[[Path, ConfirmationManifest], None]",
        vars(repository_module)[write_manifest_name],
    )
    writes = 0

    def count_writes(path: Path, manifest: ConfirmationManifest) -> None:
        nonlocal writes
        writes += 1
        original(path, manifest)

    monkeypatch.setattr(repository_module, "_write_manifest", count_writes)
    request = build_request(context.resource_id)
    first = context.service.confirm(request)
    second = context.service.confirm(request)

    assert first.outcome is ConfirmationOutcome.CREATED
    assert second.outcome is ConfirmationOutcome.REUSED
    assert writes == 1
    assert second.manifest.confirmed_at_utc == first.manifest.confirmed_at_utc


def test_final_appearing_during_claim_acquisition_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    request = build_request(context.resource_id)
    first = context.service.confirm(request)
    _remove_final(first)
    original = ConfirmationClaimStore.acquire

    def race(
        store: ConfirmationClaimStore,
        investigation_id: str,
        *,
        final_directory: Path | None = None,
    ) -> ConfirmationClaim:
        _write_final(first)
        return original(store, investigation_id, final_directory=final_directory)

    monkeypatch.setattr(ConfirmationClaimStore, "acquire", race)
    result = context.service.confirm(request)

    assert result.outcome is ConfirmationOutcome.REUSED
    assert result.manifest.confirmed_at_utc == first.manifest.confirmed_at_utc


def test_different_request_conflicts_after_final_wins_claim_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    first = context.service.confirm(build_request(context.resource_id))
    _remove_final(first)
    original = ConfirmationClaimStore.acquire

    def race(
        store: ConfirmationClaimStore,
        investigation_id: str,
        *,
        final_directory: Path | None = None,
    ) -> ConfirmationClaim:
        _write_final(first)
        return original(store, investigation_id, final_directory=final_directory)

    monkeypatch.setattr(ConfirmationClaimStore, "acquire", race)

    with pytest.raises(ConfirmationConflictError):
        _ = context.service.confirm(build_request(context.resource_id, roi=(20, 120)))


def test_promotion_loser_removes_only_its_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    request = build_request(context.resource_id)
    first = context.service.confirm(request)
    _remove_final(first)
    foreign_staging = context.investigation_root / ".foreign.staging"
    foreign_staging.mkdir()
    foreign_claim = context.investigation_root / ".foreign.claim"
    _ = foreign_claim.write_text("foreign", encoding="utf-8")
    original = InvestigationConfirmationRepository.resolve_resource_for_manifest

    def competing_writer(
        repository: InvestigationConfirmationRepository, manifest: ConfirmationManifest
    ) -> ReferenceFrameResourceMetadata:
        _write_final(first)
        return original(repository, manifest)

    monkeypatch.setattr(
        InvestigationConfirmationRepository,
        "resolve_resource_for_manifest",
        competing_writer,
    )
    result = context.service.confirm(request)

    assert result.outcome is ConfirmationOutcome.REUSED
    assert [
        path for path in context.investigation_root.glob("*.staging") if path != foreign_staging
    ] == []
    assert [
        path for path in context.investigation_root.glob(".*.claim") if path != foreign_claim
    ] == []
    assert foreign_staging.exists()
    assert foreign_claim.read_text(encoding="utf-8") == "foreign"


def test_staged_confirmed_time_corruption_fails_before_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    read_manifest_name = "_read_manifest"
    original = cast(
        "Callable[[Path, str], ConfirmationManifest]",
        vars(repository_module)[read_manifest_name],
    )

    def corrupt(path: Path, investigation_id: str) -> ConfirmationManifest:
        manifest = original(path, investigation_id)
        if ".staging" in path.parent.name:
            return manifest.model_copy(
                update={"confirmed_at_utc": manifest.confirmed_at_utc + timedelta(seconds=1)}
            )
        return manifest

    monkeypatch.setattr(repository_module, "_read_manifest", corrupt)

    with pytest.raises(ConfirmationArtifactError):
        _ = context.service.confirm(build_request(context.resource_id))

    assert list(context.investigation_root.iterdir()) == []


@pytest.mark.parametrize("foreign_contents", [False, True])
def test_no_replace_publication_preserves_existing_destination(
    tmp_path: Path, *, foreign_contents: bool
) -> None:
    # Given
    staging = tmp_path / "staging"
    destination = tmp_path / "final"
    staging.mkdir()
    _ = (staging / "manifest.json").write_text("owned", encoding="utf-8")
    destination.mkdir()
    if foreign_contents:
        _ = (destination / "manifest.json").write_text("foreign", encoding="utf-8")
    before = sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*"))
    before_bytes = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }

    # When
    published = publish_directory_no_replace(staging, destination)

    # Then
    assert published is False
    assert staging.exists()
    assert (
        sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*"))
        == before
    )
    assert {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    } == before_bytes


def test_no_replace_publication_publishes_when_destination_is_absent(tmp_path: Path) -> None:
    # Given
    staging = tmp_path / "staging"
    destination = tmp_path / "final"
    staging.mkdir()
    _ = (staging / "manifest.json").write_text("owned", encoding="utf-8")

    # When
    published = publish_directory_no_replace(staging, destination)

    # Then
    assert published is True
    assert not staging.exists()
    assert (destination / "manifest.json").read_text(encoding="utf-8") == "owned"


@pytest.mark.parametrize("foreign_bytes", [b"", b"foreign"])
def test_confirmation_preserves_malformed_foreign_destination(
    tmp_path: Path, foreign_bytes: bytes
) -> None:
    # Given
    context = build_context(tmp_path)
    destination = context.investigation_root / context.investigation_id
    destination.parent.mkdir()
    destination.mkdir()
    manifest_path = destination / "manifest.json"
    _ = manifest_path.write_bytes(foreign_bytes)
    before = manifest_path.read_bytes()

    # When / Then
    with pytest.raises(ConfirmationCorruptError):
        _ = context.service.confirm(build_request(context.resource_id))
    assert manifest_path.read_bytes() == before
