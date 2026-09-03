# pyright: reportAny=false, reportArgumentType=false, reportAssignmentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportGeneralTypeIssues=false, reportImplicitOverride=false, reportOptionalMemberAccess=false, reportPrivateUsage=false, reportUnannotatedClassAttribute=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false, reportUnnecessaryIsInstance=false, reportUnusedCallResult=false, reportUnusedFunction=false, reportUnusedParameter=false
from __future__ import annotations

import json
import multiprocessing
from dataclasses import dataclass
from datetime import datetime, timezone
from multiprocessing.process import BaseProcess
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from PIL import Image

import vigi_vision.recording_search_7e_b4_process as b4_process
from vigi_vision.assisted_roi_geometry import ImageSize, Point
from vigi_vision.investigation_confirmation_integrity import compute_jpeg_integrity_from_bytes
from vigi_vision.investigation_confirmation_models import (
    ConfirmationRoi,
    ConfirmedInvestigationInput,
    RoiProvenance,
)
from vigi_vision.object_presence_comparator import ClassifierInput, ObjectPresenceClassifier
from vigi_vision.object_presence_models import BinaryMask, DecodedRgbImage
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.recording_search_7e_1c import (
    CommonSessionCancelledError,
    DecodedLocalFrame,
    Phase7EB4Input,
)
from vigi_vision.recording_search_7e_b4 import Phase7EProductionB4Adapter
from vigi_vision.recording_search_7e_b4_process import (
    B4ProcessCancelled,
    B4ProcessError,
    B4ProcessTimeout,
    EfficientSamWorkerSpec,
    StaticMaskWorkerSpec,
    _build_request,
    _decode_request,
    _decode_result,
    run_b4_in_process,
)
from vigi_vision.recording_search_7e_models import StrictIdentityEnvelope
from vigi_vision.recording_search_7e_public import approved_phase7e_policy
from vigi_vision.recording_search_b3_media import DecodedMedia


def _values() -> tuple[
    DecodedRgbImage,
    DecodedRgbImage,
    BinaryMask,
    BinaryMask,
    ConfirmationRoi,
    ObjectPresenceDecisionPolicy,
]:
    image = DecodedRgbImage.from_rows(
        tuple(tuple((x * 3, y * 5, 90) for x in range(32)) for y in range(32))
    )
    probe = DecodedRgbImage.from_rows(
        tuple(tuple((x * 3, y * 5, 90) for x in range(32)) for y in range(32))
    )
    baseline_rows = tuple(tuple(8 <= x < 24 and 8 <= y < 24 for x in range(32)) for y in range(32))
    probe_rows = tuple(tuple(8 <= x < 24 and 8 <= y < 24 for x in range(32)) for y in range(32))
    roi = ConfirmationRoi(
        x=8,
        y=8,
        width=16,
        height=16,
        coordinate_space="source_pixels",
        provenance=RoiProvenance.MANUAL,
    )
    policy = ObjectPresenceDecisionPolicy(minimum_mask_overlap_for_comparison=0.1)
    return (
        image,
        probe,
        BinaryMask.from_rows(baseline_rows),
        BinaryMask.from_rows(probe_rows),
        roi,
        policy,
    )


def _predictor(baseline: BinaryMask, probe: BinaryMask) -> object:
    values = (baseline, probe)
    index = 0

    class Predictor:
        def predict_from_rgb(self, image: object, point: Point, size: ImageSize) -> BinaryMask:
            nonlocal index
            value = values[index]
            index += 1
            return value

    return Predictor()


def _send_worker_payload(connection: object, payload: dict[str, object]) -> None:
    connection.send_bytes(json.dumps(payload).encode())
    connection.close()


def _malformed_worker_entry(connection: object, _encoded: bytes) -> None:
    _send_worker_payload(
        connection,
        {
            "version": 1,
            "correlation_id": "malformed",
            "kind": "failure",
            "code": "worker_execution_failed",
            "unexpected": True,
        },
    )


def _wrong_version_worker_entry(connection: object, _encoded: bytes) -> None:
    _send_worker_payload(
        connection,
        {
            "version": 999,
            "correlation_id": "protocol",
            "kind": "failure",
            "code": "worker_execution_failed",
        },
    )


def _wrong_correlation_worker_entry(connection: object, _encoded: bytes) -> None:
    _send_worker_payload(
        connection,
        {
            "version": 1,
            "correlation_id": "other",
            "kind": "failure",
            "code": "worker_execution_failed",
        },
    )


def _extra_key_worker_entry(connection: object, _encoded: bytes) -> None:
    _send_worker_payload(
        connection,
        {
            "version": 1,
            "correlation_id": "protocol",
            "kind": "failure",
            "code": "worker_execution_failed",
            "unexpected": True,
        },
    )


def _eof_worker_entry(connection: object, _encoded: bytes) -> None:
    connection.close()


def _active_classifier_children() -> list[BaseProcess]:
    return [child for child in multiprocessing.active_children() if child.name == "vigi-phase7e-b4"]


def _assert_reaped(pids: list[int]) -> None:
    active = multiprocessing.active_children()
    assert active == []
    assert all(child.pid not in pids for child in active)


def test_spawned_result_matches_shared_b4_computation() -> None:
    baseline, probe, baseline_mask, probe_mask, roi, policy = _values()
    expected = ObjectPresenceClassifier(policy).classify(
        ClassifierInput(baseline, probe, baseline_mask, probe_mask, roi)
    )
    actual = run_b4_in_process(
        baseline_image=baseline,
        probe_image=probe,
        source_width=32,
        source_height=32,
        roi=roi,
        policy=policy,
        worker_spec=StaticMaskWorkerSpec(baseline_mask, probe_mask),
        correlation_id="test-normal",
        timeout_seconds=3.0,
    )
    assert actual == expected
    assert not _active_classifier_children()


def test_worker_failure_envelope_is_reaped_immediately() -> None:
    baseline, probe, _baseline_mask, _probe_mask, roi, policy = _values()
    pids: list[int] = []
    with pytest.raises(B4ProcessError) as raised:
        run_b4_in_process(
            baseline_image=baseline,
            probe_image=probe,
            source_width=32,
            source_height=32,
            roi=roi,
            policy=policy,
            worker_spec=EfficientSamWorkerSpec(
                Path("missing-efficient-sam-checkpoint.pt"), "a" * 64, "cpu"
            ),
            correlation_id="failure-envelope",
            timeout_seconds=3.0,
            pid_observer=pids.append,
        )
    assert raised.value.code == "classifier_unavailable"
    assert raised.value.cleanup_failed is False
    assert len(pids) == 1
    _assert_reaped(pids)


def test_invalid_classifier_output_is_reaped_immediately() -> None:
    baseline, probe, _baseline_mask, _probe_mask, roi, policy = _values()
    empty = BinaryMask.from_rows(tuple(tuple(False for _ in range(32)) for _ in range(32)))
    pids: list[int] = []
    with pytest.raises(B4ProcessError) as raised:
        run_b4_in_process(
            baseline_image=baseline,
            probe_image=probe,
            source_width=32,
            source_height=32,
            roi=roi,
            policy=policy,
            worker_spec=StaticMaskWorkerSpec(empty, empty),
            correlation_id="invalid-output",
            timeout_seconds=3.0,
            pid_observer=pids.append,
        )
    assert raised.value.code == "invalid_classifier_output"
    assert raised.value.cleanup_failed is False
    assert len(pids) == 1
    _assert_reaped(pids)


@pytest.mark.parametrize(
    ("worker", "expected"),
    [
        (_malformed_worker_entry, "malformed_worker_protocol"),
        (_wrong_version_worker_entry, "malformed_worker_protocol"),
        (_wrong_correlation_worker_entry, "malformed_worker_protocol"),
        (_extra_key_worker_entry, "malformed_worker_protocol"),
    ],
)
def test_protocol_failure_from_spawned_worker_is_reaped(
    monkeypatch: pytest.MonkeyPatch,
    worker: object,
    expected: str,
) -> None:
    baseline, probe, baseline_mask, probe_mask, roi, policy = _values()
    monkeypatch.setattr(b4_process, "_worker_entry", worker)
    pids: list[int] = []
    with pytest.raises(B4ProcessError) as raised:
        run_b4_in_process(
            baseline_image=baseline,
            probe_image=probe,
            source_width=32,
            source_height=32,
            roi=roi,
            policy=policy,
            worker_spec=StaticMaskWorkerSpec(baseline_mask, probe_mask),
            correlation_id="protocol",
            timeout_seconds=3.0,
            pid_observer=pids.append,
        )
    assert raised.value.code == expected
    assert raised.value.cleanup_failed is False
    assert len(pids) == 1
    _assert_reaped(pids)


def test_eof_without_result_is_reaped_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline, probe, baseline_mask, probe_mask, roi, policy = _values()
    monkeypatch.setattr(b4_process, "_worker_entry", _eof_worker_entry)
    pids: list[int] = []
    with pytest.raises(B4ProcessError) as raised:
        run_b4_in_process(
            baseline_image=baseline,
            probe_image=probe,
            source_width=32,
            source_height=32,
            roi=roi,
            policy=policy,
            worker_spec=StaticMaskWorkerSpec(baseline_mask, probe_mask),
            correlation_id="eof",
            timeout_seconds=3.0,
            pid_observer=pids.append,
        )
    assert raised.value.code == "worker_abnormal_exit"
    assert raised.value.cleanup_failed is False
    assert len(pids) == 1
    _assert_reaped(pids)


def test_parent_decode_exception_is_reaped_and_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline, probe, baseline_mask, probe_mask, roi, policy = _values()

    def fail_decode(raw: bytes, expected_correlation: str) -> object:
        raise RuntimeError from None

    monkeypatch.setattr(b4_process, "_decode_result", fail_decode)
    pids: list[int] = []
    with pytest.raises(B4ProcessError) as raised:
        run_b4_in_process(
            baseline_image=baseline,
            probe_image=probe,
            source_width=32,
            source_height=32,
            roi=roi,
            policy=policy,
            worker_spec=StaticMaskWorkerSpec(baseline_mask, probe_mask),
            correlation_id="decode-error",
            timeout_seconds=3.0,
            pid_observer=pids.append,
        )
    assert raised.value.code == "worker_execution_failed"
    assert raised.value.cleanup_failed is False
    assert len(pids) == 1
    _assert_reaped(pids)


def test_cleanup_failure_is_secondary_to_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline, probe, baseline_mask, probe_mask, roi, policy = _values()
    original_close = b4_process._close_connection
    close_calls = 0

    def close_and_report_failure(connection: object) -> bool:
        nonlocal close_calls
        result = original_close(connection)
        close_calls += 1
        return result and close_calls == 1

    monkeypatch.setattr(b4_process, "_close_connection", close_and_report_failure)
    with pytest.raises(B4ProcessError) as raised:
        run_b4_in_process(
            baseline_image=baseline,
            probe_image=probe,
            source_width=32,
            source_height=32,
            roi=roi,
            policy=policy,
            worker_spec=StaticMaskWorkerSpec(baseline_mask, probe_mask),
            correlation_id="cleanup-secondary",
            timeout_seconds=0.05,
        )
    assert raised.value.code == "classifier_timeout"
    assert raised.value.cleanup_failed is True
    assert not _active_classifier_children()


def test_timeout_terminates_process_and_retry_does_not_overlap() -> None:
    baseline, probe, baseline_mask, probe_mask, roi, policy = _values()
    pids: list[int] = []
    with pytest.raises(B4ProcessTimeout):
        run_b4_in_process(
            baseline_image=baseline,
            probe_image=probe,
            source_width=32,
            source_height=32,
            roi=roi,
            policy=policy,
            worker_spec=StaticMaskWorkerSpec(baseline_mask, probe_mask, delay_seconds=0.25),
            correlation_id="test-timeout",
            timeout_seconds=0.05,
            pid_observer=pids.append,
        )
    assert len(pids) == 1
    _assert_reaped(pids)
    assert not _active_classifier_children()
    result = run_b4_in_process(
        baseline_image=baseline,
        probe_image=probe,
        source_width=32,
        source_height=32,
        roi=roi,
        policy=policy,
        worker_spec=StaticMaskWorkerSpec(baseline_mask, probe_mask),
        correlation_id="test-retry",
        timeout_seconds=3.0,
    )
    assert result.outcome.value == "INDETERMINATE"
    assert not _active_classifier_children()


def test_cancellation_terminates_process_without_result() -> None:
    baseline, probe, baseline_mask, probe_mask, roi, policy = _values()
    cancelled = Event()
    outcome: list[BaseException] = []
    pids: list[int] = []

    def invoke() -> None:
        try:
            run_b4_in_process(
                baseline_image=baseline,
                probe_image=probe,
                source_width=32,
                source_height=32,
                roi=roi,
                policy=policy,
                worker_spec=StaticMaskWorkerSpec(baseline_mask, probe_mask, delay_seconds=0.25),
                correlation_id="test-cancel",
                timeout_seconds=3.0,
                cancellation=cancelled.is_set,
                pid_observer=pids.append,
            )
        except BaseException as error:  # noqa: BLE001 - assert the closed cancellation type.
            outcome.append(error)

    thread = Thread(target=invoke)
    thread.start()
    cancelled.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], B4ProcessCancelled)
    assert len(pids) == 1
    _assert_reaped(pids)


@dataclass
class _Budget:
    timeout: float
    cancellation: object | None = None

    def admit_classification(self) -> float:
        return self.timeout

    def check(self) -> None:
        return None


def _phase7e_adapter_fixture(
    tmp_path: Path,
    *,
    timeout: float,
    delay_seconds: float = 0.0,
    worker_spec: StaticMaskWorkerSpec | None = None,
) -> tuple[Phase7EProductionB4Adapter, Phase7EB4Input]:
    width = 32
    height = 32
    rgb = bytes((80, 80, 80)) * (width * height)
    image = DecodedRgbImage.from_rows(
        tuple(tuple((rgb[(y * width + x) * 3],) * 3 for x in range(width)) for y in range(height))
    )
    roi = ConfirmationRoi(
        x=8,
        y=8,
        width=16,
        height=16,
        coordinate_space="source_pixels",
        provenance=RoiProvenance.MANUAL,
    )
    mask = BinaryMask.from_rows(
        tuple(tuple(8 <= x < 24 and 8 <= y < 24 for x in range(width)) for y in range(height))
    )
    policy_envelope, classifier_policy, object_policy = approved_phase7e_policy()
    investigation_id = "inv-process"
    run_id = "run-process"
    session_id = "session-process"
    requested = datetime(2026, 1, 1, tzinfo=timezone.utc)
    plan = StrictIdentityEnvelope.from_payload(
        "coarse-plan",
        {
            "investigation_id": investigation_id,
            "run_id": run_id,
            "channel_id": 1,
            "policy_id": policy_envelope.identity,
            "start_requested_time_utc": "2026-01-01T00:00:00Z",
            "end_requested_time_utc": "2026-01-01T00:00:04Z",
            "target_requested_times_utc": ["2026-01-01T00:00:00Z"],
        },
    )
    target = StrictIdentityEnvelope.from_payload(
        "target-request",
        {
            "investigation_id": investigation_id,
            "run_id": run_id,
            "plan_id": plan.identity,
            "sequence": 0,
            "kind": "COARSE",
            "requested_time_utc": "2026-01-01T00:00:00Z",
            "selection_rule": "NEAREST_IN_HALF_OPEN_SESSION",
        },
    )
    baseline_path = tmp_path / "baseline.jpg"
    Image.new("RGB", (width, height), (80, 80, 80)).save(baseline_path, format="JPEG")
    jpeg = baseline_path.read_bytes()
    digest = compute_jpeg_integrity_from_bytes(jpeg, width, height)
    frame = DecodedLocalFrame(
        requested,
        0,
        0,
        width,
        height,
        rgb,
        decoder_operation_id="decoder-process",
        decode_session_id=session_id,
    )
    frame_record = StrictIdentityEnvelope.from_payload(
        "frame",
        {
            "investigation_id": investigation_id,
            "run_id": run_id,
            "common_session_id": session_id,
            "decoder_operation_id": "decoder-process",
            "selected_video_stream_index": 0,
            "target_request_id": target.identity,
            "raw_pts": 0,
            "container_start_pts": 0,
            "time_base_num": 1,
            "time_base_den": 1,
            "estimated_requested_time_utc": "2026-01-01T00:00:00Z",
            "ordinal": 0,
            "width": width,
            "height": height,
            "jpeg_size_bytes": digest.size_bytes,
            "jpeg_sha256": digest.sha256,
            "rgb24_sha256": frame.rgb24_sha256,
        },
    )
    run = SimpleNamespace(
        investigation_id=investigation_id,
        run_id=run_id,
        manifest=SimpleNamespace(
            payload={
                "common_session_id": session_id,
                "classifier_policy_id": classifier_policy.identity,
            }
        ),
        records=(target, frame_record, classifier_policy),
        frame_bytes={frame_record.identity: jpeg},
    )
    budget = _Budget(timeout)
    authoritative = Phase7EB4Input(
        run,
        frame_record,
        jpeg,
        frame,
        target,
        "attempt-process",
        budget,
    )
    confirmed = ConfirmedInvestigationInput(
        investigation_id,
        1,
        requested,
        "UTC",
        0,
        "resource-process",
        "2026-01-01T00:00:00Z",
        requested,
        3,
        "exact",
        None,
        None,
        "EXACT",
        (),
        width,
        height,
        roi,
        digest.sha256,
        digest.size_bytes,
        baseline_path,
    )

    class Decoder:
        def decode(self, payload: bytes, source_width: int, source_height: int) -> DecodedMedia:
            return DecodedMedia(
                compute_jpeg_integrity_from_bytes(payload, source_width, source_height), image
            )

    return (
        Phase7EProductionB4Adapter(
            lambda _investigation_id: confirmed,
            Decoder(),
            worker_spec or StaticMaskWorkerSpec(mask, mask, delay_seconds=delay_seconds),
            object_policy,
        ),
        authoritative,
    )


def test_phase7e_production_adapter_keeps_existing_operation_shape(tmp_path: Path) -> None:
    adapter, authoritative = _phase7e_adapter_fixture(tmp_path, timeout=3.0)
    result = adapter.classify(authoritative)
    assert result.family == "classification-operation"
    assert result.payload["result_kind"] == "VISUAL"
    assert result.payload["frame_id"] == authoritative.frame_record.identity
    assert result.payload["target_request_id"] == authoritative.target_request.identity
    assert result.payload["classifier_evidence"] is not None
    assert not _active_classifier_children()


def test_phase7e_production_adapter_timeout_is_operational_only(tmp_path: Path) -> None:
    adapter, authoritative = _phase7e_adapter_fixture(
        tmp_path,
        timeout=0.05,
        delay_seconds=0.25,
    )
    result = adapter.classify(authoritative)
    assert result.payload["result_kind"] == "OPERATIONAL"
    assert result.payload["operational_reason"] == "classifier_timeout"
    assert result.payload["classifier_evidence"] is None
    assert not _active_classifier_children()


def test_phase7e_production_adapter_invalid_output_is_canonical_operational(
    tmp_path: Path,
) -> None:
    empty = BinaryMask.from_rows(tuple(tuple(False for _ in range(32)) for _ in range(32)))
    adapter, authoritative = _phase7e_adapter_fixture(
        tmp_path,
        timeout=3.0,
        worker_spec=StaticMaskWorkerSpec(empty, empty),
    )
    result = adapter.classify(authoritative)
    assert result.payload["result_kind"] == "OPERATIONAL"
    assert result.payload["operational_reason"] == "invalid_classifier_result"
    assert result.payload["classifier_evidence"] is None


def test_phase7e_production_adapter_pre_spawn_cancellation_has_no_operation(
    tmp_path: Path,
) -> None:
    adapter, authoritative = _phase7e_adapter_fixture(tmp_path, timeout=3.0)
    cancelled = Event()
    authoritative.budget.cancellation = cancelled.is_set
    cancelled.set()
    with pytest.raises(CommonSessionCancelledError):
        adapter.classify(authoritative)


def test_protocol_rejects_unknown_keys_and_wrong_version() -> None:
    baseline, probe, baseline_mask, probe_mask, roi, policy = _values()
    request = _build_request(
        baseline,
        probe,
        32,
        32,
        roi,
        policy,
        StaticMaskWorkerSpec(baseline_mask, probe_mask),
        "protocol",
    )
    unknown = {**request, "unexpected": True}
    with pytest.raises(ValueError, match=r".*"):
        _decode_request(json.dumps(unknown).encode())
    wrong_version = {**request, "version": 999}
    with pytest.raises(ValueError, match=r".*"):
        _decode_request(json.dumps(wrong_version).encode())


def test_protocol_rejects_mismatched_or_malformed_result() -> None:
    mismatched = {
        "version": 1,
        "correlation_id": "other",
        "kind": "failure",
        "code": "worker_execution_failed",
    }
    with pytest.raises(B4ProcessError):
        _decode_result(json.dumps(mismatched).encode(), "expected")
    malformed = {
        "version": 1,
        "correlation_id": "expected",
        "kind": "failure",
        "code": "worker_execution_failed",
        "unexpected": True,
    }
    with pytest.raises(B4ProcessError):
        _decode_result(json.dumps(malformed).encode(), "expected")
