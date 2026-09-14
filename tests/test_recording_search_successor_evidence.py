from __future__ import annotations

# Compact fixture helpers intentionally use dynamic namespace objects.
# ruff: noqa: ANN001, ANN201, ANN202
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from vigi_vision.investigation_confirmation_models import ConfirmationRoi, RoiProvenance
from vigi_vision.recording_search_successor_acquisition import (
    SuccessorTargetStatus,
)
from vigi_vision.recording_search_successor_classification import (
    SuccessorObservation,
    SuccessorObservationState,
)
from vigi_vision.recording_search_successor_evidence import (
    SuccessorEvidenceError,
    SuccessorEvidenceRepository,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 12, 5, 0, tzinfo=UTC)


def _jpeg(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="JPEG")
    return output.getvalue()


def _fixture(tmp_path):
    baseline = _jpeg((40, 50, 60))
    probe = _jpeg((80, 90, 100))
    roi = ConfirmationRoi(
        x=4,
        y=3,
        width=12,
        height=10,
        coordinate_space="source_pixels",
        provenance=RoiProvenance.MANUAL,
    )
    authority = SimpleNamespace(
        authority_identity="successor-authority-v1-test",
        roi_identity="successor-roi-v1-test",
        reference_frame_resource_id="reference-frame-test",
        reference_frame_jpeg_sha256=hashlib.sha256(baseline).hexdigest(),
        reference_frame_jpeg_size_bytes=len(baseline),
        source_width=32,
        source_height=24,
        roi=roi,
    )
    request = SimpleNamespace(
        investigation_id="object-disappearance-v3-ch1-20260912T050000Z",
        run_id="search-run-" + "a" * 32,
    )
    prepared = SimpleNamespace(
        request=request,
        plan=SimpleNamespace(plan_id="successor-plan-v1-test"),
        authority=authority,
        baseline_time_utc=NOW,
        baseline_pts_seconds=0.0,
        baseline_jpeg_bytes=baseline,
    )
    digest = hashlib.sha256(probe).hexdigest()
    baseline_link = SuccessorObservation(
        "successor-plan-v1-test",
        "successor-baseline-v1-test",
        "successor-baseline-acquisition-v1-test",
        1,
        NOW,
        NOW,
        0.0,
        None,
        authority.authority_identity,
        "reference-frame-test",
        authority.roi_identity,
        "policy-test",
        SuccessorTargetStatus.FRAME_AVAILABLE,
        SuccessorObservationState.PRESENT,
        None,
        1,
        "successor-observation-v1-baseline-link",
        "measured_clip_relative",
        (),
        authority.reference_frame_jpeg_sha256,
    )
    observation = SuccessorObservation(
        "successor-plan-v1-test",
        "successor-target-v1-test",
        "successor-acquisition-v1-test",
        2,
        NOW,
        NOW,
        1.0,
        0.0,
        authority.authority_identity,
        "reference-frame-test",
        authority.roi_identity,
        "policy-test",
        SuccessorTargetStatus.FRAME_AVAILABLE,
        SuccessorObservationState.PRESENT,
        None,
        1,
        "successor-observation-v1-test",
        "measured_clip_relative",
        (),
        digest,
        probe,
        32,
        24,
        {"mask_iou": 1.0, "roi_luma_ncc": 1.0},
        "completed",
        10,
    )
    terminal = SimpleNamespace(
        status="NOT_FOUND",
        reason_code="complete_present_coverage",
        last_present_time_utc=None,
        first_absent_time_utc=None,
    )
    return prepared, (baseline_link, observation), terminal


def test_publish_reopens_identity_bound_full_and_roi_frames(tmp_path):
    prepared, observations, terminal = _fixture(tmp_path)
    repository = SuccessorEvidenceRepository(tmp_path / ".successor")

    manifest = repository.publish(prepared, observations, terminal)
    assert manifest["run_id"] == prepared.request.run_id
    entries = manifest["entries"]
    assert len(entries) == 3
    assert all(item["width"] == 32 and item["height"] == 24 for item in entries)
    assert entries[1]["digest"] == entries[0]["digest"]
    roi_digest = entries[2]["roi_digest"]
    assert isinstance(roi_digest, str)
    assert repository.read_frame(
        prepared.request.investigation_id, prepared.request.run_id, roi_digest
    )
    assert repository.read(prepared.request.investigation_id, prepared.request.run_id) == manifest


def test_digest_mismatch_fails_closed(tmp_path):
    prepared, observations, terminal = _fixture(tmp_path)
    observation = observations[-1]
    bad = SuccessorObservation(
        observation.plan_id,
        observation.target_id,
        observation.acquisition_id,
        observation.sequence,
        observation.requested_time_utc,
        observation.frame_utc,
        observation.frame_pts_seconds,
        observation.frame_offset_seconds,
        observation.authority_identity,
        observation.reference_frame_resource_id,
        observation.roi_identity,
        observation.classifier_policy_identity,
        observation.acquisition_status,
        observation.state,
        observation.reason_code,
        observation.ordinal,
        observation.observation_id,
        observation.timing_precision_status,
        observation.timing_warnings,
        "0" * 64,
        observation.frame_bytes,
        observation.frame_width,
        observation.frame_height,
    )
    with pytest.raises(SuccessorEvidenceError, match="evidence_digest_mismatch"):
        SuccessorEvidenceRepository(tmp_path / ".successor").publish(prepared, (bad,), terminal)


def test_missing_old_run_is_evidence_unavailable(tmp_path):
    repository = SuccessorEvidenceRepository(tmp_path / ".successor")
    assert (
        repository.read("object-disappearance-v3-ch1-20260912T050000Z", "search-run-" + "b" * 32)
        is None
    )


def test_baseline_support_comparison_reopens_with_all_metrics(tmp_path):
    prepared, observations, terminal = _fixture(tmp_path)
    comparison = {
        "baseline_mask_pixel_count": 20,
        "probe_mask_pixel_count": 360,
        "roi_pixel_count": 400,
        "mask_intersection_pixel_count": 20,
        "mask_union_pixel_count": 360,
        "baseline_mask_coverage": 0.05,
        "probe_mask_coverage": 0.9,
        "mask_iou": 0.055556,
        "effective_comparison_area": None,
        "roi_luma_ncc": 0.208525,
        "comparison_mode": "baseline_support_v1",
        "baseline_support_pixel_count": 20,
        "baseline_support_luma_similarity": 0.12,
        "baseline_support_luma_ncc": -0.2,
        "baseline_support_edge_similarity": 0.1,
        "baseline_support_change_ratio": 0.95,
        "baseline_support_foreground_retention": 0.05,
        "baseline_support_background_change_ratio": 0.0,
        "visual_status": "comparable",
        "unusable_reason": None,
    }
    observation = replace(observations[-1], comparison=comparison)
    repository = SuccessorEvidenceRepository(tmp_path / ".successor")
    manifest = repository.publish(prepared, (*observations[:-1], observation), terminal)
    assert manifest["entries"][-1]["comparison"] == comparison


def test_aligned_baseline_support_comparison_reopens_with_alignment_facts(tmp_path):
    prepared, observations, terminal = _fixture(tmp_path)
    comparison = {
        "baseline_mask_pixel_count": 20,
        "probe_mask_pixel_count": 360,
        "roi_pixel_count": 400,
        "mask_intersection_pixel_count": 20,
        "mask_union_pixel_count": 360,
        "baseline_mask_coverage": 0.05,
        "probe_mask_coverage": 0.9,
        "mask_iou": 0.055556,
        "effective_comparison_area": None,
        "roi_luma_ncc": 0.208525,
        "comparison_mode": "baseline_support_v3",
        "baseline_support_pixel_count": 20,
        "baseline_support_luma_similarity": 0.92,
        "baseline_support_luma_ncc": 0.86,
        "baseline_support_edge_similarity": 0.9,
        "baseline_support_change_ratio": 0.1,
        "baseline_support_foreground_retention": 0.95,
        "baseline_support_background_change_ratio": 0.0,
        "baseline_support_alignment_dx": 2,
        "baseline_support_alignment_dy": -1,
        "baseline_support_alignment_rotation_degrees": 5,
        "baseline_support_alignment_overlap": 0.95,
        "baseline_support_alignment_score": 0.87,
        "baseline_support_alignment_margin": 0.12,
        "baseline_support_present_gate_passed": False,
        "baseline_support_absent_gate_passed": True,
        "baseline_support_empty_background_evidence": True,
        "baseline_support_replacement_evidence": False,
        "baseline_support_occlusion_evidence": False,
        "baseline_support_decision_path": "absent",
        "baseline_support_decision_reason": "absent_empty_background",
        "visual_status": "comparable",
        "unusable_reason": None,
    }
    observation = replace(observations[-1], comparison=comparison)
    repository = SuccessorEvidenceRepository(tmp_path / ".successor")
    manifest = repository.publish(prepared, (*observations[:-1], observation), terminal)
    assert manifest["entries"][-1]["comparison"] == comparison
