"""Measure S4 candidate mechanics on preserved S3 replay rows.

The preserved corpus has requested sample times and classifier outcomes, but no
independent event-window labels.  This read-only report therefore measures
deterministic candidate formation sanity only; it does not claim recall,
accuracy, or production readiness and never contacts a live NVR/camera.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
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
    SuccessorSearchSample,
    form_disappearance_candidates,
)
from vigi_vision.recording_search_successor_search_evidence import (  # noqa: E402
    SearchEvidence,
    SearchEvidenceBand,
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


def _old_state_candidate(rows: list[dict[str, Any]]) -> bool:
    states = [row.get("classifier_result") for row in rows]
    return any(left == "PRESENT" and right == "ABSENT" for left, right in zip(states, states[1:]))


def _report(records: list[dict[str, Any]], policy: object) -> dict[str, object]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = (
            str(record.get("investigation_id")),
            str(record.get("run_id")),
            str(record.get("baseline_digest")),
        )
        groups[key].append(record)
    candidate_counts: list[int] = []
    widths: list[float] = []
    overflow_cases = 0
    recovery_cases = 0
    multiple_cases = 0
    old_candidates = 0
    useful_indeterminate = 0
    unresolved = 0
    s3_evaluations = 0
    segmentation_calls = 0
    alignment_comparisons = 0
    for rows in groups.values():
        rows.sort(key=lambda row: (str(row.get("requested_time_utc")), str(row.get("identity"))))
        samples: list[SuccessorSearchSample] = []
        for row in rows:
            timestamp = _timestamp(row.get("requested_time_utc"))
            if timestamp is None:
                continue
            evidence = _evidence(row)
            s3_evaluations += evidence is not None
            samples.append(
                SuccessorSearchSample(
                    str(row.get("observation_id") or row.get("identity")),
                    timestamp,
                    evidence,
                    str(row.get("classifier_result")),
                    timestamp,
                )
            )
            segmentation_calls += int(row.get("v3_segmentation_calls") or 0)
            alignment_comparisons += int(row.get("v3_alignment_comparisons") or 0)
        if not samples:
            continue
        old_candidates += _old_state_candidate(rows)
        result = form_disappearance_candidates(samples, seed_reference_time_utc=samples[0].frame_utc)
        count = len(result.candidates)
        candidate_counts.append(count)
        widths.extend(item.width_seconds for item in result.candidates)
        overflow_cases += result.overflowed
        recovery_cases += any(item.recovery_observation_id is not None for item in result.candidates)
        multiple_cases += count > 1
        unresolved += sum(item.provisional for item in result.candidates)
        useful_indeterminate += any(
            item.classifier_state == "INDETERMINATE"
            and item.evidence is not None
            and item.evidence.band is SearchEvidenceBand.MATERIAL_DROP
            for item in samples
        ) and count > 0
    return {
        "measurement_kind": "S4_candidate_formation_preserved_replay",
        "ground_truth": "not_available; independent S6 manual event windows required",
        "total_evaluated_search_groups": len(groups),
        "total_evaluated_observations": len(records),
        "old_state_only_candidate_groups": old_candidates,
        "new_s4_candidate_groups": sum(count > 0 for count in candidate_counts),
        "new_s4_candidate_count": sum(candidate_counts),
        "s4_useful_indeterminate_groups": useful_indeterminate,
        "interval_width_seconds": {
            "count": len(widths),
            "minimum": min(widths) if widths else None,
            "maximum": max(widths) if widths else None,
            "mean": sum(widths) / len(widths) if widths else None,
        },
        "recovery_cases": recovery_cases,
        "multiple_candidate_cases": multiple_cases,
        "overflow_cases": overflow_cases,
        "provisional_unresolved_candidates": unresolved,
        "s3_evidence_evaluations": s3_evaluations,
        "full_v3_segmentation_calls": segmentation_calls,
        "full_v3_alignment_comparisons": alignment_comparisons,
        "interpretation": (
            "Preserved replay supports deterministic distribution/sanity checks only; "
            "it cannot establish disappearance recall, precision, or event accuracy."
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
    sys.stdout.write(json.dumps(_report(shadow_records, policy), indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
