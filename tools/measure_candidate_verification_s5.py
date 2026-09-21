"""Measure Phase S5 candidate-local verification on preserved replay rows.

The harness replays only the preserved successor artifacts already used by the
S2/S3/S4 tools.  It never contacts a camera/NVR, writes an artifact, or claims
event recall/precision without independent event-window labels.  v3, predictor,
alignment, and verification timings are reported separately so a future S6
study can compare candidate-local work with the prior coarse path.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from measure_presence_s2_2b import (  # noqa: E402
    DEFAULT_LIMIT,
    _discover_observations,
    _evaluate,
    _policy,
)
from measure_search_evidence_s3 import _record_shadow  # noqa: E402

from vigi_vision.recording_search_successor_candidate_search import (  # noqa: E402
    EvidenceNarrowingCompletion,
    EvidenceNarrowingPolicy,
    SuccessorSearchSample,
    form_disappearance_candidates,
    narrow_candidate_interval,
)
from vigi_vision.recording_search_successor_search_evidence import (  # noqa: E402
    SearchEvidence,
    SearchEvidenceBand,
)
from vigi_vision.recording_search_successor_verification import (  # noqa: E402
    verify_disappearance_candidates,
)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _evidence(record: dict[str, Any]) -> SearchEvidence | None:
    value = record.get("search_evidence")
    if not isinstance(value, str):
        return None
    try:
        band = SearchEvidenceBand(value)
    except ValueError:
        return None
    if band is SearchEvidenceBand.STRONG_REFERENCE:
        return SearchEvidence(band, "preserved_strong_reference", scene_stable=True)
    if band is SearchEvidenceBand.MATERIAL_DROP:
        return SearchEvidence(
            band,
            "preserved_material_drop",
            scene_stable=True,
            object_degradation=True,
        )
    if record.get("scene_only_suppressed"):
        return SearchEvidence(
            band,
            "preserved_scene_only_suppression",
            scene_stable=False,
            scene_discontinuity=True,
            scene_only_suppressed=True,
        )
    return SearchEvidence(band, "preserved_non_directional", scene_stable=None)


def _sample(record: dict[str, Any]) -> SuccessorSearchSample | None:
    timestamp = _timestamp(record.get("requested_time_utc"))
    if timestamp is None:
        return None
    return SuccessorSearchSample(
        str(record.get("observation_id") or record.get("identity")),
        timestamp,
        _evidence(record),
        str(record.get("classifier_result") or record.get("persisted_v3_outcome")),
        timestamp,
    )


def _sum_numeric(rows: list[dict[str, Any]], key: str) -> int:
    return sum(int(row.get(key) or 0) for row in rows if isinstance(row.get(key), (int, float)))


def _candidate_rows(
    rows: list[dict[str, Any]],
    candidate_ids: set[str],
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if str(row.get("observation_id") or row.get("identity")) in candidate_ids
    ]


def _report(records: list[dict[str, Any]]) -> dict[str, object]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[
            (
                str(record.get("investigation_id")),
                str(record.get("run_id")),
                str(record.get("baseline_digest")),
            )
        ].append(record)
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    candidate_count = 0
    candidate_groups = 0
    overflow_groups = 0
    verification_attempts = 0
    candidate_type_counts: Counter[str] = Counter()
    verification_durations: list[float] = []
    candidate_rows_all: list[dict[str, Any]] = []
    candidate_local_v3 = 0
    candidate_local_segmentation = 0
    candidate_local_predictor = 0
    candidate_local_alignment_invocations = 0
    candidate_local_alignment_comparisons = 0
    candidate_local_replayed_ms = 0.0
    for rows in groups.values():
        rows.sort(key=lambda row: (str(row.get("requested_time_utc")), str(row.get("identity"))))
        samples = tuple(sample for row in rows if (sample := _sample(row)) is not None)
        if not samples:
            continue
        formed = form_disappearance_candidates(
            samples,
            seed_reference_time_utc=samples[0].frame_utc,
            seed_reference_observation_id="confirmed_reference",
        )
        if not formed.candidates:
            continue
        candidate_groups += 1
        overflow_groups += formed.overflowed
        candidate_count += len(formed.candidates)
        if len(formed.candidates) > 1:
            candidate_type_counts["multiple"] += 1
        if formed.overflowed:
            candidate_type_counts["overflow"] += 1
        for candidate in formed.candidates:
            candidate_type_counts["qualified" if candidate.qualified else "provisional"] += 1
            if candidate.recovery_observation_id is not None:
                candidate_type_counts["recovery"] += 1
            if candidate.coverage_incomplete:
                candidate_type_counts["gap"] += 1
        nonmonotonic_ids: list[str] = []
        coverage_incomplete_ids: list[str] = []
        midpoint_samples: list[SuccessorSearchSample] = []

        # Replay the bounded S4 midpoint policy against preserved actual frames
        # so S5 can report gap/nonmonotonic widening without fabricating media.
        narrowed_samples: list[SuccessorSearchSample] = []
        for candidate in formed.candidates:
            def midpoint_sampler(requested_time_utc: datetime) -> SuccessorSearchSample | None:
                eligible = tuple(
                    item
                    for item in samples
                    if item.frame_utc is not None
                    and item.frame_utc > candidate.interval_start_utc
                    and item.frame_utc < candidate.interval_end_utc
                )
                if not eligible:
                    return SuccessorSearchSample(
                        f"s5-replay-gap-{requested_time_utc.isoformat()}",
                        None,
                        requested_time_utc=requested_time_utc,
                        available=False,
                        gap=True,
                    )
                return min(
                    eligible,
                    key=lambda item: abs((item.frame_utc - requested_time_utc).total_seconds()),
                )

            result = narrow_candidate_interval(
                candidate,
                midpoint_sampler,
                policy=EvidenceNarrowingPolicy(),
            )
            narrowed_samples.extend(result.midpoint_samples)
            if result.completion is EvidenceNarrowingCompletion.NONMONOTONIC:
                nonmonotonic_ids.append(result.candidate_id)
                candidate_type_counts["nonmonotonic"] += 1
            if result.coverage_incomplete:
                coverage_incomplete_ids.append(result.candidate_id)
        midpoint_samples.extend(narrowed_samples)
        report = verify_disappearance_candidates(
            formed.candidates,
            (*samples, *midpoint_samples),
            overflowed=formed.overflowed,
            overflow_count=formed.overflow_count,
            nonmonotonic_candidate_ids=nonmonotonic_ids,
            coverage_incomplete_candidate_ids=coverage_incomplete_ids,
        )
        verification_attempts += report.metrics.candidate_evaluations
        verification_durations.append(report.metrics.verification_duration_ms)
        for result in report.results:
            status_counts[result.status.value] += 1
            reason_counts[result.reason_code] += 1
            selected = _candidate_rows(rows, set(result.sample_ids))
            candidate_rows_all.extend(selected)
            candidate_local_v3 += sum(
                row.get("v3_comparison_outcome") is not None for row in selected
            )
            candidate_local_segmentation += _sum_numeric(selected, "v3_segmentation_calls")
            candidate_local_predictor += sum(
                bool(row.get("candidate_model_ran_for_v3_replay")) for row in selected
            )
            candidate_local_alignment_invocations += sum(
                bool(row.get("v3_alignment_ran")) for row in selected
            )
            candidate_local_alignment_comparisons += _sum_numeric(
                selected, "v3_alignment_comparisons"
            )
            candidate_local_replayed_ms += sum(
                float(row.get("v3_replay_ms") or 0) for row in selected
            )
    dedup_candidate_rows = {
        str(row.get("observation_id") or row.get("identity")): row
        for row in candidate_rows_all
    }
    actual_local_v3 = sum(row.get("v3_comparison_outcome") is not None for row in records)
    return {
        "measurement_kind": "S5_candidate_local_verification_preserved_replay",
        "ground_truth": "not_available; independent S6 manual event windows required",
        "total_evaluated_search_groups": len(groups),
        "total_evaluated_observations": len(records),
        "frame_timestamp_source": (
            "requested_time_utc fallback; preserved manifest rows do not carry decoded frame UTC"
        ),
        "candidate_groups": candidate_groups,
        "candidate_count": candidate_count,
        "verification_attempts": verification_attempts,
        "candidate_type_distribution": dict(sorted(candidate_type_counts.items())),
        "candidate_status_distribution": dict(sorted(status_counts.items())),
        "candidate_reason_distribution": dict(sorted(reason_counts.items())),
        "overflow_groups": overflow_groups,
        "actual_local_v3_invocations": actual_local_v3,
        "preserved_v3_state_rows": sum(
            row.get("persisted_v3_outcome")
            in {"PRESENT", "ABSENT", "INDETERMINATE"}
            for row in records
        ),
        "candidate_local_reused_v3_invocations": candidate_local_v3,
        "candidate_local_additional_v3_invocations": 0,
        "candidate_local_segmentation_calls": candidate_local_segmentation,
        "candidate_local_model_predictor_calls": candidate_local_predictor,
        "candidate_local_alignment_invocations": candidate_local_alignment_invocations,
        "candidate_local_alignment_comparisons": candidate_local_alignment_comparisons,
        "candidate_local_v3_replay_duration_ms": round(candidate_local_replayed_ms, 3),
        "candidate_local_verification_duration_ms": {
            "count": len(verification_durations),
            "total": round(sum(verification_durations), 3),
            "mean": round(sum(verification_durations) / len(verification_durations), 3)
            if verification_durations
            else None,
            "maximum": round(max(verification_durations), 3)
            if verification_durations
            else None,
        },
        "candidate_local_unique_observations": len(dedup_candidate_rows),
        "full_path_segmentation_calls": _sum_numeric(records, "v3_segmentation_calls"),
        "full_path_alignment_invocations": sum(bool(row.get("v3_alignment_ran")) for row in records),
        "full_path_alignment_comparisons": _sum_numeric(records, "v3_alignment_comparisons"),
        "interpretation": (
            "The verifier reuses existing precise observations and adds no v3 calls. "
            "Preserved replay measures bounded distributions and local work only; it "
            "cannot establish recall, precision, or event accuracy."
        ),
    }


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LIMIT
    if limit <= 0:
        raise SystemExit
    observations, manifest_count = _discover_observations()
    del manifest_count
    _, records = _evaluate(observations, len(observations), limit)
    policy = _policy()
    shadow_records = [_record_shadow(record, policy) | record for record in records]
    sys.stdout.write(json.dumps(_report(shadow_records), indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
