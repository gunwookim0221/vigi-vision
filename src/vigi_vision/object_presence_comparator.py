"""Pure Phase 7B classifier entry point and logits preparation."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from time import perf_counter
from typing import TYPE_CHECKING, Final, cast

from vigi_vision.object_presence_evidence import RawComparison
from vigi_vision.object_presence_metrics import (
    ComparisonMeasurements,
    background_dominant,
    clipped_masks,
    contains_prompt,
    fail,
    invalid_mask,
    mask_count,
    mask_intersection,
    mean_centered_ncc,
    ratio,
    roi_luma,
    unusable,
)
from vigi_vision.object_presence_models import (
    BinaryMask,
    ClassificationFailureReason,
    ClassificationOperationalError,
    ClassificationResult,
    DecodedRgbImage,
    VisualReason,
    VisualStatus,
    quantize_metric,
)

if TYPE_CHECKING:
    from vigi_vision.investigation_confirmation_models import ConfirmationRoi
    from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy


@dataclass(frozen=True, slots=True)
class ClassifierInput:
    """Already decoded, source-aligned values accepted by the pure classifier."""

    baseline_image: DecodedRgbImage
    probe_image: DecodedRgbImage
    baseline_mask: BinaryMask
    probe_mask: BinaryMask
    roi: ConfirmationRoi


@dataclass(frozen=True, slots=True)
class FastPresenceReference:
    """Run-scoped fixed-support values used by the cheap PRESENT gate."""

    baseline_luma: tuple[float, ...]
    baseline_mask: tuple[tuple[bool, ...], ...]
    support_indices: tuple[int, ...]
    baseline_background: tuple[float, ...]
    background_indices: tuple[int, ...]
    roi_width: int
    roi_height: int
    roi_pixel_count: int
    baseline_support_pixel_count: int
    baseline_support_coverage: float


def prepare_fast_presence_reference(
    baseline_image: DecodedRgbImage,
    baseline_mask: BinaryMask,
    roi: ConfirmationRoi,
    policy: ObjectPresenceDecisionPolicy,
) -> FastPresenceReference | None:
    """Prepare immutable fixed-support values without candidate model work."""
    if (
        baseline_image.width != baseline_mask.width
        or baseline_image.height != baseline_mask.height
        or roi.coordinate_space != "source_pixels"
        or roi.x < 0
        or roi.y < 0
        or roi.width <= 0
        or roi.height <= 0
        or roi.x + roi.width > baseline_image.width
        or roi.y + roi.height > baseline_image.height
    ):
        return None
    clipped_baseline, _ = clipped_masks(baseline_mask.rows, baseline_mask.rows, roi)
    roi_pixels = roi.width * roi.height
    support_count = mask_count(clipped_baseline)
    if (
        not contains_prompt(clipped_baseline, roi)
        or support_count < policy.minimum_clipped_mask_pixels
        or ratio(support_count, roi_pixels) >= policy.maximum_roi_mask_coverage_ratio
    ):
        return None
    support_indices = tuple(
        index
        for index, value in enumerate(value for row in clipped_baseline for value in row)
        if value
    )
    baseline_luma = roi_luma(baseline_image, roi)
    radius = _stability_dilation_radius(
        roi.width,
        roi.height,
        support_count,
        include_segmentation_margin=policy.baseline_support_alignment_mode,
    )
    exclusion = _dilated_exclusion_mask(clipped_baseline, radius)
    background_indices = tuple(
        index
        for index, excluded in enumerate(value for row in exclusion for value in row)
        if not excluded
    )
    baseline_background = tuple(baseline_luma[index] for index in background_indices)
    return FastPresenceReference(
        baseline_luma=baseline_luma,
        baseline_mask=clipped_baseline,
        support_indices=support_indices,
        baseline_background=baseline_background,
        background_indices=background_indices,
        roi_width=roi.width,
        roi_height=roi.height,
        roi_pixel_count=roi_pixels,
        baseline_support_pixel_count=support_count,
        baseline_support_coverage=ratio(support_count, roi_pixels),
    )


def fast_present_comparison(
    reference: FastPresenceReference,
    probe_image: DecodedRgbImage,
    roi: ConfirmationRoi,
    policy: ObjectPresenceDecisionPolicy,
) -> RawComparison | None:
    """Return comparable PRESENT evidence only for a decisive fixed-support match."""
    if (
        probe_image.width <= 0
        or probe_image.height <= 0
        or roi.width != reference.roi_width
        or roi.height != reference.roi_height
        or roi.x < 0
        or roi.y < 0
        or roi.x + roi.width > probe_image.width
        or roi.y + roi.height > probe_image.height
        or not reference.support_indices
        or not reference.baseline_background
        or not reference.background_indices
    ):
        return None
    probe_luma = roi_luma(probe_image, roi)
    probe_background = tuple(probe_luma[index] for index in reference.background_indices)
    normalization = _build_luma_normalization(reference.baseline_background, probe_background)
    if normalization is None:
        return None
    normalized_probe = _apply_luma_normalization(probe_luma, normalization)
    baseline_support = tuple(reference.baseline_luma[index] for index in reference.support_indices)
    probe_support = tuple(probe_luma[index] for index in reference.support_indices)
    similarity, change, foreground, background_change, normalized_probe = _support_luma_metrics(
        baseline_support,
        probe_support,
        reference.baseline_background,
        probe_background,
        (reference.background_indices, reference.roi_width),
        normalization,
    )
    stability = _stable_background_profile(
        reference.baseline_background,
        normalization.normalized_background,
        reference.background_indices,
        reference.roi_width,
        reference.roi_height,
        reference.roi_pixel_count,
    )
    support_ncc_raw = mean_centered_ncc(
        tuple(reference.baseline_luma[index] for index in reference.support_indices),
        tuple(probe_luma[index] for index in reference.support_indices),
    )
    roi_ncc_raw = mean_centered_ncc(reference.baseline_luma, probe_luma)
    edge = _support_edge_similarity(
        reference.baseline_luma,
        reference.baseline_mask,
        reference.roi_width,
        normalized_probe,
        reference.support_indices,
    )
    if (
        similarity is None
        or support_ncc_raw is None
        or change is None
        or foreground is None
        or background_change is None
        or edge is None
        or not stability.scene_stable
    ):
        return None
    if not (
        similarity >= policy.baseline_support_present_similarity_minimum
        and quantize_metric(support_ncc_raw) >= policy.baseline_support_present_ncc_minimum
        and edge >= policy.baseline_support_present_edge_minimum
        and change <= policy.baseline_support_present_change_maximum
        and foreground >= policy.baseline_support_present_foreground_minimum
    ):
        return None
    return RawComparison(
        baseline_mask_pixel_count=reference.baseline_support_pixel_count,
        probe_mask_pixel_count=None,
        roi_pixel_count=reference.roi_pixel_count,
        mask_intersection_pixel_count=None,
        mask_union_pixel_count=None,
        baseline_mask_coverage=reference.baseline_support_coverage,
        probe_mask_coverage=None,
        mask_iou=None,
        effective_comparison_area=None,
        roi_luma_ncc=None if roi_ncc_raw is None else quantize_metric(roi_ncc_raw),
        visual_status=VisualStatus.COMPARABLE,
        unusable_reason=None,
        comparison_mode=(
            "baseline_support_v3"
            if policy.baseline_support_alignment_mode
            else "baseline_support_v2"
        ),
        baseline_support_pixel_count=reference.baseline_support_pixel_count,
        baseline_support_luma_similarity=similarity,
        baseline_support_luma_ncc=quantize_metric(support_ncc_raw),
        baseline_support_edge_similarity=edge,
        baseline_support_change_ratio=change,
        baseline_support_foreground_retention=foreground,
        baseline_support_background_change_ratio=background_change,
        baseline_support_alignment_state=(
            "not_required" if policy.baseline_support_alignment_mode else None
        ),
        baseline_support_stability_pixel_count=stability.total_pixel_count,
        baseline_support_stability_changed_pixel_count=stability.changed_pixel_count,
        baseline_support_stability_valid_pixel_count=stability.valid_pixel_count,
        baseline_support_stability_excluded_pixel_count=stability.excluded_pixel_count,
        baseline_support_scene_stable=stability.scene_stable,
        baseline_support_scene_stability_veto_reason=stability.veto_reason,
        baseline_support_present_gate_passed=True,
        baseline_support_absent_gate_passed=False,
        baseline_support_empty_background_evidence=False,
        baseline_support_replacement_evidence=False,
        baseline_support_occlusion_evidence=False,
        baseline_support_decision_path="present",
        baseline_support_decision_reason="present_identity_retained",
    )


_SUPPORT_CHANGE_THRESHOLD: Final[float] = 32.0
_FOREGROUND_CONTRAST_THRESHOLD: Final[float] = 40.0
_STABILITY_DILATION_PIXELS: Final[int] = 4
# EfficientSAM's point-prompt support can stop just inside a real object's
# edge.  Keep one bounded source-pixel margin outside the scale-derived ring
# so those immutable baseline-object pixels cannot be sampled as "background".
_STABILITY_SEGMENTATION_MARGIN_PIXELS: Final[int] = 1
_BACKGROUND_GRADIENT_CHANGE_THRESHOLD: Final[float] = 32.0
_GLOBAL_CHANGE_RATIO_THRESHOLD: Final[float] = 0.30
_GLOBAL_CHANGE_SPAN_THRESHOLD: Final[float] = 0.60
_GLOBAL_CHANGE_QUADRANT_COUNT: Final[int] = 4
_GLOBAL_CHANGE_FALLBACK_RATIO: Final[float] = 0.65
_ALIGNMENT_ROTATIONS: Final[tuple[int, ...]] = (-10, -5, 0, 5, 10)
_ALIGNMENT_DISTINCT_ROTATION_DEGREES: Final[int] = 5
_DENSE_ALIGNMENT_RADIUS_THRESHOLD: Final[int] = 4
_COARSE_ALIGNMENT_STEP: Final[int] = 4
_ALIGNMENT_REFINE_RADIUS: Final[int] = 2
_ALIGNMENT_SEED_COUNT: Final[int] = 4


@dataclass(frozen=True, slots=True)
class ObjectPresenceClassifier:
    """Deterministic in-memory mask and aligned-ROI comparison."""

    policy: ObjectPresenceDecisionPolicy

    def compare(
        self,
        values: ClassifierInput,
        *,
        diagnostics_sink: Callable[[str, int], None] | None = None,
    ) -> RawComparison:
        """Return one validated raw comparison without performing I/O."""
        _validate_input(values)
        if self.policy.baseline_support_mode:
            return _compare_with_baseline_support(
                values, self.policy, diagnostics_sink=diagnostics_sink
            )
        roi_pixels = values.roi.width * values.roi.height
        baseline_mask, probe_mask = clipped_masks(
            values.baseline_mask.rows, values.probe_mask.rows, values.roi
        )
        measurements, terminal = _measure_masks(
            baseline_mask, probe_mask, values.roi, self.policy, roi_pixels
        )
        if terminal is not None:
            return terminal
        if measurements is None:
            fail(ClassificationFailureReason.INVALID_CLASSIFIER_OUTPUT)
        ncc = mean_centered_ncc(
            roi_luma(values.baseline_image, values.roi),
            roi_luma(values.probe_image, values.roi),
        )
        if ncc is None:
            return unusable(measurements, VisualReason.ZERO_LUMA_VARIANCE)
        return RawComparison(
            baseline_mask_pixel_count=measurements.baseline_mask_pixel_count,
            probe_mask_pixel_count=measurements.probe_mask_pixel_count,
            roi_pixel_count=measurements.roi_pixel_count,
            mask_intersection_pixel_count=measurements.mask_intersection_pixel_count,
            mask_union_pixel_count=measurements.mask_union_pixel_count,
            baseline_mask_coverage=measurements.baseline_mask_coverage,
            probe_mask_coverage=measurements.probe_mask_coverage,
            mask_iou=measurements.mask_iou,
            effective_comparison_area=measurements.effective_comparison_area,
            roi_luma_ncc=quantize_metric(ncc),
            visual_status=VisualStatus.COMPARABLE,
            unusable_reason=None,
        )

    def classify(
        self,
        values: ClassifierInput,
        *,
        diagnostics_sink: Callable[[str, int], None] | None = None,
    ) -> ClassificationResult:
        """Return the conservative three-state result for one in-memory input."""
        return self.policy.decide(self.compare(values, diagnostics_sink=diagnostics_sink))


@dataclass(frozen=True, slots=True)
class _AlignmentChoice:
    """Best bounded local alignment and its support-space measurements."""

    similarity: float | None
    ncc: float | None
    edge: float | None
    change: float | None
    foreground: float | None
    background_change: float | None
    normalized_probe: tuple[float, ...]
    dx: int
    dy: int
    rotation_degrees: int
    overlap: float
    score: float
    margin: float


@dataclass(frozen=True, slots=True)
class _AlignmentResolution:
    """Bounded alignment result plus safe candidate counters."""

    choice: _AlignmentChoice
    generated: int
    evaluated: int
    valid: int


@dataclass(frozen=True, slots=True)
class _LumaNormalization:
    """Cached fixed-ring normalization shared by alignment candidates."""

    baseline_location: float
    probe_location: float
    scale: float
    normalized_background: tuple[float, ...]


def binarize_mask_logits(logits: object, threshold: float = 0.0) -> BinaryMask:
    """Apply the inclusive threshold to finite source-sized logits."""
    if not math.isfinite(threshold):
        fail(ClassificationFailureReason.INVALID_NUMERIC_INPUT)
    try:
        if not isinstance(logits, Sequence):
            fail(ClassificationFailureReason.INVALID_CLASSIFIER_OUTPUT)
        rows = tuple(_binarize_row(row, threshold) for row in logits)
        return BinaryMask.from_rows(rows)
    except (TypeError, ValueError) as error:
        raise ClassificationOperationalError(
            ClassificationFailureReason.INVALID_CLASSIFIER_OUTPUT
        ) from error


def _binarize_row(row: object, threshold: float) -> tuple[bool, ...]:
    if not isinstance(row, Sequence):
        fail(ClassificationFailureReason.INVALID_CLASSIFIER_OUTPUT)
    return tuple(_logit_is_positive(value, threshold) for value in row)


def _logit_is_positive(value: object, threshold: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        fail(ClassificationFailureReason.INVALID_NUMERIC_INPUT)
    return value >= threshold


def _validate_input(values: ClassifierInput) -> None:
    if (
        values.baseline_image.width != values.probe_image.width
        or values.baseline_image.height != values.probe_image.height
    ):
        fail(ClassificationFailureReason.INVALID_INPUT_SHAPE)
    for mask in (values.baseline_mask, values.probe_mask):
        if mask.width != values.baseline_image.width or mask.height != values.baseline_image.height:
            fail(ClassificationFailureReason.INVALID_MASK_STRUCTURE)
    if (
        values.roi.coordinate_space != "source_pixels"
        or values.roi.x < 0
        or values.roi.y < 0
        or values.roi.x + values.roi.width > values.baseline_image.width
        or values.roi.y + values.roi.height > values.baseline_image.height
    ):
        fail(ClassificationFailureReason.INVALID_GEOMETRY)


def _measure_masks(
    baseline: tuple[tuple[bool, ...], ...],
    probe: tuple[tuple[bool, ...], ...],
    roi: ConfirmationRoi,
    policy: ObjectPresenceDecisionPolicy,
    roi_pixels: int,
) -> tuple[ComparisonMeasurements | None, RawComparison | None]:
    terminal: RawComparison | None = None
    measurements: ComparisonMeasurements | None = None
    baseline_count = mask_count(baseline)
    probe_count = mask_count(probe)
    if not contains_prompt(baseline, roi) or not contains_prompt(probe, roi):
        terminal = invalid_mask(roi_pixels)
    else:
        baseline_coverage = ratio(baseline_count, roi_pixels)
        probe_coverage = ratio(probe_count, roi_pixels)
        measurements = ComparisonMeasurements(
            roi_pixel_count=roi_pixels,
            baseline_mask_pixel_count=baseline_count,
            probe_mask_pixel_count=probe_count,
            baseline_mask_coverage=baseline_coverage,
            probe_mask_coverage=probe_coverage,
        )
        if (
            baseline_coverage >= policy.maximum_roi_mask_coverage_ratio
            or probe_coverage >= policy.maximum_roi_mask_coverage_ratio
        ):
            terminal = background_dominant(measurements)
        elif (
            baseline_count < policy.minimum_clipped_mask_pixels
            or probe_count < policy.minimum_clipped_mask_pixels
        ):
            terminal = invalid_mask(roi_pixels)
        else:
            intersection = mask_intersection(baseline, probe)
            union = baseline_count + probe_count - intersection
            if union <= 0:
                terminal = invalid_mask(roi_pixels)
            else:
                mask_iou = ratio(intersection, union)
                measurements = replace(
                    measurements,
                    mask_intersection_pixel_count=intersection,
                    mask_union_pixel_count=union,
                    mask_iou=mask_iou,
                )
                if mask_iou < policy.minimum_mask_overlap_for_comparison:
                    terminal = unusable(measurements, VisualReason.INSUFFICIENT_MASK_OVERLAP)
                else:
                    measurements = replace(measurements, effective_comparison_area=intersection)
                    if (
                        roi_pixels < policy.minimum_roi_pixels
                        or intersection < policy.minimum_comparison_area
                    ):
                        terminal = unusable(measurements, VisualReason.INSUFFICIENT_COMPARISON_AREA)
    return measurements, terminal


def _compare_with_baseline_support(  # noqa: PLR0915 - explicit evidence-gate assembly
    values: ClassifierInput,
    policy: ObjectPresenceDecisionPolicy,
    *,
    diagnostics_sink: Callable[[str, int], None] | None = None,
) -> RawComparison:
    """Compare probe pixels against the immutable baseline mask support.

    The independently predicted probe mask remains diagnostic evidence only;
    it never defines the target identity or gates the support comparison.
    """
    roi_pixels = values.roi.width * values.roi.height
    baseline_mask, probe_mask = clipped_masks(
        values.baseline_mask.rows, values.probe_mask.rows, values.roi
    )
    baseline_count = mask_count(baseline_mask)
    if not contains_prompt(baseline_mask, values.roi):
        return invalid_mask(roi_pixels)
    baseline_coverage = ratio(baseline_count, roi_pixels)
    if baseline_coverage >= policy.maximum_roi_mask_coverage_ratio:
        return background_dominant(
            ComparisonMeasurements(
                roi_pixels,
                baseline_count,
                None,
                None,
                None,
                baseline_coverage,
                None,
                None,
                None,
                None,
            )
        )
    if baseline_count < policy.minimum_clipped_mask_pixels:
        return invalid_mask(roi_pixels)

    probe_count = mask_count(probe_mask)
    probe_coverage = ratio(probe_count, roi_pixels) if probe_count else None
    intersection = mask_intersection(baseline_mask, probe_mask)
    union = baseline_count + probe_count - intersection
    mask_iou = ratio(intersection, union) if union else None
    baseline_luma = roi_luma(values.baseline_image, values.roi)
    probe_luma = roi_luma(values.probe_image, values.roi)
    support_indices = tuple(
        index
        for index, present in enumerate(value for row in baseline_mask for value in row)
        if present
    )
    alignment_radius_x, alignment_radius_y = _alignment_radii(
        values.roi.width, values.roi.height, policy
    )
    stability_radius = min(
        _STABILITY_DILATION_PIXELS + max(alignment_radius_x, alignment_radius_y),
        min(values.roi.width, values.roi.height) // 4,
    )
    alignment_stability_mask = _dilated_exclusion_mask(baseline_mask, stability_radius)
    alignment_background_indices = tuple(
        index
        for index, excluded in enumerate(value for row in alignment_stability_mask for value in row)
        if not excluded
    )
    fixed_stability_radius = _stability_dilation_radius(
        values.roi.width,
        values.roi.height,
        baseline_count,
        include_segmentation_margin=policy.baseline_support_alignment_mode,
    )
    fixed_stability_mask = _dilated_exclusion_mask(baseline_mask, fixed_stability_radius)
    fixed_background_indices = tuple(
        index
        for index, excluded in enumerate(value for row in fixed_stability_mask for value in row)
        if not excluded
    )
    alignment_background_baseline = tuple(
        baseline_luma[index] for index in alignment_background_indices
    )
    alignment_background_probe = tuple(probe_luma[index] for index in alignment_background_indices)
    fixed_background_baseline = tuple(baseline_luma[index] for index in fixed_background_indices)
    fixed_background_probe = tuple(probe_luma[index] for index in fixed_background_indices)
    support_baseline = tuple(baseline_luma[index] for index in support_indices)
    support_probe = tuple(probe_luma[index] for index in support_indices)
    fixed_support_ncc_raw = mean_centered_ncc(support_baseline, support_probe)
    fixed_support_ncc = (
        None if fixed_support_ncc_raw is None else quantize_metric(fixed_support_ncc_raw)
    )
    fixed_stability = _stable_background_profile(
        fixed_background_baseline,
        _normalize_probe_values(
            fixed_background_baseline, fixed_background_probe, fixed_background_probe
        )[1]
        or fixed_background_probe,
        fixed_background_indices,
        values.roi.width,
        values.roi.height,
        roi_pixels,
    )
    (
        fixed_similarity,
        fixed_change,
        fixed_foreground,
        fixed_background_change,
        fixed_normalized_probe,
    ) = _support_luma_metrics(
        support_baseline,
        support_probe,
        fixed_background_baseline,
        fixed_background_probe,
        (fixed_background_indices, values.roi.width),
    )
    alignment: _AlignmentResolution | None = None
    if policy.baseline_support_alignment_mode:
        alignment = _aligned_support_choice(
            baseline_luma,
            probe_luma,
            baseline_mask,
            support_indices,
            alignment_background_baseline,
            alignment_background_probe,
            alignment_background_indices,
            values.roi.width,
            alignment_radius_x,
            alignment_radius_y,
            policy,
            diagnostics_sink=diagnostics_sink,
        )
        alignment_is_confident = (
            alignment.choice.overlap >= policy.baseline_support_alignment_min_support_overlap
            and alignment.choice.margin >= policy.baseline_support_alignment_margin_minimum
        )
        if alignment_is_confident:
            support_luma_similarity = alignment.choice.similarity
            support_luma_ncc = alignment.choice.ncc
            edge_similarity = alignment.choice.edge
            change_ratio = alignment.choice.change
            foreground_retention = alignment.choice.foreground
            background_change_ratio = alignment.choice.background_change
            normalized_probe = alignment.choice.normalized_probe
        else:
            # A low-margin alignment is ambiguous.  Preserve fixed baseline
            # support metrics so removal remains observable instead of letting
            # a floor/background correlation mask the absence signal.
            support_luma_similarity = fixed_similarity
            support_luma_ncc = fixed_support_ncc
            edge_similarity = _support_edge_similarity(
                baseline_luma,
                baseline_mask,
                values.roi.width,
                fixed_normalized_probe,
                support_indices,
            )
            change_ratio = fixed_change
            foreground_retention = fixed_foreground
            background_change_ratio = fixed_background_change
            if alignment.choice.background_change is not None:
                background_change_ratio = max(
                    alignment.choice.background_change,
                    fixed_background_change or 0.0,
                )
            normalized_probe = fixed_normalized_probe
        alignment_fields: tuple[
            int | None, int | None, int | None, float | None, float | None, float | None
        ] = (
            alignment.choice.dx if alignment.choice.overlap > 0.0 else None,
            alignment.choice.dy if alignment.choice.overlap > 0.0 else None,
            alignment.choice.rotation_degrees if alignment.choice.overlap > 0.0 else None,
            alignment.choice.overlap if alignment.choice.overlap > 0.0 else None,
            alignment.choice.score if alignment.choice.overlap > 0.0 else None,
            alignment.choice.margin if alignment.choice.overlap > 0.0 else None,
        )
        alignment_state = (
            "aligned"
            if alignment.choice.overlap >= policy.baseline_support_alignment_min_support_overlap
            and alignment.choice.margin >= policy.baseline_support_alignment_margin_minimum
            else "ambiguous"
        )
        if alignment.valid == 0:
            alignment_state = (
                "not_required" if fixed_stability.scene_stable else "no_valid_candidate"
            )
    else:
        support_luma_similarity = fixed_similarity
        support_luma_ncc = fixed_support_ncc
        change_ratio = fixed_change
        foreground_retention = fixed_foreground
        background_change_ratio = fixed_background_change
        normalized_probe = fixed_normalized_probe
        edge_similarity = _support_edge_similarity(
            baseline_luma,
            baseline_mask,
            values.roi.width,
            normalized_probe,
            support_indices,
        )
        alignment_fields = (None, None, None, None, None, None)
        alignment_state = "not_required"
    roi_ncc_raw = mean_centered_ncc(baseline_luma, probe_luma)
    roi_ncc = None if roi_ncc_raw is None else quantize_metric(roi_ncc_raw)
    if (
        support_luma_similarity is None
        or support_luma_ncc is None
        or change_ratio is None
        or foreground_retention is None
        or background_change_ratio is None
    ):
        return unusable(
            ComparisonMeasurements(
                roi_pixels,
                baseline_count,
                probe_count or None,
                intersection if probe_count else None,
                union if probe_count else None,
                baseline_coverage,
                probe_coverage,
                mask_iou,
                None,
                roi_ncc,
            ),
            VisualReason.ZERO_LUMA_VARIANCE,
        )
    return RawComparison(
        baseline_mask_pixel_count=baseline_count,
        probe_mask_pixel_count=probe_count or None,
        roi_pixel_count=roi_pixels,
        mask_intersection_pixel_count=intersection if probe_count else None,
        mask_union_pixel_count=union if probe_count else None,
        baseline_mask_coverage=baseline_coverage,
        probe_mask_coverage=probe_coverage,
        mask_iou=mask_iou,
        effective_comparison_area=None,
        roi_luma_ncc=roi_ncc,
        visual_status=VisualStatus.COMPARABLE,
        unusable_reason=None,
        comparison_mode=(
            "baseline_support_v3"
            if policy.baseline_support_alignment_mode
            else "baseline_support_v2"
        ),
        baseline_support_pixel_count=baseline_count,
        baseline_support_luma_similarity=support_luma_similarity,
        baseline_support_luma_ncc=support_luma_ncc,
        baseline_support_edge_similarity=edge_similarity,
        baseline_support_change_ratio=change_ratio,
        baseline_support_foreground_retention=foreground_retention,
        baseline_support_background_change_ratio=background_change_ratio,
        baseline_support_alignment_dx=alignment_fields[0],
        baseline_support_alignment_dy=alignment_fields[1],
        baseline_support_alignment_rotation_degrees=alignment_fields[2],
        baseline_support_alignment_overlap=alignment_fields[3],
        baseline_support_alignment_score=alignment_fields[4],
        baseline_support_alignment_margin=alignment_fields[5],
        baseline_support_stability_pixel_count=fixed_stability.total_pixel_count,
        baseline_support_stability_changed_pixel_count=fixed_stability.changed_pixel_count,
        baseline_support_stability_valid_pixel_count=fixed_stability.valid_pixel_count,
        baseline_support_stability_excluded_pixel_count=fixed_stability.excluded_pixel_count,
        baseline_support_alignment_candidates_generated=(
            alignment.generated if alignment is not None else None
        ),
        baseline_support_alignment_candidates_evaluated=(
            alignment.evaluated if alignment is not None else None
        ),
        baseline_support_alignment_valid_candidates=(
            alignment.valid if alignment is not None else None
        ),
        baseline_support_alignment_state=alignment_state,
        baseline_support_scene_stable=fixed_stability.scene_stable,
        baseline_support_scene_stability_veto_reason=fixed_stability.veto_reason,
    )


def _support_luma_metrics(  # noqa: PLR0913 - explicit support/background inputs
    baseline: tuple[float, ...],
    probe: tuple[float, ...],
    baseline_background: tuple[float, ...],
    probe_background: tuple[float, ...],
    stability: tuple[tuple[int, ...], int],
    normalization: _LumaNormalization | None = None,
) -> tuple[float | None, float | None, float | None, float | None, tuple[float, ...]]:
    if (
        not baseline
        or len(baseline) != len(probe)
        or not baseline_background
        or not probe_background
    ):
        return None, None, None, None, ()
    if normalization is None:
        normalized, normalized_background, baseline_background_mean = _normalize_probe_values(
            baseline_background, probe_background, probe
        )
    else:
        normalized = _apply_luma_normalization(probe, normalization)
        normalized_background = normalization.normalized_background
        baseline_background_mean = normalization.baseline_location
    if normalized is None or normalized_background is None or baseline_background_mean is None:
        return None, None, None, None, ()
    mean_difference = sum(
        abs(left - right) for left, right in zip(baseline, normalized, strict=True)
    ) / len(baseline)
    similarity = quantize_metric(max(0.0, 1.0 - mean_difference / 255.0))
    changed = sum(
        abs(left - right) > _SUPPORT_CHANGE_THRESHOLD
        for left, right in zip(baseline, normalized, strict=True)
    )
    background_indices, width = stability
    background_changed = _stable_background_change_ratio(
        baseline_background,
        normalized_background,
        background_indices,
        width,
    )
    baseline_contrast = tuple(value - baseline_background_mean for value in baseline)
    # The normalized probe is centered on its own fixed-background ring before
    # being mapped to the baseline scale.  This makes exposed flooring low
    # contrast even when its absolute luma is brighter than the old object.
    probe_contrast = tuple(value - baseline_background_mean for value in normalized)
    baseline_foreground = tuple(
        index
        for index, value in enumerate(baseline_contrast)
        if abs(value) >= _FOREGROUND_CONTRAST_THRESHOLD
    )
    if not baseline_foreground or not all(math.isfinite(value) for value in probe_contrast):
        foreground_retention = None
    else:
        foreground_retention = quantize_metric(
            sum(
                1
                for index in baseline_foreground
                if abs(probe_contrast[index]) >= _FOREGROUND_CONTRAST_THRESHOLD
                and abs(probe_contrast[index]) >= abs(baseline_contrast[index]) * 0.25
            )
            / len(baseline_foreground)
        )
    return (
        similarity,
        quantize_metric(changed / len(baseline)),
        foreground_retention,
        background_changed,
        normalized,
    )


def _normalize_probe_values(
    baseline_background: tuple[float, ...],
    probe_background: tuple[float, ...],
    probe: tuple[float, ...],
) -> tuple[tuple[float, ...] | None, tuple[float, ...] | None, float | None]:
    """Normalize probe luma to the fixed-background baseline scale."""
    if not baseline_background or not probe_background:
        return None, None, None
    context = _build_luma_normalization(baseline_background, probe_background)
    if context is None:
        return None, None, None
    return (
        _apply_luma_normalization(probe, context),
        context.normalized_background,
        context.baseline_location,
    )


def _build_luma_normalization(
    baseline_background: tuple[float, ...], probe_background: tuple[float, ...]
) -> _LumaNormalization | None:
    """Build robust ring normalization once per aligned replay window."""
    if not baseline_background or not probe_background:
        # A tight ROI can leave no pixels outside the alignment exclusion ring.
        # Treat that as unavailable normalization so the bounded alignment
        # search can fail closed and fall back to fixed-support evidence.
        return None
    # A local person/object in the ring must not move the global brightness
    # anchor.  Medians keep the registration/contrast normalization stable
    # when a bounded portion of the external scene changes.
    baseline_ordered = sorted(baseline_background)
    probe_ordered = sorted(probe_background)
    middle = len(baseline_ordered) // 2
    baseline_mean = (
        baseline_ordered[middle]
        if len(baseline_ordered) % 2
        else (baseline_ordered[middle - 1] + baseline_ordered[middle]) / 2.0
    )
    middle = len(probe_ordered) // 2
    probe_mean = (
        probe_ordered[middle]
        if len(probe_ordered) % 2
        else (probe_ordered[middle - 1] + probe_ordered[middle]) / 2.0
    )
    baseline_variance = _robust_variance(baseline_background, baseline_mean)
    probe_variance = _robust_variance(probe_background, probe_mean)
    if not math.isfinite(baseline_variance) or not math.isfinite(probe_variance):
        return None
    if probe_variance <= 0.0 or baseline_variance <= 0.0:
        scale = 1.0
    else:
        scale = min(2.0, max(0.5, math.sqrt(baseline_variance / probe_variance)))

    context = _LumaNormalization(baseline_mean, probe_mean, scale, ())
    return _LumaNormalization(
        context.baseline_location,
        context.probe_location,
        context.scale,
        _apply_luma_normalization(probe_background, context),
    )


def _apply_luma_normalization(
    values: tuple[float, ...], context: _LumaNormalization
) -> tuple[float, ...]:
    return tuple(
        min(
            255.0,
            max(
                0.0,
                (value - context.probe_location) * context.scale + context.baseline_location,
            ),
        )
        for value in values
    )


def _robust_variance(values: tuple[float, ...], center: float) -> float:
    """Estimate spread after trimming a bounded fraction of scene outliers."""
    deviations = sorted((value - center) ** 2 for value in values)
    trim = min(len(deviations) // 4, len(deviations) // 5)
    core = deviations[trim : len(deviations) - trim] if trim else deviations
    return sum(core)


def _alignment_radii(
    roi_width: int, roi_height: int, policy: ObjectPresenceDecisionPolicy
) -> tuple[int, int]:
    """Return bounded source-pixel translation radii derived from the ROI."""
    if not policy.baseline_support_alignment_mode:
        return 0, 0
    fraction = policy.baseline_support_alignment_max_translation_fraction
    cap = policy.baseline_support_alignment_max_translation_pixels
    return min(cap, max(0, math.floor(roi_width * fraction))), min(
        cap, max(0, math.floor(roi_height * fraction))
    )


def _stability_dilation_radius(
    roi_width: int,
    roi_height: int,
    support_pixels: int,
    *,
    include_segmentation_margin: bool = False,
) -> int:
    """Choose a bounded reveal ring from the confirmed support scale.

    A fixed four-pixel ring is too small for a source-sized CCTV ROI.  The
    radius grows with the square-root support scale, while retaining a
    bounded minimum/maximum and never consuming more than a quarter of the
    shorter ROI edge.  This excludes newly revealed flooring without turning
    the whole ROI into an unobserved area.
    """
    shorter_edge = min(roi_width, roi_height)
    if shorter_edge <= 0:
        return 0
    scale_radius = math.ceil(math.sqrt(max(1, support_pixels)) / 8.0)
    radius = max(_STABILITY_DILATION_PIXELS, scale_radius)
    if include_segmentation_margin:
        radius += _STABILITY_SEGMENTATION_MARGIN_PIXELS
    return min(shorter_edge // 4, radius)


@dataclass(frozen=True, slots=True)
class _StabilityProfile:
    total_pixel_count: int
    changed_pixel_count: int
    valid_pixel_count: int
    excluded_pixel_count: int
    scene_stable: bool
    veto_reason: str | None


def _stable_background_profile(  # noqa: PLR0913 - profile inputs remain explicit
    baseline: tuple[float, ...],
    probe: tuple[float, ...],
    indices: tuple[int, ...],
    width: int,
    height: int,
    total_pixel_count: int,
) -> _StabilityProfile:
    """Measure bounded background change and distinguish local from global motion."""
    valid_pixel_count = len(indices)
    excluded_pixel_count = max(0, total_pixel_count - valid_pixel_count)
    if (
        not indices
        or width <= 0
        or height <= 0
        or len(baseline) != len(probe)
        or len(baseline) != valid_pixel_count
    ):
        return _StabilityProfile(
            total_pixel_count,
            0,
            valid_pixel_count,
            excluded_pixel_count,
            scene_stable=False,
            veto_reason="insufficient_stability_area",
        )
    stable = set(indices)
    baseline_by_index = dict(zip(indices, baseline, strict=True))
    probe_by_index = dict(zip(indices, probe, strict=True))
    changed: set[int] = {
        index
        for index in indices
        if abs(baseline_by_index[index] - probe_by_index[index]) > _SUPPORT_CHANGE_THRESHOLD
    }
    for index in indices:
        x = index % width
        for neighbor in (
            (index + 1) if x + 1 < width else -1,
            (index + width) if index + width in stable else -1,
        ):
            if neighbor not in stable:
                continue
            baseline_gradient = abs(baseline_by_index[index] - baseline_by_index[neighbor])
            probe_gradient = abs(probe_by_index[index] - probe_by_index[neighbor])
            if abs(baseline_gradient - probe_gradient) > _BACKGROUND_GRADIENT_CHANGE_THRESHOLD:
                changed.add(index)
                changed.add(neighbor)
    changed_count = len(changed)
    if changed_count == 0:
        return _StabilityProfile(
            total_pixel_count,
            0,
            valid_pixel_count,
            excluded_pixel_count,
            scene_stable=True,
            veto_reason=None,
        )

    # A local person/object movement may change many pixels but cannot move
    # all independent quadrants coherently.  Camera translation/scene cuts do.
    xs = [index % width for index in changed]
    ys = [index // width for index in changed]
    span_x = (max(xs) - min(xs) + 1) / width
    span_y = (max(ys) - min(ys) + 1) / height
    quadrants = {
        (0 if x < width / 2 else 1, 0 if y < height / 2 else 1) for x, y in zip(xs, ys, strict=True)
    }
    changed_ratio = changed_count / valid_pixel_count
    global_change = (
        changed_ratio >= _GLOBAL_CHANGE_RATIO_THRESHOLD
        and span_x >= _GLOBAL_CHANGE_SPAN_THRESHOLD
        and span_y >= _GLOBAL_CHANGE_SPAN_THRESHOLD
        and len(quadrants) >= _GLOBAL_CHANGE_QUADRANT_COUNT
    ) or changed_ratio >= _GLOBAL_CHANGE_FALLBACK_RATIO
    return _StabilityProfile(
        total_pixel_count,
        changed_count,
        valid_pixel_count,
        excluded_pixel_count,
        not global_change,
        None if not global_change else "global_scene_change",
    )


def _aligned_support_choice(  # noqa: PLR0913 - each alignment boundary is explicit
    baseline_luma: tuple[float, ...],
    probe_luma: tuple[float, ...],
    baseline_mask: tuple[tuple[bool, ...], ...],
    support_indices: tuple[int, ...],
    background_baseline: tuple[float, ...],
    background_probe: tuple[float, ...],
    background_indices: tuple[int, ...],
    width: int,
    radius_x: int,
    radius_y: int,
    policy: ObjectPresenceDecisionPolicy,
    *,
    diagnostics_sink: Callable[[str, int], None] | None = None,
) -> _AlignmentResolution:
    """Choose one deterministic bounded translation/rotation candidate.

    A coarse translation grid is refined around the best few candidates.  The
    rigid transform remains closed and bounded, while avoiding a full Python
    metric pass for every pixel-level translation in the configured radius.
    """
    started = perf_counter()
    height = len(baseline_mask)
    candidates: list[_AlignmentChoice] = []
    evaluated: set[tuple[int, int, int]] = set()
    comparison_count = 0
    dense_grid = (
        radius_x <= _DENSE_ALIGNMENT_RADIUS_THRESHOLD
        and radius_y <= _DENSE_ALIGNMENT_RADIUS_THRESHOLD
    )
    translation_x = _alignment_offsets(radius_x, dense=dense_grid)
    translation_y = _alignment_offsets(radius_y, dense=dense_grid)
    normalization = _build_luma_normalization(background_baseline, background_probe)

    def evaluate(transforms: Sequence[tuple[int, int, int]]) -> None:
        nonlocal comparison_count
        for rotation, dy, dx in transforms:
            key = (dx, dy, rotation)
            if key in evaluated:
                continue
            evaluated.add(key)
            comparison_count += 1
            transformed = _transformed_support(
                probe_luma,
                support_indices,
                width,
                height,
                dx,
                dy,
                rotation,
            )
            if transformed is None:
                continue
            mapped_indices, support_probe, overlap = transformed
            if overlap < policy.baseline_support_alignment_min_support_overlap:
                continue
            candidate_baseline = tuple(baseline_luma[index] for index in mapped_indices)
            (
                similarity,
                change,
                foreground,
                background_change,
                normalized_probe,
            ) = _support_luma_metrics(
                candidate_baseline,
                support_probe,
                background_baseline,
                background_probe,
                (background_indices, width),
                normalization,
            )
            support_ncc_raw = mean_centered_ncc(candidate_baseline, support_probe)
            support_ncc = None if support_ncc_raw is None else quantize_metric(support_ncc_raw)
            if (
                similarity is None
                or support_ncc is None
                or change is None
                or foreground is None
                or background_change is None
                or len(normalized_probe) != len(mapped_indices)
            ):
                continue
            edge = _support_edge_similarity(
                baseline_luma,
                baseline_mask,
                width,
                normalized_probe,
                mapped_indices,
            )
            score = _alignment_score(similarity, support_ncc, edge, change, foreground)
            if edge is None or score is None:
                continue
            candidates.append(
                _AlignmentChoice(
                    similarity,
                    support_ncc,
                    edge,
                    change,
                    foreground,
                    background_change,
                    normalized_probe,
                    dx,
                    dy,
                    rotation,
                    overlap,
                    score,
                    0.0,
                )
            )

    coarse_transforms = tuple(
        (rotation, dy, dx)
        for rotation in _ALIGNMENT_ROTATIONS
        for dy in translation_y
        for dx in translation_x
    )
    evaluate(coarse_transforms)
    if candidates and not dense_grid:
        seeds = sorted(candidates, key=_alignment_sort_key, reverse=True)[:_ALIGNMENT_SEED_COUNT]
        refine_transforms = tuple(
            (seed.rotation_degrees, dy, dx)
            for seed in seeds
            for dy in range(
                max(-radius_y, seed.dy - _ALIGNMENT_REFINE_RADIUS),
                min(radius_y, seed.dy + _ALIGNMENT_REFINE_RADIUS) + 1,
            )
            for dx in range(
                max(-radius_x, seed.dx - _ALIGNMENT_REFINE_RADIUS),
                min(radius_x, seed.dx + _ALIGNMENT_REFINE_RADIUS) + 1,
            )
        )
        evaluate(refine_transforms)
    generated_count = len(translation_x) * len(translation_y) * len(_ALIGNMENT_ROTATIONS)
    if not candidates:
        _emit_alignment_diagnostics(
            diagnostics_sink,
            len(translation_x),
            len(translation_y),
            comparison_count,
            started,
        )
        return _AlignmentResolution(
            _AlignmentChoice(
                None,
                None,
                None,
                None,
                None,
                None,
                (),
                0,
                0,
                0,
                0.0,
                -1.0,
                0.0,
            ),
            max(generated_count, comparison_count),
            comparison_count,
            0,
        )
    ordered = sorted(candidates, key=_alignment_sort_key, reverse=True)
    best = ordered[0]
    second_scores = [
        item.score
        for item in ordered[1:]
        if abs(item.dx - best.dx) > 1
        or abs(item.dy - best.dy) > 1
        or abs(item.rotation_degrees - best.rotation_degrees) > _ALIGNMENT_DISTINCT_ROTATION_DEGREES
    ]
    second = max(second_scores, default=-1.0)
    margin = quantize_metric(max(0.0, best.score - second))
    _emit_alignment_diagnostics(
        diagnostics_sink,
        len(translation_x),
        len(translation_y),
        comparison_count,
        started,
    )
    return _AlignmentResolution(
        replace(best, margin=margin),
        max(generated_count, comparison_count),
        comparison_count,
        len(candidates),
    )


def _alignment_offsets(radius: int, *, dense: bool) -> tuple[int, ...]:
    """Return a bounded translation grid with a stable zero anchor."""
    step = 1 if dense else _COARSE_ALIGNMENT_STEP
    offsets = set(range(-radius, radius + 1, step))
    offsets.update({-radius, 0, radius})
    return tuple(sorted(offsets))


def _alignment_sort_key(item: _AlignmentChoice) -> tuple[float, float, int, int, int, int]:
    return (
        item.score,
        item.overlap,
        -abs(item.dx) - abs(item.dy),
        -abs(item.rotation_degrees),
        -item.dy,
        -item.dx,
    )


def _emit_alignment_diagnostics(
    sink: Callable[[str, int], None] | None,
    translation_x_count: int,
    translation_y_count: int,
    comparison_count: int,
    started: float,
) -> None:
    """Emit bounded, non-sensitive alignment counters to the process sink."""
    if sink is None:
        return
    elapsed_ms = max(0, round((perf_counter() - started) * 1000))
    values = (
        ("alignment_translation_candidates", translation_x_count * translation_y_count),
        ("alignment_rotation_candidates", len(_ALIGNMENT_ROTATIONS)),
        ("alignment_scale_candidates", 1),
        ("alignment_comparisons", comparison_count),
        ("alignment_elapsed_ms", elapsed_ms),
    )
    try:
        for name, value in values:
            sink(name, value)
    except Exception:  # noqa: BLE001, S110 - diagnostics cannot affect classification.
        pass


def _alignment_score(
    similarity: float | None,
    ncc: float | None,
    edge: float | None,
    change: float | None,
    foreground: float | None,
) -> float | None:
    if any(value is None for value in (similarity, ncc, edge, change, foreground)):
        return None
    similarity, ncc, edge, change, foreground = cast(
        "tuple[float, float, float, float, float]",
        (similarity, ncc, edge, change, foreground),
    )
    score = (
        0.40 * ((ncc + 1.0) / 2.0)
        + 0.25 * similarity
        + 0.15 * edge
        + 0.20 * foreground
        - 0.20 * change
    )
    return quantize_metric(max(-1.0, min(1.0, score)))


def _transformed_support(  # noqa: PLR0913 - explicit transform coordinates
    probe_luma: tuple[float, ...],
    support_indices: tuple[int, ...],
    width: int,
    height: int,
    dx: int,
    dy: int,
    rotation_degrees: int,
) -> tuple[tuple[int, ...], tuple[float, ...], float] | None:
    """Sample probe luma at one bounded rigid transform of baseline support."""
    angle = math.radians(rotation_degrees)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    center_x = (width - 1) / 2.0
    center_y = (height - 1) / 2.0
    mapped: list[int] = []
    sampled: list[float] = []
    for index in support_indices:
        x = index % width
        y = index // width
        relative_x = x - center_x
        relative_y = y - center_y
        source_x = round(center_x + cosine * relative_x + sine * relative_y + dx)
        source_y = round(center_y - sine * relative_x + cosine * relative_y + dy)
        if not (0 <= source_x < width and 0 <= source_y < height):
            continue
        mapped.append(index)
        sampled.append(probe_luma[source_y * width + source_x])
    if not support_indices or len(mapped) != len(sampled):
        return None
    overlap = len(mapped) / len(support_indices)
    return tuple(mapped), tuple(sampled), quantize_metric(overlap)


def _dilated_exclusion_mask(
    support: tuple[tuple[bool, ...], ...], radius: int
) -> tuple[tuple[bool, ...], ...]:
    """Exclude immutable support and its local reveal ring from stability pixels."""
    height = len(support)
    width = len(support[0]) if height else 0
    return tuple(
        tuple(
            any(
                0 <= y + dy < height and 0 <= x + dx < width and support[y + dy][x + dx]
                for dy in range(-radius, radius + 1)
                for dx in range(-radius, radius + 1)
            )
            for x in range(width)
        )
        for y in range(height)
    )


def _stable_background_change_ratio(
    baseline: tuple[float, ...],
    probe: tuple[float, ...],
    indices: tuple[int, ...],
    width: int,
) -> float | None:
    """Measure luma and local-gradient changes only on fixed background pixels."""
    profile = _stable_background_profile(
        baseline,
        probe,
        indices,
        width,
        max(1, math.ceil(len(baseline) / max(1, width))),
        len(indices),
    )
    return quantize_metric(profile.changed_pixel_count / len(indices)) if indices else None


def _support_edge_similarity(
    baseline_luma: tuple[float, ...],
    support: tuple[tuple[bool, ...], ...],
    width: int,
    normalized_probe: tuple[float, ...],
    support_indices: tuple[int, ...],
) -> float | None:
    if not support_indices:
        return None
    support_set = set(support_indices)
    normalized_by_index = dict(zip(support_indices, normalized_probe, strict=True))
    gradients: list[tuple[float, float]] = []
    height = len(support)
    for y in range(height):
        for x in range(width):
            index = y * width + x
            if index not in support_set:
                continue
            if x + 1 < width and (index + 1) in support_set:
                gradients.append(
                    (
                        abs(baseline_luma[index] - baseline_luma[index + 1]),
                        abs(normalized_by_index[index] - normalized_by_index[index + 1]),
                    )
                )
    if not gradients:
        return None
    return quantize_metric(
        max(0.0, 1.0 - sum(abs(left - right) for left, right in gradients) / len(gradients) / 255.0)
    )
