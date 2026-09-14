"""Versioned Phase 7B comparison policy and deterministic identity."""

from __future__ import annotations

import hashlib
import json
import math
from typing import ClassVar, Final, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from vigi_vision.object_presence_evidence import ClassificationResult, RawComparison
from vigi_vision.object_presence_values import ClassificationOutcome, VisualReason, VisualStatus

_SOURCE_COMMIT: Final = "d525f622e6f640acf5a0fc37c7ca1f243da5bde0"
_CHECKPOINT_SHA256: Final = "dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a"
_LUMA_COEFFICIENTS: Final = (299, 587, 114)
_LUMA_DIVISOR: Final = 1000
_METRIC_DECIMAL_PLACES: Final = 6
_SOURCE_COMMIT_LENGTH: Final = 40
_DIGEST_LENGTH: Final = 64
_HEX_DIGITS: Final = frozenset("0123456789abcdef")
_ABSENCE_ALIGNMENT_MAX_TRANSLATION_PIXELS: Final = 2
_ABSENCE_ALIGNMENT_MAX_ROTATION_DEGREES: Final = 5
_ABSENCE_ALIGNMENT_NO_CANDIDATE_SCORE_MAXIMUM: Final = 0.40


class ObjectPresenceDecisionPolicy(BaseModel):
    """Immutable policy snapshot for deterministic visual classification."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )

    classifier_policy_version: StrictStr = Field(
        default="efficient-sam-ti-roi-ncc-v1", min_length=1
    )
    classifier_preprocessing_version: StrictStr = Field(default="phase7b-roi-luma-v1", min_length=1)
    mask_logit_threshold: StrictFloat = 0.0
    luma_integer_coefficients: tuple[StrictInt, StrictInt, StrictInt] = _LUMA_COEFFICIENTS
    luma_integer_divisor: StrictInt = Field(default=_LUMA_DIVISOR, gt=0)
    luma_rounding_rule: StrictStr = "add_500_then_floor"
    comparison_dtype: StrictStr = "float64"
    metric_decimal_places: StrictInt = Field(
        default=_METRIC_DECIMAL_PLACES, ge=0, le=_METRIC_DECIMAL_PLACES
    )
    metric_rounding_rule: StrictStr = "half_even"
    minimum_mask_overlap_for_comparison: StrictFloat
    minimum_comparison_area: StrictInt = Field(default=64, gt=0)
    minimum_roi_pixels: StrictInt = Field(default=64, gt=0)
    minimum_clipped_mask_pixels: StrictInt = Field(default=64, gt=0)
    maximum_roi_mask_coverage_ratio: StrictFloat = 0.95
    present_mask_iou_minimum: StrictFloat = Field(default=0.5, ge=0.0, le=1.0)
    present_luma_ncc_minimum: StrictFloat = Field(default=0.6, ge=-1.0, le=1.0)
    absent_mask_iou_maximum: StrictFloat = Field(default=0.1, ge=0.0, le=1.0)
    absent_luma_ncc_maximum: StrictFloat = Field(default=0.2, ge=-1.0, le=1.0)
    efficient_sam_source_commit: StrictStr = _SOURCE_COMMIT
    checkpoint_sha256: StrictStr = _CHECKPOINT_SHA256
    prompt_rule: StrictStr = "confirmed_roi_center_v1"
    baseline_support_mode: StrictBool = False
    baseline_support_alignment_mode: StrictBool = False
    baseline_support_alignment_max_translation_fraction: StrictFloat = Field(
        default=0.15, ge=0.0, le=0.25
    )
    baseline_support_alignment_max_translation_pixels: StrictInt = Field(default=16, ge=0, le=32)
    baseline_support_alignment_min_support_overlap: StrictFloat = Field(
        default=0.75, ge=0.5, le=1.0
    )
    baseline_support_alignment_margin_minimum: StrictFloat = Field(default=0.02, ge=0.0, le=1.0)
    baseline_support_present_similarity_minimum: StrictFloat = Field(default=0.70, ge=0.0, le=1.0)
    baseline_support_present_edge_minimum: StrictFloat = Field(default=0.60, ge=0.0, le=1.0)
    baseline_support_present_change_maximum: StrictFloat = Field(default=0.30, ge=0.0, le=1.0)
    baseline_support_present_ncc_minimum: StrictFloat = Field(default=0.50, ge=-1.0, le=1.0)
    baseline_support_present_foreground_minimum: StrictFloat = Field(default=0.70, ge=0.0, le=1.0)
    baseline_support_absent_similarity_maximum: StrictFloat = Field(default=0.60, ge=0.0, le=1.0)
    baseline_support_absent_ncc_maximum: StrictFloat = Field(default=0.20, ge=-1.0, le=1.0)
    baseline_support_absent_change_minimum: StrictFloat = Field(default=0.70, ge=0.0, le=1.0)
    baseline_support_absent_foreground_maximum: StrictFloat = Field(default=0.30, ge=0.0, le=1.0)
    baseline_support_background_change_maximum: StrictFloat = Field(default=0.10, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_policy(self) -> ObjectPresenceDecisionPolicy:
        """Reject non-finite, contradictory, or unsupported policy values."""
        self._validate_numeric_ranges()
        self._validate_determinism()
        self._validate_thresholds()
        self._validate_identity()
        return self

    def _validate_numeric_ranges(self) -> None:
        finite = (
            self.mask_logit_threshold,
            self.minimum_mask_overlap_for_comparison,
            self.maximum_roi_mask_coverage_ratio,
            self.present_mask_iou_minimum,
            self.present_luma_ncc_minimum,
            self.absent_mask_iou_maximum,
            self.absent_luma_ncc_maximum,
            self.baseline_support_present_similarity_minimum,
            self.baseline_support_present_edge_minimum,
            self.baseline_support_present_change_maximum,
            self.baseline_support_present_ncc_minimum,
            self.baseline_support_present_foreground_minimum,
            self.baseline_support_absent_similarity_maximum,
            self.baseline_support_absent_ncc_maximum,
            self.baseline_support_absent_change_minimum,
            self.baseline_support_absent_foreground_maximum,
            self.baseline_support_background_change_maximum,
            self.baseline_support_alignment_max_translation_fraction,
            self.baseline_support_alignment_min_support_overlap,
            self.baseline_support_alignment_margin_minimum,
        )
        if any(not math.isfinite(value) for value in finite):
            raise ValueError
        if not 0.0 <= self.minimum_mask_overlap_for_comparison <= 1.0:
            raise ValueError
        if not 0.0 < self.maximum_roi_mask_coverage_ratio < 1.0:
            raise ValueError

    def _validate_determinism(self) -> None:
        if self.luma_integer_coefficients != _LUMA_COEFFICIENTS:
            raise ValueError
        if self.luma_integer_divisor != _LUMA_DIVISOR:
            raise ValueError
        if self.luma_rounding_rule != "add_500_then_floor":
            raise ValueError
        if self.comparison_dtype != "float64":
            raise ValueError
        if self.metric_rounding_rule != "half_even":
            raise ValueError
        if self.metric_decimal_places != _METRIC_DECIMAL_PLACES:
            raise ValueError

    def _validate_thresholds(self) -> None:
        if self.present_mask_iou_minimum <= self.absent_mask_iou_maximum:
            raise ValueError
        if self.present_luma_ncc_minimum <= self.absent_luma_ncc_maximum:
            raise ValueError
        if (
            self.baseline_support_present_similarity_minimum
            <= self.baseline_support_absent_similarity_maximum
            or self.baseline_support_present_change_maximum
            >= self.baseline_support_absent_change_minimum
            or self.baseline_support_present_ncc_minimum <= self.baseline_support_absent_ncc_maximum
            or self.baseline_support_present_foreground_minimum
            <= self.baseline_support_absent_foreground_maximum
        ):
            raise ValueError
        if self.baseline_support_alignment_mode and (
            self.baseline_support_alignment_max_translation_pixels <= 0
            or self.baseline_support_alignment_min_support_overlap <= 0.0
        ):
            raise ValueError

    def _validate_identity(self) -> None:
        if len(self.efficient_sam_source_commit) != _SOURCE_COMMIT_LENGTH or any(
            character not in _HEX_DIGITS for character in self.efficient_sam_source_commit
        ):
            raise ValueError
        if len(self.checkpoint_sha256) != _DIGEST_LENGTH or any(
            character not in _HEX_DIGITS for character in self.checkpoint_sha256
        ):
            raise ValueError

    def decide(self, comparison: RawComparison) -> ClassificationResult:
        """Map one valid raw comparison to the conservative public state."""
        if comparison.unusable_reason is not None:
            _ = comparison.validate_against(self.minimum_mask_overlap_for_comparison)
            return ClassificationResult(
                outcome=ClassificationOutcome.INDETERMINATE,
                reason_code=comparison.unusable_reason,
                comparison=comparison,
            )
        if comparison.visual_status is not VisualStatus.COMPARABLE:
            raise ValueError
        if comparison.comparison_mode in {
            "baseline_support_v1",
            "baseline_support_v2",
            "baseline_support_v3",
        }:
            if not self.baseline_support_mode:
                raise ValueError
            return _decide_baseline_support(self, comparison)
        if comparison.mask_iou is None or comparison.roi_luma_ncc is None:
            raise ValueError
        _validate_comparable_gates(self, comparison)
        if (
            comparison.mask_iou >= self.present_mask_iou_minimum
            and comparison.roi_luma_ncc >= self.present_luma_ncc_minimum
        ):
            return ClassificationResult(
                outcome=ClassificationOutcome.PRESENT, reason_code=None, comparison=comparison
            )
        if (
            comparison.mask_iou <= self.absent_mask_iou_maximum
            and comparison.roi_luma_ncc <= self.absent_luma_ncc_maximum
        ):
            return ClassificationResult(
                outcome=ClassificationOutcome.ABSENT, reason_code=None, comparison=comparison
            )
        return ClassificationResult(
            outcome=ClassificationOutcome.INDETERMINATE,
            reason_code=VisualReason.INSUFFICIENT_VISUAL_EVIDENCE,
            comparison=comparison,
        )

    @property
    def identity_digest(self) -> str:
        """Return the stable SHA-256 digest of every policy field."""
        fields = cast("dict[str, object]", self.model_dump(mode="json"))
        if not self.baseline_support_mode:
            fields = {
                key: value
                for key, value in fields.items()
                if not key.startswith("baseline_support_")
            }
        serialized = json.dumps(
            fields,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    @property
    def identity(self) -> str:
        """Return the credential-free policy identity."""
        return f"policy-{self.identity_digest}"


def _validate_comparable_gates(
    policy: ObjectPresenceDecisionPolicy, comparison: RawComparison
) -> None:
    if comparison.mask_iou is None:
        raise ValueError
    if comparison.mask_iou < policy.minimum_mask_overlap_for_comparison:
        raise ValueError
    if comparison.effective_comparison_area is None:
        raise ValueError
    if comparison.effective_comparison_area < policy.minimum_comparison_area:
        raise ValueError
    if comparison.roi_pixel_count < policy.minimum_roi_pixels:
        raise ValueError
    if (
        comparison.baseline_mask_pixel_count is None
        or comparison.probe_mask_pixel_count is None
        or comparison.baseline_mask_pixel_count < policy.minimum_clipped_mask_pixels
        or comparison.probe_mask_pixel_count < policy.minimum_clipped_mask_pixels
    ):
        raise ValueError
    if (
        comparison.baseline_mask_coverage is None
        or comparison.probe_mask_coverage is None
        or comparison.baseline_mask_coverage >= policy.maximum_roi_mask_coverage_ratio
        or comparison.probe_mask_coverage >= policy.maximum_roi_mask_coverage_ratio
    ):
        raise ValueError


def _decide_baseline_support(
    policy: ObjectPresenceDecisionPolicy, comparison: RawComparison
) -> ClassificationResult:
    """Apply the successor support-space matrix without probe-mask identity."""
    _validate_baseline_support_gates(policy, comparison)
    similarity = comparison.baseline_support_luma_similarity
    ncc = comparison.baseline_support_luma_ncc
    edge = comparison.baseline_support_edge_similarity
    change = comparison.baseline_support_change_ratio
    foreground = comparison.baseline_support_foreground_retention
    background_change = comparison.baseline_support_background_change_ratio
    if (
        similarity is None
        or ncc is None
        or change is None
        or foreground is None
        or background_change is None
    ):
        raise ValueError
    background_stable = (
        comparison.baseline_support_scene_stable
        if comparison.comparison_mode == "baseline_support_v3"
        and comparison.baseline_support_scene_stable is not None
        else background_change <= policy.baseline_support_background_change_maximum
    )
    if comparison.comparison_mode != "baseline_support_v3" or (
        comparison.baseline_support_alignment_state in {"aligned", "not_required"}
    ):
        alignment_confident = True
    elif comparison.baseline_support_alignment_state is not None:
        alignment_confident = False
    else:
        alignment_confident = (
            comparison.baseline_support_alignment_overlap is not None
            and comparison.baseline_support_alignment_overlap
            >= policy.baseline_support_alignment_min_support_overlap
            and comparison.baseline_support_alignment_margin is not None
            and comparison.baseline_support_alignment_margin
            >= policy.baseline_support_alignment_margin_minimum
        )
    if (
        background_stable
        and alignment_confident
        and similarity >= policy.baseline_support_present_similarity_minimum
        and ncc >= policy.baseline_support_present_ncc_minimum
        and edge is not None
        and edge >= policy.baseline_support_present_edge_minimum
        and change <= policy.baseline_support_present_change_maximum
        and foreground >= policy.baseline_support_present_foreground_minimum
    ):
        return ClassificationResult(
            outcome=ClassificationOutcome.PRESENT, reason_code=None, comparison=comparison
        )
    # v2 treats local-background foreground loss plus low support NCC as the
    # independent absence evidence.  Similarity/change remain diagnostic and
    # are intentionally not required to agree when newly exposed flooring has
    # a similar luma distribution to the former object support.
    if (
        background_stable
        and _absence_alignment_safe(comparison)
        and ncc <= policy.baseline_support_absent_ncc_maximum
        and foreground <= policy.baseline_support_absent_foreground_maximum
        and (
            comparison.comparison_mode in {"baseline_support_v2", "baseline_support_v3"}
            or (
                similarity <= policy.baseline_support_absent_similarity_maximum
                and change >= policy.baseline_support_absent_change_minimum
            )
        )
    ):
        return ClassificationResult(
            outcome=ClassificationOutcome.ABSENT, reason_code=None, comparison=comparison
        )
    return ClassificationResult(
        outcome=ClassificationOutcome.INDETERMINATE,
        reason_code=VisualReason.INSUFFICIENT_VISUAL_EVIDENCE,
        comparison=comparison,
    )


def _absence_alignment_safe(comparison: RawComparison) -> bool:
    """Reject absence when the best transform indicates broad camera motion."""
    if comparison.comparison_mode != "baseline_support_v3":
        return True
    state = comparison.baseline_support_alignment_state
    if state in {None, "aligned", "not_required", "no_valid_candidate"}:
        return True
    dx = comparison.baseline_support_alignment_dx
    dy = comparison.baseline_support_alignment_dy
    rotation = comparison.baseline_support_alignment_rotation_degrees
    return (
        dx is not None
        and dy is not None
        and rotation is not None
        and (
            (
                abs(dx) <= _ABSENCE_ALIGNMENT_MAX_TRANSLATION_PIXELS
                and abs(dy) <= _ABSENCE_ALIGNMENT_MAX_TRANSLATION_PIXELS
                and abs(rotation) < _ABSENCE_ALIGNMENT_MAX_ROTATION_DEGREES
            )
            or (
                comparison.baseline_support_alignment_score is not None
                and comparison.baseline_support_alignment_score
                <= _ABSENCE_ALIGNMENT_NO_CANDIDATE_SCORE_MAXIMUM
            )
        )
    )


def _validate_baseline_support_gates(
    policy: ObjectPresenceDecisionPolicy, comparison: RawComparison
) -> None:
    if comparison.baseline_support_pixel_count is None:
        raise ValueError
    if comparison.baseline_support_pixel_count < policy.minimum_clipped_mask_pixels:
        raise ValueError
    if comparison.roi_pixel_count < policy.minimum_roi_pixels:
        raise ValueError


ClassificationPolicy = ObjectPresenceDecisionPolicy
ObjectPresenceClassificationPolicy = ObjectPresenceDecisionPolicy
