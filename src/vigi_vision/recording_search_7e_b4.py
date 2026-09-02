"""Persistence-neutral production B4 adapter for Phase 7E.

The legacy B3/B4 services remain responsible for schema 2/3 admission and
publication.  This module only validates the already reopened Phase 7E input,
loads the immutable Phase 6 baseline through its trusted service boundary, and
invokes the same pure mask/comparison computation used by the legacy path. The
production computation is process-isolated so timeout/cancellation can
terminate and reap the worker before any output is admitted.
"""

# Adapter validation intentionally receives protocol-shaped reopened records;
# suppress only static diagnostics that cannot refine those runtime boundaries.
# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportImplicitOverride=false, reportUnnecessaryIsInstance=false, reportUnknownMemberType=false

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from vigi_vision.assisted_roi_predictor import LazyEfficientSamPredictor
from vigi_vision.investigation_confirmation_integrity import (
    compute_jpeg_integrity_from_bytes,
)
from vigi_vision.investigation_confirmation_models import (
    ConfirmedInvestigationInput,
)
from vigi_vision.object_presence_values import DecodedRgbImage
from vigi_vision.recording_search_7e_1c import (
    B4Bridge,
    CommonSessionCancelledError,
    CommonSessionValidationError,
    Phase7EB4Input,
)
from vigi_vision.recording_search_7e_b4_process import (
    B4ProcessCancelled,
    B4ProcessError,
    B4ProcessInterrupted,
    B4ProcessTimeout,
    EfficientSamWorkerSpec,
    StaticMaskWorkerSpec,
    run_b4_in_process,
)
from vigi_vision.recording_search_7e_models import StrictIdentityEnvelope
from vigi_vision.recording_search_b3_masks import LimitedRgbMaskPredictor
from vigi_vision.recording_search_b3_media import DecodedMedia
from vigi_vision.recording_search_b3_models import (
    ClassificationPreparationError,
)
from vigi_vision.recording_search_b4_models import ClassificationOperationalReason
from vigi_vision.recording_search_b4_support import map_preparation_reason

if TYPE_CHECKING:
    from vigi_vision.investigation_confirmation_models import ConfirmationRoi
    from vigi_vision.object_presence_evidence import ClassificationResult
    from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
    from vigi_vision.recording_search_b3_contracts import MediaDecoder
    from vigi_vision.recording_search_b3_masks import MaskPredictor


ConfirmedInputLoader = Callable[[str], ConfirmedInvestigationInput]


@dataclass(frozen=True, slots=True)
class Phase7EProductionB4Adapter(B4Bridge):
    """Run authoritative B4 computation without legacy persistence."""

    confirmation_loader: ConfirmedInputLoader = field(repr=False)
    media_decoder: MediaDecoder = field(repr=False)
    mask_predictor: MaskPredictor | None = field(repr=False)
    policy: ObjectPresenceDecisionPolicy = field(repr=False)

    def classify(self, authoritative: Phase7EB4Input) -> StrictIdentityEnvelope:
        """Validate reopened authority, then return one Phase 7 operation."""
        timeout = authoritative.budget.admit_classification()
        self._validate_phase7_input(authoritative)
        confirmed = self._load_baseline(authoritative.run.investigation_id)
        if (
            authoritative.frame.width != confirmed.source_width
            or authoritative.frame.height != confirmed.source_height
        ):
            raise CommonSessionValidationError
        baseline = self._decode_baseline(confirmed)
        probe = self._probe_image(authoritative)
        try:
            worker_spec = _worker_spec(self.mask_predictor)
        except (TypeError, ValueError):
            return _operational_completion(
                authoritative,
                confirmed.reference_frame_resource_id,
                ClassificationOperationalReason.CLASSIFIER_EXECUTION_FAILED,
            )
        try:
            result = _bounded_classification(
                timeout,
                baseline.image,
                probe,
                confirmed.source_width,
                confirmed.source_height,
                confirmed.roi,
                self.policy,
                worker_spec,
                correlation_id=authoritative.classification_attempt_id,
                cancellation=getattr(authoritative.budget, "cancellation", None),
            )
        except B4ProcessTimeout:
            return _operational_completion(
                authoritative,
                confirmed.reference_frame_resource_id,
                ClassificationOperationalReason.CLASSIFIER_TIMEOUT,
            )
        except (B4ProcessCancelled, B4ProcessInterrupted):
            raise CommonSessionCancelledError from None
        except B4ProcessError as error:
            reason = (
                ClassificationOperationalReason.INVALID_CLASSIFIER_OUTPUT
                if error.code == "invalid_classifier_output"
                else ClassificationOperationalReason.CLASSIFIER_EXECUTION_FAILED
            )
            return _operational_completion(
                authoritative,
                confirmed.reference_frame_resource_id,
                reason,
            )
        except ClassificationPreparationError as error:
            return _operational_completion(
                authoritative,
                confirmed.reference_frame_resource_id,
                _operational_reason(error),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _operational_completion(
                authoritative,
                confirmed.reference_frame_resource_id,
                ClassificationOperationalReason.CLASSIFIER_EXECUTION_FAILED,
            )
        authoritative.budget.check()
        comparison = result.comparison.model_dump(mode="python")
        evidence = _evidence_payload(comparison)
        return StrictIdentityEnvelope.from_payload(
            "classification-operation",
            {
                "investigation_id": authoritative.run.investigation_id,
                "run_id": authoritative.run.run_id,
                "frame_id": authoritative.frame_record.identity,
                "target_request_id": authoritative.target_request.identity,
                "baseline_identity": confirmed.reference_frame_resource_id,
                "classifier_policy_id": _classifier_policy_id(authoritative),
                "attempt": 1,
                "result_kind": "VISUAL",
                "outcome": result.outcome.value,
                "reason_code": None if result.reason_code is None else result.reason_code.value,
                "classifier_evidence": evidence,
                "operational_reason": None,
            },
        )

    def _validate_phase7_input(self, authoritative: Phase7EB4Input) -> None:
        """Bind every supplied value to the strictly reopened run tree."""
        run = authoritative.run
        frame = authoritative.frame_record
        target = authoritative.target_request
        if (
            frame.family != "frame"
            or target.family != "target-request"
            or type(authoritative.frame_jpeg_bytes) is not bytes
            or not authoritative.classification_attempt_id
        ):
            raise CommonSessionValidationError
        frame_payload = frame.payload
        target_payload = target.payload
        if (
            frame_payload.get("investigation_id") != run.investigation_id
            or frame_payload.get("run_id") != run.run_id
            or target_payload.get("investigation_id") != run.investigation_id
            or target_payload.get("run_id") != run.run_id
            or frame_payload.get("target_request_id") != target.identity
            or frame_payload.get("common_session_id")
            != run.manifest.payload.get("common_session_id")
            or frame_payload.get("common_session_id")
            != run.manifest.payload.get("common_session_id")
        ):
            raise CommonSessionValidationError
        stored = run.frame_bytes.get(frame.identity)
        if type(stored) is not bytes or stored != authoritative.frame_jpeg_bytes:
            raise CommonSessionValidationError
        width = frame_payload.get("width")
        height = frame_payload.get("height")
        if width != authoritative.frame.width or height != authoritative.frame.height:
            raise CommonSessionValidationError
        try:
            authoritative.frame.validate()
            digest = compute_jpeg_integrity_from_bytes(
                authoritative.frame_jpeg_bytes, width, height
            )
        except Exception as error:
            raise CommonSessionValidationError from error
        if (
            digest.sha256 != frame_payload.get("jpeg_sha256")
            or digest.size_bytes != frame_payload.get("jpeg_size_bytes")
            or hashlib.sha256(authoritative.frame.rgb24_bytes).hexdigest()
            != frame_payload.get("rgb24_sha256")
        ):
            raise CommonSessionValidationError
        if authoritative.frame.requested_time_utc != _target_time(target_payload):
            raise CommonSessionValidationError
        if not any(
            item.identity == frame.identity and item.family == "frame" for item in run.records
        ):
            raise CommonSessionValidationError
        if not any(
            item.identity == target.identity and item.family == "target-request"
            for item in run.records
        ):
            raise CommonSessionValidationError
        _ = _classifier_policy_id(authoritative)

    def _load_baseline(self, investigation_id: str) -> ConfirmedInvestigationInput:
        """Load only the server-owned, strictly revalidated Phase 6 input."""
        try:
            confirmed = self.confirmation_loader(investigation_id)
        except Exception as error:
            raise CommonSessionValidationError from error
        if (
            not isinstance(confirmed, ConfirmedInvestigationInput)
            or confirmed.investigation_id != investigation_id
            or not isinstance(confirmed.jpeg_path, Path)
            or not confirmed.jpeg_path.is_file()
        ):
            raise CommonSessionValidationError
        return confirmed

    def _decode_baseline(self, confirmed: ConfirmedInvestigationInput) -> DecodedMedia:
        """Read and verify the exact trusted baseline bytes before decoding."""
        try:
            payload = confirmed.jpeg_path.read_bytes()
            digest = compute_jpeg_integrity_from_bytes(
                payload, confirmed.source_width, confirmed.source_height
            )
        except Exception as error:
            raise CommonSessionValidationError from error
        if digest.sha256 != confirmed.jpeg_sha256 or digest.size_bytes != confirmed.jpeg_size_bytes:
            raise CommonSessionValidationError
        try:
            decoded = self.media_decoder.decode(
                payload, confirmed.source_width, confirmed.source_height
            )
        except Exception as error:
            raise CommonSessionValidationError from error
        if (
            not isinstance(decoded, DecodedMedia)
            or decoded.integrity.sha256 != digest.sha256
            or decoded.integrity.size_bytes != digest.size_bytes
            or decoded.image.width != confirmed.source_width
            or decoded.image.height != confirmed.source_height
        ):
            raise CommonSessionValidationError
        return decoded

    def _probe_image(self, authoritative: Phase7EB4Input) -> DecodedRgbImage:
        """Convert only the reopened frame's verified RGB24 bytes."""
        frame = authoritative.frame
        try:
            rows = tuple(
                tuple(
                    (
                        frame.rgb24_bytes[(y * frame.width + x) * 3],
                        frame.rgb24_bytes[(y * frame.width + x) * 3 + 1],
                        frame.rgb24_bytes[(y * frame.width + x) * 3 + 2],
                    )
                    for x in range(frame.width)
                )
                for y in range(frame.height)
            )
            image = DecodedRgbImage.from_rows(rows)
        except (IndexError, TypeError, ValueError) as error:
            raise CommonSessionValidationError from error
        if image.width != frame.width or image.height != frame.height:
            raise CommonSessionValidationError
        return image


def _bounded_classification(  # noqa: PLR0913
    timeout: float,
    baseline: DecodedRgbImage,
    probe: DecodedRgbImage,
    width: int,
    height: int,
    roi: ConfirmationRoi,
    policy: ObjectPresenceDecisionPolicy,
    predictor: object,
    *,
    correlation_id: str = "phase7e-b4",
    cancellation: object | None = None,
) -> ClassificationResult:
    """Run the shared computation under the Phase 7 bounded classifier budget."""
    return run_b4_in_process(
        baseline_image=baseline,
        probe_image=probe,
        source_width=width,
        source_height=height,
        roi=roi,
        policy=policy,
        worker_spec=predictor,
        correlation_id=correlation_id,
        timeout_seconds=timeout,
        cancellation=cancellation,
    )


def _worker_spec(predictor: object) -> EfficientSamWorkerSpec | StaticMaskWorkerSpec:
    """Convert only explicitly supported predictors to primitive child config."""
    if isinstance(predictor, StaticMaskWorkerSpec):
        return predictor
    if isinstance(predictor, LazyEfficientSamPredictor):
        return EfficientSamWorkerSpec(
            predictor.checkpoint_path,
            predictor.expected_sha256,
            predictor.device_mode,
        )
    if isinstance(predictor, LimitedRgbMaskPredictor):
        source = predictor.service.predictor
        if isinstance(source, LazyEfficientSamPredictor):
            return EfficientSamWorkerSpec(
                source.checkpoint_path,
                source.expected_sha256,
                source.device_mode,
            )
    raise ValueError


def _classifier_policy_id(authoritative: Phase7EB4Input) -> str:
    values = tuple(item for item in authoritative.run.records if item.family == "classifier-policy")
    expected = authoritative.run.manifest.payload.get("classifier_policy_id")
    if len(values) != 1 or values[0].identity != expected:
        raise CommonSessionValidationError
    return values[0].identity


def _target_time(payload: dict[str, object]) -> datetime:
    value = payload.get("requested_time_utc")
    if type(value) is not str:
        raise CommonSessionValidationError
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise CommonSessionValidationError from error


def _operational_reason(error: ClassificationPreparationError) -> ClassificationOperationalReason:
    mapped = map_preparation_reason(error.reason)
    if mapped is ClassificationOperationalReason.CLASSIFIER_TIMEOUT:
        return mapped
    if mapped is ClassificationOperationalReason.INVALID_CLASSIFIER_OUTPUT:
        return mapped
    return ClassificationOperationalReason.CLASSIFIER_EXECUTION_FAILED


def _operational_completion(
    authoritative: Phase7EB4Input,
    baseline_identity: str,
    reason: ClassificationOperationalReason,
) -> StrictIdentityEnvelope:
    return StrictIdentityEnvelope.from_payload(
        "classification-operation",
        {
            "investigation_id": authoritative.run.investigation_id,
            "run_id": authoritative.run.run_id,
            "frame_id": authoritative.frame_record.identity,
            "target_request_id": authoritative.target_request.identity,
            "baseline_identity": baseline_identity,
            "classifier_policy_id": _classifier_policy_id(authoritative),
            "attempt": 1,
            "result_kind": "OPERATIONAL",
            "outcome": None,
            "reason_code": None,
            "classifier_evidence": None,
            "operational_reason": _canonical_operational_reason(reason),
        },
    )


def _canonical_operational_reason(reason: ClassificationOperationalReason) -> str:
    """Map internal B4 categories to the closed Phase 7E vocabulary."""
    if reason is ClassificationOperationalReason.CLASSIFIER_TIMEOUT:
        return "classifier_timeout"
    if reason is ClassificationOperationalReason.INVALID_CLASSIFIER_OUTPUT:
        return "invalid_classifier_result"
    return "classification_failed"


def _evidence_payload(raw: dict[str, object]) -> dict[str, object]:
    decimal_fields = {
        "baseline_mask_coverage",
        "probe_mask_coverage",
        "mask_iou",
        "roi_luma_ncc",
    }
    result: dict[str, object] = {}
    for key, value in raw.items():
        if value is None:
            result[key] = None
        elif key in decimal_fields:
            result[key] = f"{value:.6f}"
        elif hasattr(value, "value"):
            result[key] = value.value
        else:
            result[key] = value
    return result


__all__ = ["Phase7EProductionB4Adapter"]
