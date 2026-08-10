import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.test_investigation_confirmation import Context, build_context, build_request

from vigi_vision.investigation_confirmation_integrity import (
    FfmpegJpegDecoder,
    compute_jpeg_integrity,
)
from vigi_vision.investigation_confirmation_models import (
    ConfirmationArtifactError,
    ConfirmationManifest,
    ConfirmationReferenceFrame,
    ConfirmedInputInvalidError,
    LegacyInvestigationError,
    canonical_manifest_json,
    investigation_id_for,
)
from vigi_vision.reference_frame_models import ReferenceFrameResourceCorruptError


@dataclass
class _ChangingDecoder:
    first: bytes
    second: bytes
    calls: int = 0

    def decode(self, path: Path) -> bytes:
        _ = path
        self.calls += 1
        return self.first if self.calls == 1 else self.second


def write_schema_two_package(tmp_path: Path) -> tuple[Context, str, bytes]:
    context = build_context(tmp_path)
    request = build_request(context.resource_id)
    created = context.service.confirm(request)
    schema_three = created.manifest
    shutil.rmtree(created.artifact_directory)
    legacy_id = investigation_id_for(1, schema_three.anchor_time_utc, schema_version=2)
    reference = schema_three.confirmation.reference_frame.model_copy(
        update={"jpeg_sha256": None, "jpeg_size_bytes": None}
    )
    confirmation = schema_three.confirmation.model_copy(update={"reference_frame": reference})
    manifest = ConfirmationManifest.model_validate(
        schema_three.model_copy(
            update={
                "schema_version": 2,
                "investigation_id": legacy_id,
                "artifact_directory_relative": f"artifacts/investigations/{legacy_id}",
                "confirmation": confirmation,
            }
        ),
        strict=True,
    )
    directory = context.investigation_root / legacy_id
    directory.mkdir(parents=True)
    manifest_path = directory / "manifest.json"
    _ = manifest_path.write_text(canonical_manifest_json(manifest), encoding="utf-8")
    return context, legacy_id, manifest_path.read_bytes()


def test_new_confirmation_publishes_schema_three_jpeg_integrity(tmp_path: Path) -> None:
    context = build_context(tmp_path)

    result = context.service.confirm(build_request(context.resource_id))

    reference = result.manifest.confirmation.reference_frame
    expected = context.resource_root.joinpath(context.resource_id, "frame.jpg").read_bytes()
    assert result.manifest.schema_version == 3
    assert result.manifest.investigation_id.startswith("object-disappearance-v3-")
    assert reference.jpeg_sha256 == hashlib.sha256(expected).hexdigest()
    assert reference.jpeg_size_bytes == len(expected)
    loaded = context.service.load_confirmed(result.manifest.investigation_id)
    assert loaded.jpeg_sha256 == reference.jpeg_sha256


def test_schema_two_reconfirmation_publishes_new_schema_three_without_mutation(
    tmp_path: Path,
) -> None:
    context, legacy_id, before = write_schema_two_package(tmp_path)

    result = context.service.reconfirm_for_recording_search(legacy_id)

    assert result.manifest.schema_version == 3
    assert result.manifest.investigation_id.startswith("object-disappearance-v3-")
    assert result.manifest.investigation_id != legacy_id
    assert (context.investigation_root / legacy_id / "manifest.json").read_bytes() == before
    assert context.service.load_confirmed(result.manifest.investigation_id).jpeg_size_bytes > 0


def test_schema_two_is_readable_but_not_phase_seven_eligible(tmp_path: Path) -> None:
    context, legacy_id, _ = write_schema_two_package(tmp_path)

    with pytest.raises(LegacyInvestigationError):
        _ = context.service.load_confirmed(legacy_id)


def test_schema_three_load_rejects_changed_jpeg_bytes(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    result = context.service.confirm(build_request(context.resource_id))
    frame = context.resource_root / context.resource_id / "frame.jpg"
    original = frame.read_bytes()
    _ = frame.write_bytes(original + b"changed")

    with pytest.raises(ConfirmedInputInvalidError):
        _ = context.service.load_confirmed(result.manifest.investigation_id)


def test_invalid_jpeg_bytes_are_rejected_before_publication(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    frame = context.resource_root / context.resource_id / "frame.jpg"
    _ = frame.write_bytes(b"not-a-jpeg")

    with pytest.raises(ReferenceFrameResourceCorruptError):
        _ = context.service.confirm(build_request(context.resource_id))

    assert not context.investigation_root.exists()


def test_jpeg_dimension_mismatch_is_rejected(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    frame = context.resource_root / context.resource_id / "frame.jpg"

    with pytest.raises(ConfirmationArtifactError):
        _ = compute_jpeg_integrity(frame, 640, 720)


def test_ffmpeg_decoder_uses_fixed_safe_validation_arguments(tmp_path: Path) -> None:
    frame = tmp_path / "frame.jpg"
    _ = frame.write_bytes(b"jpeg")
    calls: list[tuple[tuple[str, ...], float]] = []

    def runner(arguments: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, timeout))
        return subprocess.CompletedProcess(arguments, 0, "", "")

    _ = FfmpegJpegDecoder(tmp_path / "ffmpeg", runner).decode(frame)

    assert calls == [
        (
            (
                str(tmp_path / "ffmpeg"),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(frame),
                "-f",
                "null",
                "-",
            ),
            15.0,
        )
    ]


def test_publication_rechecks_changed_digest_and_leaves_no_final_package(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    original = (context.resource_root / context.resource_id / "frame.jpg").read_bytes()
    changed = original.replace(b"Lavc", b"Xavc", 1)
    decoder = _ChangingDecoder(original, changed)
    object.__setattr__(context.service, "jpeg_decoder", decoder)
    object.__setattr__(context.service.repository, "jpeg_decoder", decoder)

    with pytest.raises(ConfirmationArtifactError):
        _ = context.service.confirm(build_request(context.resource_id))

    assert decoder.calls == 2
    assert context.investigation_root.exists()
    assert list(context.investigation_root.iterdir()) == []


def test_schema_three_requires_both_integrity_fields(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    result = context.service.confirm(build_request(context.resource_id))
    reference = result.manifest.confirmation.reference_frame.model_dump(mode="json")
    reference["jpeg_sha256"] = None

    with pytest.raises(ValidationError):
        _ = ConfirmationReferenceFrame.model_validate(reference)
