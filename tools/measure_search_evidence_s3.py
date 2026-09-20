"""Measure the S3 search-evidence signal on preserved successor artifacts.

This read-only harness reuses the S2-2B preserved replay evaluator and applies
the pure S3 evidence function to current fast-PRESENT comparisons or preserved
v3 comparison rows.  It does not contact an NVR, write artifacts, or claim
event recall without independent human labels.
"""

# This tool intentionally consumes the existing preserved-data harness and
# keeps artifact fields dynamic; it is excluded from the repository lint scope.

from __future__ import annotations

import json
import sys
from collections import Counter
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

from vigi_vision.object_presence_evidence import RawComparison  # noqa: E402
from vigi_vision.object_presence_values import VisualReason, VisualStatus  # noqa: E402
from vigi_vision.recording_search_successor_search_evidence import (  # noqa: E402
    SearchEvidence,
    SearchEvidenceBand,
    evaluate_search_evidence,
)


def _comparison(payload: object) -> RawComparison | None:
    if not isinstance(payload, dict):
        return None
    try:
        normalized = dict(payload)
        if isinstance(normalized.get("visual_status"), str):
            normalized["visual_status"] = VisualStatus(normalized["visual_status"])
        if isinstance(normalized.get("unusable_reason"), str):
            normalized["unusable_reason"] = VisualReason(normalized["unusable_reason"])
        return RawComparison.model_validate(normalized)
    except (TypeError, ValueError):
        return None


def _record_shadow(record: dict[str, Any], policy: object) -> dict[str, Any]:
    fast_hit = bool(record.get("fast_present"))
    if fast_hit:
        comparison = _comparison(record.get("fast_comparison"))
        comparison_source = "s2_current_fast_comparison"
        classifier_result = record.get("v3_comparison_outcome") or record.get(
            "persisted_v3_outcome"
        )
        classifier_source = (
            "current_v3_replay" if record.get("v3_comparison_outcome") else "preserved_v3_state"
        )
    else:
        comparison = _comparison(record.get("persisted_v3_comparison"))
        comparison_source = "preserved_v3_comparison" if comparison is not None else "none"
        classifier_result = record.get("persisted_v3_outcome")
        classifier_source = "preserved_v3_state"
    evidence: SearchEvidence | None
    if comparison is None:
        evidence = None
    else:
        evidence = evaluate_search_evidence(
            comparison,
            policy,
            fast_present_hit=fast_hit,
        )
    return {
        "identity": record.get("identity"),
        "persisted_classifier_result": record.get("persisted_v3_outcome"),
        "classifier_result": classifier_result,
        "classifier_result_source": classifier_source,
        "comparison_source": comparison_source,
        "fast_present_hit": fast_hit,
        "search_evidence": None if evidence is None else evidence.band.value,
        "search_evidence_reason": None if evidence is None else evidence.reason_code,
        "scene_discontinuity": False if evidence is None else evidence.scene_discontinuity,
        "scene_only_suppressed": False
        if evidence is None
        else evidence.scene_only_suppressed,
        "object_degradation": False if evidence is None else evidence.object_degradation,
    }


def _summary(records: list[dict[str, Any]], shadow_records: list[dict[str, Any]]) -> dict[str, Any]:
    bands = Counter(
        str(record["search_evidence"])
        for record in shadow_records
        if record["search_evidence"] is not None
    )
    outcomes = Counter(
        str(record["classifier_result"])
        for record in shadow_records
        if record["classifier_result"] in {"PRESENT", "ABSENT", "INDETERMINATE"}
    )
    cross_tab = Counter(
        f"{record['classifier_result']}/{record['search_evidence']}"
        for record in shadow_records
        if record["search_evidence"] is not None
        and record["classifier_result"] in {"PRESENT", "ABSENT", "INDETERMINATE"}
    )
    material_indeterminate = [
        record
        for record in shadow_records
        if record["search_evidence"] == SearchEvidenceBand.MATERIAL_DROP.value
        and record["classifier_result"] == "INDETERMINATE"
    ]
    suspicious = [
        record
        for record in shadow_records
        if record["search_evidence"] == SearchEvidenceBand.MATERIAL_DROP.value
        and (record["fast_present_hit"] or record["scene_discontinuity"])
    ]
    return {
        "measurement_kind": "S3_shadow_preserved_replay",
        "ground_truth": "not_available; S6 independent manual event-window labels required",
        "total_observations": len(records),
        "shadow_evaluations": sum(
            record["search_evidence"] is not None for record in shadow_records
        ),
        "shadow_without_comparison": sum(
            record["search_evidence"] is None for record in shadow_records
        ),
        "classifier_result_distribution": dict(sorted(outcomes.items())),
        "search_evidence_distribution": dict(sorted(bands.items())),
        "classifier_result_x_search_evidence": dict(sorted(cross_tab.items())),
        "material_drop_indeterminate": len(material_indeterminate),
        "fast_present_strong_reference_overlap": sum(
            record["fast_present_hit"]
            and record["search_evidence"] == SearchEvidenceBand.STRONG_REFERENCE.value
            for record in shadow_records
        ),
        "scene_only_changes_suppressed": sum(
            record["scene_only_suppressed"] for record in shadow_records
        ),
        "suspicious_material_drop_count": len(suspicious),
        "suspicious_material_drop_cases": suspicious[:20],
        "ambiguous_or_insufficient_cases": sum(
            record["search_evidence"]
            in {
                SearchEvidenceBand.USABLE_AMBIGUOUS.value,
                SearchEvidenceBand.INSUFFICIENT.value,
            }
            for record in shadow_records
        ),
        "interpretation": (
            "Preserved replay measures deterministic shadow distribution only; "
            "it is not disappearance recall or accuracy ground truth."
        ),
    }


def main() -> None:
    """Print a bounded shadow-evidence report for preserved observations."""
    limit = DEFAULT_LIMIT
    if len(sys.argv) > 1:
        limit = int(sys.argv[1])
    if limit <= 0:
        raise SystemExit
    observations, manifest_count = _discover_observations()
    summary, records = _evaluate(observations, manifest_count, limit)
    del summary
    policy = _policy()
    shadow_records = [_record_shadow(record, policy) for record in records]
    payload = {
        "summary": _summary(records, shadow_records),
        "source_manifest_count": manifest_count,
        "records": shadow_records,
    }
    output = payload["summary"] if "--summary" in sys.argv[1:] else payload
    sys.stdout.write(json.dumps(output, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
