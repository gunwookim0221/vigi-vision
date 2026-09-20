"""Bounded, process-isolated Phase 7E B4 computation.

The parent process owns the Phase 7E authority and all publication decisions.
The child receives only a canonical, bounded JSON request and can return only a
closed result or a closed safe failure envelope.  This module deliberately has
no repository, SDK, credential, or dotenv imports.
"""

# The closed JSON boundary intentionally narrows dynamic decoded values only
# after runtime key/type checks; suppress structural diagnostics for that
# explicit protocol parser and multiprocessing platform types.
# pyright: reportAny=false, reportArgumentType=false, reportAssignmentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportGeneralTypeIssues=false, reportImplicitOverride=false, reportOptionalMemberAccess=false, reportPrivateUsage=false, reportUnannotatedClassAttribute=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false, reportUnnecessaryIsInstance=false, reportUnusedCallResult=false, reportUnusedFunction=false, reportUnusedParameter=false

# This module is an intentionally explicit lifecycle boundary; its small
# protocol state machine is clearer as one unit than as a collection of
# callback helpers.  Keep the contract-focused implementation readable.
# ruff: noqa: ARG002, BLE001, C901, D107, EM101, N818, PLC0415, PLR0912, PLR0913, PLR0915, PT018, RSE102, S101, SIM105, TRY004, TRY301

from __future__ import annotations

import base64
import json
import logging
import math
import multiprocessing
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import TYPE_CHECKING, Final, Literal

from vigi_vision.assisted_roi_geometry import ImageSize, Point
from vigi_vision.assisted_roi_predictor import LazyEfficientSamPredictor
from vigi_vision.assisted_roi_service import RoiSuggestionUnavailableError
from vigi_vision.investigation_confirmation_models import ConfirmationRoi
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.object_presence_values import BinaryMask, DecodedRgbImage
from vigi_vision.recording_search_b3_masks import _prediction_to_mask
from vigi_vision.recording_search_b3_models import (
    ClassificationPreparationError,
    ClassificationPreparationReason,
)
from vigi_vision.recording_search_b3_service import classify_decoded_images

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

    from vigi_vision.object_presence_evidence import ClassificationResult


PROTOCOL_VERSION: Final = 1
MAX_PROTOCOL_BYTES: Final = 768 * 1024 * 1024
MAX_RESULT_BYTES: Final = 4 * 1024 * 1024
MAX_CHECKPOINT_PATH_CHARS: Final = 4096
MAX_CORRELATION_CHARS: Final = 256
MAX_STATIC_DELAY_SECONDS: Final = 60.0
# Windows EfficientSAM workers can still finalize native model/runtime state
# briefly after sending a valid result.  Keep the reap bounded, but allow that
# normal graceful-exit tail to finish before treating cleanup as a failure.
_CLEANUP_CEILING_SECONDS: Final = 2.0
_SHA256_PATTERN: Final = re.compile(r"^[0-9a-fA-F]{64}$")
_REQUEST_KEYS: Final = frozenset(
    {
        "version",
        "correlation_id",
        "source_width",
        "source_height",
        "baseline_rgb24",
        "probe_rgb24",
        "roi",
        "policy",
        "predictor",
    }
)
_REFERENCE_REQUEST_KEYS: Final = _REQUEST_KEYS | frozenset({"reference_only"})
_PREDICTOR_KEYS: Final = {
    "efficient_sam": frozenset({"kind", "checkpoint_path", "expected_sha256", "device_mode"}),
    "static_masks": frozenset({"kind", "baseline_rows", "probe_rows", "delay_seconds"}),
}
_RESULT_KEYS: Final = frozenset({"version", "correlation_id", "kind", "result"})
_READY_KEYS: Final = frozenset({"version", "correlation_id", "kind", "timings_ms"})
_FAILURE_KEYS: Final = frozenset({"version", "correlation_id", "kind", "code"})
_FAILURE_CODES: Final = frozenset(
    {
        "classifier_unavailable",
        "classifier_execution_failed",
        "invalid_classifier_output",
        "worker_execution_failed",
    }
)
_REFERENCE_KEYS: Final = frozenset({"version", "correlation_id", "kind", "mask"})
_TIMING_KEYS: Final = frozenset(
    {
        "child_started_ms",
        "request_decoded_ms",
        "checkpoint_validated_ms",
        "runtime_ready_ms",
        "model_ready_ms",
        "preprocessing_ms",
        "child_inference_ms",
        "decoder_calls",
        "segmentation_calls",
        "alignment_translation_candidates",
        "alignment_rotation_candidates",
        "alignment_scale_candidates",
        "alignment_comparisons",
        "alignment_elapsed_ms",
        "duplicate_processing_count",
    }
)
_LOGGER = logging.getLogger("uvicorn.error.vigi_vision.phase7e")


@dataclass(frozen=True, slots=True)
class EfficientSamWorkerSpec:
    """Explicit serializable configuration for the approved model runtime."""

    checkpoint_path: Path
    expected_sha256: str
    device_mode: Literal["cpu", "cuda", "auto"]

    def payload(self) -> dict[str, object]:
        """Return the closed predictor payload without serializing an object."""
        path = str(self.checkpoint_path)
        if (
            not path
            or len(path) > MAX_CHECKPOINT_PATH_CHARS
            or "\0" in path
            or _SHA256_PATTERN.fullmatch(self.expected_sha256) is None
            or self.device_mode not in {"cpu", "cuda", "auto"}
        ):
            raise ValueError
        return {
            "kind": "efficient_sam",
            "checkpoint_path": path,
            "expected_sha256": self.expected_sha256,
            "device_mode": self.device_mode,
        }


@dataclass(frozen=True, slots=True)
class StaticMaskWorkerSpec:
    """Closed deterministic mask source used by local production-shaped tests."""

    baseline_mask: BinaryMask
    probe_mask: BinaryMask
    delay_seconds: float = 0.0

    def payload(self) -> dict[str, object]:
        """Return only bounded primitive mask rows."""
        if (
            type(self.delay_seconds) is not float
            or not math.isfinite(self.delay_seconds)
            or self.delay_seconds < 0
            or self.delay_seconds > MAX_STATIC_DELAY_SECONDS
        ):
            raise ValueError
        return {
            "kind": "static_masks",
            "baseline_rows": [list(row) for row in self.baseline_mask.rows],
            "probe_rows": [list(row) for row in self.probe_mask.rows],
            "delay_seconds": self.delay_seconds,
        }


WorkerSpec = EfficientSamWorkerSpec | StaticMaskWorkerSpec
TimingSink = Callable[[dict[str, int | str]], None]


class B4ProcessError(RuntimeError):
    """Internal safe process-boundary failure with a stable category."""

    def __init__(self, code: str, *, cleanup_failed: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.cleanup_failed = cleanup_failed


class B4ProcessTimeout(B4ProcessError):
    """The child exceeded the classifier operation ceiling."""

    def __init__(self, *, stage: str = "inference", cleanup_failed: bool = False) -> None:
        super().__init__("classifier_timeout", cleanup_failed=cleanup_failed)
        self.stage = stage


class B4ProcessCancelled(B4ProcessError):
    """The invocation cancellation authority won before result admission."""

    def __init__(self, *, cleanup_failed: bool = False) -> None:
        super().__init__("interrupted", cleanup_failed=cleanup_failed)


class B4ProcessInterrupted(B4ProcessError):
    """The parent was interrupted while waiting or terminating the child."""

    def __init__(self, *, cleanup_failed: bool = False) -> None:
        super().__init__("interrupted", cleanup_failed=cleanup_failed)


def run_b4_in_process(
    *,
    baseline_image: DecodedRgbImage,
    probe_image: DecodedRgbImage,
    source_width: int,
    source_height: int,
    roi: ConfirmationRoi,
    policy: ObjectPresenceDecisionPolicy,
    worker_spec: WorkerSpec,
    correlation_id: str,
    timeout_seconds: float,
    startup_timeout_seconds: float | None = None,
    cancellation: object | None = None,
    pid_observer: Callable[[int], None] | None = None,
    timing_sink: TimingSink | None = None,
    reference_only: bool = False,
) -> ClassificationResult | BinaryMask:
    """Compute B4 in one spawned child and accept only a fully reaped result."""
    if not _valid_timeout(timeout_seconds):
        raise B4ProcessError("worker_start_failed")
    startup_timeout = (
        timeout_seconds if startup_timeout_seconds is None else startup_timeout_seconds
    )
    if not _valid_timeout(startup_timeout):
        raise B4ProcessError("worker_start_failed")
    try:
        request = _build_request(
            baseline_image,
            probe_image,
            source_width,
            source_height,
            roi,
            policy,
            worker_spec,
            correlation_id,
            reference_only=reference_only,
        )
        encoded = _encode_json(request)
    except B4ProcessError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise B4ProcessError("worker_start_failed") from error
    if len(encoded) > MAX_PROTOCOL_BYTES:
        raise B4ProcessError("worker_start_failed")
    if _is_cancelled(cancellation):
        raise B4ProcessCancelled()
    context = multiprocessing.get_context("spawn")
    parent: Connection | None = None
    child: Connection | None = None
    process: multiprocessing.Process | None = None
    started = False
    primary_error: B4ProcessError | None = None
    result: ClassificationResult | BinaryMask | None = None
    ready = False
    process_started_at: float | None = None
    inference_started_at: float | None = None
    result_received_at: float | None = None
    ready_received_at: float | None = None
    child_timings: dict[str, int | str] = {}
    try:
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=_worker_entry,
            args=(child, encoded),
            name="vigi-phase7e-b4",
        )
        process.daemon = True
        try:
            process.start()
        except Exception as error:
            # A platform start failure normally occurs before a child exists,
            # but a partially-created Process must not be allowed to escape
            # this boundary if the platform reports a PID.
            if process.pid is not None:
                started = True
            raise B4ProcessError("worker_start_failed") from error
        started = True
        process_started_at = monotonic()
        startup_deadline = process_started_at + float(startup_timeout)
        inference_deadline: float | None = None
        if pid_observer is not None and process.pid is not None:
            try:
                pid_observer(process.pid)
            except Exception as error:
                raise B4ProcessError("worker_execution_failed") from error
        while True:
            if _is_cancelled(cancellation):
                raise B4ProcessCancelled()
            deadline = startup_deadline if not ready else inference_deadline
            assert deadline is not None
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise B4ProcessTimeout(stage="startup" if not ready else "inference")
            try:
                available = parent.poll(min(0.05, remaining))
            except (OSError, EOFError) as error:
                raise B4ProcessError("worker_execution_failed") from error
            if available:
                try:
                    raw = parent.recv_bytes(MAX_RESULT_BYTES + 1)
                except (OSError, EOFError, ValueError) as error:
                    raise B4ProcessError("worker_execution_failed") from error
                # Cancellation and deadline win over a result that became
                # available concurrently with the authority check.
                if _is_cancelled(cancellation):
                    raise B4ProcessCancelled()
                if monotonic() >= deadline:
                    raise B4ProcessTimeout(stage="startup" if not ready else "inference")
                message_kind = _message_kind(raw)
                if message_kind == "reference":
                    if not reference_only:
                        raise B4ProcessError("malformed_worker_protocol")
                    result = _decode_reference_mask(
                        raw, correlation_id, source_width, source_height
                    )
                    result_received_at = monotonic()
                    if inference_deadline is None or result_received_at >= inference_deadline:
                        raise B4ProcessTimeout(stage="inference")
                    return result
                if message_kind in {"ready", "timing", "inference"}:
                    if message_kind == "inference":
                        child_timings.update(_decode_timing(raw, correlation_id, "inference"))
                        inference_started_at = monotonic()
                        inference_deadline = inference_started_at + float(timeout_seconds)
                        continue
                    if ready:
                        child_timings.update(_decode_timing(raw, correlation_id, "timing"))
                        continue
                    child_timings = _decode_timing(raw, correlation_id, "ready")
                    ready = True
                    ready_received_at = monotonic()
                    inference_deadline = monotonic() + float(startup_timeout)
                    continue
                result = _decode_result(raw, correlation_id)
                result_received_at = monotonic()
                if _is_cancelled(cancellation):
                    raise B4ProcessCancelled()
                if inference_deadline is None or result_received_at >= inference_deadline:
                    raise B4ProcessTimeout(stage="inference")
                return result
            if not process.is_alive():
                # Drain one queued message before classifying an exit without
                # output; malformed/truncated output is never visual evidence.
                if _is_cancelled(cancellation):
                    raise B4ProcessCancelled()
                if monotonic() >= deadline:
                    raise B4ProcessTimeout(stage="startup" if not ready else "inference")
                try:
                    if parent.poll(0):
                        raw = parent.recv_bytes(MAX_RESULT_BYTES + 1)
                        message_kind = _message_kind(raw)
                        if message_kind == "reference":
                            if not reference_only:
                                raise B4ProcessError("malformed_worker_protocol")
                            result = _decode_reference_mask(
                                raw, correlation_id, source_width, source_height
                            )
                            result_received_at = monotonic()
                            if (
                                inference_deadline is None
                                or result_received_at >= inference_deadline
                            ):
                                raise B4ProcessTimeout(stage="inference")
                            return result
                        if message_kind in {"ready", "timing", "inference"}:
                            if message_kind == "inference":
                                child_timings.update(
                                    _decode_timing(raw, correlation_id, "inference")
                                )
                                inference_started_at = monotonic()
                                inference_deadline = inference_started_at + float(timeout_seconds)
                                continue
                            if ready:
                                child_timings.update(_decode_timing(raw, correlation_id, "timing"))
                                continue
                            child_timings = _decode_timing(raw, correlation_id, "ready")
                            ready = True
                            ready_received_at = monotonic()
                            inference_deadline = monotonic() + float(startup_timeout)
                            continue
                        result = _decode_result(raw, correlation_id)
                        result_received_at = monotonic()
                        if _is_cancelled(cancellation):
                            raise B4ProcessCancelled()
                        if inference_deadline is None or result_received_at >= inference_deadline:
                            raise B4ProcessTimeout(stage="inference")
                        return result
                except B4ProcessError:
                    raise
                except (OSError, EOFError, ValueError):
                    pass
                raise B4ProcessError("worker_abnormal_exit")
    except (KeyboardInterrupt, SystemExit) as error:
        primary_error = B4ProcessInterrupted()
        raise primary_error from error
    except B4ProcessError as error:
        primary_error = error
        raise
    except BaseException as error:
        primary_error = B4ProcessError("worker_execution_failed")
        raise primary_error from error
    finally:
        cleanup_started_at = monotonic()
        cleanup_failed = True
        exitcode: int | None = None
        try:
            cleanup_failed, exitcode = _cleanup_process_boundary(
                process,
                parent,
                child,
                started=started,
                allow_graceful_exit=result is not None,
                budget_seconds=_CLEANUP_CEILING_SECONDS,
            )
        except BaseException:
            # The cleanup owner is defensive by itself, but a patched or
            # platform-specific failure must never mask the primary outcome.
            cleanup_failed = True
        event: dict[str, int | str] = {
            "event": "phase7e.classifier_timing",
            "stage": (
                "timeout"
                if isinstance(primary_error, B4ProcessTimeout)
                else "failure"
                if primary_error is not None
                else "completed"
            ),
            "cleanup_ms": _elapsed_ms(cleanup_started_at),
        }
        if isinstance(primary_error, B4ProcessTimeout):
            event["timeout_stage"] = primary_error.stage
        if isinstance(primary_error, B4ProcessError):
            event["error_code"] = primary_error.code
        if process_started_at is not None:
            if ready and ready_received_at is not None:
                event["startup_ms"] = _elapsed_ms(process_started_at, ready_received_at)
            if result_received_at is not None:
                event["ipc_result_ms"] = _elapsed_ms(process_started_at, result_received_at)
            if inference_started_at is not None:
                event["inference_ms"] = _elapsed_ms(inference_started_at, result_received_at)
        event.update(child_timings)
        _emit_timing(event, timing_sink)
        if primary_error is not None:
            primary_error.cleanup_failed = primary_error.cleanup_failed or cleanup_failed
        elif result is not None:
            if exitcode != 0:
                raise B4ProcessError("worker_abnormal_exit", cleanup_failed=cleanup_failed)
            if cleanup_failed:
                raise B4ProcessError("worker_execution_failed", cleanup_failed=True)


def _valid_timeout(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _elapsed_ms(started: float, ended: float | None = None) -> int:
    return max(0, round(((monotonic() if ended is None else ended) - started) * 1000))


def _message_kind(raw: bytes) -> object:
    try:
        value = _decode_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value.get("kind") if isinstance(value, dict) else None


def _decode_timing(
    raw: bytes,
    expected_correlation: str,
    expected_kind: str,
) -> dict[str, int | str]:
    try:
        value = _decode_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise B4ProcessError("malformed_worker_protocol") from error
    if (
        not isinstance(value, dict)
        or set(value) != _READY_KEYS
        or value.get("version") != PROTOCOL_VERSION
        or value.get("correlation_id") != expected_correlation
        or value.get("kind") != expected_kind
        or not isinstance(value.get("timings_ms"), dict)
    ):
        raise B4ProcessError("malformed_worker_protocol")
    timings = value["timings_ms"]
    assert isinstance(timings, dict)
    if not set(timings).issubset(_TIMING_KEYS):
        raise B4ProcessError("malformed_worker_protocol")
    result: dict[str, int | str] = {}
    for key, timing in timings.items():
        if type(key) is not str or type(timing) is not int or timing < 0:
            raise B4ProcessError("malformed_worker_protocol")
        result[key] = timing
    return result


def _emit_timing(event: dict[str, int | str], sink: TimingSink | None) -> None:
    """Emit only fixed, non-sensitive classifier stage facts."""
    if sink is not None:
        try:
            sink(dict(event))
        except Exception:  # noqa: S110 - diagnostics cannot affect authority.
            pass
    try:
        _LOGGER.info("phase7e.classifier_timing %s", json.dumps(event, sort_keys=True))
    except Exception:  # noqa: S110 - diagnostics cannot affect authority.
        pass


def _emit_stage(sink: Callable[[str, int], None] | None, name: str, elapsed_ms: int) -> None:
    if sink is not None:
        try:
            sink(name, elapsed_ms)
        except Exception:  # noqa: S110 - diagnostics cannot affect authority.
            pass


def _build_request(
    baseline: DecodedRgbImage,
    probe: DecodedRgbImage,
    width: int,
    height: int,
    roi: ConfirmationRoi,
    policy: ObjectPresenceDecisionPolicy,
    worker_spec: object,
    correlation_id: str,
    *,
    reference_only: bool = False,
) -> dict[str, object]:
    """Build one exact primitive-only request after parent-side validation."""
    if not isinstance(worker_spec, (EfficientSamWorkerSpec, StaticMaskWorkerSpec)):
        raise B4ProcessError("worker_start_failed")
    expected = _expected_rgb_bytes(width, height)
    baseline_bytes = _image_bytes(baseline, expected)
    probe_bytes = _image_bytes(probe, expected)
    if (
        type(correlation_id) is not str
        or not correlation_id
        or len(correlation_id) > MAX_CORRELATION_CHARS
        or "\0" in correlation_id
    ):
        raise B4ProcessError("worker_start_failed")
    predictor = worker_spec.payload()
    request: dict[str, object] = {
        "version": PROTOCOL_VERSION,
        "correlation_id": correlation_id,
        "source_width": width,
        "source_height": height,
        "baseline_rgb24": _b64encode(baseline_bytes),
        "probe_rgb24": _b64encode(probe_bytes),
        "roi": roi.model_dump(mode="json"),
        "policy": policy.model_dump(mode="json"),
        "predictor": predictor,
    }
    if reference_only:
        request["reference_only"] = True
    return request


def _send_payload(connection: Connection, payload: dict[str, object]) -> None:
    try:
        encoded = _encode_json(payload)
        if len(encoded) <= MAX_RESULT_BYTES:
            connection.send_bytes(encoded)
    except (OSError, EOFError, ValueError):
        pass


def _worker_entry(connection: Connection, encoded: bytes) -> None:
    """Top-level spawn target with no parent capability or persistence access."""
    child_started_at = monotonic()
    timings: dict[str, int | str] = {}

    def stage_sink(name: str, elapsed_ms: int) -> None:
        if name in _TIMING_KEYS:
            timings[name] = elapsed_ms
        if name == "preprocessing_ms":
            _send_payload(
                connection,
                {
                    "version": PROTOCOL_VERSION,
                    "correlation_id": request["correlation_id"],
                    "kind": "inference",
                    "timings_ms": timings,
                },
            )

    try:
        request = _decode_request(encoded)
        timings["request_decoded_ms"] = _elapsed_ms(child_started_at)
        predictor = _predictor_from_payload(
            request["predictor"],
            request["source_width"],
            request["source_height"],
        )
        if isinstance(predictor, LazyEfficientSamPredictor):
            predictor.ensure_ready(stage_sink)
        else:
            timings["model_ready_ms"] = timings.get("request_decoded_ms", 0)
        timings["child_started_ms"] = _elapsed_ms(child_started_at)
        _send_payload(
            connection,
            {
                "version": PROTOCOL_VERSION,
                "correlation_id": request["correlation_id"],
                "kind": "ready",
                "timings_ms": timings,
            },
        )
        if request.get("reference_only") is True:
            result = _reference_mask(request, predictor)
            payload = {
                "version": PROTOCOL_VERSION,
                "correlation_id": request["correlation_id"],
                "kind": "reference",
                "mask": _encode_reference_mask(result),
            }
            _send_payload(connection, payload)
            try:
                connection.close()
            except OSError:
                pass
            return
        result = _compute(request, predictor=predictor, stage_sink=stage_sink)
        _send_payload(
            connection,
            {
                "version": PROTOCOL_VERSION,
                "correlation_id": request["correlation_id"],
                "kind": "timing",
                "timings_ms": timings,
            },
        )
        payload = {
            "version": PROTOCOL_VERSION,
            "correlation_id": request["correlation_id"],
            "kind": "result",
            "result": result.model_dump(mode="json"),
        }
    except RoiSuggestionUnavailableError:
        payload = {
            "version": PROTOCOL_VERSION,
            "correlation_id": _correlation_or_empty(encoded),
            "kind": "failure",
            "code": "classifier_unavailable",
        }
    except ClassificationPreparationError as error:
        payload = {
            "version": PROTOCOL_VERSION,
            "correlation_id": _correlation_or_empty(encoded),
            "kind": "failure",
            "code": _preparation_code(error.reason),
        }
    except Exception:
        payload = {
            "version": PROTOCOL_VERSION,
            "correlation_id": _correlation_or_empty(encoded),
            "kind": "failure",
            "code": "worker_execution_failed",
        }
    _send_payload(connection, payload)
    try:
        connection.close()
    except OSError:
        pass


def _compute(
    request: dict[str, object],
    *,
    predictor: object | None = None,
    stage_sink: Callable[[str, int], None] | None = None,
) -> ClassificationResult:
    """Reconstruct only validated values and run the shared pure computation."""
    width = request["source_width"]
    height = request["source_height"]
    assert type(width) is int and type(height) is int
    preprocessing_started = monotonic()
    baseline = _image_from_b64(request["baseline_rgb24"], width, height)
    probe = _image_from_b64(request["probe_rgb24"], width, height)
    _emit_stage(stage_sink, "decoder_calls", 2)
    _emit_stage(stage_sink, "duplicate_processing_count", 0)
    roi = ConfirmationRoi.model_validate(request["roi"])
    policy_payload = request["policy"]
    if not isinstance(policy_payload, dict):
        raise ValueError
    coefficients = policy_payload.get("luma_integer_coefficients")
    if not isinstance(coefficients, list):
        raise ValueError
    policy_payload = {
        **policy_payload,
        "luma_integer_coefficients": tuple(coefficients),
    }
    policy = ObjectPresenceDecisionPolicy.model_validate(policy_payload)
    if predictor is None:
        predictor = _predictor_from_payload(request["predictor"], width, height)
    _emit_stage(stage_sink, "preprocessing_ms", _elapsed_ms(preprocessing_started))
    started = monotonic()
    result = classify_decoded_images(
        baseline_image=baseline,
        probe_image=probe,
        source_width=width,
        source_height=height,
        roi=roi,
        policy=policy,
        mask_predictor=predictor,
        diagnostics_sink=stage_sink,
    )
    _emit_stage(stage_sink, "child_inference_ms", _elapsed_ms(started))
    return result


def _reference_mask(request: dict[str, object], predictor: object) -> BinaryMask:
    """Prepare one validated baseline mask for a run-scoped reference context."""
    width = request["source_width"]
    height = request["source_height"]
    assert type(width) is int and type(height) is int
    baseline = _image_from_b64(request["baseline_rgb24"], width, height)
    roi = ConfirmationRoi.model_validate(request["roi"])
    policy = _policy_from_request(request)
    point = Point(roi.x + (roi.width - 1) // 2, roi.y + (roi.height - 1) // 2)
    size = ImageSize(width, height)
    prediction = predictor.predict_from_rgb(baseline, point, size)
    return _prediction_to_mask(prediction, size, point, policy)


def _policy_from_request(request: dict[str, object]) -> ObjectPresenceDecisionPolicy:
    policy_payload = request["policy"]
    if not isinstance(policy_payload, dict):
        raise ValueError
    coefficients = policy_payload.get("luma_integer_coefficients")
    if not isinstance(coefficients, list):
        raise ValueError
    return ObjectPresenceDecisionPolicy.model_validate(
        {**policy_payload, "luma_integer_coefficients": tuple(coefficients)}
    )


class _StaticPredictor:
    def __init__(self, baseline: BinaryMask, probe: BinaryMask, delay_seconds: float) -> None:
        self._values = (baseline, probe)
        self._index = 0
        self._delay_seconds = delay_seconds

    def predict_from_rgb(self, image: DecodedRgbImage, point: object, size: object) -> BinaryMask:
        if self._index >= len(self._values):
            raise ValueError
        if self._delay_seconds:
            sleep(self._delay_seconds)
        result = self._values[self._index]
        self._index += 1
        return result


def _predictor_from_payload(payload: object, width: int, height: int) -> object:
    if not isinstance(payload, dict) or set(payload) != _PREDICTOR_KEYS.get(
        str(payload.get("kind")), frozenset()
    ):
        raise ValueError
    kind = payload.get("kind")
    if kind == "efficient_sam":
        path = payload.get("checkpoint_path")
        digest = payload.get("expected_sha256")
        device = payload.get("device_mode")
        if (
            type(path) is not str
            or not path
            or len(path) > MAX_CHECKPOINT_PATH_CHARS
            or "\0" in path
            or type(digest) is not str
            or _SHA256_PATTERN.fullmatch(digest) is None
            or device not in {"cpu", "cuda", "auto"}
        ):
            raise ValueError
        return LazyEfficientSamPredictor(Path(path), digest, device)
    if kind == "static_masks":
        baseline = BinaryMask.from_rows(_rows(payload.get("baseline_rows"), width, height))
        probe = BinaryMask.from_rows(_rows(payload.get("probe_rows"), width, height))
        delay = payload.get("delay_seconds")
        if (
            type(delay) is not float
            or not math.isfinite(delay)
            or not 0 <= delay <= MAX_STATIC_DELAY_SECONDS
        ):
            raise ValueError
        return _StaticPredictor(baseline, probe, delay)
    raise ValueError


def _rows(value: object, width: int, height: int) -> tuple[tuple[bool, ...], ...]:
    if (
        not isinstance(value, list)
        or len(value) != height
        or any(not isinstance(row, list) or len(row) != width for row in value)
        or any(type(cell) is not bool for row in value for cell in row)
    ):
        raise ValueError
    return tuple(tuple(row) for row in value)


def _decode_request(encoded: bytes) -> dict[str, object]:
    if type(encoded) is not bytes or not encoded or len(encoded) > MAX_PROTOCOL_BYTES:
        raise ValueError
    value = _decode_json(encoded)
    if not isinstance(value, dict):
        raise ValueError
    keys = frozenset(value.keys())
    if keys not in {_REQUEST_KEYS, _REFERENCE_REQUEST_KEYS}:
        raise ValueError
    if value.get("version") != PROTOCOL_VERSION:
        raise ValueError
    correlation = value.get("correlation_id")
    if (
        type(correlation) is not str
        or not correlation
        or len(correlation) > MAX_CORRELATION_CHARS
        or "\0" in correlation
    ):
        raise ValueError
    width = value.get("source_width")
    height = value.get("source_height")
    _ = _expected_rgb_bytes(width, height)
    _ = _decode_b64(value.get("baseline_rgb24"))
    _ = _decode_b64(value.get("probe_rgb24"))
    if not isinstance(value.get("roi"), dict) or not isinstance(value.get("policy"), dict):
        raise ValueError
    if not isinstance(value.get("predictor"), dict):
        raise ValueError
    if "reference_only" in value and type(value["reference_only"]) is not bool:
        raise ValueError
    return value


def _encode_reference_mask(mask: BinaryMask) -> str:
    bits = bytearray((mask.width * mask.height + 7) // 8)
    for index, value in enumerate(value for row in mask.rows for value in row):
        if value:
            bits[index // 8] |= 1 << (index % 8)
    return base64.b64encode(bits).decode("ascii")


def _decode_reference_mask(
    raw: bytes, expected_correlation: str, width: int, height: int
) -> BinaryMask:
    try:
        value = _decode_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise B4ProcessError("malformed_worker_protocol") from error
    if (
        not isinstance(value, dict)
        or set(value) != _REFERENCE_KEYS
        or value.get("version") != PROTOCOL_VERSION
        or value.get("correlation_id") != expected_correlation
        or value.get("kind") != "reference"
    ):
        raise B4ProcessError("malformed_worker_protocol")
    encoded_mask = value.get("mask")
    if not isinstance(encoded_mask, str):
        raise B4ProcessError("invalid_classifier_output")
    try:
        packed = base64.b64decode(encoded_mask.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as error:
        raise B4ProcessError("invalid_classifier_output") from error
    cell_count = width * height
    expected_bytes = (cell_count + 7) // 8
    if (
        width <= 0
        or height <= 0
        or len(packed) != expected_bytes
        or (
            cell_count % 8
            and packed[-1] & ~((1 << (cell_count % 8)) - 1)
        )
    ):
        raise B4ProcessError("invalid_classifier_output")
    rows = tuple(
        tuple(
            bool(packed[(y * width + x) // 8] & (1 << ((y * width + x) % 8)))
            for x in range(width)
        )
        for y in range(height)
    )
    try:
        return BinaryMask.from_rows(rows)
    except (TypeError, ValueError) as error:
        raise B4ProcessError("invalid_classifier_output") from error


def _decode_result(raw: bytes, expected_correlation: str) -> ClassificationResult:
    if type(raw) is not bytes or not raw or len(raw) > MAX_RESULT_BYTES:
        raise B4ProcessError("malformed_worker_protocol")
    try:
        value = _decode_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise B4ProcessError("malformed_worker_protocol") from error
    if not isinstance(value, dict):
        raise B4ProcessError("malformed_worker_protocol")
    if (
        value.get("version") != PROTOCOL_VERSION
        or value.get("correlation_id") != expected_correlation
    ):
        raise B4ProcessError("malformed_worker_protocol")
    kind = value.get("kind")
    if kind == "result":
        if set(value) != _RESULT_KEYS or not isinstance(value.get("result"), dict):
            raise B4ProcessError("malformed_worker_protocol")
        try:
            from vigi_vision.object_presence_evidence import ClassificationResult

            return ClassificationResult.model_validate_json(_encode_json(value["result"]))
        except Exception as error:
            raise B4ProcessError("invalid_classifier_output") from error
    if kind == "failure":
        if set(value) != _FAILURE_KEYS or value.get("code") not in _FAILURE_CODES:
            raise B4ProcessError("malformed_worker_protocol")
        raise B4ProcessError(str(value["code"]))
    raise B4ProcessError("malformed_worker_protocol")


def _preparation_code(reason: ClassificationPreparationReason) -> str:
    if reason is ClassificationPreparationReason.CLASSIFIER_UNAVAILABLE:
        return "classifier_unavailable"
    if reason is ClassificationPreparationReason.INVALID_CLASSIFIER_OUTPUT:
        return "invalid_classifier_output"
    return "classifier_execution_failed"


def _correlation_or_empty(encoded: bytes) -> str:
    try:
        value = _decode_json(encoded)
        correlation = value.get("correlation_id") if isinstance(value, dict) else ""
        return correlation if type(correlation) is str else ""
    except Exception:
        return ""


def _encode_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _decode_json(value: bytes) -> object:
    return json.loads(value.decode("utf-8"))


def _expected_rgb_bytes(width: object, height: object) -> int:
    if (
        type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
        or width * height * 3 > 256 * 1024 * 1024
    ):
        raise ValueError
    return width * height * 3


def _image_bytes(image: DecodedRgbImage, expected: int) -> bytes:
    payload = bytes(channel for row in image.pixels for pixel in row for channel in pixel)
    if len(payload) != expected:
        raise B4ProcessError("worker_start_failed")
    return payload


def _image_from_b64(value: object, width: int, height: int) -> DecodedRgbImage:
    payload = _decode_b64(value)
    expected = _expected_rgb_bytes(width, height)
    if len(payload) != expected:
        raise ValueError
    rows = tuple(
        tuple(
            (
                payload[(y * width + x) * 3],
                payload[(y * width + x) * 3 + 1],
                payload[(y * width + x) * 3 + 2],
            )
            for x in range(width)
        )
        for y in range(height)
    )
    return DecodedRgbImage.from_rows(rows)


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_b64(value: object) -> bytes:
    if type(value) is not str:
        raise ValueError
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise ValueError from None


def _is_cancelled(cancellation: object | None) -> bool:
    if cancellation is None:
        return False
    try:
        return bool(cancellation())
    except Exception:
        # A broken authority callback must fail closed.  Treating it as
        # inactive would allow a result to cross the final evidence gate.
        return True


def _cleanup_process_boundary(
    process: multiprocessing.Process | None,
    connection: Connection | None,
    child_connection: Connection | None,
    *,
    started: bool,
    allow_graceful_exit: bool,
    budget_seconds: float,
) -> tuple[bool, int | None]:
    """Own every post-creation process/IPC cleanup path exactly once."""
    cleanup_failed = False
    exitcode: int | None = None
    reaped = False
    deadline = monotonic() + max(0.0, min(_CLEANUP_CEILING_SECONDS, budget_seconds))
    if process is not None and started:
        alive = True
        if allow_graceful_exit:
            try:
                remaining = max(0.0, deadline - monotonic())
                process.join(timeout=remaining)
            except BaseException:
                cleanup_failed = True
        try:
            alive = process.is_alive()
        except BaseException:
            cleanup_failed = True
        if not alive and not allow_graceful_exit:
            try:
                remaining = max(0.0, deadline - monotonic())
                process.join(timeout=remaining)
            except BaseException:
                cleanup_failed = True
        if alive:
            try:
                process.terminate()
            except BaseException:
                cleanup_failed = True
            try:
                remaining = max(0.0, deadline - monotonic())
                process.join(timeout=remaining)
            except BaseException:
                cleanup_failed = True
        try:
            alive = process.is_alive()
        except BaseException:
            cleanup_failed = True
            alive = True
        if alive:
            try:
                process.kill()
            except BaseException:
                cleanup_failed = True
            try:
                remaining = max(0.0, deadline - monotonic())
                process.join(timeout=remaining)
            except BaseException:
                cleanup_failed = True
            try:
                alive = process.is_alive()
            except BaseException:
                cleanup_failed = True
                alive = True
        if alive:
            cleanup_failed = True
        else:
            reaped = True
            try:
                exitcode = process.exitcode
            except BaseException:
                cleanup_failed = True
    elif process is not None:
        # A start failure may leave an unstarted Process wrapper behind.  It
        # has no child to reap, but its native handle still belongs to us.
        try:
            process.close()
        except BaseException:
            cleanup_failed = True

    if not _close_connection(connection):
        cleanup_failed = True
    if not _close_connection(child_connection):
        cleanup_failed = True

    if process is not None and started and reaped:
        # close() is deliberately reached only after the final is_alive check
        # above proved the child was reaped.
        try:
            process.close()
        except BaseException:
            cleanup_failed = True
    return cleanup_failed, exitcode


def _close_connection(connection: Connection | None) -> bool:
    if connection is None:
        return True
    try:
        connection.close()
    except BaseException:
        return False
    return True


__all__ = [
    "B4ProcessCancelled",
    "B4ProcessError",
    "B4ProcessInterrupted",
    "B4ProcessTimeout",
    "EfficientSamWorkerSpec",
    "StaticMaskWorkerSpec",
    "run_b4_in_process",
]
