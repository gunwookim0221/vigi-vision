from __future__ import annotations

# Compact fixture helpers intentionally use dynamic namespace objects.
# ruff: noqa: ANN001, ANN201, ANN202
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from vigi_vision.investigation_confirmation_models import ConfirmationRoi, RoiProvenance
from vigi_vision.recording_search_successor_acquisition import (
    SuccessorTargetStatus,
)
from vigi_vision.recording_search_successor_candidate_search import (
    SuccessorCandidateInterval,
    SuccessorSearchSample,
    candidate_interval_identity,
    candidate_persistence_eligible,
    form_disappearance_candidates,
)
from vigi_vision.recording_search_successor_classification import (
    SuccessorObservation,
    SuccessorObservationState,
)
from vigi_vision.recording_search_successor_evidence import (
    LEGACY_EVIDENCE_VERSION,
    SuccessorEvidenceError,
    SuccessorEvidenceRepository,
)
from vigi_vision.recording_search_successor_search_evidence import (
    SearchEvidence,
    SearchEvidenceBand,
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


def _qualified_candidate_fixture(tmp_path):
    prepared, observations, _terminal = _fixture(tmp_path)
    material = SearchEvidence(
        SearchEvidenceBand.MATERIAL_DROP,
        "object_reference_support_drop",
        scene_stable=True,
        object_degradation=True,
    )
    first_time = NOW.replace(second=1)
    second_time = NOW.replace(second=2)
    first = replace(
        observations[-1],
        observation_id="successor-observation-v1-material-a",
        requested_time_utc=first_time,
        frame_utc=first_time,
        state=SuccessorObservationState.INDETERMINATE,
        reason_code="insufficient_visual_evidence",
        _search_evidence=material,
    )
    second = replace(
        observations[-1],
        observation_id="successor-observation-v1-material-b",
        requested_time_utc=second_time,
        frame_utc=second_time,
        state=SuccessorObservationState.INDETERMINATE,
        reason_code="insufficient_visual_evidence",
        _search_evidence=material,
    )
    candidate = SuccessorCandidateInterval(
        candidate_interval_identity("confirmed_reference", first.observation_id, NOW, first_time),
        "confirmed_reference",
        first.observation_id,
        NOW,
        first_time,
        qualified=True,
        provisional=False,
        supporting_observation_ids=(
            "confirmed_reference",
            first.observation_id,
            second.observation_id,
        ),
    )
    terminal = SimpleNamespace(
        status="INCONCLUSIVE",
        reason_code="indeterminate_observation",
        last_present_time_utc=None,
        first_absent_time_utc=None,
    )
    return prepared, (observations[0], first, second), terminal, candidate


def _manifest_path(root, prepared):
    return (
        root
        / prepared.request.investigation_id
        / prepared.request.run_id
        / "evidence"
        / "manifest.json"
    )


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


def test_found_bracket_ids_match_second_precision_terminal_times(tmp_path):
    prepared, observations, _terminal = _fixture(tmp_path)
    present = replace(
        observations[-1],
        requested_time_utc=NOW,
        frame_utc=NOW.replace(microsecond=987654),
        observation_id="successor-observation-v1-present",
    )
    absent = replace(
        observations[-1],
        requested_time_utc=NOW.replace(second=2),
        frame_utc=NOW.replace(second=2, microsecond=123456),
        observation_id="successor-observation-v1-absent",
        state=SuccessorObservationState.ABSENT,
    )
    terminal = SimpleNamespace(
        status="FOUND",
        reason_code="disappearance_confirmed",
        last_present_time_utc="2026-09-12T05:00:00Z",
        first_absent_time_utc="2026-09-12T05:00:02Z",
    )
    repository = SuccessorEvidenceRepository(tmp_path / ".successor")

    manifest = repository.publish(prepared, (observations[0], present, absent), terminal)

    assert manifest["last_present_observation_id"] == present.observation_id
    assert manifest["first_absent_observation_id"] == absent.observation_id


def test_found_bracket_id_stays_unavailable_for_ambiguous_second(tmp_path):
    prepared, observations, _terminal = _fixture(tmp_path)
    present = replace(
        observations[-1],
        requested_time_utc=NOW,
        frame_utc=NOW.replace(microsecond=100000),
        observation_id="successor-observation-v1-present-a",
    )
    duplicate = replace(
        present,
        frame_utc=NOW.replace(microsecond=900000),
        observation_id="successor-observation-v1-present-b",
    )
    absent = replace(
        observations[-1],
        requested_time_utc=NOW.replace(second=2),
        frame_utc=NOW.replace(second=2, microsecond=123456),
        observation_id="successor-observation-v1-absent",
        state=SuccessorObservationState.ABSENT,
    )
    terminal = SimpleNamespace(
        status="FOUND",
        reason_code="disappearance_confirmed",
        last_present_time_utc="2026-09-12T05:00:00Z",
        first_absent_time_utc="2026-09-12T05:00:02Z",
    )
    repository = SuccessorEvidenceRepository(tmp_path / ".successor")

    manifest = repository.publish(prepared, (observations[0], present, duplicate, absent), terminal)

    assert manifest["last_present_observation_id"] is None
    assert manifest["first_absent_observation_id"] == absent.observation_id


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


def test_qualified_candidate_round_trip_preserves_internal_identity_and_proof(tmp_path):
    prepared, observations, terminal, candidate = _qualified_candidate_fixture(tmp_path)
    root = tmp_path / ".successor"
    repository = SuccessorEvidenceRepository(root)

    manifest = repository.publish(prepared, observations, terminal, (candidate,))
    reopened = repository.read_candidates(
        prepared.request.investigation_id, prepared.request.run_id
    )

    assert reopened == (candidate,)
    assert (
        repository.read_candidates(prepared.request.investigation_id, prepared.request.run_id)
        == reopened
    )
    assert manifest["terminal_status"] == "INCONCLUSIVE"
    assert manifest["first_absent_observation_id"] is None
    assert all(item["state"] != "ABSENT" for item in manifest["entries"])
    candidate_state = manifest["candidate_state"]
    assert candidate_state is not None
    assert len(candidate_state["evidence_rows"]) == 2


def test_missing_candidate_state_reopens_as_no_candidate_for_legacy_manifest(tmp_path):
    prepared, observations, terminal = _fixture(tmp_path)
    root = tmp_path / ".successor"
    repository = SuccessorEvidenceRepository(root)
    manifest = repository.publish(prepared, observations, terminal)
    manifest.pop("candidate_state")
    manifest["version"] = LEGACY_EVIDENCE_VERSION
    _manifest_path(root, prepared).write_text(json.dumps(manifest), encoding="utf-8")

    assert (
        repository.read_candidates(prepared.request.investigation_id, prepared.request.run_id) == ()
    )


def test_candidate_state_with_unsupported_version_fails_closed(tmp_path):
    prepared, observations, terminal, candidate = _qualified_candidate_fixture(tmp_path)
    root = tmp_path / ".successor"
    repository = SuccessorEvidenceRepository(root)
    manifest = repository.publish(prepared, observations, terminal, (candidate,))
    manifest["candidate_state"]["version"] = "phase7e-successor-candidates-v999"
    _manifest_path(root, prepared).write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SuccessorEvidenceError, match="candidate_evidence_schema_unsupported"):
        repository.read_candidates(prepared.request.investigation_id, prepared.request.run_id)


def test_successor_evidence_with_unsupported_version_fails_closed(tmp_path):
    prepared, observations, terminal = _fixture(tmp_path)
    root = tmp_path / ".successor"
    repository = SuccessorEvidenceRepository(root)
    manifest = repository.publish(prepared, observations, terminal)
    manifest["version"] = "phase7e-successor-evidence-v999"
    _manifest_path(root, prepared).write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SuccessorEvidenceError, match="evidence_schema_unsupported"):
        repository.read_candidates(prepared.request.investigation_id, prepared.request.run_id)


def test_candidate_state_tampering_fails_digest_validation(tmp_path):
    prepared, observations, terminal, candidate = _qualified_candidate_fixture(tmp_path)
    root = tmp_path / ".successor"
    repository = SuccessorEvidenceRepository(root)
    manifest = repository.publish(prepared, observations, terminal, (candidate,))
    manifest["candidate_state"]["candidates"][0]["interval_end_utc"] = "2026-09-12T05:00:03.000000Z"
    _manifest_path(root, prepared).write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SuccessorEvidenceError, match="candidate_evidence_digest_mismatch"):
        repository.read_candidates(prepared.request.investigation_id, prepared.request.run_id)


def test_candidate_evidence_row_mismatch_fails_even_with_recomputed_digest(tmp_path):
    prepared, observations, terminal, candidate = _qualified_candidate_fixture(tmp_path)
    root = tmp_path / ".successor"
    repository = SuccessorEvidenceRepository(root)
    manifest = repository.publish(prepared, observations, terminal, (candidate,))
    state = manifest["candidate_state"]
    state["evidence_rows"][0]["frame_utc"] = "2026-09-12T05:00:09.000000Z"
    unsigned = {key: value for key, value in state.items() if key != "digest"}
    state["digest"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _manifest_path(root, prepared).write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SuccessorEvidenceError, match="candidate_evidence_corrupt"):
        repository.read_candidates(prepared.request.investigation_id, prepared.request.run_id)


def test_candidate_source_entry_tampering_fails_source_digest_validation(tmp_path):
    prepared, observations, terminal, candidate = _qualified_candidate_fixture(tmp_path)
    root = tmp_path / ".successor"
    repository = SuccessorEvidenceRepository(root)
    manifest = repository.publish(prepared, observations, terminal, (candidate,))
    candidate_entry = next(
        item
        for item in manifest["entries"]
        if item["observation_id"] == candidate.drop_observation_id
    )
    candidate_entry["classifier_elapsed_ms"] = 11
    _manifest_path(root, prepared).write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SuccessorEvidenceError, match="candidate_evidence_digest_mismatch"):
        repository.read_candidates(prepared.request.investigation_id, prepared.request.run_id)


def test_malformed_candidate_support_fails_before_publication(tmp_path):
    prepared, observations, terminal, candidate = _qualified_candidate_fixture(tmp_path)
    malformed = replace(
        candidate,
        supporting_observation_ids=("confirmed_reference", candidate.drop_observation_id),
    )

    with pytest.raises(SuccessorEvidenceError, match="candidate_evidence_corrupt"):
        SuccessorEvidenceRepository(tmp_path / ".successor").publish(
            prepared, observations, terminal, (malformed,)
        )

    assert not _manifest_path(tmp_path / ".successor", prepared).exists()


def test_provisional_candidate_is_not_publishable_as_durable_candidate(tmp_path):
    prepared, observations, terminal, candidate = _qualified_candidate_fixture(tmp_path)
    provisional = replace(candidate, qualified=False, provisional=True)

    with pytest.raises(SuccessorEvidenceError, match="candidate_evidence_corrupt"):
        SuccessorEvidenceRepository(tmp_path / ".successor").publish(
            prepared, observations, terminal, (provisional,)
        )


def test_search_gap_then_repeated_material_drop_is_not_persistable(tmp_path):
    prepared, observations, terminal, candidate = _qualified_candidate_fixture(tmp_path)
    material = observations[1]._search_evidence
    assert material is not None
    samples = (
        SuccessorSearchSample(
            observations[1].observation_id,
            observations[1].frame_utc,
            material,
            "INDETERMINATE",
            observations[1].requested_time_utc,
            available=True,
            gap=False,
        ),
        SuccessorSearchSample(
            "successor-observation-v1-coverage-gap",
            None,
            None,
            "REPLAY_TIMEOUT",
            NOW + timedelta(milliseconds=1500),
            available=False,
            gap=True,
        ),
        SuccessorSearchSample(
            observations[2].observation_id,
            observations[2].frame_utc,
            observations[2]._search_evidence,
            "INDETERMINATE",
            observations[2].requested_time_utc,
            available=True,
            gap=False,
        ),
    )

    formed = form_disappearance_candidates(
        samples,
        seed_reference_time_utc=NOW,
        seed_reference_observation_id="confirmed_reference",
    )

    assert len(formed.qualified_candidates) == 1
    incomplete = formed.qualified_candidates[0]
    assert incomplete.qualified is True
    assert incomplete.coverage_incomplete is True
    assert candidate_persistence_eligible(incomplete) is False
    assert incomplete.candidate_id == candidate.candidate_id
    repository = SuccessorEvidenceRepository(tmp_path / ".successor")
    with pytest.raises(SuccessorEvidenceError, match="candidate_evidence_corrupt"):
        repository.publish(prepared, observations, terminal, (incomplete,))
    assert (
        repository.read_candidates(prepared.request.investigation_id, prepared.request.run_id) == ()
    )
    assert not _manifest_path(tmp_path / ".successor", prepared).exists()


def test_candidate_with_interval_gap_observation_is_not_publishable(tmp_path):
    prepared, observations, terminal, candidate = _qualified_candidate_fixture(tmp_path)
    gap = replace(
        observations[-1],
        observation_id="successor-observation-v1-coverage-gap-entry",
        target_id="successor-target-v1-coverage-gap-entry",
        acquisition_id="successor-acquisition-v1-coverage-gap-entry",
        sequence=3,
        ordinal=3,
        requested_time_utc=NOW + timedelta(milliseconds=500),
        frame_utc=None,
        frame_pts_seconds=None,
        frame_offset_seconds=None,
        acquisition_status=SuccessorTargetStatus.REPLAY_TIMEOUT,
        state=SuccessorObservationState.REPLAY_TIMEOUT,
        reason_code="target_replay_timeout",
        frame_sha256=None,
        frame_bytes=None,
        frame_width=None,
        frame_height=None,
        comparison=None,
        classifier_stage=None,
        classifier_elapsed_ms=None,
        _search_evidence=None,
    )
    repository = SuccessorEvidenceRepository(tmp_path / ".successor")

    with pytest.raises(SuccessorEvidenceError, match="candidate_evidence_corrupt"):
        repository.publish(prepared, (*observations, gap), terminal, (candidate,))

    assert (
        repository.read_candidates(prepared.request.investigation_id, prepared.request.run_id) == ()
    )
    assert not _manifest_path(tmp_path / ".successor", prepared).exists()

    valid_repository = SuccessorEvidenceRepository(tmp_path / "valid")
    valid_manifest = valid_repository.publish(prepared, observations, terminal, (candidate,))
    gap_repository = SuccessorEvidenceRepository(tmp_path / "gap")
    gap_manifest = gap_repository.publish(prepared, (*observations, gap), terminal)
    gap_manifest["candidate_state"] = valid_manifest["candidate_state"]
    _manifest_path(tmp_path / "gap", prepared).write_text(
        json.dumps(gap_manifest), encoding="utf-8"
    )

    with pytest.raises(SuccessorEvidenceError, match="candidate_evidence_corrupt"):
        gap_repository.read_candidates(prepared.request.investigation_id, prepared.request.run_id)


def test_strict_reopen_rejects_incomplete_candidate_with_valid_candidate_digest(tmp_path):
    prepared, observations, terminal, candidate = _qualified_candidate_fixture(tmp_path)
    root = tmp_path / ".successor"
    repository = SuccessorEvidenceRepository(root)
    manifest = repository.publish(prepared, observations, terminal, (candidate,))
    candidate_record = manifest["candidate_state"]["candidates"][0]
    candidate_record["coverage_incomplete"] = True
    state = manifest["candidate_state"]
    unsigned = {key: value for key, value in state.items() if key != "digest"}
    state["digest"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _manifest_path(root, prepared).write_text(json.dumps(manifest), encoding="utf-8")

    for _ in range(2):
        with pytest.raises(SuccessorEvidenceError, match="candidate_evidence_corrupt"):
            repository.read_candidates(prepared.request.investigation_id, prepared.request.run_id)


@pytest.mark.parametrize("reason", ["camera_motion", "occlusion", "replacement"])
def test_scene_or_confounder_only_evidence_does_not_form_a_persistable_candidate(reason):
    strong = SearchEvidence(
        SearchEvidenceBand.STRONG_REFERENCE,
        "reference_support_retained",
        scene_stable=True,
    )
    ambiguous = SearchEvidence(
        SearchEvidenceBand.USABLE_AMBIGUOUS,
        f"{reason}_suppressed_direction",
        scene_stable=False,
        scene_discontinuity=True,
        scene_only_suppressed=True,
    )
    material = SearchEvidence(
        SearchEvidenceBand.MATERIAL_DROP,
        "object_reference_support_drop",
        scene_stable=True,
        object_degradation=True,
    )
    samples = tuple(
        SuccessorSearchSample(
            f"successor-observation-v1-{reason}-{index}",
            NOW + timedelta(seconds=index),
            evidence,
            "PRESENT" if evidence.band is SearchEvidenceBand.STRONG_REFERENCE else "INDETERMINATE",
            NOW + timedelta(seconds=index),
            available=True,
            gap=False,
        )
        for index, evidence in enumerate((strong, ambiguous, material))
    )

    formed = form_disappearance_candidates(samples)

    assert not formed.qualified_candidates
    assert all(not candidate_persistence_eligible(item) for item in formed.candidates)


def test_multiple_candidates_publish_and_reopen_in_deterministic_order(tmp_path):
    prepared, observations, terminal, first_candidate = _qualified_candidate_fixture(tmp_path)
    template = observations[-1]
    strong_time = NOW.replace(second=3)
    third_time = NOW.replace(second=4)
    fourth_time = NOW.replace(second=5)
    strong = replace(
        template,
        observation_id="successor-observation-v1-strong-b",
        requested_time_utc=strong_time,
        frame_utc=strong_time,
        state=SuccessorObservationState.PRESENT,
        reason_code=None,
        _search_evidence=SearchEvidence(
            SearchEvidenceBand.STRONG_REFERENCE,
            "reference_support_retained",
            scene_stable=True,
        ),
    )
    material = SearchEvidence(
        SearchEvidenceBand.MATERIAL_DROP,
        "object_reference_support_drop",
        scene_stable=True,
        object_degradation=True,
    )
    third = replace(
        template,
        observation_id="successor-observation-v1-material-c",
        requested_time_utc=third_time,
        frame_utc=third_time,
        _search_evidence=material,
    )
    fourth = replace(
        template,
        observation_id="successor-observation-v1-material-d",
        requested_time_utc=fourth_time,
        frame_utc=fourth_time,
        _search_evidence=material,
    )
    first_candidate = replace(
        first_candidate,
        recovery_observation_id=strong.observation_id,
    )
    second_candidate = SuccessorCandidateInterval(
        candidate_interval_identity(
            strong.observation_id,
            third.observation_id,
            strong_time,
            third_time,
        ),
        strong.observation_id,
        third.observation_id,
        strong_time,
        third_time,
        qualified=True,
        provisional=False,
        supporting_observation_ids=(
            strong.observation_id,
            third.observation_id,
            fourth.observation_id,
        ),
    )
    repository = SuccessorEvidenceRepository(tmp_path / ".successor")

    repository.publish(
        prepared,
        (*observations, strong, third, fourth),
        terminal,
        (second_candidate, first_candidate),
    )

    assert repository.read_candidates(
        prepared.request.investigation_id, prepared.request.run_id
    ) == (first_candidate, second_candidate)


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


_ALIGNMENT_KEYS = (
    "baseline_support_alignment_dx",
    "baseline_support_alignment_dy",
    "baseline_support_alignment_rotation_degrees",
    "baseline_support_alignment_overlap",
    "baseline_support_alignment_score",
    "baseline_support_alignment_margin",
)


def _v3_comparison(alignment_state, alignment_values):
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
        "baseline_support_alignment_state": alignment_state,
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
    comparison.update(dict(zip(_ALIGNMENT_KEYS, alignment_values, strict=True)))
    return comparison


def _publish_v3(tmp_path, comparison):
    prepared, observations, terminal = _fixture(tmp_path)
    observation = replace(observations[-1], comparison=comparison)
    repository = SuccessorEvidenceRepository(tmp_path / ".successor")
    return repository.publish(prepared, (*observations[:-1], observation), terminal)


@pytest.mark.parametrize("alignment_state", ["not_required", "no_valid_candidate"])
def test_v3_optional_alignment_state_with_all_null_facts_reopens(tmp_path, alignment_state):
    comparison = _v3_comparison(alignment_state, [None] * len(_ALIGNMENT_KEYS))

    manifest = _publish_v3(tmp_path, comparison)
    repository = SuccessorEvidenceRepository(tmp_path / ".successor")

    assert (
        repository.read("object-disappearance-v3-ch1-20260912T050000Z", "search-run-" + "a" * 32)
        == manifest
    )


@pytest.mark.parametrize("alignment_state", ["not_required", "no_valid_candidate"])
def test_v3_optional_alignment_state_with_partial_null_facts_is_rejected(tmp_path, alignment_state):
    comparison = _v3_comparison(alignment_state, [2, -1, 5, 0.95, 0.87, None])

    with pytest.raises(SuccessorEvidenceError, match="evidence_corrupt"):
        _publish_v3(tmp_path, comparison)


@pytest.mark.parametrize("alignment_state", ["aligned", "ambiguous"])
def test_v3_numeric_alignment_states_reopen_with_numeric_facts(tmp_path, alignment_state):
    comparison = _v3_comparison(alignment_state, [2, -1, 5, 0.95, 0.87, 0.12])

    manifest = _publish_v3(tmp_path, comparison)
    repository = SuccessorEvidenceRepository(tmp_path / ".successor")

    assert (
        repository.read("object-disappearance-v3-ch1-20260912T050000Z", "search-run-" + "a" * 32)
        == manifest
    )


@pytest.mark.parametrize("alignment_state", ["aligned", "ambiguous"])
def test_v3_numeric_alignment_states_with_null_fact_is_rejected(tmp_path, alignment_state):
    comparison = _v3_comparison(alignment_state, [2, -1, 5, 0.95, 0.87, None])

    with pytest.raises(SuccessorEvidenceError, match="evidence_corrupt"):
        _publish_v3(tmp_path, comparison)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("baseline_support_alignment_dx", 33),
        ("baseline_support_alignment_overlap", 0.0),
        ("baseline_support_alignment_score", float("nan")),
        ("baseline_support_alignment_margin", float("inf")),
    ],
)
def test_v3_alignment_nan_infinity_and_out_of_range_facts_are_rejected(tmp_path, key, value):
    comparison = _v3_comparison("aligned", [2, -1, 5, 0.95, 0.87, 0.12])
    comparison[key] = value

    with pytest.raises(SuccessorEvidenceError, match="evidence_corrupt"):
        _publish_v3(tmp_path, comparison)


def test_v3_unknown_alignment_state_is_rejected(tmp_path):
    comparison = _v3_comparison("unknown", [2, -1, 5, 0.95, 0.87, 0.12])

    with pytest.raises(SuccessorEvidenceError, match="evidence_corrupt"):
        _publish_v3(tmp_path, comparison)


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
