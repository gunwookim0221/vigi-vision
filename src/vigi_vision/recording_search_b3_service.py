"""Handle-owned Phase 7B-3 admission and non-authoritative classification."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from vigi_vision.investigation_confirmation_integrity import (
    compute_jpeg_integrity_from_bytes,
)
from vigi_vision.investigation_confirmation_models import ConfirmationArtifactError
from vigi_vision.object_presence_comparator import ClassifierInput, ObjectPresenceClassifier
from vigi_vision.recording_search_a2_models import (
    RecordingSearchManifestV2,
)
from vigi_vision.recording_search_a2_service import (
    AdmittedProbeFrame,
    admit_probe_frame_bytes,
)
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b3_duplicates import find_canonical_duplicate
from vigi_vision.recording_search_b3_masks import (
    MaskPredictor,
    predict_masks_for_images,
)
from vigi_vision.recording_search_b3_media import DecodedMedia
from vigi_vision.recording_search_b3_models import (
    CanonicalDuplicateResult,
    ClassificationPreparationError,
    ClassificationPreparationReason,
    ClassificationPreparationResult,
    ClassificationSnapshot,
    ClassifyRecordingProbeRequest,
    NonAuthoritativeClassificationResult,
)
from vigi_vision.recording_search_b3_snapshot import build_classification_snapshot
from vigi_vision.recording_search_models import (
    RecordingSearchBaselineError,
    RecordingSearchError,
    RecordingSearchState,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from vigi_vision.investigation_confirmation_models import ConfirmationRoi
    from vigi_vision.object_presence_evidence import ClassificationResult
    from vigi_vision.object_presence_models import DecodedRgbImage
    from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
    from vigi_vision.recording_search_b3_contracts import (
        ClassificationHandle,
        ClassificationHost,
        MediaDecoder,
    )


@dataclass(frozen=True, slots=True)
class RecordingSearchClassificationService:
    """Compose the active handle, byte admission, decoder, predictor, and comparator."""

    host: ClassificationHost
    media_decoder: MediaDecoder | None = None
    mask_predictor: MaskPredictor | None = None
    policy: ObjectPresenceDecisionPolicy | None = None

    def classify(
        self,
        handle: ClassificationHandle,
        request: ClassifyRecordingProbeRequest,
    ) -> ClassificationPreparationResult:
        """Return a typed in-memory result without invoking persistence."""
        with self._mutation(handle, request):
            captured = self.capture_locked(handle, request)
        if isinstance(captured, CanonicalDuplicateResult):
            return captured
        return self.classify_snapshot(captured)

    def capture_locked(
        self,
        handle: ClassificationHandle,
        request: ClassifyRecordingProbeRequest,
    ) -> ClassificationSnapshot | CanonicalDuplicateResult:
        """Capture one immutable input while the caller owns the mutation mutex."""
        manifest = self._load_manifest(handle, request)
        duplicate = self._duplicate(manifest, request)
        if duplicate is not None:
            return duplicate
        policy = self.policy
        if policy is None:
            _fail(ClassificationPreparationReason.POLICY_IDENTITY_MISMATCH)
        probe = self._admit_probe(handle, request)
        if type(handle.baseline_bytes) is not bytes:
            _fail(ClassificationPreparationReason.BASELINE_CORRUPT)
        baseline_bytes = bytes(handle.baseline_bytes)
        baseline_media = self._decode_baseline(manifest, baseline_bytes)
        probe_media = self._decode_probe(manifest, probe)
        return build_classification_snapshot(
            manifest,
            request,
            probe,
            baseline_bytes,
            baseline_media,
            probe_media,
            policy,
        )

    def classify_snapshot(
        self, snapshot: ClassificationSnapshot
    ) -> NonAuthoritativeClassificationResult:
        """Classify only the immutable snapshot without repository authority."""
        result = classify_decoded_images(
            baseline_image=snapshot.baseline_image,
            probe_image=snapshot.probe_image,
            source_width=snapshot.source_width,
            source_height=snapshot.source_height,
            roi=snapshot.confirmed_roi,
            policy=snapshot.policy,
            mask_predictor=self.mask_predictor,
        )
        return NonAuthoritativeClassificationResult(snapshot, result)

    def find_duplicate_locked(
        self,
        manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
        request: ClassifyRecordingProbeRequest,
    ) -> CanonicalDuplicateResult | None:
        """Resolve a committed canonical observation under the mutation mutex."""
        return self._duplicate(manifest, request)

    @contextmanager
    def _mutation(
        self, handle: ClassificationHandle, request: ClassifyRecordingProbeRequest
    ) -> Generator[None, None, None]:
        if handle.closed:
            _fail(ClassificationPreparationReason.INACTIVE_HANDLE)
        if (
            handle.investigation_id != request.investigation_id
            or handle.search_run_id != request.search_run_id
        ):
            _fail(ClassificationPreparationReason.OWNERSHIP_MISMATCH)
        try:
            with self.host.a2_mutation(handle):
                yield
        except RecordingSearchBaselineError:
            _fail(ClassificationPreparationReason.INACTIVE_HANDLE)

    def _load_manifest(
        self,
        handle: ClassificationHandle,
        request: ClassifyRecordingProbeRequest,
    ) -> RecordingSearchManifestV2 | RecordingSearchManifestV3:
        try:
            manifest = self.host.repository.load_for_probe_admission(
                request.investigation_id,
                request.search_run_id,
            )
        except RecordingSearchError:
            _fail(ClassificationPreparationReason.STALE_MANIFEST)
        if not isinstance(manifest, RecordingSearchManifestV2 | RecordingSearchManifestV3):
            _fail(ClassificationPreparationReason.STALE_MANIFEST)
        if manifest.state not in {RecordingSearchState.RUNNING, "RUNNING"}:
            _fail(ClassificationPreparationReason.LIFECYCLE_NOT_ELIGIBLE)
        if (
            manifest.investigation_id != handle.investigation_id
            or manifest.search_run_id != handle.search_run_id
        ):
            _fail(ClassificationPreparationReason.OWNERSHIP_MISMATCH)
        return manifest

    def _duplicate(
        self,
        manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
        request: ClassifyRecordingProbeRequest,
    ) -> CanonicalDuplicateResult | None:
        if not isinstance(manifest, RecordingSearchManifestV3):
            return None
        return find_canonical_duplicate(self.host.repository, manifest, request)

    def _admit_probe(
        self,
        handle: ClassificationHandle,
        request: ClassifyRecordingProbeRequest,
    ) -> AdmittedProbeFrame:
        try:
            return admit_probe_frame_bytes(
                self.host,
                handle.investigation_id,
                handle.search_run_id,
                request.probe_request_id,
            )
        except RecordingSearchError:
            _fail(ClassificationPreparationReason.PROBE_ARTIFACT_CORRUPT)

    def _decode_baseline(
        self, manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3, payload: bytes
    ) -> DecodedMedia:
        confirmation = manifest.confirmation
        try:
            integrity = compute_jpeg_integrity_from_bytes(
                payload,
                confirmation.source_width,
                confirmation.source_height,
            )
        except ConfirmationArtifactError:
            _fail(ClassificationPreparationReason.BASELINE_CORRUPT)
        if (
            integrity.sha256 != confirmation.jpeg_sha256
            or integrity.size_bytes != confirmation.jpeg_size_bytes
        ):
            _fail(ClassificationPreparationReason.BASELINE_CORRUPT)
        return self._decode(payload, confirmation.source_width, confirmation.source_height)

    def _decode_probe(
        self,
        manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
        probe: AdmittedProbeFrame,
    ) -> DecodedMedia:
        confirmation = manifest.confirmation
        if (
            probe.frame.source_width != confirmation.source_width
            or probe.frame.source_height != confirmation.source_height
        ):
            _fail(ClassificationPreparationReason.INVALID_REQUEST_FRAME)
        return self._decode(probe.jpeg_bytes, confirmation.source_width, confirmation.source_height)

    def _decode(self, payload: bytes, width: int, height: int) -> DecodedMedia:
        decoder = self.media_decoder
        if decoder is None:
            _fail(ClassificationPreparationReason.INVALID_MEDIA_INPUT)
        try:
            decoded = decoder.decode(payload, width, height)
        except Exception:  # noqa: BLE001 - decoder failures are one safe category.
            _fail(ClassificationPreparationReason.INVALID_MEDIA_INPUT)
        else:
            if (
                type(decoded) is not DecodedMedia
                or decoded.image.width != width
                or decoded.image.height != height
                or decoded.integrity.size_bytes != len(payload)
                or decoded.integrity.sha256 != hashlib.sha256(payload).hexdigest()
            ):
                _fail(ClassificationPreparationReason.INVALID_MEDIA_INPUT)
            return decoded


def classify_decoded_images(  # noqa: PLR0913
    *,
    baseline_image: DecodedRgbImage,
    probe_image: DecodedRgbImage,
    source_width: int,
    source_height: int,
    roi: ConfirmationRoi,
    policy: ObjectPresenceDecisionPolicy,
    mask_predictor: MaskPredictor | None,
) -> ClassificationResult:
    """Run the authoritative B4 computation without legacy persistence.

    Both the legacy B3 snapshot service and the Phase 7E production adapter
    enter this function.  It owns only mask inference and deterministic
    comparison; admission, claims, and publication remain in their callers.
    """
    masks = predict_masks_for_images(
        baseline_image,
        probe_image,
        source_width,
        source_height,
        roi,
        policy,
        mask_predictor,
    )
    try:
        return ObjectPresenceClassifier(policy).classify(
            ClassifierInput(
                baseline_image=baseline_image,
                probe_image=probe_image,
                baseline_mask=masks[0],
                probe_mask=masks[1],
                roi=roi,
            )
        )
    except Exception:  # noqa: BLE001 - classifier failures are one safe category.
        _fail(ClassificationPreparationReason.INVALID_CLASSIFIER_OUTPUT)


def _fail(reason: ClassificationPreparationReason) -> NoReturn:
    raise ClassificationPreparationError(reason)
