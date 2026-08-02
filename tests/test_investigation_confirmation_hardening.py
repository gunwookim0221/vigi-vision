import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import JsonValue
from tests.test_investigation_confirmation import build_context, build_request

from vigi_vision._investigation_confirmation_storage import ensure_root, remove_staging
from vigi_vision.durable_io import load_durable_json_object
from vigi_vision.investigation_confirmation_models import (
    ConfirmationArtifactError,
    ConfirmationCorruptError,
    ConfirmationResult,
    ConfirmedInputInvalidError,
)
from vigi_vision.investigation_confirmation_repository import InvestigationConfirmationRepository
from vigi_vision.investigation_confirmation_service import InvestigationConfirmationService
from vigi_vision.reference_frame_models import ReferenceFrameResourceCorruptError
from vigi_vision.reference_frame_resources import ReferenceFrameResourceStore


def _manifest_path(result: ConfirmationResult) -> Path:
    return result.artifact_directory / "manifest.json"


def test_duplicate_top_level_manifest_key_is_rejected(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    result = context.service.confirm(build_request(context.resource_id))
    path = _manifest_path(result)
    raw = path.read_text(encoding="utf-8")
    _ = path.write_text(
        raw.replace(
            '  "status": "confirmed"',
            '  "status": "confirmed",\n  "status": "confirmed"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfirmationCorruptError):
        _ = context.service.load_confirmed(result.manifest.investigation_id)


def test_duplicate_nested_manifest_key_is_rejected(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    result = context.service.confirm(build_request(context.resource_id))
    path = _manifest_path(result)
    raw = path.read_text(encoding="utf-8")
    _ = path.write_text(
        raw.replace(
            '      "coordinate_space": "source_pixels",',
            "{}{}".format(
                '        "coordinate_space": "source_pixels",\n',
                '      "coordinate_space": "source_pixels",',
            ),
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfirmationCorruptError):
        _ = context.service.load_confirmed(result.manifest.investigation_id)


@pytest.mark.parametrize("field", ["status", "investigation_kind", "scenario_id"])
def test_missing_durable_manifest_field_is_rejected(tmp_path: Path, field: str) -> None:
    context = build_context(tmp_path)
    result = context.service.confirm(build_request(context.resource_id))
    path = _manifest_path(result)
    payload = load_durable_json_object(path.read_text(encoding="utf-8"))
    _ = payload.pop(field)
    _ = path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfirmationCorruptError):
        _ = context.service.load_confirmed(result.manifest.investigation_id)


def test_missing_coordinate_space_is_rejected(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    result = context.service.confirm(build_request(context.resource_id))
    path = _manifest_path(result)
    payload = load_durable_json_object(path.read_text(encoding="utf-8"))
    confirmation = payload["confirmation"]
    assert isinstance(confirmation, dict)
    roi = confirmation["roi"]
    assert isinstance(roi, dict)
    _ = roi.pop("coordinate_space")
    _ = path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfirmationCorruptError):
        _ = context.service.load_confirmed(result.manifest.investigation_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("anchor_time_utc", 1784518468),
        ("confirmation.timing.decoded_local_pts_seconds", "2.0"),
        ("confirmation.roi.x", True),
    ],
)
def test_coercible_persisted_value_is_rejected(
    tmp_path: Path, field: str, value: JsonValue
) -> None:
    context = build_context(tmp_path)
    result = context.service.confirm(build_request(context.resource_id))
    path = _manifest_path(result)
    payload = load_durable_json_object(path.read_text(encoding="utf-8"))
    target = payload
    parts = field.split(".")
    for part in parts[:-1]:
        nested = target[part]
        assert isinstance(nested, dict)
        target = nested
    target[parts[-1]] = value
    _ = path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfirmationCorruptError):
        _ = context.service.load_confirmed(result.manifest.investigation_id)


def test_unknown_durable_field_is_rejected(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    result = context.service.confirm(build_request(context.resource_id))
    path = _manifest_path(result)
    payload = load_durable_json_object(path.read_text(encoding="utf-8"))
    payload["unsafe_path"] = "outside-root"
    _ = path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfirmationCorruptError):
        _ = context.service.load_confirmed(result.manifest.investigation_id)


def _create_junction(link: Path, target: Path) -> None:
    executable = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    _ = subprocess.run(  # noqa: S603
        [executable, "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction coverage")
def test_junctioned_confirmation_directory_is_rejected(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    result = context.service.confirm(build_request(context.resource_id))
    final = result.artifact_directory
    outside = tmp_path / "outside-confirmation"
    _ = final.rename(outside)
    _create_junction(final, outside)
    try:
        with pytest.raises(ConfirmationCorruptError):
            _ = context.service.load_confirmed(result.manifest.investigation_id)
    finally:
        _ = final.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction coverage")
def test_junctioned_reference_resource_is_rejected(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    result = context.service.confirm(build_request(context.resource_id))
    resource = context.resource_root / context.resource_id
    outside = tmp_path / "outside-resource"
    _ = resource.rename(outside)
    _create_junction(resource, outside)
    try:
        with pytest.raises(ConfirmedInputInvalidError):
            _ = context.service.load_confirmed(result.manifest.investigation_id)
    finally:
        _ = resource.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction coverage")
@pytest.mark.parametrize("nested", [False, True])
def test_ancestor_junction_rejects_trusted_root_creation(tmp_path: Path, *, nested: bool) -> None:
    external = tmp_path / "outside"
    external.mkdir()
    junction = tmp_path / "redirected"
    _create_junction(junction, external)
    root = junction / ("nested" if nested else "artifact") / "artifacts"

    try:
        with pytest.raises(ConfirmationArtifactError):
            ensure_root(root)
        assert not external.joinpath("nested", "artifacts").exists()
        assert not external.joinpath("artifact", "artifacts").exists()
    finally:
        _ = junction.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction coverage")
@pytest.mark.parametrize("nested", [False, True])
def test_ancestor_junction_rejects_confirmation_and_resource_reads(
    tmp_path: Path, *, nested: bool
) -> None:
    safe_root = tmp_path / "safe"
    context = build_context(safe_root)
    result = context.service.confirm(build_request(context.resource_id))
    external = tmp_path / "outside"
    external.mkdir()
    suffix = Path("nested") if nested else Path()
    external_resource = external / suffix / "reference-frames"
    external_investigation = external / suffix / "investigations"
    external_resource.parent.mkdir(parents=True, exist_ok=True)
    external_investigation.parent.mkdir(parents=True, exist_ok=True)
    _ = context.resource_root.rename(external_resource)
    _ = context.investigation_root.rename(external_investigation)
    junction = tmp_path / "redirected"
    _create_junction(junction, external)
    resource_root = junction / suffix / "reference-frames"
    investigation_root = junction / suffix / "investigations"
    staged = external_investigation / "foreign.staging"
    staged.mkdir()
    before_manifest = (
        external_investigation / result.manifest.investigation_id / "manifest.json"
    ).read_bytes()
    before_jpeg = (external_resource / context.resource_id / "frame.jpg").read_bytes()

    try:
        resources = ReferenceFrameResourceStore(resource_root)
        repository = InvestigationConfirmationRepository(
            investigation_root, resources, lambda: datetime.now(timezone.utc)
        )
        service = InvestigationConfirmationService(resources, repository)
        with pytest.raises(ConfirmationCorruptError):
            _ = service.load_confirmed(result.manifest.investigation_id)
        with pytest.raises(ReferenceFrameResourceCorruptError):
            _ = resources.resolve_image(context.resource_id)
        remove_staging(investigation_root, investigation_root / "foreign.staging")
        assert staged.exists()
        assert (
            external_investigation / result.manifest.investigation_id / "manifest.json"
        ).read_bytes() == before_manifest
        assert (external_resource / context.resource_id / "frame.jpg").read_bytes() == before_jpeg
    finally:
        _ = junction.unlink()
