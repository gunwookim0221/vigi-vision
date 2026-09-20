"""Measure the committed S2-1 PRESENT-only cascade on deterministic fixtures.

This is an offline evaluation harness. It intentionally runs the existing B3/v3
classifier beside the S2-1 gate for the same decoded synthetic observations;
it does not alter production policy, thresholds, or persisted evidence.
"""

# The fixture module is deliberately reused instead of copying its image
# construction into a second test-only implementation.
# ruff: noqa: D101, D102, D103, E501

from __future__ import annotations

import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_presence_first_s2 import (  # noqa: E402
    HEIGHT,
    ROI,
    WIDTH,
    _image,
    _mask,
    _policy,
)
from vigi_vision.object_presence_comparator import (  # noqa: E402
    fast_present_comparison,
    prepare_fast_presence_reference,
)
from vigi_vision.object_presence_models import BinaryMask, DecodedRgbImage  # noqa: E402
from vigi_vision.recording_search_b3_service import classify_decoded_images  # noqa: E402

REPEATS = 9
POLICY = _policy()
BASELINE = _image()
BASELINE_MASK = _mask()


@dataclass(frozen=True, slots=True)
class ObservationCase:
    name: str
    group: str
    image: DecodedRgbImage
    probe_mask: BinaryMask
    reference_mask: BinaryMask | None = BASELINE_MASK


class CountingPredictor:
    """Deterministic two-call predictor proxy for the existing B3 boundary."""

    def __init__(self, baseline_mask: BinaryMask, probe_mask: BinaryMask) -> None:  # noqa: D107
        self.baseline_mask = baseline_mask
        self.probe_mask = probe_mask
        self.calls = 0

    def predict_from_rgb(self, _image: object, _point: object, _size: object) -> BinaryMask:
        self.calls += 1
        return self.baseline_mask if self.calls == 1 else self.probe_mask


def _all_true_mask() -> BinaryMask:
    return BinaryMask.from_rows(tuple(tuple(True for _ in range(WIDTH)) for _ in range(HEIGHT)))


def _occluded_image() -> DecodedRgbImage:
    return _image(occluded=True)


def _replacement_image() -> DecodedRgbImage:
    return _image(replacement=True)


def _translated_image() -> DecodedRgbImage:
    return _image(dx=2, dy=1)


def _removed_image() -> DecodedRgbImage:
    rows = [list(row) for row in BASELINE.pixels]
    for y in range(12, 20):
        for x in range(12, 20):
            value = 170 + ((x * 3 + y * 5) % 9)
            rows[y][x] = (value, value, value)
    return DecodedRgbImage.from_rows(tuple(tuple(row) for row in rows))


def _cases() -> tuple[ObservationCase, ...]:
    return (
        ObservationCase("unchanged", "stable_present", BASELINE, BASELINE_MASK),
        ObservationCase("lighting_variation", "exposure_quality", _image(scene_shift=24), BASELINE_MASK),
        ObservationCase(
            "local_background_patch",
            "exposure_quality",
            _image(scene_shift=3, scene_patch=True),
            BASELINE_MASK,
        ),
        ObservationCase(
            "removed_with_reveal",
            "absent",
            _removed_image(),
            BASELINE_MASK,
        ),
        ObservationCase("partial_occlusion", "occlusion", _occluded_image(), BASELINE_MASK),
        ObservationCase("replacement_like", "replacement", _replacement_image(), BASELINE_MASK),
        ObservationCase("camera_translation", "scene_instability", _translated_image(), BASELINE_MASK),
        ObservationCase(
            "scene_patch",
            "scene_instability",
            _image(scene_patch=True),
            BASELINE_MASK,
        ),
        ObservationCase(
            "missing_reference",
            "missing_prerequisite",
            BASELINE,
            BASELINE_MASK,
            reference_mask=None,
        ),
        ObservationCase(
            "invalid_reference_coverage",
            "missing_prerequisite",
            BASELINE,
            BASELINE_MASK,
            reference_mask=_all_true_mask(),
        ),
    )


def _slow_once(case: ObservationCase) -> tuple[str, float, dict[str, int], int]:
    predictor = CountingPredictor(BASELINE_MASK, case.probe_mask)
    diagnostics: dict[str, int] = {}
    started = perf_counter_ns()
    result = classify_decoded_images(
        baseline_image=BASELINE,
        probe_image=case.image,
        source_width=WIDTH,
        source_height=HEIGHT,
        roi=ROI,
        policy=POLICY,
        mask_predictor=predictor,
        diagnostics_sink=lambda name, value: diagnostics.__setitem__(name, value),  # noqa: PLW0108
    )
    elapsed_ms = round((perf_counter_ns() - started) / 1_000_000, 3)
    return result.outcome.value, elapsed_ms, diagnostics, predictor.calls


def _fast_once(case: ObservationCase, reference: object) -> tuple[bool, float]:
    if reference is None:
        return False, 0
    started = perf_counter_ns()
    result = fast_present_comparison(reference, case.image, ROI, POLICY)
    elapsed_ms = round((perf_counter_ns() - started) / 1_000_000, 3)
    return result is not None, elapsed_ms


def _timed_case(case: ObservationCase) -> dict[str, object]:
    reference = (
        None
        if case.reference_mask is None
        else prepare_fast_presence_reference(BASELINE, case.reference_mask, ROI, POLICY)
    )
    fast_hit, _ = _fast_once(case, reference)
    fast_timings = [_fast_once(case, reference)[1] for _ in range(REPEATS)] if reference else []
    slow_outcome, _, diagnostics, slow_calls = _slow_once(case)
    slow_timings = [_slow_once(case)[1] for _ in range(REPEATS)]
    alignment_comparisons = diagnostics.get("alignment_comparisons", 0)
    return {
        "name": case.name,
        "group": case.group,
        "fast_gate_ran": reference is not None,
        "fast_present": fast_hit,
        "slow_outcome": slow_outcome,
        "agreement": (not fast_hit) or slow_outcome == "PRESENT",
        "segmentation_calls_slow": slow_calls,
        "alignment_comparisons_slow": alignment_comparisons,
        "fast_ms": fast_timings,
        "slow_ms": slow_timings,
    }


def _summary(records: list[dict[str, object]]) -> dict[str, object]:
    total = len(records)
    evaluated = sum(bool(record["fast_gate_ran"]) for record in records)
    hits = sum(bool(record["fast_present"]) for record in records)
    delegated = total - hits
    fast_times = [value for record in records for value in record["fast_ms"]]
    delegated_times = [
        value
        for record in records
        if not record["fast_present"]
        for value in record["slow_ms"]
    ]
    fast_hit_records = [record for record in records if record["fast_present"]]
    return {
        "total_observations": total,
        "fast_path_evaluations": evaluated,
        "fast_present_hits": hits,
        "delegated_cases": delegated,
        "fast_present_hit_rate_of_evaluated": round(hits / evaluated, 6) if evaluated else None,
        "delegation_rate_of_total": round(delegated / total, 6) if total else None,
        "candidate_segmentation_calls_avoided": sum(
            1 for record in fast_hit_records
        ),
        "full_classifier_mask_calls_avoided": sum(
            record["segmentation_calls_slow"] for record in fast_hit_records
        ),
        "alignment_comparisons_avoided": sum(
            record["alignment_comparisons_slow"] for record in fast_hit_records
        ),
        "fast_ms_mean": round(statistics.mean(fast_times), 3) if fast_times else None,
        "fast_ms_median": round(statistics.median(fast_times), 3) if fast_times else None,
        "delegated_slow_ms_mean": (
            round(statistics.mean(delegated_times), 3) if delegated_times else None
        ),
        "delegated_slow_ms_median": (
            round(statistics.median(delegated_times), 3) if delegated_times else None
        ),
        "fast_present_agreements": sum(bool(record["agreement"]) for record in fast_hit_records),
        "fast_present_disagreements": sum(
            not bool(record["agreement"]) for record in fast_hit_records
        ),
        "false_present_cases": [
            {"name": record["name"], "slow_outcome": record["slow_outcome"]}
            for record in fast_hit_records
            if not record["agreement"]
        ],
        "prerequisite_prevented_cases": [
            record["name"] for record in records if not record["fast_gate_ran"]
        ],
    }


def main() -> None:
    records = [_timed_case(case) for case in _cases()]
    payload = {"summary": _summary(records), "records": records}
    if "--records" in sys.argv[1:]:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(json.dumps(payload["summary"], indent=2) + "\n")


if __name__ == "__main__":
    main()
