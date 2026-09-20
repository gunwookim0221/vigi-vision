"""Evaluate the committed S2-1 PRESENT gate on preserved successor artifacts.

The tool is deliberately read-only. It loads preserved JPEGs and manifests,
reconstructs the already-approved EfficientSAM baseline support mask in memory,
and never writes to the evidence tree. Existing persisted v3 outcomes are used
for delegated observations; the full pure v3 comparator is replayed only when a
real fast PRESENT hit requires the critical safety comparison.
"""

# This evaluation utility intentionally keeps the artifact schema dynamic.
# ruff: noqa: D101, D102, D103, E501

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SUCCESSOR_ROOT = ROOT / "artifacts" / "investigation-searches" / ".successor"
CHECKPOINT = Path(r"D:\models\efficient_sam_vitt.pt")
CHECKPOINT_SHA256 = "dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a"
DEFAULT_LIMIT = 50

from PIL import Image  # noqa: E402

from vigi_vision.assisted_roi_geometry import ImageSize, MaskPreview, Point  # noqa: E402
from vigi_vision.assisted_roi_predictor import LazyEfficientSamPredictor  # noqa: E402
from vigi_vision.investigation_confirmation_models import (  # noqa: E402
    ConfirmationRoi,
    RoiProvenance,
)
from vigi_vision.object_presence_comparator import (  # noqa: E402
    FastPresenceReference,
    fast_present_comparison,
    prepare_fast_presence_reference,
)
from vigi_vision.object_presence_models import BinaryMask, DecodedRgbImage  # noqa: E402
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy  # noqa: E402
from vigi_vision.recording_search_b3_service import classify_decoded_images  # noqa: E402


@dataclass(frozen=True, slots=True)
class ArtifactObservation:
    manifest_path: Path
    investigation_id: str
    run_id: str
    reference_resource_id: str
    baseline_digest: str
    candidate_digest: str
    baseline_path: Path
    candidate_path: Path
    source_width: int
    source_height: int
    roi: ConfirmationRoi
    persisted_v3_outcome: str
    persisted_reason: str | None
    persisted_classifier_ms: int | None
    persisted_comparison: dict[str, Any] | None
    observation_id: str | None
    requested_time_utc: str | None

    @property
    def identity(self) -> str:
        return f"{self.reference_resource_id}|{self.candidate_digest}"


@dataclass(frozen=True, slots=True)
class BaselineContext:
    image: DecodedRgbImage
    mask: BinaryMask
    reference: FastPresenceReference | None
    model_ms: float
    source_width: int
    source_height: int
    roi: ConfirmationRoi


class StaticPredictor:
    """Return already-generated baseline/candidate masks for the v3 comparator."""

    def __init__(self, baseline_mask: BinaryMask, candidate_mask: BinaryMask) -> None:  # noqa: D107
        self._values = (baseline_mask, candidate_mask)
        self.calls = 0

    def predict_from_rgb(self, _image: object, _point: object, _size: object) -> BinaryMask:
        if self.calls >= len(self._values):
            raise ValueError
        value = self._values[self.calls]
        self.calls += 1
        return value


def _policy() -> ObjectPresenceDecisionPolicy:
    """Return the same approved successor baseline-support policy as production."""
    return ObjectPresenceDecisionPolicy(
        classifier_policy_version="efficient-sam-ti-baseline-support-v3",
        classifier_preprocessing_version="phase7e-baseline-support-v3",
        baseline_support_mode=True,
        baseline_support_alignment_mode=True,
        minimum_mask_overlap_for_comparison=0.1,
    )


POLICY = _policy()


def _roi(payload: dict[str, Any]) -> ConfirmationRoi:
    return ConfirmationRoi(
        x=int(payload["x"]),
        y=int(payload["y"]),
        width=int(payload["width"]),
        height=int(payload["height"]),
        coordinate_space="source_pixels",
        provenance=RoiProvenance(str(payload["provenance"])),
    )


def _relative_frame(manifest_path: Path, entry: dict[str, Any]) -> Path:
    relative = entry.get("path")
    if not isinstance(relative, str) or not relative:
        raise ValueError
    path = (manifest_path.parent / Path(relative)).resolve()
    if not path.is_file() or not path.is_relative_to(SUCCESSOR_ROOT.resolve()):
        raise ValueError
    return path


def _discover_observations() -> tuple[tuple[ArtifactObservation, ...], int]:
    manifests = tuple(sorted(SUCCESSOR_ROOT.rglob("manifest.json")))
    selected: dict[str, ArtifactObservation] = {}
    for manifest_path in manifests:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = payload["entries"]
            investigation_id = str(payload["investigation_id"])
            run_id = str(payload["run_id"])
            roi = _roi(payload["roi"])
            baseline_entry = next(item for item in entries if item.get("role") == "baseline")
            baseline_digest = str(baseline_entry["digest"])
            baseline_path = _relative_frame(manifest_path, baseline_entry)
            source_width = int(payload["source_width"])
            source_height = int(payload["source_height"])
        except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
            continue
        for entry in entries:
            if entry.get("role") not in {"anchor", "observation"}:
                continue
            candidate_digest = entry.get("digest")
            if not isinstance(candidate_digest, str) or not candidate_digest:
                continue
            try:
                candidate_path = _relative_frame(manifest_path, entry)
                comparison = entry.get("comparison")
                if comparison is not None and not isinstance(comparison, dict):
                    comparison = None
                observation = ArtifactObservation(
                    manifest_path=manifest_path,
                    investigation_id=investigation_id,
                    run_id=run_id,
                    reference_resource_id=str(entry.get("reference_frame_resource_id", "")),
                    baseline_digest=baseline_digest,
                    candidate_digest=candidate_digest,
                    baseline_path=baseline_path,
                    candidate_path=candidate_path,
                    source_width=source_width,
                    source_height=source_height,
                    roi=roi,
                    persisted_v3_outcome=str(entry.get("state", "OPERATIONAL_FAILURE")),
                    persisted_reason=(
                        str(entry["reason_code"]) if entry.get("reason_code") is not None else None
                    ),
                    persisted_classifier_ms=(
                        int(entry["classifier_elapsed_ms"])
                        if isinstance(entry.get("classifier_elapsed_ms"), int)
                        else None
                    ),
                    persisted_comparison=comparison,
                    observation_id=(
                        str(entry["observation_id"])
                        if entry.get("observation_id") is not None
                        else None
                    ),
                    requested_time_utc=(
                        str(entry["requested_time_utc"])
                        if entry.get("requested_time_utc") is not None
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError):
                continue
            previous = selected.get(observation.identity)
            if previous is None or (
                previous.persisted_comparison is None and observation.persisted_comparison is not None
            ):
                selected[observation.identity] = observation
    return tuple(sorted(selected.values(), key=lambda item: item.identity)), len(manifests)


def _load_roi_image(path: Path, source_width: int, source_height: int, roi: ConfirmationRoi) -> DecodedRgbImage:
    with Image.open(path) as opened:
        image = opened.convert("RGB")
        if image.size != (source_width, source_height):
            raise ValueError
        crop = image.crop((roi.x, roi.y, roi.x + roi.width, roi.y + roi.height))
        values = tuple(crop.getdata())
    rows = tuple(
        tuple(values[offset : offset + roi.width])
        for offset in range(0, len(values), roi.width)
    )
    return DecodedRgbImage.from_rows(rows)


def _crop_mask_preview(preview: MaskPreview, roi: ConfirmationRoi) -> BinaryMask:
    width = preview.width
    height = preview.height
    rows = preview.rows
    if roi.y + roi.height > height or roi.x + roi.width > width:
        raise ValueError
    cropped = []
    for y in range(roi.y, roi.y + roi.height):
        source_runs = rows[y]
        cropped.append(
            tuple(
                any(start <= roi.x + x < end for start, end in source_runs)
                for x in range(roi.width)
            )
        )
    return BinaryMask.from_rows(tuple(cropped))


def _predict_roi_mask(
    predictor: LazyEfficientSamPredictor,
    path: Path,
    source_width: int,
    source_height: int,
    roi: ConfirmationRoi,
) -> tuple[BinaryMask, float]:
    started = perf_counter()
    point = Point(roi.x + roi.width // 2, roi.y + roi.height // 2)
    prediction = predictor.predict_with_mask(path, point, ImageSize(source_width, source_height))
    return _crop_mask_preview(prediction.mask_preview, roi), (perf_counter() - started) * 1000.0


def _local_roi(roi: ConfirmationRoi) -> ConfirmationRoi:
    return ConfirmationRoi(
        x=0,
        y=0,
        width=roi.width,
        height=roi.height,
        coordinate_space="source_pixels",
        provenance=roi.provenance,
    )


def _replay_v3(
    baseline_image: DecodedRgbImage,
    candidate_image: DecodedRgbImage,
    baseline_mask: BinaryMask,
    candidate_mask: BinaryMask,
    roi: ConfirmationRoi,
) -> dict[str, Any]:
    diagnostics: dict[str, int] = {}
    predictor = StaticPredictor(baseline_mask, candidate_mask)
    started = perf_counter()
    result = classify_decoded_images(
        baseline_image=baseline_image,
        probe_image=candidate_image,
        source_width=roi.width,
        source_height=roi.height,
        roi=roi,
        policy=POLICY,
        mask_predictor=predictor,
        diagnostics_sink=lambda name, value: diagnostics.__setitem__(name, value),  # noqa: PLW0108
    )
    return {
        "outcome": result.outcome.value,
        "elapsed_ms": (perf_counter() - started) * 1000.0,
        "segmentation_calls": diagnostics.get("segmentation_calls", predictor.calls),
        "alignment_comparisons": diagnostics.get("alignment_comparisons", 0),
        "comparison": result.comparison.model_dump(mode="json"),
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summary(records: list[dict[str, Any]], manifest_count: int) -> dict[str, Any]:
    total = len(records)
    eligible = sum(bool(item["fast_gate_eligible"]) for item in records)
    hits = sum(bool(item["fast_present"]) for item in records)
    delegated = sum(bool(item["delegated"]) for item in records)
    fast_values = [float(item["fast_ms"]) for item in records if item["fast_ms"] is not None]
    persisted_values = [
        float(item["persisted_v3_classifier_ms"])
        for item in records
        if item["persisted_v3_classifier_ms"] is not None
    ]
    hit_records = [item for item in records if item["fast_present"]]
    replayed_hits = [item for item in hit_records if item["v3_comparison_outcome"] is not None]
    replay_agreements = [
        item
        for item in replayed_hits
        if item["v3_comparison_outcome"] == item["persisted_v3_outcome"]
    ]
    replay_differences = [
        item
        for item in replayed_hits
        if item["v3_comparison_outcome"] != item["persisted_v3_outcome"]
    ]
    # Safety disagreement is defined against the freshly replayed comparator,
    # not against a potentially stale persisted artifact outcome. A PRESENT
    # fast hit is unsafe only when the current v3 comparator is non-PRESENT.
    material_disagreements = [
        item
        for item in hit_records
        if item["v3_comparison_outcome"] in {"ABSENT", "INDETERMINATE"}
    ]
    replay_failures = [item for item in hit_records if item["v3_comparison_outcome"] is None]
    alignment_avoided = sum(
        int(item["v3_alignment_comparisons"] or 0)
        for item in hit_records
        if item["v3_comparison_outcome"] is not None
    )
    prerequisite_counts = Counter(
        str(item["prerequisite_reason"])
        for item in records
        if item["prerequisite_reason"] is not None
    )
    category_counts = Counter(str(item["category"]) for item in records)
    outcome_counts = Counter(str(item["persisted_v3_outcome"]) for item in records)
    return {
        "source_manifest_count": manifest_count,
        "independent_observations": total,
        "fast_gate_eligible": eligible,
        "fast_present_hits": hits,
        "fast_present_hit_rate": round(hits / eligible, 6) if eligible else None,
        "delegated_observations": delegated,
        "delegation_rate": round(delegated / total, 6) if total else None,
        "v3_agreements_for_fast_hits": sum(
            item["v3_comparison_outcome"] == "PRESENT" for item in replayed_hits
        ),
        "persisted_v3_replay_agreements_for_fast_hits": len(replay_agreements),
        "v3_replay_present_hits": sum(
            item["v3_comparison_outcome"] == "PRESENT" for item in replayed_hits
        ),
        "v3_replay_non_present_hits": len(material_disagreements),
        "persisted_v3_replay_differences_for_fast_hits": len(replay_differences),
        "false_present_or_material_disagreements": len(material_disagreements),
        "fast_hit_replay_failures": len(replay_failures),
        "candidate_segmentation_calls_avoided": hits,
        "predictor_model_calls_avoided": hits,
        "slow_b4_classifier_invocations_avoided": hits,
        "alignment_comparisons_avoided": alignment_avoided,
        "fast_ms_mean": round(statistics.mean(fast_values), 3) if fast_values else None,
        "fast_ms_median": round(statistics.median(fast_values), 3) if fast_values else None,
        "fast_ms_p90": round(_percentile(fast_values, 0.9), 3)
        if _percentile(fast_values, 0.9) is not None
        else None,
        "persisted_v3_classifier_ms_mean": (
            round(statistics.mean(persisted_values), 3) if persisted_values else None
        ),
        "persisted_v3_classifier_ms_median": (
            round(statistics.median(persisted_values), 3) if persisted_values else None
        ),
        "persisted_v3_classifier_ms_p90": (
            round(_percentile(persisted_values, 0.9), 3)
            if _percentile(persisted_values, 0.9) is not None
            else None
        ),
        "persisted_v3_outcomes": dict(sorted(outcome_counts.items())),
        "evaluation_categories": dict(sorted(category_counts.items())),
        "prerequisite_prevented_evaluations": dict(sorted(prerequisite_counts.items())),
        "material_disagreement_cases": [
            {
                "identity": item["identity"],
                "persisted_v3": item["persisted_v3_outcome"],
                "replayed_v3": item["v3_comparison_outcome"],
            }
            for item in material_disagreements
        ],
        "recommendation": (
            "FIX_S2_1_BEFORE_CONTINUING"
            if material_disagreements or replay_failures
            else "KEEP_PRESENT_ONLY_AND_COLLECT_MORE_DATA"
        ),
    }


def _evaluate(  # noqa: PLR0915
    observations: tuple[ArtifactObservation, ...],
    manifest_count: int,
    limit: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    predictor = LazyEfficientSamPredictor(CHECKPOINT, CHECKPOINT_SHA256, "cpu")
    baseline_cache: dict[str, BaselineContext | str] = {}
    records: list[dict[str, Any]] = []
    for observation in observations[:limit]:
        base_record: dict[str, Any] = {
            "identity": observation.identity,
            "investigation_id": observation.investigation_id,
            "run_id": observation.run_id,
            "observation_id": observation.observation_id,
            "requested_time_utc": observation.requested_time_utc,
            "baseline_identity": observation.reference_resource_id,
            "baseline_digest": observation.baseline_digest,
            "candidate_digest": observation.candidate_digest,
            "source_manifest": str(observation.manifest_path.relative_to(ROOT)),
            "category": (
                "preserved_present"
                if observation.persisted_v3_outcome == "PRESENT"
                else "preserved_absent"
                if observation.persisted_v3_outcome == "ABSENT"
                else "preserved_ambiguous"
                if observation.persisted_v3_outcome == "INDETERMINATE"
                else "preserved_operational_failure"
            ),
            "persisted_v3_outcome": observation.persisted_v3_outcome,
            "persisted_v3_reason": observation.persisted_reason,
            "persisted_v3_comparison": observation.persisted_comparison,
            "persisted_v3_classifier_ms": observation.persisted_classifier_ms,
            "fast_gate_eligible": False,
            "fast_present": False,
            "delegated": True,
            "prerequisite_reason": None,
            "decode_ms": None,
            "fast_ms": None,
            "fast_comparison": None,
            "baseline_model_ms": None,
            "candidate_model_ms": None,
            "candidate_model_ran_for_v3_replay": False,
            "v3_comparison_outcome": None,
            "v3_replay_comparison": None,
            "v3_comparison_source": "not_replayed",
            "v3_segmentation_calls": None,
            "v3_alignment_ran": False,
            "v3_alignment_comparisons": None,
            "v3_replay_ms": None,
        }
        try:
            context = baseline_cache.get(observation.baseline_digest)
            if context is None:
                baseline_image = _load_roi_image(
                    observation.baseline_path,
                    observation.source_width,
                    observation.source_height,
                    observation.roi,
                )
                baseline_mask, model_ms = _predict_roi_mask(
                    predictor,
                    observation.baseline_path,
                    observation.source_width,
                    observation.source_height,
                    observation.roi,
                )
                local_roi = _local_roi(observation.roi)
                reference = prepare_fast_presence_reference(
                    baseline_image, baseline_mask, local_roi, POLICY
                )
                context = BaselineContext(
                    baseline_image,
                    baseline_mask,
                    reference,
                    model_ms,
                    observation.source_width,
                    observation.source_height,
                    local_roi,
                )
                baseline_cache[observation.baseline_digest] = context
            if isinstance(context, str):
                base_record["prerequisite_reason"] = context
                records.append(base_record)
                continue
            base_record["baseline_model_ms"] = round(context.model_ms, 3)
            if context.reference is None:
                base_record["prerequisite_reason"] = "baseline_mask_rejected_by_fast_reference"
                records.append(base_record)
                continue
            decode_started = perf_counter()
            candidate_image = _load_roi_image(
                observation.candidate_path,
                observation.source_width,
                observation.source_height,
                observation.roi,
            )
            base_record["decode_ms"] = round((perf_counter() - decode_started) * 1000.0, 3)
            fast_started = perf_counter()
            fast_result = fast_present_comparison(
                context.reference,
                candidate_image,
                context.roi,
                POLICY,
            )
            base_record["fast_ms"] = round((perf_counter() - fast_started) * 1000.0, 3)
            base_record["fast_gate_eligible"] = True
            base_record["fast_present"] = fast_result is not None
            base_record["delegated"] = fast_result is None
            if fast_result is None:
                records.append(base_record)
                continue
            base_record["fast_comparison"] = fast_result.model_dump(mode="json")
            candidate_mask, candidate_model_ms = _predict_roi_mask(
                predictor,
                observation.candidate_path,
                observation.source_width,
                observation.source_height,
                observation.roi,
            )
            base_record["candidate_model_ms"] = round(candidate_model_ms, 3)
            base_record["candidate_model_ran_for_v3_replay"] = True
            replay = _replay_v3(
                context.image,
                candidate_image,
                context.mask,
                candidate_mask,
                context.roi,
            )
            base_record["v3_comparison_outcome"] = replay["outcome"]
            base_record["v3_replay_comparison"] = replay["comparison"]
            base_record["v3_comparison_source"] = "replayed_existing_v3_comparator"
            base_record["v3_segmentation_calls"] = replay["segmentation_calls"]
            base_record["v3_alignment_ran"] = True
            base_record["v3_alignment_comparisons"] = replay["alignment_comparisons"]
            base_record["v3_replay_ms"] = round(float(replay["elapsed_ms"]), 3)
        except Exception as error:  # noqa: BLE001 - evaluation records a safe category.
            base_record["prerequisite_reason"] = type(error).__name__
            if observation.baseline_digest not in baseline_cache:
                baseline_cache[observation.baseline_digest] = type(error).__name__
        records.append(base_record)
    return _summary(records, manifest_count), records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--records", action="store_true")
    parser.add_argument("--identity", action="append", default=[])
    args = parser.parse_args()
    if args.limit <= 0:
        raise SystemExit
    observations, manifest_count = _discover_observations()
    if args.identity:
        requested = set(args.identity)
        observations = tuple(item for item in observations if item.identity in requested)
        if len(observations) != len(requested):
            raise SystemExit
    summary, records = _evaluate(observations, manifest_count, args.limit)
    payload = {"summary": summary, "records": records}
    output = payload if args.records else summary
    sys.stdout.write(json.dumps(output, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
