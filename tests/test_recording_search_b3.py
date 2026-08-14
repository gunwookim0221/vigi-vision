import base64
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import IO, cast

import pytest
from tests.test_recording_search_a2 import successful_a2_run

from vigi_vision.assisted_roi_geometry import ImageSize, Point
from vigi_vision.investigation_confirmation_integrity import (
    compute_jpeg_integrity_from_bytes,
)
from vigi_vision.object_presence_models import BinaryMask, DecodedRgbImage
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.recording_search_a2_service import admit_probe_frame_bytes
from vigi_vision.recording_search_b3_media import (
    DecodedMedia,
    InMemoryRgbDecoder,
    InvalidMediaInputError,
)
from vigi_vision.recording_search_b3_models import (
    ClassificationPreparationError,
    ClassificationPreparationReason,
    ClassifyRecordingProbeRequest,
    NonAuthoritativeClassificationResult,
)
from vigi_vision.recording_search_b3_service import RecordingSearchClassificationService
from vigi_vision.recording_search_repository import RecordingSearchRepository

_ONE_PIXEL_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9k="
)


def test_in_memory_decoder_consumes_exact_bytes() -> None:
    calls: list[tuple[tuple[str, ...], bytes, float]] = []

    def runner(
        arguments: tuple[str, ...], payload: bytes, timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((arguments, payload, timeout))
        return subprocess.CompletedProcess(arguments, 0, b"\x01\x02\x03", b"")

    decoded = InMemoryRgbDecoder(Path("ffmpeg"), runner).decode(_ONE_PIXEL_JPEG, 1, 1)

    assert decoded.integrity == compute_jpeg_integrity_from_bytes(_ONE_PIXEL_JPEG, 1, 1)
    assert decoded.image.pixels == (((1, 2, 3),),)
    assert calls[0][1] is _ONE_PIXEL_JPEG


def test_in_memory_decoder_maps_malformed_media_to_safe_category() -> None:
    called = False

    def runner(
        _arguments: tuple[str, ...], _payload: bytes, _timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess((), 0, b"", b"")

    with pytest.raises(InvalidMediaInputError) as error:
        _ = InMemoryRgbDecoder(Path("ffmpeg"), runner).decode(b"not-jpeg", 1, 1)

    assert str(error.value) == "invalid_media_input"
    assert not called


def test_probe_admission_reads_the_selected_jpeg_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, investigation_id, handle, manifest, request = successful_a2_run(tmp_path)
    calls: list[Path] = []
    original_open = Path.open
    frame_path = (
        service.repository.run_path(investigation_id, manifest.search_run_id)
        / f"evidence/frames/{request.canonical_frame_id}.jpg"
    )
    expected_bytes = frame_path.read_bytes()

    def counted_open(  # noqa: PLR0913 - mirrors pathlib's open boundary.
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> IO[bytes]:
        if path.suffix == ".jpg":
            calls.append(path)
        return cast("IO[bytes]", original_open(path, mode, buffering, encoding, errors, newline))

    monkeypatch.setattr(Path, "open", counted_open)
    admitted = admit_probe_frame_bytes(
        service,
        investigation_id,
        manifest.search_run_id,
        request.probe_request_id,
    )

    assert len(calls) == 1
    assert admitted.jpeg_bytes == expected_bytes
    handle.release()


def test_classification_command_cannot_replace_handle_authority() -> None:
    command = ClassifyRecordingProbeRequest("investigation", "run", "request")
    with pytest.raises(FrozenInstanceError):
        command.__setattr__("investigation_id", "foreign")

    assert ClassificationPreparationReason.INVALID_MEDIA_INPUT.value == "invalid_media_input"
    assert issubclass(ClassificationPreparationError, RuntimeError)


def test_active_handle_bindings_and_baseline_are_read_only(tmp_path: Path) -> None:
    _service, _investigation_id, handle, _manifest, _request = successful_a2_run(tmp_path)

    for name, value in (
        ("investigation_id", "foreign"),
        ("search_run_id", "search-run-foreign"),
        ("baseline_bytes", b"foreign"),
    ):
        with pytest.raises(AttributeError):
            setattr(handle, name, value)

    handle.release()


def test_production_service_uses_admission_loader_and_single_probe_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, investigation_id, handle, manifest, request = successful_a2_run(tmp_path)
    image = DecodedRgbImage.from_rows(
        tuple(tuple((0, 0, 0) for _ in range(1280)) for _ in range(720))
    )
    frame_path = (
        service.repository.run_path(investigation_id, manifest.search_run_id)
        / f"evidence/frames/{request.canonical_frame_id}.jpg"
    )
    expected_size = frame_path.stat().st_size

    class Decoder:
        def decode(self, payload: bytes, width: int, height: int) -> DecodedMedia:
            assert payload
            assert (width, height) == (1280, 720)
            return DecodedMedia(compute_jpeg_integrity_from_bytes(payload, width, height), image)

    class Predictor:
        def predict_from_rgb(
            self, image: DecodedRgbImage, point: Point, size: ImageSize
        ) -> BinaryMask:
            assert image.width == size.width
            assert point is not None
            rows = tuple(
                tuple(20 <= x < 80 and 30 <= y < 90 for x in range(1280)) for y in range(720)
            )
            return BinaryMask.from_rows(rows)

    preparer = RecordingSearchClassificationService(
        host=service,
        media_decoder=Decoder(),
        mask_predictor=Predictor(),
        policy=ObjectPresenceDecisionPolicy(minimum_mask_overlap_for_comparison=0.1),
    )

    def forbidden_full_media_load(
        _repository: RecordingSearchRepository, *_args: object, **_kwargs: object
    ) -> object:
        raise AssertionError

    monkeypatch.setattr(RecordingSearchRepository, "load", forbidden_full_media_load)
    jpg_opens: list[Path] = []
    original_open = Path.open

    def counted_open(  # noqa: PLR0913 - mirrors pathlib's open boundary.
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> IO[bytes]:
        if path.suffix == ".jpg":
            jpg_opens.append(path)
        return cast("IO[bytes]", original_open(path, mode, buffering, encoding, errors, newline))

    monkeypatch.setattr(Path, "open", counted_open)
    result = preparer.classify(
        handle,
        ClassifyRecordingProbeRequest(
            investigation_id=investigation_id,
            search_run_id=manifest.search_run_id,
            probe_request_id=request.probe_request_id,
        ),
    )

    assert isinstance(result, NonAuthoritativeClassificationResult)
    assert result.snapshot.probe_jpeg_size_bytes == expected_size
    assert len(jpg_opens) == 1
    handle.release()
