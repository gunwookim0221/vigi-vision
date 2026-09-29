from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event, Lock, Thread, current_thread
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest
from anyio import CapacityLimiter
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vigi_vision.recording_search_successor_execution as execution_module
from test_recording_search_successor_search_evidence import (
    _comparison,
    _policy,
    _rack_absent_comparison,
)
from vigi_vision.investigation_confirmation_api import install_investigation_confirmation_routes
from vigi_vision.investigation_confirmation_models import (
    ConfirmationManifest,
    ConfirmationRecord,
    ConfirmationReferenceFrame,
    ConfirmationRoi,
    ConfirmationTiming,
    ConfirmedInvestigationInput,
    RoiProvenance,
    artifact_relative_path,
)
from vigi_vision.object_presence_evidence import RawComparison
from vigi_vision.object_presence_values import (
    ClassificationOutcome,
    DecodedRgbImage,
    VisualReason,
    VisualStatus,
)
from vigi_vision.recording_models import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.recording_search_7e_background import (
    Phase7EBackgroundManager,
    Phase7EStartReceipt,
)
from vigi_vision.recording_search_7e_public import (
    Phase7EPreparedRequest,
    Phase7EPublicError,
    Phase7EPublicService,
    Phase7EPublicStatus,
    approved_phase7e_policy,
)
from vigi_vision.recording_search_7e_repository import RecordingSearch7ERepository
from vigi_vision.recording_search_api import install_recording_search_routes
from vigi_vision.recording_search_b3_media import DecodedMedia
from vigi_vision.recording_search_lock import LocalInvestigationLock
from vigi_vision.recording_search_successor import SuccessorPlanService
from vigi_vision.recording_search_successor_acquisition import SuccessorTargetAcquisitionService
from vigi_vision.recording_search_successor_candidate_search import (
    EvidenceNarrowingCompletion,
    EvidenceNarrowingResult,
    SuccessorCandidateFormationResult,
    SuccessorCandidateInterval,
)
from vigi_vision.recording_search_successor_classification import (
    SuccessorClassifierResult,
    SuccessorCoarseClassificationService,
    SuccessorObservation,
    SuccessorObservationState,
)
from vigi_vision.recording_search_successor_evidence import (
    SuccessorEvidenceError,
    SuccessorEvidenceRepository,
)
from vigi_vision.recording_search_successor_execution import (
    SuccessorExecutionError,
    SuccessorExecutionService,
    SuccessorPreparedExecution,
    SuccessorRequest,
    SuccessorTerminal,
    SuccessorTerminalRepository,
)
from vigi_vision.recording_search_successor_narrowing import SuccessorBinaryNarrowingService
from vigi_vision.recording_search_successor_search_evidence import (
    SearchEvidence,
    SearchEvidenceBand,
)
from vigi_vision.reference_frame_decoder import ReferenceFrameDecodeRequest
from vigi_vision.reference_frame_models import (
    DecodedFrameEvidence,
    FrameSelectionPolicy,
    TimingPrecisionStatus,
)
from vigi_vision.reference_frame_web_ui import install_reference_frame_web_ui
from vigi_vision.replay import ReplayClip, ReplayTimeoutError

UTC = timezone.utc
ANCHOR = datetime(2026, 9, 4, 5, 17, 32, tzinfo=UTC)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _http_json(
    base_url: str,
    path: str,
    body: dict[str, object] | None = None,
    *,
    timeout: float = 3,
) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(  # noqa: S310 - loopback URL is fixed by the test.
        f"{base_url}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback test URL.
        return response.status, json.load(response)


def _segment(end: datetime) -> RecordingSegment:
    return RecordingSegment(
        1,
        date(2026, 9, 4),
        int((ANCHOR - timedelta(seconds=1)).timestamp()),
        int(end.timestamp()),
        ANCHOR - timedelta(seconds=1),
        end,
    )


class _Planner:
    def __init__(self, segment: RecordingSegment) -> None:
        self.segment = segment
        self.windows: list[RecordingWindow] = []

    def find_segments_for_window(self, window: RecordingWindow) -> tuple[RecordingSegment, ...]:
        self.windows.append(window)
        return (self.segment,)

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        assert segment.channel_id == self.segment.channel_id
        assert self.segment.start_utc <= window.start_utc < window.end_utc <= self.segment.end_utc
        assert segment.start_utc <= window.start_utc
        assert window.end_utc <= segment.end_utc
        return ReplayRequest(window, "rtsp://redacted.example/replay")


class _Extractor:
    def __init__(self, root: Path, absent_after: datetime) -> None:
        self.root = root
        self.absent_after = absent_after
        self.calls = 0
        self.requests: list[ReplayRequest] = []

    def extract(self, request: ReplayRequest) -> ReplayClip:
        self.calls += 1
        self.requests.append(request)
        path = self.root / f"replay-{self.calls}.mp4"
        marker = b"absent" if request.window.end_utc >= self.absent_after else b"present"
        path.write_bytes(marker)
        return ReplayClip(
            request.window.channel_id,
            request.window.start_utc,
            request.window.end_utc,
            request.replay_url,
            path,
            request.window.duration_seconds,
        )


class _LateTargetTimeoutExtractor(_Extractor):
    """Fail only targets well after the first coarse bracket."""

    def extract(self, request: ReplayRequest) -> ReplayClip:
        if request.window.start_utc >= ANCHOR + timedelta(minutes=15):
            self.calls += 1
            self.requests.append(request)
            raise ReplayTimeoutError
        return super().extract(request)


class _FirstCoarseTimeoutExtractor(_Extractor):
    """Fail the first post-anchor target before any bracket can be formed."""

    def extract(self, request: ReplayRequest) -> ReplayClip:
        if request.window.start_utc > ANCHOR + timedelta(seconds=1):
            self.calls += 1
            self.requests.append(request)
            raise ReplayTimeoutError
        return super().extract(request)


class _ClassificationProxy:
    """Record coarse classification calls while delegating production behavior."""

    def __init__(self, delegate: SuccessorCoarseClassificationService) -> None:
        self.delegate = delegate
        self.coarse_sequences: list[int] = []

    def classify_anchor_target(self, *args: object, **kwargs: object) -> object:
        return self.delegate.classify_anchor_target(*args, **kwargs)  # type: ignore[arg-type]

    def classify_coarse_target(
        self, plan: object, target: object, *args: object, **kwargs: object
    ) -> object:
        self.coarse_sequences.append(target.sequence)  # type: ignore[union-attr]
        return self.delegate.classify_coarse_target(plan, target, *args, **kwargs)  # type: ignore[arg-type]


class _CoarseFallbackEvidenceProxy:
    """Attach deterministic S3 bands to production-shaped observations."""

    def __init__(
        self,
        delegate: SuccessorCoarseClassificationService,
        *,
        material_after: datetime | None = ANCHOR + timedelta(minutes=6),
        probes_present: bool = False,
        cancel_after_first_probe: bool = False,
    ) -> None:
        self.delegate = delegate
        self.probe_times: list[datetime] = []
        self.material_after = material_after
        self.probes_present = probes_present
        self.cancel_after_first_probe = cancel_after_first_probe
        self.cancel_requested = False

    def prepare_reference(self, *args: object, **kwargs: object) -> object:
        return self.delegate.prepare_reference(*args, **kwargs)  # type: ignore[arg-type]

    def classify_anchor_target(self, *args: object, **kwargs: object) -> object:
        observation = self.delegate.classify_anchor_target(*args, **kwargs)  # type: ignore[arg-type]
        return replace(
            observation,
            state=SuccessorObservationState.PRESENT,
            reason_code=None,
            _search_evidence=SearchEvidence(
                SearchEvidenceBand.STRONG_REFERENCE,
                "reference_support_retained",
                scene_stable=True,
                fast_present_hit=True,
            ),
        )

    def classify_coarse_target(
        self, plan: object, target: object, *args: object, **kwargs: object
    ) -> object:
        observation = self.delegate.classify_coarse_target(  # type: ignore[arg-type]
            plan, target, *args, **kwargs
        )
        return replace(
            observation,
            _search_evidence=SearchEvidence(
                SearchEvidenceBand.USABLE_AMBIGUOUS,
                "scene_instability_suppressed_direction",
                scene_stable=False,
                scene_discontinuity=True,
                scene_only_suppressed=True,
            ),
        )

    def classify_target(
        self, plan: object, target: object, *args: object, **kwargs: object
    ) -> object:
        observation = self.delegate.classify_target(plan, target, *args, **kwargs)  # type: ignore[arg-type]
        requested_time = observation.requested_time_utc  # type: ignore[union-attr]
        self.probe_times.append(requested_time)
        if self.probes_present:
            evidence = SearchEvidence(
                SearchEvidenceBand.STRONG_REFERENCE,
                "reference_support_retained",
                scene_stable=True,
                fast_present_hit=True,
            )
            observation = replace(
                observation,
                state=SuccessorObservationState.PRESENT,
                reason_code=None,
            )
        elif self.material_after is not None and requested_time >= self.material_after:
            evidence = SearchEvidence(
                SearchEvidenceBand.MATERIAL_DROP,
                "object_reference_support_drop",
                scene_stable=True,
                object_degradation=True,
            )
        else:
            evidence = SearchEvidence(
                SearchEvidenceBand.USABLE_AMBIGUOUS,
                "usable_reference_evidence_not_directional",
                scene_stable=True,
            )
        if self.cancel_after_first_probe and len(self.probe_times) == 1:
            self.cancel_requested = True
        return replace(observation, _search_evidence=evidence)


class _FrameDecoder:
    def __init__(self, fractional_offset_seconds: float = 0.0) -> None:
        self.fractional_offset_seconds = fractional_offset_seconds

    def decode(self, request: ReferenceFrameDecodeRequest) -> DecodedFrameEvidence:
        payload = request.clip_path.read_bytes()
        request.output_path.write_bytes(payload)
        target_offset_seconds = request.target_offset_seconds
        if target_offset_seconds > 0:
            target_offset_seconds += self.fractional_offset_seconds
        return DecodedFrameEvidence(
            request.output_path,
            target_offset_seconds,
            4,
            4,
            TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
            (),
        )


class _MediaDecoder:
    def decode(self, payload: bytes, width: int, height: int) -> DecodedMedia:
        value = 255 if payload == b"absent" else 0
        image = DecodedRgbImage.from_rows(
            tuple(tuple((value, value, value) for _ in range(width)) for _ in range(height))
        )
        return SimpleNamespace(image=image, integrity=None)


class _Classifier:
    policy_identity = "successor-test-classifier-v1"

    def classify(
        self,
        _baseline: object,
        probe: DecodedRgbImage,
        _width: int,
        _height: int,
        _roi: object,
        _correlation_id: str,
    ) -> SuccessorClassifierResult:
        outcome = (
            ClassificationOutcome.ABSENT if probe.pixels[0][0][0] else ClassificationOutcome.PRESENT
        )
        return SuccessorClassifierResult(outcome)


class _PublishThenFailEvidence(SuccessorEvidenceRepository):
    """Persist evidence, then fail the terminal publication boundary."""

    def stage(
        self,
        prepared: SuccessorPreparedExecution,
        observations: tuple[SuccessorObservation, ...],
        terminal: SuccessorTerminal,
    ) -> dict[str, object]:
        super().stage(prepared, observations, terminal)
        raise SuccessorEvidenceError

    def publish(
        self,
        prepared: SuccessorPreparedExecution,
        observations: tuple[SuccessorObservation, ...],
        terminal: SuccessorTerminal,
    ) -> None:
        _ = super().publish(prepared, observations, terminal)
        raise SuccessorEvidenceError


class _IndeterminateClassifier(_Classifier):
    def classify(
        self,
        _baseline: object,
        _probe: DecodedRgbImage,
        _width: int,
        _height: int,
        _roi: object,
        _correlation_id: str,
    ) -> SuccessorClassifierResult:
        return SuccessorClassifierResult(
            ClassificationOutcome.INDETERMINATE,
            "insufficient_visual_evidence",
        )


class _CoarseOcclusionClassifier(_Classifier):
    def __init__(self, *, later_clean_absent: bool) -> None:
        self.calls = 0
        self.later_clean_absent = later_clean_absent

    def classify(
        self,
        _baseline: object,
        _probe: DecodedRgbImage,
        _width: int,
        _height: int,
        _roi: object,
        _correlation_id: str,
    ) -> SuccessorClassifierResult:
        self.calls += 1
        outcome = (
            ClassificationOutcome.PRESENT
            if self.calls <= 2
            else (
                ClassificationOutcome.ABSENT
                if self.calls == 4 and self.later_clean_absent
                else ClassificationOutcome.INDETERMINATE
            )
        )
        return SuccessorClassifierResult(
            outcome,
            "insufficient_visual_evidence"
            if outcome is ClassificationOutcome.INDETERMINATE
            else None,
            _comparison(
                present_gate=False,
                decision_path="indeterminate",
                decision_reason="insufficient_visual_evidence",
            )
            if outcome is ClassificationOutcome.INDETERMINATE
            else _comparison(),
        )


class _CoarseUnusableClassifier(_Classifier):
    def __init__(self) -> None:
        self.calls = 0

    def classify(
        self,
        _baseline: object,
        _probe: DecodedRgbImage,
        _width: int,
        _height: int,
        _roi: object,
        _correlation_id: str,
    ) -> SuccessorClassifierResult:
        self.calls += 1
        if self.calls <= 2:
            return SuccessorClassifierResult(ClassificationOutcome.PRESENT, None, _comparison())
        if self.calls == 3:
            return SuccessorClassifierResult(
                ClassificationOutcome.INDETERMINATE,
                VisualReason.INVALID_MASK.value,
                RawComparison(
                    baseline_mask_pixel_count=None,
                    probe_mask_pixel_count=None,
                    roi_pixel_count=16,
                    mask_intersection_pixel_count=None,
                    mask_union_pixel_count=None,
                    baseline_mask_coverage=None,
                    probe_mask_coverage=None,
                    mask_iou=None,
                    effective_comparison_area=None,
                    roi_luma_ncc=None,
                    visual_status=VisualStatus.UNUSABLE,
                    unusable_reason=VisualReason.INVALID_MASK,
                ),
            )
        return SuccessorClassifierResult(ClassificationOutcome.ABSENT, None, _comparison())


class _AnchorIndeterminateThenPresentClassifier(_Classifier):
    """Model an uncertain anchor followed by a usable final target."""

    def __init__(self) -> None:
        self.calls = 0

    def classify(
        self,
        _baseline: object,
        _probe: DecodedRgbImage,
        _width: int,
        _height: int,
        _roi: object,
        _correlation_id: str,
    ) -> SuccessorClassifierResult:
        self.calls += 1
        if self.calls == 1:
            return SuccessorClassifierResult(
                ClassificationOutcome.INDETERMINATE,
                "insufficient_visual_evidence",
            )
        return SuccessorClassifierResult(ClassificationOutcome.PRESENT)


class _CancelAfterB4ResultClassifier(_Classifier):
    """Return a late coarse result while requesting cancellation before return."""

    def __init__(self) -> None:
        self.calls = 0
        self.cancel_requested = False
        self.cancellation_callbacks: list[object] = []

    def classify_with_cancellation(  # noqa: PLR0913
        self,
        baseline: object,
        probe: DecodedRgbImage,
        width: int,
        height: int,
        roi: object,
        correlation_id: str,
        *,
        cancellation: object,
    ) -> SuccessorClassifierResult:
        self.calls += 1
        self.cancellation_callbacks.append(cancellation)
        result = self.classify(baseline, probe, width, height, roi, correlation_id)
        if self.calls == 2:
            self.cancel_requested = True
        return result


def _confirmed(tmp_path: Path) -> ConfirmedInvestigationInput:
    path = tmp_path / "baseline.jpg"
    path.write_bytes(b"baseline")
    return ConfirmedInvestigationInput(
        "object-disappearance-v3-ch1-20260904T051732Z",
        1,
        ANCHOR,
        "Asia/Seoul",
        0,
        "reference-frame-resource-v1-test",
        "2026-09-04T14:17:32",
        ANCHOR,
        3,
        "nearest_decoded_frame",
        None,
        None,
        "MEASURED_CLIP_RELATIVE",
        (),
        4,
        4,
        ConfirmationRoi(
            x=0,
            y=0,
            width=4,
            height=4,
            coordinate_space="source_pixels",
            provenance=RoiProvenance.MANUAL,
        ),
        "a" * 64,
        len(b"baseline"),
        path,
    )


def _service(
    tmp_path: Path,
    absent_after: datetime,
    classifier: object | None = None,
) -> SuccessorExecutionService:
    segment = _segment(ANCHOR + timedelta(minutes=30, seconds=1))
    planner = _Planner(segment)
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        _Extractor(tmp_path, absent_after),
        _FrameDecoder(),
        temporary_directory=tmp_path / "temporary",
    )
    classification = SuccessorCoarseClassificationService(
        _Classifier() if classifier is None else classifier,
        _MediaDecoder(),
    )
    return SuccessorExecutionService(
        SuccessorPlanService(planner),
        acquisition,
        classification,
        SuccessorBinaryNarrowingService(acquisition, classification),
        _MediaDecoder(),
        SuccessorTerminalRepository(tmp_path / "successor"),
    )


def _coarse_fallback_service(
    tmp_path: Path,
    *,
    fractional_frame_offset_seconds: float = 0.0,
    **proxy_kwargs: object,
) -> tuple[SuccessorExecutionService, _CoarseFallbackEvidenceProxy]:
    segment = _segment(ANCHOR + timedelta(minutes=30, seconds=1))
    planner = _Planner(segment)
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        _Extractor(tmp_path, ANCHOR + timedelta(minutes=30)),
        _FrameDecoder(fractional_frame_offset_seconds),
        temporary_directory=tmp_path / "temporary",
    )
    classification = SuccessorCoarseClassificationService(
        _IndeterminateClassifier(),
        _MediaDecoder(),
    )
    proxy = _CoarseFallbackEvidenceProxy(classification, **proxy_kwargs)
    return (
        SuccessorExecutionService(
            SuccessorPlanService(planner),
            acquisition,
            proxy,  # type: ignore[arg-type]
            SuccessorBinaryNarrowingService(acquisition, classification),
            _MediaDecoder(),
            SuccessorTerminalRepository(tmp_path / "successor"),
        ),
        proxy,
    )


def create_successor_browser_uvicorn_app() -> FastAPI:
    """Build a real-Uvicorn successor app with only deterministic local doubles."""
    root = Path(os.environ["VIGI_SUCCESSOR_BROWSER_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    confirmed = _confirmed(root)
    execution = _service(root, ANCHOR + timedelta(minutes=15))

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            if investigation_id != confirmed.investigation_id:
                raise AssertionError
            return confirmed

        def load_confirmation_manifest(self, investigation_id: str) -> ConfirmationManifest:
            if investigation_id != confirmed.investigation_id:
                raise AssertionError
            return ConfirmationManifest(
                schema_version=3,
                investigation_id=confirmed.investigation_id,
                investigation_kind="object_disappearance",
                scenario_id="object-disappearance",
                status="confirmed",
                anchor_time_utc=confirmed.anchor_time_utc,
                source_timezone=confirmed.source_timezone,
                confirmed_at_utc=confirmed.anchor_time_utc,
                artifact_directory_relative=artifact_relative_path(confirmed.investigation_id),
                confirmation=ConfirmationRecord(
                    channel_id=confirmed.channel_id,
                    candidate_offset_seconds=confirmed.candidate_offset_seconds,
                    reference_frame=ConfirmationReferenceFrame(
                        resource_id=confirmed.reference_frame_resource_id,
                        schema_version=1,
                        generation_policy_version=confirmed.generation_policy_version,
                        requested_time=confirmed.requested_time_text,
                        requested_time_utc=confirmed.requested_time_utc,
                        source_timezone=confirmed.source_timezone,
                        frame_selection_policy=FrameSelectionPolicy(
                            confirmed.frame_selection_policy
                        ),
                        width=confirmed.source_width,
                        height=confirmed.source_height,
                        jpeg_sha256=confirmed.jpeg_sha256,
                        jpeg_size_bytes=confirmed.jpeg_size_bytes,
                    ),
                    timing=ConfirmationTiming(
                        decoded_local_pts_seconds=confirmed.decoded_local_pts_seconds,
                        estimated_source_time_utc=confirmed.estimated_source_time_utc,
                        offset_from_requested_seconds=None,
                        timing_precision_status=TimingPrecisionStatus(
                            confirmed.timing_precision_status.lower()
                        ),
                        warnings=confirmed.warnings,
                    ),
                    roi=confirmed.roi,
                ),
            )

    policy, classifier_policy, object_policy = approved_phase7e_policy()
    service = Phase7EPublicService(
        RecordingSearch7ERepository(root / "legacy"),
        SimpleNamespace(),
        _Confirmation(),
        None,
        None,
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(status=lambda *_args: (None, None)),
        lambda: ANCHOR + timedelta(hours=1),
        None,
        execution,
    )
    app = FastAPI()
    install_reference_frame_web_ui(app)
    limiter = CapacityLimiter(2)
    install_investigation_confirmation_routes(app, _Confirmation(), limiter)
    install_recording_search_routes(app, None, limiter, phase7e_service=service)
    return app


def test_successor_http_shaped_execution_publishes_found_and_reopens(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    result = service.execute(prepared)
    assert result.status == "FOUND"
    assert result.last_present_time_utc is not None
    assert result.first_absent_time_utc is not None
    assert result.last_present_time_utc < result.first_absent_time_utc
    assert result.phase8_status == "NOT_REQUESTED"
    reopened = service.publisher.read(confirmed.investigation_id, prepared.request.run_id)
    assert reopened is not None
    assert reopened["status"] == "FOUND"
    assert reopened["schema_version"] == 8
    assert reopened["policy_version"] == prepared.plan.policy_version
    assert reopened["requested_end_time_utc"] == "2026-09-04T05:47:32Z"
    assert reopened["coarse_observation_ids"]
    assert reopened["coarse_target_ids"]
    assert reopened["target_statuses"]
    assert reopened["phase8_status"] == "NOT_REQUESTED"
    assert reopened["narrowing_id"]
    assert not tuple((tmp_path / "temporary").glob("**/*"))


def test_successor_complete_present_publishes_not_found(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(hours=2))
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    result = service.execute(prepared)
    assert result.status == "NOT_FOUND"
    assert result.reason_code == "complete_present_coverage"


def test_coarse_occlusion_then_clean_absent_brackets_only_clean_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    classifier = _CoarseOcclusionClassifier(later_clean_absent=True)
    service = _service(tmp_path, ANCHOR + timedelta(hours=2), classifier)
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-occlusionclean00000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    seen_brackets: list[tuple[str, str]] = []
    original = SuccessorBinaryNarrowingService.narrow

    def capture(self, plan, coarse_result, authority, **kwargs):  # noqa: ANN001, ANN003, ANN202
        bracket = coarse_result.candidate_bracket
        assert bracket is not None
        seen_brackets.append((bracket.present_observation_id, bracket.absent_observation_id))
        return original(self, plan, coarse_result, authority, **kwargs)

    monkeypatch.setattr(SuccessorBinaryNarrowingService, "narrow", capture)
    result = service.execute(prepared)
    assert seen_brackets
    observations = result.coarse_observations
    assert any(item["state"] == "INDETERMINATE" for item in observations)
    assert seen_brackets[0][1] == next(
        item["observation_id"] for item in observations if item["state"] == "ABSENT"
    )
    assert result.status == "INCONCLUSIVE"


def test_coarse_occlusion_without_clean_absent_remains_inconclusive(tmp_path: Path) -> None:
    classifier = _CoarseOcclusionClassifier(later_clean_absent=False)
    service = _service(tmp_path, ANCHOR + timedelta(hours=2), classifier)
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-occlusiononly000000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    result = service.execute(prepared)
    assert result.status == "INCONCLUSIVE"
    assert result.first_absent_time_utc is None
    assert all(item["state"] != "ABSENT" for item in result.coarse_observations)


def test_unusable_coarse_observation_does_not_start_early_narrowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    classifier = _CoarseUnusableClassifier()
    service = _service(tmp_path, ANCHOR + timedelta(hours=2), classifier)
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-invalidmask0000000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    narrowing_calls: list[str] = []
    original = SuccessorBinaryNarrowingService.narrow

    def capture(self, plan, coarse_result, authority, **kwargs):  # noqa: ANN001, ANN003, ANN202
        bracket = coarse_result.candidate_bracket
        assert bracket is not None
        narrowing_calls.append(bracket.absent_observation_id)
        return original(self, plan, coarse_result, authority, **kwargs)

    monkeypatch.setattr(SuccessorBinaryNarrowingService, "narrow", capture)

    result = service.execute(prepared)

    assert classifier.calls >= 4
    assert not narrowing_calls
    assert result.status == "INCONCLUSIVE"


def test_ambiguous_endpoint_uses_bounded_coarse_fallback_and_forms_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, proxy = _coarse_fallback_service(tmp_path)
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:32:32",
        run_id="search-run-coarsefallback000000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    candidate_results: list[SuccessorCandidateFormationResult] = []
    original_candidate_search = execution_module._candidate_search

    def capture_candidate_search(
        prepared_execution: SuccessorPreparedExecution,
        coarse: object,
    ) -> SuccessorCandidateFormationResult:
        result = original_candidate_search(prepared_execution, coarse)  # type: ignore[arg-type]
        candidate_results.append(result)
        return result

    monkeypatch.setattr(execution_module, "_candidate_search", capture_candidate_search)
    narrowing_inputs: list[SuccessorCandidateFormationResult] = []
    original_run_s4 = SuccessorExecutionService._run_s4_narrowing

    def capture_s4_narrowing(
        execution: SuccessorExecutionService,
        prepared_execution: SuccessorPreparedExecution,
        coarse: object,
        candidate_search: SuccessorCandidateFormationResult,
        *,
        cancellation: object,
    ) -> object:
        narrowing_inputs.append(candidate_search)
        return original_run_s4(
            execution,
            prepared_execution,
            coarse,  # type: ignore[arg-type]
            candidate_search,
            cancellation=cancellation,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(SuccessorExecutionService, "_run_s4_narrowing", capture_s4_narrowing)

    result = service.execute(prepared)

    assert result.status == "INCONCLUSIVE"
    assert result.reason_code == "indeterminate_observation"
    assert proxy.probe_times == [
        ANCHOR + timedelta(minutes=3),
        ANCHOR + timedelta(minutes=6),
    ]
    assert any(item.candidates for item in candidate_results)
    assert any(item.candidates for item in narrowing_inputs)
    requested_times = tuple(item["requested_time_utc"] for item in result.coarse_observations)
    assert "2026-09-04T05:20:32Z" in requested_times
    assert "2026-09-04T05:23:32Z" in requested_times


def test_qualified_s4_candidate_persists_and_reopens_without_public_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _proxy = _coarse_fallback_service(
        tmp_path,
        material_after=ANCHOR + timedelta(minutes=3),
    )
    service.evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
    confirmed = replace(
        _confirmed(tmp_path),
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:32:32",
        run_id="search-run-77777777777777777777777777777777",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    original_candidate_search = execution_module._candidate_search

    def wait_for_qualified_candidate(
        prepared_execution: SuccessorPreparedExecution,
        coarse: object,
    ) -> SuccessorCandidateFormationResult:
        result = original_candidate_search(prepared_execution, coarse)  # type: ignore[arg-type]
        if result.candidates and not result.qualified_candidates:
            return SuccessorCandidateFormationResult(())
        return result

    monkeypatch.setattr(
        execution_module,
        "_candidate_search",
        wait_for_qualified_candidate,
    )

    result = service.execute(prepared)
    reopened = SuccessorEvidenceRepository(tmp_path / "successor").read_candidates(
        confirmed.investigation_id,
        prepared.request.run_id,
    )
    public_evidence = service.read_evidence(
        confirmed.investigation_id,
        prepared.request.run_id,
    )

    assert result.status == "INCONCLUSIVE"
    assert result.reason_code == "indeterminate_observation"
    assert result.first_absent_time_utc is None
    assert len(reopened) == 1
    assert reopened[0].qualified
    assert reopened[0].candidate_id.startswith("successor-candidate-v1-")
    assert len(reopened[0].supporting_observation_ids) >= 3
    assert public_evidence is not None
    assert public_evidence["version"] == "phase7e-successor-evidence-v1"
    assert "candidate_state" not in public_evidence


def test_public_retry_reopens_committed_candidate_and_rejects_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution, _proxy = _coarse_fallback_service(
        tmp_path, material_after=ANCHOR + timedelta(minutes=3)
    )
    execution.evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
    confirmed = replace(_confirmed(tmp_path), jpeg_sha256=hashlib.sha256(b"baseline").hexdigest())
    policy, classifier_policy, object_policy = approved_phase7e_policy()

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    public = Phase7EPublicService(
        RecordingSearch7ERepository(tmp_path / "legacy"),
        SimpleNamespace(),
        _Confirmation(),
        None,
        None,
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(status=lambda *_args: (None, None)),
        lambda: ANCHOR + timedelta(hours=1),
        None,
        execution,
    )
    original_candidate_search = execution_module._candidate_search

    def wait_for_qualified_candidate(
        prepared_execution: SuccessorPreparedExecution, coarse: object
    ) -> SuccessorCandidateFormationResult:
        result = original_candidate_search(prepared_execution, coarse)  # type: ignore[arg-type]
        if result.candidates and not result.qualified_candidates:
            return SuccessorCandidateFormationResult(())
        return result

    monkeypatch.setattr(execution_module, "_candidate_search", wait_for_qualified_candidate)
    prepared = public.prepare_http(
        confirmed.investigation_id,
        "2026-09-04T14:32:32",
        "99999999-9999-4999-8999-999999999999",
    )
    assert prepared.successor is not None
    assert public.execute_prepared(prepared).phase7.status == "INCONCLUSIVE"
    candidate = execution.read_candidates(confirmed.investigation_id, prepared.request.run_id)
    assert len(candidate) == 1
    assert candidate[0].qualified

    reopened_execution, _ = _coarse_fallback_service(tmp_path)
    reopened_execution.evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
    reopened_public = replace(public, successor_execution=reopened_execution)
    reopened_prepared = reopened_public.prepare_http(
        confirmed.investigation_id,
        "2026-09-04T14:32:32",
        "99999999-9999-4999-8999-999999999999",
    )
    assert reopened_public.resolve_existing(reopened_prepared).phase7.status == "INCONCLUSIVE"  # type: ignore[union-attr]
    assert (
        reopened_execution.read_candidates(confirmed.investigation_id, prepared.request.run_id)
        == candidate
    )

    manifest_path = (
        tmp_path
        / "successor"
        / confirmed.investigation_id
        / prepared.request.run_id
        / "evidence"
        / "manifest.json"
    )
    assert manifest_path.is_file()
    manifest_text = manifest_path.read_text(encoding="utf-8")
    legacy_manifest = json.loads(manifest_text)
    legacy_manifest["version"] = "phase7e-successor-evidence-v1"
    legacy_manifest.pop("candidate_state")
    manifest_path.write_text(json.dumps(legacy_manifest), encoding="utf-8")
    assert reopened_public.resolve_existing(reopened_prepared).phase7.status == "INCONCLUSIVE"  # type: ignore[union-attr]
    assert (
        reopened_execution.read_candidates(confirmed.investigation_id, prepared.request.run_id)
        == ()
    )
    manifest_path.write_text(manifest_text, encoding="utf-8")
    manifest = json.loads(manifest_text)
    manifest["candidate_state"]["candidates"][0]["interval_end_utc"] = "2026-09-04T05:32:31Z"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(Phase7EPublicError, match="search_run_corrupt"):
        reopened_public.resolve_existing(reopened_prepared)
    app = FastAPI()
    install_recording_search_routes(app, None, CapacityLimiter(2), phase7e_service=reopened_public)
    with TestClient(app) as client:
        retry = client.post(
            "/api/v1/recording-searches",
            json={
                "investigation_id": confirmed.investigation_id,
                "search_end": "2026-09-04T14:32:32",
                "request_id": "99999999-9999-4999-8999-999999999999",
            },
        )
        evidence = client.get(
            f"/api/v1/recording-searches/{confirmed.investigation_id}/"
            f"{prepared.request.run_id}/evidence"
        )
    assert retry.status_code >= 400
    assert evidence.status_code != 200
    manifest_path.unlink()  # Only the test-owned disposable repository is removed.
    with pytest.raises(Phase7EPublicError, match="search_run_corrupt"):
        reopened_public.resolve_existing(reopened_prepared)


@pytest.mark.parametrize("evidence_state", ["valid", "missing", "tampered"])
def test_completed_same_manager_http_retry_and_status_require_committed_evidence(  # noqa: C901, PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence_state: str
) -> None:
    confirmed = replace(_confirmed(tmp_path), jpeg_sha256=hashlib.sha256(b"baseline").hexdigest())
    execution = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
    execution.evidence_repository = evidence_repository
    extractor = execution.acquisition.replay_extractor
    publications = {"evidence": 0, "terminal": 0, "reopen": 0}
    original_stage = evidence_repository.stage
    original_terminal = execution.publisher.publish_terminal

    def stage(*args: object, **kwargs: object) -> object:
        publications["evidence"] += 1
        return original_stage(*args, **kwargs)  # type: ignore[arg-type]

    def publish_terminal(*args: object, **kwargs: object) -> object:
        publications["terminal"] += 1
        return original_terminal(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(evidence_repository, "stage", stage)
    monkeypatch.setattr(execution.publisher, "publish_terminal", publish_terminal)

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    policy, classifier_policy, object_policy = approved_phase7e_policy()
    public = Phase7EPublicService(
        RecordingSearch7ERepository(tmp_path / "legacy"),
        SimpleNamespace(),
        _Confirmation(),
        None,
        None,
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(status=lambda *_args: (None, None)),
        lambda: ANCHOR + timedelta(hours=1),
        None,
        execution,
    )
    original_resolve = Phase7EPublicService.resolve_existing

    def resolve_existing(service: Phase7EPublicService, prepared: object) -> object:
        publications["reopen"] += 1
        return original_resolve(service, prepared)  # type: ignore[arg-type]

    monkeypatch.setattr(Phase7EPublicService, "resolve_existing", resolve_existing)
    app = FastAPI()
    install_recording_search_routes(app, None, CapacityLimiter(2), phase7e_service=public)
    body = {
        "investigation_id": confirmed.investigation_id,
        "search_end": "2026-09-04T14:47:32",
        "request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    }
    run_id = "search-run-bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb"
    terminal_path = tmp_path / "successor" / confirmed.investigation_id / run_id / "terminal.json"
    manifest_path = (
        tmp_path / "successor" / confirmed.investigation_id / run_id / "evidence" / "manifest.json"
    )
    with TestClient(app) as client:
        accepted = client.post("/api/v1/recording-searches", json=body)
        assert accepted.status_code == 202
        assert accepted.json()["run_id"] == run_id
        route = accepted.json()["status_url"]
        for _ in range(500):
            result = client.get(route)
            if result.json()["status"] not in {"ACCEPTED", "RUNNING"}:
                break
            time.sleep(0.01)
        assert result.json()["status"] == "FOUND"
        # Durable publication can precede the worker's process-local ledger
        # update; wait until the cached receipt itself is completed.
        for _ in range(500):
            settled = client.post("/api/v1/recording-searches", json=body)
            if settled.json()["status"] == "FOUND":
                break
            time.sleep(0.01)
        assert settled.json()["status"] == "FOUND"
        assert manifest_path.is_file()
        terminal_before = terminal_path.read_bytes()
        calls_before = extractor.calls
        publications_before = publications.copy()
        assert publications_before["evidence"] == 1
        assert publications_before["terminal"] == 1
        if evidence_state == "missing":
            manifest_path.unlink()  # Only this test-owned temporary evidence is removed.
        elif evidence_state == "tampered":
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["plan_id"] = "successor-plan-v1-tampered"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        evidence_before_retry = manifest_path.read_bytes() if manifest_path.exists() else None
        retry = client.post("/api/v1/recording-searches", json=body)
        status = client.get(route)

    assert publications["reopen"] == publications_before["reopen"] + 1
    assert extractor.calls == calls_before
    assert publications["evidence"] == publications_before["evidence"]
    assert publications["terminal"] == publications_before["terminal"]
    assert terminal_path.read_bytes() == terminal_before
    assert (manifest_path.read_bytes() if manifest_path.exists() else None) == evidence_before_retry
    if evidence_state == "valid":
        assert retry.status_code == 202
        assert retry.json()["status"] == "FOUND"
        assert status.status_code == 200
        assert status.json()["status"] == "FOUND"
    else:
        assert retry.status_code == 500
        assert retry.json()["error"]["code"] == "search_run_corrupt"
        assert status.status_code == 500
        assert status.json()["error"]["code"] == "search_run_corrupt"


def test_active_successor_duplicate_does_not_require_uncommitted_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = Event()
    release = Event()

    class _BlockingExtractor(_Extractor):
        def extract(self, request: ReplayRequest) -> ReplayClip:
            started.set()
            if not release.wait(5):
                raise ReplayTimeoutError
            return super().extract(request)

    confirmed = replace(_confirmed(tmp_path), jpeg_sha256=hashlib.sha256(b"baseline").hexdigest())
    execution = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    extraction = _BlockingExtractor(tmp_path, ANCHOR + timedelta(minutes=15))
    execution.acquisition.replay_extractor = extraction
    execution.evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
    calls = 0
    original_execute = SuccessorExecutionService.execute

    def execute(service: SuccessorExecutionService, *args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original_execute(service, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(SuccessorExecutionService, "execute", execute)

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    policy, classifier_policy, object_policy = approved_phase7e_policy()
    public = Phase7EPublicService(
        RecordingSearch7ERepository(tmp_path / "legacy"),
        SimpleNamespace(),
        _Confirmation(),
        None,
        None,
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(status=lambda *_args: (None, None)),
        lambda: ANCHOR + timedelta(hours=1),
        None,
        execution,
    )
    manager = Phase7EBackgroundManager(public)
    try:
        first = manager.start(
            confirmed.investigation_id,
            "2026-09-04T14:47:32",
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        )
        assert started.wait(2)
        duplicate = manager.start(
            confirmed.investigation_id,
            "2026-09-04T14:47:32",
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        )
        assert duplicate.run_id == first.run_id
        assert duplicate.status in {"ACCEPTED", "RUNNING"}
        assert calls == 1
        release.set()
        for _ in range(500):
            status = manager.status(confirmed.investigation_id, first.run_id)
            if status.phase7.status not in {"ACCEPTED", "RUNNING"}:
                break
            time.sleep(0.01)
        assert status.phase7.status == "FOUND"
        assert calls == 1
    finally:
        release.set()
        manager.close()


@pytest.mark.parametrize("same_request", [True, False])
def test_independent_managers_share_one_successor_execution_owner(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    same_request: bool,  # noqa: FBT001
) -> None:
    """A second server instance cannot admit another worker for one investigation."""
    release = Event()
    entered = [Event(), Event()]
    publications = {"evidence": 0, "terminal": 0}
    confirmed = replace(_confirmed(tmp_path), jpeg_sha256=hashlib.sha256(b"baseline").hexdigest())
    policy, classifier_policy, object_policy = approved_phase7e_policy()

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    class _GatedPublic:
        def __init__(self, delegate: Phase7EPublicService, index: int) -> None:
            self.delegate = delegate
            self.index = index

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        def execute_prepared(self, *args: object, **kwargs: object) -> Phase7EPublicStatus:
            entered[self.index].set()
            assert release.wait(5)
            return self.delegate.execute_prepared(*args, **kwargs)  # type: ignore[arg-type]

    managers: list[Phase7EBackgroundManager] = []
    executions: list[SuccessorExecutionService] = []
    for index in range(2):
        execution = _service(tmp_path, ANCHOR + timedelta(minutes=15))
        execution.evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
        executions.append(execution)
        original_stage = execution.evidence_repository.stage
        original_terminal = execution.publisher.publish_terminal

        def stage(*args: object, _original: object = original_stage, **kwargs: object) -> object:
            publications["evidence"] += 1
            return _original(*args, **kwargs)  # type: ignore[operator]

        def terminal(
            *args: object, _original: object = original_terminal, **kwargs: object
        ) -> object:
            publications["terminal"] += 1
            return _original(*args, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(execution.evidence_repository, "stage", stage)
        monkeypatch.setattr(execution.publisher, "publish_terminal", terminal)
        public = Phase7EPublicService(
            RecordingSearch7ERepository(tmp_path / "legacy"),
            SimpleNamespace(),
            _Confirmation(),
            None,
            None,
            policy,
            classifier_policy,
            object_policy,
            SimpleNamespace(status=lambda *_args: (None, None)),
            lambda: ANCHOR + timedelta(hours=1),
            None,
            execution,
        )
        managers.append(Phase7EBackgroundManager(_GatedPublic(public, index)))  # type: ignore[arg-type]
    first_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    second_id = first_id if same_request else "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    try:
        first = managers[0].start(confirmed.investigation_id, "2026-09-04T14:47:32", first_id)
        assert first.status == "ACCEPTED"
        assert entered[0].wait(2)
        managers[1].recover_startup()
        assert (
            managers[0].status(confirmed.investigation_id, first.run_id).phase7.status == "RUNNING"
        )
        if same_request:
            second = managers[1].start(confirmed.investigation_id, "2026-09-04T14:47:32", second_id)
            assert second.run_id == first.run_id
            assert second.status in {"ACCEPTED", "RUNNING"}
        else:
            with pytest.raises(Phase7EPublicError, match="already_running"):
                managers[1].start(confirmed.investigation_id, "2026-09-04T14:47:32", second_id)
        assert not entered[1].wait(0.1)
        release.set()
        first_future = managers[0]._jobs[first_id].future
        assert first_future is not None
        first_future.result(timeout=10)
        assert managers[0].status(confirmed.investigation_id, first.run_id).phase7.status == "FOUND"
        assert publications == {"evidence": 1, "terminal": 1}
        assert executions[1].acquisition.replay_extractor.calls == 0
        if same_request:
            retry = managers[1].start(confirmed.investigation_id, "2026-09-04T14:47:32", first_id)
            assert retry.status == "FOUND"
            assert retry.run_id == first.run_id
            assert publications == {"evidence": 1, "terminal": 1}
            manifest_path = (
                tmp_path
                / "successor"
                / confirmed.investigation_id
                / first.run_id
                / "evidence"
                / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["plan_id"] = "successor-plan-v1-tampered"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with pytest.raises(Phase7EPublicError, match="search_run_corrupt"):
                managers[1].start(confirmed.investigation_id, "2026-09-04T14:47:32", first_id)
        else:
            next_run = managers[1].start(
                confirmed.investigation_id, "2026-09-04T14:47:32", second_id
            )
            assert next_run.status == "ACCEPTED"
            next_future = managers[1]._jobs[second_id].future
            assert next_future is not None
            next_future.result(timeout=10)
            next_status = managers[1].status(confirmed.investigation_id, next_run.run_id)
            assert next_status.phase7.status == "FOUND"
            assert publications == {"evidence": 2, "terminal": 2}
    finally:
        release.set()
        for manager in managers:
            manager.close()


def test_successor_recovery_skips_live_owner_and_releases_crashed_owner(tmp_path: Path) -> None:
    """A RUNNING record is interrupted only after its OS owner is gone."""
    repository = RecordingSearch7ERepository(tmp_path / "legacy")
    publisher = SuccessorTerminalRepository(tmp_path / "legacy" / ".successor")
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-" + "d" * 32,
        now_utc=ANCHOR + timedelta(hours=1),
    )
    publisher.publish_running(prepared)
    lock = LocalInvestigationLock(repository.lock_path(prepared.request.investigation_id))
    assert lock.try_acquire(0)
    try:
        assert publisher.recover_abandoned(lock_path_for=repository.lock_path) == 0
        active = publisher.read(prepared.request.investigation_id, prepared.request.run_id)
        assert active is not None
        assert active["status"] == "RUNNING"
    finally:
        lock.release()
    assert publisher.recover_abandoned(lock_path_for=repository.lock_path) == 1
    recovered = publisher.read(prepared.request.investigation_id, prepared.request.run_id)
    assert recovered is not None
    assert recovered["status"] == "INTERRUPTED"
    assert publisher.recover_abandoned(lock_path_for=repository.lock_path) == 0


def test_worker_reconciliation_error_releases_owner_and_allows_next_manager(  # noqa: PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt/readback failure remains observable without leaking ownership."""
    confirmed = replace(_confirmed(tmp_path), jpeg_sha256=hashlib.sha256(b"baseline").hexdigest())
    policy, classifier_policy, object_policy = approved_phase7e_policy()
    entered = Event()
    release = Event()
    fail_readback = Event()
    readback_error_code = "successor_publication_corrupt"
    publications = {"evidence": 0, "terminal": 0}

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    class _GatedPublic:
        def __init__(self, delegate: Phase7EPublicService) -> None:
            self.delegate = delegate

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        def resolve_existing(self, prepared: Phase7EPreparedRequest) -> Phase7EPublicStatus | None:
            if fail_readback.is_set():
                raise SuccessorExecutionError(readback_error_code)
            return self.delegate.resolve_existing(prepared)

        def execute_prepared(self, *args: object, **kwargs: object) -> Phase7EPublicStatus:
            entered.set()
            assert release.wait(5)
            return self.delegate.status(confirmed.investigation_id, args[0].request.run_id)  # type: ignore[attr-defined]

    def make_public() -> tuple[Phase7EPublicService, SuccessorExecutionService]:
        execution = _service(tmp_path, ANCHOR + timedelta(minutes=15))
        execution.evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
        original_stage = execution.evidence_repository.stage
        original_terminal = execution.publisher.publish_terminal

        def stage(*args: object, **kwargs: object) -> object:
            publications["evidence"] += 1
            return original_stage(*args, **kwargs)

        def terminal(*args: object, **kwargs: object) -> object:
            publications["terminal"] += 1
            return original_terminal(*args, **kwargs)

        monkeypatch.setattr(execution.evidence_repository, "stage", stage)
        monkeypatch.setattr(execution.publisher, "publish_terminal", terminal)
        public = Phase7EPublicService(
            RecordingSearch7ERepository(tmp_path / "legacy"),
            SimpleNamespace(),
            _Confirmation(),
            None,
            None,
            policy,
            classifier_policy,
            object_policy,
            SimpleNamespace(status=lambda *_args: (None, None)),
            lambda: ANCHOR + timedelta(hours=1),
            None,
            execution,
        )
        return public, execution

    public_a, _execution_a = make_public()
    manager_a = Phase7EBackgroundManager(_GatedPublic(public_a))  # type: ignore[arg-type]
    first_request = "11111111-1111-4111-8111-111111111111"
    second_request = "22222222-2222-4222-8222-222222222222"
    first = manager_a.start(confirmed.investigation_id, "2026-09-04T14:47:32", first_request)
    first_job = manager_a._jobs[first_request]
    assert first_job.future is not None
    try:
        assert entered.wait(2)
        fail_readback.set()
        release.set()
        with pytest.raises(SuccessorExecutionError, match="successor_publication_corrupt"):
            first_job.future.result(timeout=5)
        assert manager_a._active_request_id is None
        assert first_job.ownership is not None
        assert not first_job.ownership.held
        assert publications == {"evidence": 0, "terminal": 0}

        probe_lock = LocalInvestigationLock(
            public_a.repository.lock_path(confirmed.investigation_id)
        )
        assert probe_lock.try_acquire(0)
        probe_lock.release()

        public_b, execution_b = make_public()
        manager_b = Phase7EBackgroundManager(public_b)
        try:
            second = manager_b.start(
                confirmed.investigation_id, "2026-09-04T14:47:32", second_request
            )
            assert second.status == "ACCEPTED"
            second_job = manager_b._jobs[second_request]
            assert second_job.future is not None
            second_job.future.result(timeout=10)
            assert manager_b.status(confirmed.investigation_id, first.run_id).phase7.status == (
                "INTERRUPTED"
            )
            assert (
                manager_b.status(confirmed.investigation_id, second.run_id).phase7.status == "FOUND"
            )
            assert execution_b.acquisition.replay_extractor.calls > 0
            assert publications == {"evidence": 1, "terminal": 2}
        finally:
            manager_b.close()
    finally:
        release.set()
        manager_a.close()


def test_successor_worker_submit_failure_closes_running_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected worker submission cannot strand its published RUNNING claim."""
    confirmed = replace(_confirmed(tmp_path), jpeg_sha256=hashlib.sha256(b"baseline").hexdigest())
    policy, classifier_policy, object_policy = approved_phase7e_policy()

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    execution = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    public = Phase7EPublicService(
        RecordingSearch7ERepository(tmp_path / "legacy"),
        SimpleNamespace(),
        _Confirmation(),
        None,
        None,
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(status=lambda *_args: (None, None)),
        lambda: ANCHOR + timedelta(hours=1),
        None,
        execution,
    )
    manager = Phase7EBackgroundManager(public)
    request_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"

    def reject_submit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError

    monkeypatch.setattr(manager._executor, "submit", reject_submit)
    try:
        with pytest.raises(Phase7EPublicError, match="recording_search_execution_unavailable"):
            manager.start(confirmed.investigation_id, "2026-09-04T14:47:32", request_id)
        run_id = "search-run-" + request_id.replace("-", "")
        assert public.status(confirmed.investigation_id, run_id).phase7.status == "FAILED"
        assert execution.publisher.recover_abandoned(lock_path_for=public.repository.lock_path) == 0
    finally:
        manager.close()


def test_simultaneous_successor_admission_is_one_owned_worker(  # noqa: C901
    tmp_path: Path,
) -> None:
    """Both managers read absent first; only one may launch the same run."""
    confirmed = replace(_confirmed(tmp_path), jpeg_sha256=hashlib.sha256(b"baseline").hexdigest())
    policy, classifier_policy, object_policy = approved_phase7e_policy()
    barrier = Barrier(2)
    release = Event()
    entered = Event()
    count_lock = Lock()
    worker_count = 0
    results: list[Phase7EStartReceipt | Phase7EPublicError] = []

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    class _RacingPublic:
        def __init__(self, delegate: Phase7EPublicService) -> None:
            self.delegate = delegate

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        def acquire_successor_ownership(self, prepared: object) -> object:
            barrier.wait(5)
            return self.delegate.acquire_successor_ownership(prepared)  # type: ignore[arg-type]

        def execute_prepared(self, *args: object, **kwargs: object) -> Phase7EPublicStatus:
            nonlocal worker_count
            with count_lock:
                worker_count += 1
            entered.set()
            assert release.wait(5)
            return self.delegate.execute_prepared(*args, **kwargs)  # type: ignore[arg-type]

    managers: list[Phase7EBackgroundManager] = []
    for _ in range(2):
        execution = _service(tmp_path, ANCHOR + timedelta(minutes=15))
        execution.evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
        public = Phase7EPublicService(
            RecordingSearch7ERepository(tmp_path / "legacy"),
            SimpleNamespace(),
            _Confirmation(),
            None,
            None,
            policy,
            classifier_policy,
            object_policy,
            SimpleNamespace(status=lambda *_args: (None, None)),
            lambda: ANCHOR + timedelta(hours=1),
            None,
            execution,
        )
        managers.append(Phase7EBackgroundManager(_RacingPublic(public)))  # type: ignore[arg-type]

    request_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

    def start(manager: Phase7EBackgroundManager) -> None:
        try:
            results.append(
                manager.start(confirmed.investigation_id, "2026-09-04T14:47:32", request_id)
            )
        except Phase7EPublicError as error:
            results.append(error)

    threads = [Thread(target=start, args=(manager,)) for manager in managers]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(8)
        assert all(not thread.is_alive() for thread in threads)
        assert len(results) == 2
        assert all(isinstance(result, Phase7EStartReceipt) for result in results)
        assert {result.status for result in results if isinstance(result, Phase7EStartReceipt)} == {
            "ACCEPTED",
            "RUNNING",
        }
        assert entered.is_set()
        assert worker_count == 1
    finally:
        release.set()
        for manager in managers:
            manager.close()


@pytest.mark.parametrize(
    ("case", "expected_candidate_count"),
    [
        ("rack", 1),
        ("camera_motion", 0),
        ("occluded", 0),
        ("replacement", 0),
        ("present", 0),
    ],
)
def test_s7_public_lifecycle_uses_real_s3_evidence_without_public_promotion(  # noqa: PLR0915
    tmp_path: Path, case: str, expected_candidate_count: int
) -> None:
    """Exercise S3 -> S4 -> durable evidence -> public reopen without NVR."""

    target_comparisons = {
        "rack": _rack_absent_comparison(),
        "camera_motion": _comparison(
            similarity=0.65,
            ncc=0.05,
            edge=0.80,
            change=0.80,
            foreground=0.55,
            background_change=0.25,
            roi_ncc=0.18,
            alignment_state="ambiguous",
            alignment_dx=8,
            alignment_dy=6,
            alignment_rotation=5,
            alignment_overlap=0.98,
            alignment_score=0.62,
            alignment_margin=0.001,
            present_gate=False,
            occlusion_evidence=True,
            decision_path="indeterminate",
            decision_reason="unstable_scene",
        ),
        "occluded": _comparison(
            similarity=0.758071,
            ncc=0.068171,
            edge=0.903155,
            change=0.699227,
            foreground=0.596733,
            background_change=0.577731,
            scene_stable=False,
            scene_veto="global_scene_change",
            present_gate=False,
            occlusion_evidence=True,
            decision_path="indeterminate",
            decision_reason="unstable_scene",
        ),
        "replacement": _comparison(
            similarity=0.522460,
            ncc=0.135319,
            edge=0.882568,
            change=0.854659,
            foreground=0.808227,
            background_change=0.969883,
            scene_stable=False,
            scene_veto="global_scene_change",
            present_gate=False,
            replacement_evidence=True,
            decision_path="indeterminate",
            decision_reason="unstable_scene",
        ),
        "present": _comparison(),
    }

    class _CaseClassifier:
        policy_identity = "s7-e2e-classifier-v1"

        def __init__(self) -> None:
            self.calls = 0

        def classify(
            self,
            _baseline: object,
            probe: DecodedRgbImage,
            _width: int,
            _height: int,
            _roi: object,
            _correlation_id: str,
        ) -> SuccessorClassifierResult:
            self.calls += 1
            if probe.pixels[0][0][0] == 0 or case == "present":
                return SuccessorClassifierResult(
                    ClassificationOutcome.PRESENT, comparison=_comparison()
                )
            return SuccessorClassifierResult(
                ClassificationOutcome.INDETERMINATE,
                "insufficient_visual_evidence",
                target_comparisons[case],
            )

    planner = _Planner(_segment(ANCHOR + timedelta(minutes=30, seconds=1)))
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        _Extractor(tmp_path, ANCHOR + timedelta(minutes=3)),
        _FrameDecoder(),
        temporary_directory=tmp_path / "temporary",
    )
    classifier = _CaseClassifier()
    classification = SuccessorCoarseClassificationService(
        classifier, _MediaDecoder(), search_evidence_policy=_policy()
    )
    execution = SuccessorExecutionService(
        SuccessorPlanService(planner),
        acquisition,
        classification,
        SuccessorBinaryNarrowingService(acquisition, classification),
        _MediaDecoder(),
        SuccessorTerminalRepository(tmp_path / "successor"),
        SuccessorEvidenceRepository(tmp_path / "successor"),
    )
    confirmed = replace(_confirmed(tmp_path), jpeg_sha256=hashlib.sha256(b"baseline").hexdigest())
    policy, classifier_policy, object_policy = approved_phase7e_policy()

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    public = Phase7EPublicService(
        RecordingSearch7ERepository(tmp_path / "legacy"),
        SimpleNamespace(),
        _Confirmation(),
        None,
        None,
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(status=lambda *_args: (None, None)),
        lambda: ANCHOR + timedelta(hours=1),
        None,
        execution,
    )
    prepared = public.prepare_http(
        confirmed.investigation_id,
        "2026-09-04T14:47:32",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
    )
    assert prepared.successor is not None
    app = FastAPI()
    install_recording_search_routes(app, None, CapacityLimiter(2), phase7e_service=public)
    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/recording-searches",
            json={
                "investigation_id": confirmed.investigation_id,
                "search_end": "2026-09-04T14:47:32",
                "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
            },
        )
        assert accepted.status_code == 202
        route = accepted.json()["status_url"]
        for _ in range(500):
            status_response = client.get(route)
            if status_response.json()["status"] not in {"ACCEPTED", "RUNNING"}:
                break
            time.sleep(0.01)
        assert status_response.json()["status"] not in {"ACCEPTED", "RUNNING"}
        calls_at_terminal = classifier.calls
        duplicate = client.post(
            "/api/v1/recording-searches",
            json={
                "investigation_id": confirmed.investigation_id,
                "search_end": "2026-09-04T14:47:32",
                "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
            },
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["status"] == status_response.json()["status"]
        assert classifier.calls == calls_at_terminal
        evidence_response = client.get(f"{route}/evidence")
    public_status = public.status(confirmed.investigation_id, prepared.request.run_id)
    candidates = execution.read_candidates(confirmed.investigation_id, prepared.request.run_id)
    public_evidence = execution.read_evidence(confirmed.investigation_id, prepared.request.run_id)
    assert len(candidates) == expected_candidate_count
    assert public_status.phase7.schema_version == 8
    assert public_status.phase7.status in {"INCONCLUSIVE", "NOT_FOUND"}
    assert public_status.terminal_details is not None
    assert public_status.terminal_details.first_absent_time_utc is None
    assert public_evidence is not None
    assert "candidate_state" not in public_evidence
    assert status_response.status_code == 200
    assert status_response.json()["status"] == public_status.phase7.status
    assert evidence_response.status_code == 200
    assert "candidate_state" not in evidence_response.json()
    assert (
        SuccessorTerminalRepository(tmp_path / "successor").read(
            confirmed.investigation_id, prepared.request.run_id
        )["status"]
        == public_status.phase7.status
    )  # type: ignore[index]
    assert (
        SuccessorEvidenceRepository(tmp_path / "successor").read_candidates(
            confirmed.investigation_id, prepared.request.run_id
        )
        == candidates
    )
    calls_after_execution = classifier.calls
    assert calls_after_execution > 0
    reopened_public = replace(
        public,
        successor_execution=replace(
            execution,
            publisher=SuccessorTerminalRepository(tmp_path / "successor"),
            evidence_repository=SuccessorEvidenceRepository(tmp_path / "successor"),
        ),
    )
    reopened = reopened_public.resolve_existing(
        reopened_public.prepare_http(
            confirmed.investigation_id,
            "2026-09-04T14:47:32",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
        )
    )
    assert reopened is not None
    assert reopened.phase7.status == public_status.phase7.status
    assert classifier.calls == calls_after_execution
    if case == "rack":
        assert candidates[0].interval_start_utc <= ANCHOR + timedelta(minutes=3)
        assert candidates[0].interval_end_utc >= ANCHOR + timedelta(minutes=3)


def test_narrowing_coverage_gap_prevents_candidate_persistence_and_public_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _proxy = _coarse_fallback_service(
        tmp_path,
        material_after=ANCHOR + timedelta(minutes=3),
    )
    evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
    service.evidence_repository = evidence_repository
    confirmed = replace(
        _confirmed(tmp_path),
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:32:32",
        run_id="search-run-88888888888888888888888888888888",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    original_candidate_search = execution_module._candidate_search

    def wait_for_qualified_candidate(
        prepared_execution: SuccessorPreparedExecution,
        coarse: object,
    ) -> SuccessorCandidateFormationResult:
        result = original_candidate_search(prepared_execution, coarse)  # type: ignore[arg-type]
        if result.candidates and not result.qualified_candidates:
            return SuccessorCandidateFormationResult(())
        return result

    narrowed_candidates: list[SuccessorCandidateInterval] = []

    def narrow_with_gap(
        candidate: SuccessorCandidateInterval,
        _midpoint_sampler: object,
        **_kwargs: object,
    ) -> EvidenceNarrowingResult:
        narrowed_candidates.append(candidate)
        return EvidenceNarrowingResult(
            candidate.candidate_id,
            candidate.interval_start_utc,
            candidate.interval_end_utc,
            candidate.width_seconds,
            1,
            (),
            EvidenceNarrowingCompletion.GAP,
            "midpoint_gap",
            coverage_incomplete=True,
        )

    monkeypatch.setattr(execution_module, "_candidate_search", wait_for_qualified_candidate)
    monkeypatch.setattr(execution_module, "narrow_candidate_interval", narrow_with_gap)

    result = service.execute(prepared)
    manifest = evidence_repository.read(confirmed.investigation_id, prepared.request.run_id)

    assert len(narrowed_candidates) == 1
    assert narrowed_candidates[0].qualified is True
    assert narrowed_candidates[0].coverage_incomplete is False
    assert result.status == "INCONCLUSIVE"
    assert result.first_absent_time_utc is None
    assert all(item["state"] != "ABSENT" for item in result.coarse_observations)
    assert manifest is not None
    assert manifest["terminal_status"] == "INCONCLUSIVE"
    assert manifest["first_absent_observation_id"] is None
    assert manifest["candidate_state"] is None
    assert (
        evidence_repository.read_candidates(confirmed.investigation_id, prepared.request.run_id)
        == ()
    )


def test_fractional_reference_frame_execution_normalizes_coarse_assignments(
    tmp_path: Path,
) -> None:
    service, proxy = _coarse_fallback_service(
        tmp_path,
        fractional_frame_offset_seconds=-0.25,
        material_after=None,
    )
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:32:32",
        run_id="search-run-coarsefractional000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "INCONCLUSIVE"
    assert result.status != "FAILED"
    assert len(proxy.probe_times) == execution_module._COARSE_FALLBACK_MAX_PROBES
    assert all(item.microsecond == 0 for item in proxy.probe_times)
    assert proxy.probe_times == sorted(proxy.probe_times)
    assert len(proxy.probe_times) == len(set(proxy.probe_times))


def test_ambiguous_endpoint_with_only_ambiguous_probes_is_bounded_and_inconclusive(
    tmp_path: Path,
) -> None:
    service, proxy = _coarse_fallback_service(tmp_path, material_after=None)
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:32:32",
        run_id="search-run-coarseambiguous00000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "INCONCLUSIVE"
    assert len(proxy.probe_times) == 4
    assert len(proxy.probe_times) <= execution_module._COARSE_FALLBACK_MAX_PROBES


def test_ambiguous_endpoint_with_present_probes_does_not_form_candidate(
    tmp_path: Path,
) -> None:
    service, proxy = _coarse_fallback_service(tmp_path, probes_present=True)
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:32:32",
        run_id="search-run-coarsepresent00000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "INCONCLUSIVE"
    assert len(proxy.probe_times) == execution_module._COARSE_FALLBACK_MAX_PROBES


def test_coarse_fallback_cancellation_stops_after_current_probe_and_publishes_once(
    tmp_path: Path,
) -> None:
    service, proxy = _coarse_fallback_service(tmp_path, cancel_after_first_probe=True)
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:32:32",
        run_id="search-run-coarsecancel000000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    published: list[SuccessorTerminal] = []
    original_publish = service.publisher.publish_terminal

    def publish_terminal(terminal: SuccessorTerminal) -> SuccessorTerminal:
        published.append(terminal)
        return original_publish(terminal)

    service.publisher.publish_terminal = publish_terminal  # type: ignore[method-assign]

    result = service.execute(prepared, cancellation=lambda: proxy.cancel_requested)

    assert result.status == "INTERRUPTED"
    assert result.reason_code == "cancelled"
    assert len(proxy.probe_times) == 1
    assert len(published) == 1


def test_coarse_fallback_probe_times_are_bounded_and_deduplicate_existing_observations() -> None:
    start = ANCHOR
    end = ANCHOR + timedelta(minutes=20)
    expected = execution_module._coarse_fallback_probe_times(start, end, ())
    deduplicated = execution_module._coarse_fallback_probe_times(
        start,
        end,
        (expected[0], expected[-1]),
    )

    assert len(expected) == execution_module._COARSE_FALLBACK_MAX_PROBES
    assert len(deduplicated) == execution_module._COARSE_FALLBACK_MAX_PROBES - 2
    assert expected[0] not in deduplicated
    assert expected[-1] not in deduplicated


def test_coarse_fallback_probe_times_normalize_fractional_boundaries() -> None:
    start = datetime(2026, 9, 21, 6, 9, 0, 250_000, tzinfo=UTC)
    end = datetime(2026, 9, 21, 6, 15, tzinfo=UTC)

    probes = execution_module._coarse_fallback_probe_times(start, end, ())

    assert probes == (
        datetime(2026, 9, 21, 6, 10, 11, tzinfo=UTC),
        datetime(2026, 9, 21, 6, 11, 23, tzinfo=UTC),
        datetime(2026, 9, 21, 6, 12, 35, tzinfo=UTC),
        datetime(2026, 9, 21, 6, 13, 47, tzinfo=UTC),
    )
    assert all(item.microsecond == 0 for item in probes)
    assert all(start < item < end for item in probes)
    assert probes == tuple(sorted(probes))
    assert len(probes) == len(set(probes)) <= execution_module._COARSE_FALLBACK_MAX_PROBES

    fractional_end = end + timedelta(microseconds=750_000)
    fractional_end_probes = execution_module._coarse_fallback_probe_times(
        start,
        fractional_end,
        (),
    )
    assert all(item.microsecond == 0 for item in fractional_end_probes)
    assert all(start < item < fractional_end for item in fractional_end_probes)
    assert fractional_end_probes == tuple(sorted(set(fractional_end_probes)))


def test_coarse_fallback_probe_times_collapse_narrow_fractional_interval() -> None:
    start = datetime(2026, 9, 21, 6, 9, 0, 900_000, tzinfo=UTC)
    end = datetime(2026, 9, 21, 6, 9, 4, 100_000, tzinfo=UTC)

    probes = execution_module._coarse_fallback_probe_times(start, end, ())

    assert probes == (
        datetime(2026, 9, 21, 6, 9, 1, tzinfo=UTC),
        datetime(2026, 9, 21, 6, 9, 2, tzinfo=UTC),
    )
    assert all(start < item < end for item in probes)
    assert len(probes) < execution_module._COARSE_FALLBACK_MAX_PROBES
    assert len(probes) == len(set(probes))


def test_coarse_fallback_probe_times_preserve_whole_second_schedule() -> None:
    start = ANCHOR
    end = ANCHOR + timedelta(minutes=20)

    probes = execution_module._coarse_fallback_probe_times(start, end, ())

    assert probes == (
        ANCHOR + timedelta(minutes=4),
        ANCHOR + timedelta(minutes=8),
        ANCHOR + timedelta(minutes=12),
        ANCHOR + timedelta(minutes=16),
    )


def test_s4_narrowing_cancellation_publishes_interrupted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(hours=2))
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-s4cancel000000000000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    candidate = SuccessorCandidateInterval(
        "successor-candidate-v1-" + "a" * 64,
        "anchor",
        "drop",
        ANCHOR,
        ANCHOR + timedelta(seconds=60),
        qualified=True,
        provisional=False,
    )
    candidate_search = SuccessorCandidateFormationResult((candidate,))
    cancellation_armed = False

    def fake_candidate_search(
        *_args: object, **_kwargs: object
    ) -> SuccessorCandidateFormationResult:
        return candidate_search

    def cancellation() -> bool:
        return cancellation_armed

    original_narrow_candidate_interval = execution_module.narrow_candidate_interval

    def narrow_with_cancellation(
        candidate: object, midpoint_sampler: object, **kwargs: object
    ) -> EvidenceNarrowingResult:
        def arm_before_midpoint(midpoint: datetime) -> object:
            nonlocal cancellation_armed
            cancellation_armed = True
            return midpoint_sampler(midpoint)  # type: ignore[operator]

        return original_narrow_candidate_interval(
            candidate,  # type: ignore[arg-type]
            arm_before_midpoint,
            **kwargs,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(execution_module, "_candidate_search", fake_candidate_search)
    monkeypatch.setattr(execution_module, "narrow_candidate_interval", narrow_with_cancellation)
    published: list[SuccessorTerminal] = []
    original_publish_terminal = service.publisher.publish_terminal

    def publish_terminal(terminal: SuccessorTerminal) -> SuccessorTerminal:
        published.append(terminal)
        return original_publish_terminal(terminal)

    monkeypatch.setattr(service.publisher, "publish_terminal", publish_terminal)

    result = service.execute(prepared, cancellation=cancellation)

    assert result.status == "INTERRUPTED"
    assert result.reason_code == "cancelled"
    assert result.status != "INCONCLUSIVE"
    assert result.status != "FOUND"
    assert result.reason_code != "incomplete_coverage"
    assert len(published) == 1


def test_s5_verification_cancellation_publishes_interrupted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(hours=2))
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-s5cancel000000000000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    candidate = SuccessorCandidateInterval(
        "successor-candidate-v1-" + "b" * 64,
        "anchor",
        "drop",
        ANCHOR,
        ANCHOR + timedelta(seconds=60),
        qualified=True,
        provisional=False,
    )
    candidate_search = SuccessorCandidateFormationResult((candidate,))

    def fake_candidate_search(
        *_args: object, **_kwargs: object
    ) -> SuccessorCandidateFormationResult:
        return candidate_search

    original_with_anchor = execution_module._with_anchor_observation

    def without_bracket(
        prepared_execution: object, coarse: object, anchor_observation: object
    ) -> object:
        return replace(
            original_with_anchor(
                prepared_execution,
                coarse,
                anchor_observation,  # type: ignore[arg-type]
            ),
            candidate_bracket=None,
        )

    phase = "before_s5"
    cancellation_checks = 0

    def cancellation() -> bool:
        nonlocal cancellation_checks
        if phase != "s5":
            return False
        cancellation_checks += 1
        return cancellation_checks >= 2

    def fake_s4_narrowing(
        _service: object,
        _prepared: object,
        _coarse: object,
        _candidate_search: object,
        *,
        cancellation: object,
    ) -> EvidenceNarrowingResult:
        nonlocal phase
        _ = cancellation
        phase = "s5"
        return EvidenceNarrowingResult(
            candidate.candidate_id,
            candidate.interval_start_utc,
            candidate.interval_end_utc,
            candidate.width_seconds,
            0,
            (),
            EvidenceNarrowingCompletion.TARGET_WIDTH_REACHED,
            "target_width_reached",
        )

    monkeypatch.setattr(execution_module, "_candidate_search", fake_candidate_search)
    monkeypatch.setattr(execution_module, "_with_anchor_observation", without_bracket)
    monkeypatch.setattr(SuccessorExecutionService, "_run_s4_narrowing", fake_s4_narrowing)
    published: list[SuccessorTerminal] = []
    original_publish_terminal = service.publisher.publish_terminal

    def publish_terminal(terminal: SuccessorTerminal) -> SuccessorTerminal:
        published.append(terminal)
        return original_publish_terminal(terminal)

    monkeypatch.setattr(service.publisher, "publish_terminal", publish_terminal)

    result = service.execute(prepared, cancellation=cancellation)

    assert result.status == "INTERRUPTED"
    assert result.reason_code == "cancelled"
    assert len(published) == 1


def test_cancellation_wins_over_late_b4_bracket_result_and_publishes_once(
    tmp_path: Path,
) -> None:
    classifier = _CancelAfterB4ResultClassifier()
    service = _service(tmp_path, ANCHOR + timedelta(minutes=1), classifier)
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-b4latecancel00000000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    published: list[SuccessorTerminal] = []
    original_publish_terminal = service.publisher.publish_terminal

    def publish_terminal(terminal: SuccessorTerminal) -> SuccessorTerminal:
        published.append(terminal)
        return original_publish_terminal(terminal)

    service.publisher.publish_terminal = publish_terminal  # type: ignore[method-assign]

    result = service.execute(prepared, cancellation=lambda: classifier.cancel_requested)

    assert result.status == "INTERRUPTED"
    assert result.reason_code == "cancelled"
    assert result.status != "FOUND"
    assert result.status != "INCONCLUSIVE"
    assert classifier.calls == 2
    assert len(classifier.cancellation_callbacks) == classifier.calls
    assert len(published) == 1
    record = service.publisher.read(confirmed.investigation_id, prepared.request.run_id)
    assert record is not None
    assert record["status"] == "INTERRUPTED"


def test_cancellation_before_b4_starts_publishes_interrupted_without_classification(
    tmp_path: Path,
) -> None:
    classifier = _CancelAfterB4ResultClassifier()
    service = _service(tmp_path, ANCHOR + timedelta(minutes=1), classifier)
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-b4precancel0000000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared, cancellation=lambda: True)

    assert result.status == "INTERRUPTED"
    assert result.reason_code == "cancelled"
    assert classifier.calls == 0


def test_cancellation_during_candidate_formation_blocks_not_found_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(hours=2))
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-candidatecancel0000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    cancellation_requested = False

    def candidate_search_with_cancellation(
        *_args: object,
        **_kwargs: object,
    ) -> SuccessorCandidateFormationResult:
        nonlocal cancellation_requested
        cancellation_requested = True
        return SuccessorCandidateFormationResult(())

    monkeypatch.setattr(
        execution_module,
        "_candidate_search",
        candidate_search_with_cancellation,
    )
    published: list[SuccessorTerminal] = []
    original_publish_terminal = service.publisher.publish_terminal

    def publish_terminal(terminal: SuccessorTerminal) -> SuccessorTerminal:
        published.append(terminal)
        return original_publish_terminal(terminal)

    monkeypatch.setattr(service.publisher, "publish_terminal", publish_terminal)

    result = service.execute(prepared, cancellation=lambda: cancellation_requested)

    assert cancellation_requested
    assert result.status == "INTERRUPTED"
    assert result.reason_code == "cancelled"
    assert len(published) == 1
    assert published[0].status == "INTERRUPTED"


def test_cancellation_after_inconclusive_terminal_construction_wins_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(hours=2), _IndeterminateClassifier())
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-inconclusivecancel000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    cancellation_requested = False
    original_publish_terminal = SuccessorExecutionService._publish_terminal

    def arm_before_publish(
        execution_service: SuccessorExecutionService,
        prepared_execution: SuccessorPreparedExecution,
        terminal: SuccessorTerminal,
        observations: tuple[SuccessorObservation, ...],
        *,
        cancellation: object = None,
    ) -> SuccessorTerminal:
        nonlocal cancellation_requested
        cancellation_requested = True
        return original_publish_terminal(
            execution_service,
            prepared_execution,
            terminal,
            observations,
            cancellation=cancellation,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(SuccessorExecutionService, "_publish_terminal", arm_before_publish)
    published: list[SuccessorTerminal] = []
    original_publisher = service.publisher.publish_terminal

    def publish_terminal(terminal: SuccessorTerminal) -> SuccessorTerminal:
        published.append(terminal)
        return original_publisher(terminal)

    monkeypatch.setattr(service.publisher, "publish_terminal", publish_terminal)

    result = service.execute(prepared, cancellation=lambda: cancellation_requested)

    assert result.status == "INTERRUPTED"
    assert result.reason_code == "cancelled"
    assert result.status != "INCONCLUSIVE"
    assert len(published) == 1


def test_cancellation_after_committed_normal_terminal_does_not_publish_second_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-postcommitcancel000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    first = service.execute(prepared)
    assert first.status == "FOUND"
    published: list[SuccessorTerminal] = []
    original_publish_terminal = service.publisher.publish_terminal

    def publish_terminal(terminal: SuccessorTerminal) -> SuccessorTerminal:
        published.append(terminal)
        return original_publish_terminal(terminal)

    monkeypatch.setattr(service.publisher, "publish_terminal", publish_terminal)

    second = service.execute(prepared, cancellation=lambda: True)

    assert second.status == "FOUND"
    assert second.terminal_result_id == first.terminal_result_id
    assert published == []


@pytest.mark.parametrize(
    ("absent_after", "classifier", "expected_status"),
    [
        (ANCHOR + timedelta(hours=2), None, "NOT_FOUND"),
        (ANCHOR + timedelta(minutes=15), None, "FOUND"),
        (ANCHOR + timedelta(hours=2), _IndeterminateClassifier(), "INCONCLUSIVE"),
    ],
)
def test_cancellation_during_evidence_staging_hides_normal_manifest(
    tmp_path: Path,
    absent_after: datetime,
    classifier: object | None,
    expected_status: str,
) -> None:
    service = _service(tmp_path, absent_after, classifier)
    evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
    service.evidence_repository = evidence_repository
    confirmed = replace(
        _confirmed(tmp_path),
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-" + ("e" if expected_status == "FOUND" else "f") * 32,
        now_utc=ANCHOR + timedelta(hours=1),
    )
    cancellation_requested = False
    original_stage = evidence_repository.stage

    def stage_then_cancel(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal cancellation_requested
        result = original_stage(*args, **kwargs)
        cancellation_requested = True
        return result

    evidence_repository.stage = stage_then_cancel  # type: ignore[method-assign]

    result = service.execute(prepared, cancellation=lambda: cancellation_requested)

    assert expected_status in {"FOUND", "NOT_FOUND", "INCONCLUSIVE"}
    assert result.status == "INTERRUPTED"
    assert result.reason_code == "cancelled"
    reopened = service.publisher.read(confirmed.investigation_id, prepared.request.run_id)
    assert reopened is not None
    assert reopened["status"] == "INTERRUPTED"
    assert evidence_repository.read(confirmed.investigation_id, prepared.request.run_id) is None
    pending = (
        tmp_path
        / "successor"
        / confirmed.investigation_id
        / prepared.request.run_id
        / "evidence"
        / "manifest.json.pending"
    )
    assert not pending.exists()


@pytest.mark.parametrize(
    ("absent_after", "classifier", "expected_status"),
    [
        (ANCHOR + timedelta(hours=2), None, "NOT_FOUND"),
        (ANCHOR + timedelta(minutes=15), None, "FOUND"),
        (ANCHOR + timedelta(hours=2), _IndeterminateClassifier(), "INCONCLUSIVE"),
    ],
)
def test_non_cancelled_terminal_and_evidence_remain_consistent(
    tmp_path: Path,
    absent_after: datetime,
    classifier: object | None,
    expected_status: str,
) -> None:
    service = _service(tmp_path, absent_after, classifier)
    evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
    service.evidence_repository = evidence_repository
    confirmed = replace(
        _confirmed(tmp_path),
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-" + ("1" if expected_status != "FOUND" else "2") * 32,
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)
    evidence = evidence_repository.read(confirmed.investigation_id, prepared.request.run_id)

    assert result.status == expected_status
    assert evidence is not None
    assert evidence["terminal_status"] == result.status
    assert evidence["terminal_reason"] == result.reason_code
    public_reader = Phase7EPublicService(
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        successor_execution=service,
    )
    public_evidence = dict(evidence)
    public_evidence.pop("candidate_state")
    public_evidence["version"] = "phase7e-successor-evidence-v1"
    assert (
        public_reader.evidence(confirmed.investigation_id, prepared.request.run_id)
        == public_evidence
    )


def test_cancellation_after_authoritative_evidence_commit_preserves_normal_result(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(hours=2))
    evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
    service.evidence_repository = evidence_repository
    confirmed = replace(
        _confirmed(tmp_path),
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-" + "3" * 32,
        now_utc=ANCHOR + timedelta(hours=1),
    )
    cancellation_requested = False
    original_commit = evidence_repository.commit_staged

    def commit_then_cancel(prepared_execution: SuccessorPreparedExecution) -> dict[str, object]:
        nonlocal cancellation_requested
        result = original_commit(prepared_execution)
        cancellation_requested = True
        return result

    evidence_repository.commit_staged = commit_then_cancel  # type: ignore[method-assign]

    result = service.execute(prepared, cancellation=lambda: cancellation_requested)

    assert result.status == "NOT_FOUND"
    reopened = service.publisher.read(confirmed.investigation_id, prepared.request.run_id)
    evidence = evidence_repository.read(confirmed.investigation_id, prepared.request.run_id)
    assert reopened is not None
    assert reopened["status"] == "NOT_FOUND"
    assert evidence is not None
    assert evidence["terminal_status"] == "NOT_FOUND"
    assert evidence["terminal_reason"] == "complete_present_coverage"


def test_recovery_suppresses_evidence_after_crash_before_terminal_commit(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(hours=2))
    evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
    service.evidence_repository = evidence_repository
    confirmed = replace(
        _confirmed(tmp_path),
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-" + "4" * 32,
        now_utc=ANCHOR + timedelta(hours=1),
    )
    original_commit = evidence_repository.commit_staged

    def commit_then_stop(prepared_execution: SuccessorPreparedExecution) -> dict[str, object]:
        original_commit(prepared_execution)
        raise KeyboardInterrupt

    evidence_repository.commit_staged = commit_then_stop  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        service.execute(prepared)

    before_recovery = service.publisher.read(confirmed.investigation_id, prepared.request.run_id)
    assert before_recovery is not None
    assert before_recovery["status"] == "RUNNING"
    assert service.read_evidence(confirmed.investigation_id, prepared.request.run_id) is None

    assert service.publisher.recover_abandoned() == 1

    recovered = service.publisher.read(confirmed.investigation_id, prepared.request.run_id)
    assert recovered is not None
    assert recovered["status"] == "INTERRUPTED"
    assert recovered["reason_code"] == "abandoned_after_restart"
    assert service.read_evidence(confirmed.investigation_id, prepared.request.run_id) is None


@pytest.mark.parametrize(
    ("absent_after", "classifier", "expected_status"),
    [
        (ANCHOR + timedelta(hours=2), None, "NOT_FOUND"),
        (ANCHOR + timedelta(minutes=15), None, "FOUND"),
        (ANCHOR + timedelta(hours=2), _IndeterminateClassifier(), "INCONCLUSIVE"),
    ],
)
def test_terminal_write_failure_suppresses_normal_evidence(
    tmp_path: Path,
    absent_after: datetime,
    classifier: object | None,
    expected_status: str,
) -> None:
    service = _service(tmp_path, absent_after, classifier)
    evidence_repository = SuccessorEvidenceRepository(tmp_path / "successor")
    service.evidence_repository = evidence_repository
    confirmed = replace(
        _confirmed(tmp_path),
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-" + ("5" if expected_status == "NOT_FOUND" else "6") * 32,
        now_utc=ANCHOR + timedelta(hours=1),
    )
    original_publish = service.publisher.publish_terminal

    def fail_normal_terminal(terminal: SuccessorTerminal) -> SuccessorTerminal:
        if terminal.status == expected_status:
            raise OSError from None
        return original_publish(terminal)

    service.publisher.publish_terminal = fail_normal_terminal  # type: ignore[method-assign]

    with pytest.raises(SuccessorExecutionError, match="internal_error"):
        service.execute(prepared)

    terminal = service.publisher.read(confirmed.investigation_id, prepared.request.run_id)
    assert terminal is not None
    assert terminal["status"] == "FAILED"
    assert terminal["reason_code"] == "internal_error"
    assert service.read_evidence(confirmed.investigation_id, prepared.request.run_id) is None


def test_historical_baseline_and_actual_anchor_are_durable_and_narrowable(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(seconds=1))
    confirmed = replace(
        _confirmed(tmp_path),
        requested_time_utc=ANCHOR - timedelta(seconds=60),
        requested_time_text="2026-09-04T14:16:32",
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:27:32",
        run_id="search-run-hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "FOUND"
    records = result.coarse_observations
    assert records[0]["state"] == "PRESENT"
    assert records[0]["requested_time_utc"] == "2026-09-04T05:16:32Z"
    assert records[0]["frame_utc"] == "2026-09-04T05:16:32Z"
    assert records[0]["frame_sha256"] == confirmed.jpeg_sha256
    assert records[1]["requested_time_utc"] == "2026-09-04T05:17:32Z"
    assert records[1]["state"] == "ABSENT"
    assert records[0]["observation_id"] != records[1]["observation_id"]
    assert result.last_present_time_utc is not None
    assert result.first_absent_time_utc is not None
    assert result.last_present_time_utc < result.first_absent_time_utc


def test_first_coarse_bracket_stops_later_replay_and_classification(tmp_path: Path) -> None:
    segment = _segment(ANCHOR + timedelta(minutes=30, seconds=1))
    planner = _Planner(segment)
    extractor = _LateTargetTimeoutExtractor(tmp_path, ANCHOR + timedelta(minutes=10))
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        extractor,
        _FrameDecoder(),
        temporary_directory=tmp_path / "temporary",
    )
    classification = SuccessorCoarseClassificationService(_Classifier(), _MediaDecoder())
    proxy = _ClassificationProxy(classification)
    service = SuccessorExecutionService(
        SuccessorPlanService(planner),
        acquisition,
        proxy,  # type: ignore[arg-type]
        SuccessorBinaryNarrowingService(acquisition, classification),
        _MediaDecoder(),
        SuccessorTerminalRepository(tmp_path / "successor"),
    )
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-stopafterbracket00000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "FOUND"
    assert proxy.coarse_sequences == [1]
    assert all(
        call.window.start_utc < ANCHOR + timedelta(minutes=15) for call in extractor.requests
    )


def test_coarse_timeout_before_bracket_publishes_safe_inconclusive(
    tmp_path: Path,
) -> None:
    segment = _segment(ANCHOR + timedelta(minutes=30, seconds=1))
    planner = _Planner(segment)
    extractor = _FirstCoarseTimeoutExtractor(tmp_path, ANCHOR + timedelta(hours=2))
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        extractor,
        _FrameDecoder(),
        temporary_directory=tmp_path / "temporary",
    )
    classification = SuccessorCoarseClassificationService(_Classifier(), _MediaDecoder())
    service = SuccessorExecutionService(
        SuccessorPlanService(planner),
        acquisition,
        classification,
        SuccessorBinaryNarrowingService(acquisition, classification),
        _MediaDecoder(),
        SuccessorTerminalRepository(tmp_path / "successor"),
    )
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:27:32",
        run_id="search-run-timeoutbeforebracket000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "INCONCLUSIVE"
    assert result.reason_code == "target_replay_timeout"
    assert len(extractor.requests) == 2


def test_future_baseline_moves_successor_effective_start(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    confirmed = replace(
        _confirmed(tmp_path),
        requested_time_utc=ANCHOR + timedelta(seconds=60),
        requested_time_text="2026-09-04T14:18:32",
    )

    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-jjjjjjjjjjjjjjjjjjjjjjjjjjjjjjjj",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    assert prepared.plan.anchor_time_utc == ANCHOR + timedelta(seconds=60)
    assert prepared.request.anchor_time_utc == ANCHOR + timedelta(seconds=60)
    assert prepared.request.duration_seconds == 1_740


def test_visual_indeterminate_reason_is_not_collapsed_in_terminal(tmp_path: Path) -> None:
    segment = _segment(ANCHOR + timedelta(minutes=30, seconds=1))
    planner = _Planner(segment)
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        _Extractor(tmp_path, ANCHOR + timedelta(hours=2)),
        _FrameDecoder(),
        temporary_directory=tmp_path / "temporary",
    )
    classification = SuccessorCoarseClassificationService(
        _IndeterminateClassifier(), _MediaDecoder()
    )
    service = SuccessorExecutionService(
        SuccessorPlanService(planner),
        acquisition,
        classification,
        SuccessorBinaryNarrowingService(acquisition, classification),
        _MediaDecoder(),
        SuccessorTerminalRepository(tmp_path / "successor"),
    )
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:27:32",
        run_id="search-run-iiiiiiiiiiiiiiiiiiiiiiiiiiiiiiii",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "INCONCLUSIVE"
    assert result.reason_code == "insufficient_visual_evidence"
    assert result.coarse_observations
    assert any(
        item["reason_code"] == "insufficient_visual_evidence" for item in result.coarse_observations
    )


def test_successor_publication_failure_gets_durable_terminal_fallback(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(hours=2))
    service.evidence_repository = _PublishThenFailEvidence(tmp_path / "successor")
    confirmed = replace(
        _confirmed(tmp_path),
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:27:32",
        run_id="search-run-ffffffffffffffffffffffffffffffff",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    with pytest.raises(SuccessorExecutionError, match="publication_failed"):
        service.execute(prepared)

    terminal = service.publisher.read(confirmed.investigation_id, prepared.request.run_id)
    assert terminal is not None
    assert terminal["status"] == "FAILED"
    assert terminal["reason_code"] == "internal_error"


def test_anchor_indeterminate_still_acquires_and_publishes_search_end_evidence(
    tmp_path: Path,
) -> None:
    segment = _segment(ANCHOR + timedelta(minutes=30, seconds=1))
    planner = _Planner(segment)
    acquisition = SuccessorTargetAcquisitionService(
        planner,
        _Extractor(tmp_path, ANCHOR + timedelta(hours=2)),
        _FrameDecoder(),
        temporary_directory=tmp_path / "temporary",
    )
    classifier = _AnchorIndeterminateThenPresentClassifier()
    classification = SuccessorCoarseClassificationService(classifier, _MediaDecoder())
    service = SuccessorExecutionService(
        SuccessorPlanService(planner),
        acquisition,
        classification,
        SuccessorBinaryNarrowingService(acquisition, classification),
        _MediaDecoder(),
        SuccessorTerminalRepository(tmp_path / "successor"),
        SuccessorEvidenceRepository(tmp_path / "successor"),
    )
    confirmed = replace(
        _confirmed(tmp_path),
        jpeg_sha256=hashlib.sha256(b"baseline").hexdigest(),
    )
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:27:32",
        run_id="search-run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        now_utc=ANCHOR + timedelta(hours=1),
    )

    result = service.execute(prepared)

    assert result.status == "INCONCLUSIVE"
    assert result.reason_code == "insufficient_visual_evidence"
    assert classifier.calls == 2
    assert tuple(item["requested_time_utc"] for item in result.coarse_observations) == (
        "2026-09-04T05:17:32Z",
        "2026-09-04T05:17:32Z",
        "2026-09-04T05:27:32Z",
    )
    evidence = service.evidence_repository.read(confirmed.investigation_id, prepared.request.run_id)
    assert evidence is not None
    observed = [item for item in evidence["entries"] if item["role"] == "observation"]
    assert len(observed) == 1
    assert observed[0]["requested_time_utc"] == "2026-09-04T05:27:32.000000Z"


def test_successor_durable_running_state_recovers_as_interrupted(tmp_path: Path) -> None:
    repository = SuccessorTerminalRepository(tmp_path / "successor")
    service = _service(tmp_path, ANCHOR + timedelta(hours=2))
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-cccccccccccccccccccccccccccccccc",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    repository.publish_running(prepared)
    assert repository.recover_abandoned() == 1
    recovered = repository.read(prepared.request.investigation_id, prepared.request.run_id)
    assert recovered is not None
    assert recovered["status"] == "INTERRUPTED"


def test_ownerless_preplan_running_recovers_without_inventing_plan(tmp_path: Path) -> None:
    repository = SuccessorTerminalRepository(tmp_path / "successor")
    request = SuccessorRequest(
        "object-disappearance-v3-ch1-20260904T051732Z",
        "search-run-cccccccccccccccccccccccccccccccc",
        1,
        ANCHOR,
        ANCHOR + timedelta(minutes=30),
        "Asia/Seoul",
    )
    repository.publish_admitted(request)
    running = repository.read(request.investigation_id, request.run_id)
    assert running is not None
    assert running["status"] == "RUNNING"
    assert running["plan_id"] is None
    assert repository.recover_abandoned() == 1
    recovered = repository.read(request.investigation_id, request.run_id)
    assert recovered is not None
    assert recovered["status"] == "INTERRUPTED"
    assert recovered["plan_id"] is None
    assert repository.recover_abandoned() == 0


@pytest.mark.parametrize("changed_key", ["channel_id", "request_end_utc"])
def test_preplan_request_tamper_cannot_bind_a_worker_plan(tmp_path: Path, changed_key: str) -> None:
    execution = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    prepared = execution.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-cccccccccccccccccccccccccccccccc",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    repository = execution.publisher
    repository.publish_admitted(prepared.request)
    path = repository._path(prepared.request.investigation_id, prepared.request.run_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record[changed_key] = 2 if changed_key == "channel_id" else "2026-09-04T05:37:32Z"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(SuccessorExecutionError, match="successor_request_conflict"):
        repository.publish_running(prepared)


def test_successor_terminal_is_not_reactivated_by_late_worker(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    confirmed = _confirmed(tmp_path)
    prepared = service.prepare(
        confirmed,
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-dddddddddddddddddddddddddddddddd",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    result = service.execute(prepared)
    assert result.status == "FOUND"
    service.publisher.publish_running(prepared)
    reopened = service.publisher.read(confirmed.investigation_id, prepared.request.run_id)
    assert reopened is not None
    assert reopened["status"] == "FOUND"
    assert reopened["terminal_result_id"] == result.terminal_result_id


def test_schema8_reopen_rejects_coercion_and_malformed_lists(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-strictterminal0000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    _ = service.execute(prepared)
    path = (
        tmp_path
        / "successor"
        / prepared.request.investigation_id
        / prepared.request.run_id
        / "terminal.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["coverage_complete"] = "false"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SuccessorExecutionError, match="successor_publication_corrupt"):
        service.publisher.read(prepared.request.investigation_id, prepared.request.run_id)

    payload["coverage_complete"] = False
    payload["coarse_target_ids"] = [1]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SuccessorExecutionError, match="successor_publication_corrupt"):
        service.publisher.read(prepared.request.investigation_id, prepared.request.run_id)


def test_schema8_reopen_rejects_timestamp_identity_and_phase8_corruption(tmp_path: Path) -> None:
    service = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    prepared = service.prepare(
        _confirmed(tmp_path),
        search_end_time_text="2026-09-04T14:47:32",
        run_id="search-run-stricttime0000000000000000000",
        now_utc=ANCHOR + timedelta(hours=1),
    )
    _ = service.execute(prepared)
    path = (
        tmp_path
        / "successor"
        / prepared.request.investigation_id
        / prepared.request.run_id
        / "terminal.json"
    )
    baseline = json.loads(path.read_text(encoding="utf-8"))
    mutations = (
        {"observed_start_time_utc": "not-a-timestamp"},
        {"investigation_id": "foreign-investigation"},
        {"phase8_status": "READY", "phase8_reason": None},
        {"reason_code": 7},
    )
    for mutation in mutations:
        candidate = {**baseline, **mutation}
        path.write_text(json.dumps(candidate), encoding="utf-8")
        with pytest.raises(SuccessorExecutionError, match="successor_publication_corrupt"):
            service.publisher.read(prepared.request.investigation_id, prepared.request.run_id)
    reduced = {
        key: value
        for key, value in baseline.items()
        if key
        not in {
            "phase8_status",
            "phase8_reason",
            "policy_version",
            "requested_end_time_utc",
            "narrowing_id",
            "coverage",
            "gaps",
            "coarse_observation_ids",
            "coarse_target_ids",
            "target_statuses",
            "coarse_observations",
        }
    }
    path.write_text(json.dumps(reduced), encoding="utf-8")
    reopened = service.publisher.read(prepared.request.investigation_id, prepared.request.run_id)
    assert reopened is not None
    assert reopened["status"] == "FOUND"


def test_successor_runs_through_http_background_and_restart_status(tmp_path: Path) -> None:
    confirmed = _confirmed(tmp_path)
    execution = _service(tmp_path, ANCHOR + timedelta(minutes=15))

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    policy, classifier_policy, object_policy = approved_phase7e_policy()
    service = Phase7EPublicService(
        RecordingSearch7ERepository(tmp_path / "legacy"),
        SimpleNamespace(),
        _Confirmation(),
        None,
        None,
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(status=lambda *_args: (None, None)),
        lambda: ANCHOR + timedelta(hours=3, microseconds=123456),
        None,
        execution,
    )
    for index, search_end in enumerate(
        (
            "2026-09-04T14:27:32",
            "2026-09-04T14:47:32",
            "2026-09-04T15:17:32",
            "2026-09-04T16:17:32",
        ),
        start=1,
    ):
        prepared = service.prepare_http(
            confirmed.investigation_id,
            search_end,
            f"{index:08x}-0000-4000-8000-000000000000",
        )
        assert prepared.successor is not None
    app = FastAPI()
    install_recording_search_routes(app, None, CapacityLimiter(2), phase7e_service=service)
    body = {
        "investigation_id": confirmed.investigation_id,
        "search_end": "2026-09-04T14:47:32",
        "request_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    }
    with TestClient(app) as client:
        accepted = client.post("/api/v1/recording-searches", json=body)
        assert accepted.status_code == 202
        status_url = accepted.json()["status_url"]
        for _ in range(100):
            status = client.get(status_url)
            if status.json()["status"] not in {"ACCEPTED", "RUNNING"}:
                break
        assert status.json()["status"] == "FOUND"
        assert status.json()["schema_version"] == 8
        duplicate = client.post("/api/v1/recording-searches", json=body)
        assert duplicate.status_code == 202
        assert duplicate.json()["status"] == "FOUND"
        ten_minute = client.post(
            "/api/v1/recording-searches",
            json={
                "investigation_id": confirmed.investigation_id,
                "search_end": "2026-09-04T14:27:32",
                "request_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            },
        )
        assert ten_minute.status_code == 202
        ten_status = client.get(ten_minute.json()["status_url"])
        for _ in range(100):
            if ten_status.json()["status"] not in {"ACCEPTED", "RUNNING"}:
                break
            ten_status = client.get(ten_minute.json()["status_url"])
        assert ten_status.json()["status"] == "NOT_FOUND"
        assert ten_status.json()["schema_version"] == 8
    restarted = FastAPI()
    install_recording_search_routes(restarted, None, CapacityLimiter(2), phase7e_service=service)
    with TestClient(restarted) as client:
        restored = client.get(status_url)
    assert restored.status_code == 200
    assert restored.json()["status"] == "FOUND"


@pytest.mark.parametrize("blocked_stage", ["discovery", "baseline_decode", "reference"])
def test_successor_http_ack_precedes_expensive_preparation(  # noqa: C901, PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_stage: str
) -> None:
    """The actual HTTP receipt is independent of each expensive successor stage."""
    confirmed = _confirmed(tmp_path)
    execution = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    entered, release, returned = Event(), Event(), Event()
    calls = {"discovery": 0, "baseline_decode": 0, "reference": 0}
    stage_threads: list[str] = []

    def gate(stage: str) -> None:
        stage_threads.append(current_thread().name)
        calls[stage] += 1
        if blocked_stage == stage:
            entered.set()
            assert release.wait(5)

    original_plan = SuccessorPlanService.plan
    original_decode = _MediaDecoder.decode
    original_reference = SuccessorCoarseClassificationService.prepare_reference

    def plan(self: SuccessorPlanService, request: object) -> object:
        gate("discovery")
        return original_plan(self, request)  # type: ignore[arg-type]

    def decode(self: _MediaDecoder, payload: bytes, width: int, height: int) -> DecodedMedia:
        if payload == b"baseline":
            gate("baseline_decode")
        return original_decode(self, payload, width, height)

    def reference(self: SuccessorCoarseClassificationService, authority: object) -> object:
        gate("reference")
        return original_reference(self, authority)  # type: ignore[arg-type]

    monkeypatch.setattr(SuccessorPlanService, "plan", plan)
    monkeypatch.setattr(_MediaDecoder, "decode", decode)
    monkeypatch.setattr(SuccessorCoarseClassificationService, "prepare_reference", reference)

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    policy, classifier_policy, object_policy = approved_phase7e_policy()
    service = Phase7EPublicService(
        RecordingSearch7ERepository(tmp_path / "legacy"),
        SimpleNamespace(),
        _Confirmation(),
        None,
        None,
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(status=lambda *_args: (None, None)),
        lambda: ANCHOR + timedelta(hours=3),
        None,
        execution,
    )
    app = FastAPI()
    install_recording_search_routes(app, None, CapacityLimiter(2), phase7e_service=service)
    request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    body = {
        "investigation_id": confirmed.investigation_id,
        "search_end": "2026-09-04T14:47:32",
        "request_id": request_id,
    }
    received: dict[str, object] = {}
    with TestClient(app) as client:

        def post() -> None:
            received["response"] = client.post("/api/v1/recording-searches", json=body)
            returned.set()

        starter = Thread(target=post)
        starter.start()
        try:
            assert entered.wait(3)
            assert returned.wait(1), "HTTP 202 waited for worker preparation"
            response = received["response"]
            assert response.status_code == 202  # type: ignore[attr-defined]
            payload = response.json()  # type: ignore[attr-defined]
            assert payload["run_id"] == "search-run-" + request_id.replace("-", "")
            status = client.get(payload["status_url"])
            assert status.status_code == 200
            assert status.json()["status"] == "RUNNING"
            duplicate = client.post("/api/v1/recording-searches", json=body)
            assert duplicate.status_code == 202
            assert duplicate.json()["run_id"] == payload["run_id"]
            assert calls["discovery"] <= 1
        finally:
            release.set()
            starter.join(timeout=5)
        for _ in range(100):
            status = client.get(payload["status_url"])
            if status.json()["status"] not in {"ACCEPTED", "RUNNING"}:
                break
            time.sleep(0.01)
        assert status.json()["status"] == "FOUND"
        assert calls["discovery"] == 1
        assert calls["baseline_decode"] == 1
        assert calls["reference"] == 1
        assert all(name.startswith("phase7e-browser") for name in stage_threads)


@pytest.mark.parametrize("failed_stage", ["discovery", "baseline_decode", "reference", "deadline"])
def test_successor_worker_preparation_failure_closes_admitted_run(  # noqa: C901, PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_stage: str
) -> None:
    """A post-ACK preparation error publishes one safe terminal and releases ownership."""
    confirmed = _confirmed(tmp_path)
    execution = _service(tmp_path, ANCHOR + timedelta(minutes=15))
    original_plan = SuccessorPlanService.plan
    original_decode = _MediaDecoder.decode
    original_reference = SuccessorCoarseClassificationService.prepare_reference
    release = Event()

    def plan(self: SuccessorPlanService, request: object) -> object:
        if failed_stage == "discovery":
            raise RuntimeError
        if failed_stage == "deadline":
            assert release.wait(5)
        return original_plan(self, request)  # type: ignore[arg-type]

    def decode(self: _MediaDecoder, payload: bytes, width: int, height: int) -> DecodedMedia:
        if failed_stage == "baseline_decode" and payload == b"baseline":
            raise RuntimeError
        return original_decode(self, payload, width, height)

    def reference(self: SuccessorCoarseClassificationService, authority: object) -> object:
        if failed_stage == "reference":
            raise RuntimeError
        return original_reference(self, authority)  # type: ignore[arg-type]

    monkeypatch.setattr(SuccessorPlanService, "plan", plan)
    monkeypatch.setattr(_MediaDecoder, "decode", decode)
    monkeypatch.setattr(SuccessorCoarseClassificationService, "prepare_reference", reference)

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    policy, classifier_policy, object_policy = approved_phase7e_policy()
    public = Phase7EPublicService(
        RecordingSearch7ERepository(tmp_path / "legacy"),
        SimpleNamespace(),
        _Confirmation(),
        None,
        None,
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(status=lambda *_args: (None, None)),
        lambda: ANCHOR + timedelta(hours=3),
        None,
        execution,
    )
    manager = Phase7EBackgroundManager(
        public, execution_deadline_seconds=0.05 if failed_stage == "deadline" else 60
    )
    request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    try:
        receipt = manager.start(confirmed.investigation_id, "2026-09-04T14:47:32", request_id)
        assert receipt.status == "ACCEPTED"
        if failed_stage == "deadline":
            current_status = manager.status(confirmed.investigation_id, receipt.run_id)
            for _ in range(100):
                if current_status.phase7.status == "FAILED":
                    break
                time.sleep(0.01)
                current_status = manager.status(confirmed.investigation_id, receipt.run_id)
            assert current_status.phase7.status == "FAILED"
            release.set()
        future = manager._jobs[request_id].future
        assert future is not None
        future.result(timeout=5)
        status = manager.status(confirmed.investigation_id, receipt.run_id)
        assert status.phase7.status == "FAILED"
        assert status.phase7.reason_code == "internal_error"
        persisted = execution.publisher.read(confirmed.investigation_id, receipt.run_id)
        assert persisted is not None
        assert persisted["status"] == "FAILED"
        assert persisted["plan_id"] is None
        assert not manager._jobs[request_id].ownership.held  # type: ignore[union-attr]
        retry = manager.start(confirmed.investigation_id, "2026-09-04T14:47:32", request_id)
        assert retry.run_id == receipt.run_id
        assert retry.status == "FAILED"
    finally:
        release.set()
        manager.close()


def test_successor_browser_surface_runs_through_uvicorn_http_and_reload(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """Exercise static UI delivery and Schema 8 background polling over real Uvicorn."""
    port = _free_port()
    environment = os.environ.copy()
    environment["VIGI_SUCCESSOR_BROWSER_ROOT"] = os.fspath(tmp_path)
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "test_recording_search_successor_execution:create_successor_browser_uvicorn_app",
        "--factory",
        "--app-dir",
        "tests",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    process = subprocess.Popen(  # noqa: S603 - fixed local test command.
        command,
        cwd=Path(__file__).parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        # The worker emits bounded structured lifecycle diagnostics.  This
        # integration test does not inspect the stream; discard it so a
        # platform pipe buffer cannot backpressure the application worker.
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError
            try:
                response = urlopen(f"{base_url}/", timeout=3)  # noqa: S310 - loopback test URL.
                if response.status == 200:
                    response.close()
                    break
            except Exception:  # noqa: BLE001 - bounded readiness probe.
                time.sleep(0.05)
        else:
            raise AssertionError
        page = urlopen(f"{base_url}/", timeout=3).read().decode("utf-8")  # noqa: S310
        assert "기본 30분, 최대 2시간" in page
        assert 'id="recording-search-quick-ranges"' in page
        script = urlopen(f"{base_url}/static/recording-search.js", timeout=3).read().decode("utf-8")  # noqa: S310
        assert "MAX_SEARCH_DURATION_SECONDS" in script
        confirmation_code, confirmation = _http_json(
            base_url,
            "/api/v1/investigation-confirmations/object-disappearance-v3-ch1-20260904T051732Z",
        )
        assert confirmation_code == 200
        assert confirmation["status"] == "confirmed"
        assert confirmation["confirmation"]["source_timezone"] == "Asia/Seoul"
        request_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        code, accepted = _http_json(
            base_url,
            "/api/v1/recording-searches",
            {
                "investigation_id": "object-disappearance-v3-ch1-20260904T051732Z",
                "search_end": "2026-09-04T14:47:32",
                "request_id": request_id,
            },
        )
        assert code == 202
        status_payload: dict[str, object] = {}
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            _, status_payload = _http_json(
                base_url,
                str(accepted["status_url"]),
                timeout=10,
            )
            if status_payload["status"] not in {"ACCEPTED", "RUNNING"}:
                break
            time.sleep(0.05)
        assert status_payload["status"] == "FOUND"
        assert status_payload["schema_version"] == 8
        _, restored = _http_json(base_url, str(accepted["status_url"]))
        assert restored == status_payload
    finally:
        if process.poll() is None:
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        _stdout, _stderr = process.communicate(timeout=1)
        assert process.returncode in ({0, 3} if os.name == "nt" else {0})
