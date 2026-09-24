"""Phase S3 shadow-only search evidence for successor observations.

The evaluator in this module is deliberately separate from the classifier
state vocabulary.  It consumes the existing reference-relative comparison
matrix and returns an internal band that later candidate-search phases may
use.  It never creates a visual observation or a terminal conclusion.
"""

# The explicit metric contract is intentionally branch-shaped and keeps the
# safety predicates visible in one pure function.
# ruff: noqa: FBT001, PLR0911, PLR0913

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, cast

from vigi_vision.object_presence_policy import (
    _registration_stability_veto,  # pyright: ignore[reportPrivateUsage]
)
from vigi_vision.object_presence_values import VisualStatus

if TYPE_CHECKING:
    from vigi_vision.object_presence_evidence import RawComparison
    from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy


class SearchEvidenceBand(str, Enum):
    """Closed internal S3 search-evidence vocabulary."""

    STRONG_REFERENCE = "STRONG_REFERENCE"
    MATERIAL_DROP = "MATERIAL_DROP"
    USABLE_AMBIGUOUS = "USABLE_AMBIGUOUS"
    INSUFFICIENT = "INSUFFICIENT"


_NCC_ADVANTAGE_BOUND = 2.0


@dataclass(frozen=True, slots=True)
class SearchEvidence:
    """Bounded, reference-relative shadow evidence for one visual comparison."""

    band: SearchEvidenceBand
    reason_code: str
    reference_basis: str = "confirmed_reference"
    scene_stable: bool | None = None
    scene_discontinuity: bool = False
    object_degradation: bool = False
    scene_only_suppressed: bool = False
    fast_present_hit: bool = False
    localized_support_change_advantage: float | None = None
    localized_roi_support_ncc_advantage: float | None = None
    localized_support_drop: bool = False
    registration_stability_veto: bool = False
    registration_veto_overridden: bool = False

    def __post_init__(self) -> None:
        """Validate the closed internal evidence shape."""
        if (
            type(self.band) is not SearchEvidenceBand
            or not self.reason_code
            or self.reference_basis != "confirmed_reference"
            or type(self.scene_discontinuity) is not bool
            or type(self.object_degradation) is not bool
            or type(self.scene_only_suppressed) is not bool
            or type(self.fast_present_hit) is not bool
            or type(self.localized_support_drop) is not bool
            or type(self.registration_stability_veto) is not bool
            or type(self.registration_veto_overridden) is not bool
        ):
            raise ValueError
        if self.localized_support_change_advantage is not None and (
            type(self.localized_support_change_advantage) is not float
            or not math.isfinite(self.localized_support_change_advantage)
            or not -1.0 <= self.localized_support_change_advantage <= 1.0
        ):
            raise ValueError
        if self.localized_roi_support_ncc_advantage is not None and (
            type(self.localized_roi_support_ncc_advantage) is not float
            or not math.isfinite(self.localized_roi_support_ncc_advantage)
            or not -_NCC_ADVANTAGE_BOUND
            <= self.localized_roi_support_ncc_advantage
            <= _NCC_ADVANTAGE_BOUND
        ):
            raise ValueError
        if self.scene_only_suppressed and not self.scene_discontinuity:
            raise ValueError
        if self.band is SearchEvidenceBand.MATERIAL_DROP and (
            not self.object_degradation or self.scene_discontinuity
        ):
            raise ValueError
        if self.localized_support_drop and (
            self.band is not SearchEvidenceBand.MATERIAL_DROP
            or self.scene_stable is not True
            or self.localized_support_change_advantage is None
        ):
            raise ValueError
        if self.registration_veto_overridden and (
            not self.registration_stability_veto or not self.localized_support_drop
        ):
            raise ValueError
        if (
            self.band is SearchEvidenceBand.MATERIAL_DROP
            and self.registration_stability_veto
            and not self.registration_veto_overridden
        ):
            raise ValueError


_BASELINE_SUPPORT_MODES = frozenset(
    {"baseline_support_v1", "baseline_support_v2", "baseline_support_v3"}
)


def evaluate_search_evidence(
    comparison: RawComparison | None,
    policy: ObjectPresenceDecisionPolicy,
    *,
    fast_present_hit: bool = False,
) -> SearchEvidence | None:
    """Classify existing cheap reference metrics into a shadow evidence band.

    The comparison is always interpreted against the confirmed reference and
    ROI.  No previous-frame-only signal is used.  ``None`` means that no valid
    visual comparison exists (for example, an operational failure or a
    missing comparison), so callers must not turn it into search evidence.
    """
    if comparison is None:
        return None
    if comparison.visual_status is VisualStatus.UNUSABLE:
        return _insufficient("unusable_visual_comparison", fast_present_hit)
    if comparison.comparison_mode not in _BASELINE_SUPPORT_MODES:
        return _non_reference_evidence(fast_present_hit)
    if not _has_adequate_reference_support(comparison, policy):
        return _insufficient("insufficient_reference_support", fast_present_hit)
    metrics = _reference_metrics(comparison)
    if metrics is None:
        return _insufficient("missing_reference_support_metrics", fast_present_hit)
    similarity, ncc, edge, change, foreground, background_change = metrics
    scene_stable = comparison.baseline_support_scene_stable
    if scene_stable is None:
        scene_stable = background_change <= policy.baseline_support_background_change_maximum
    (
        localized_support_drop,
        localized_change_advantage,
        localized_ncc_advantage,
        registration_veto,
        registration_veto_overridden,
    ) = _localized_support_drop(
        comparison,
        policy,
        ncc,
        change,
        foreground,
        background_change,
    )
    registration_scene_safe = not registration_veto or registration_veto_overridden
    scene_discontinuity = _scene_discontinuity(
        comparison,
        background_change,
        policy,
        scene_stable,
        localized_support_drop=localized_support_drop,
        registration_scene_safe=registration_scene_safe,
    )
    if _is_strong_reference(
        comparison,
        policy,
        fast_present_hit,
        scene_discontinuity,
        similarity,
        ncc,
        edge,
        change,
        foreground,
    ):
        return SearchEvidence(
            SearchEvidenceBand.STRONG_REFERENCE,
            "reference_support_retained",
            scene_stable=True,
            fast_present_hit=fast_present_hit,
            localized_support_change_advantage=localized_change_advantage,
            localized_roi_support_ncc_advantage=localized_ncc_advantage,
            registration_stability_veto=registration_veto,
            registration_veto_overridden=registration_veto_overridden,
        )
    object_degradation = localized_support_drop or _has_object_degradation(
        policy, similarity, ncc, edge, change, foreground
    )
    if scene_discontinuity:
        return SearchEvidence(
            SearchEvidenceBand.USABLE_AMBIGUOUS,
            "scene_instability_suppressed_direction",
            scene_stable=False,
            scene_discontinuity=True,
            object_degradation=object_degradation,
            scene_only_suppressed=not object_degradation,
            fast_present_hit=fast_present_hit,
            localized_support_change_advantage=localized_change_advantage,
            localized_roi_support_ncc_advantage=localized_ncc_advantage,
            registration_stability_veto=registration_veto,
            registration_veto_overridden=registration_veto_overridden,
        )
    if object_degradation:
        return SearchEvidence(
            SearchEvidenceBand.MATERIAL_DROP,
            (
                "localized_object_reference_support_drop"
                if localized_support_drop
                else "object_reference_support_drop"
            ),
            scene_stable=True,
            object_degradation=True,
            fast_present_hit=fast_present_hit,
            localized_support_change_advantage=localized_change_advantage,
            localized_roi_support_ncc_advantage=localized_ncc_advantage,
            localized_support_drop=localized_support_drop,
            registration_stability_veto=registration_veto,
            registration_veto_overridden=registration_veto_overridden,
        )
    return SearchEvidence(
        SearchEvidenceBand.USABLE_AMBIGUOUS,
        "usable_reference_evidence_not_directional",
        scene_stable=True,
        fast_present_hit=fast_present_hit,
        localized_support_change_advantage=localized_change_advantage,
        localized_roi_support_ncc_advantage=localized_ncc_advantage,
        registration_stability_veto=registration_veto,
        registration_veto_overridden=registration_veto_overridden,
    )


def _insufficient(reason_code: str, fast_present_hit: bool) -> SearchEvidence:
    return SearchEvidence(
        SearchEvidenceBand.INSUFFICIENT,
        reason_code,
        scene_stable=None,
        fast_present_hit=fast_present_hit,
    )


def _non_reference_evidence(fast_present_hit: bool) -> SearchEvidence:
    if fast_present_hit:
        return SearchEvidence(
            SearchEvidenceBand.STRONG_REFERENCE,
            "approved_fast_present",
            scene_stable=True,
            fast_present_hit=True,
        )
    return SearchEvidence(
        SearchEvidenceBand.USABLE_AMBIGUOUS,
        "non_reference_support_comparison",
        scene_stable=None,
    )


def _reference_metrics(comparison: RawComparison) -> tuple[float, ...] | None:
    values = (
        comparison.baseline_support_luma_similarity,
        comparison.baseline_support_luma_ncc,
        comparison.baseline_support_edge_similarity,
        comparison.baseline_support_change_ratio,
        comparison.baseline_support_foreground_retention,
        comparison.baseline_support_background_change_ratio,
    )
    if any(value is None or not math.isfinite(value) for value in values):
        return None
    return tuple(cast("float", value) for value in values)


def _has_adequate_reference_support(
    comparison: RawComparison,
    policy: ObjectPresenceDecisionPolicy,
) -> bool:
    """Reuse the classifier's minimum support and ROI prerequisites."""
    return (
        comparison.baseline_support_pixel_count is not None
        and comparison.baseline_support_pixel_count >= policy.minimum_clipped_mask_pixels
        and comparison.roi_pixel_count >= policy.minimum_roi_pixels
    )


def _scene_discontinuity(
    comparison: RawComparison,
    background_change: float,
    policy: ObjectPresenceDecisionPolicy,
    scene_stable: bool,
    *,
    localized_support_drop: bool,
    registration_scene_safe: bool,
) -> bool:
    return (
        not scene_stable
        or not registration_scene_safe
        or comparison.baseline_support_scene_stability_veto_reason is not None
        or (
            background_change > policy.baseline_support_background_change_maximum
            and not localized_support_drop
        )
    )


def _localized_support_drop(
    comparison: RawComparison,
    policy: ObjectPresenceDecisionPolicy,
    ncc: float,
    change: float,
    foreground: float,
    background_change: float,
) -> tuple[bool, float, float | None, bool, bool]:
    """Detect support-localized change without relaxing the context bound.

    The existing background ratio is an aggregate over fixed context.  A
    moderately noisy context can therefore hide a much stronger change on the
    immutable object support.  The change advantage preserves the original
    background ceiling as the normal path and admits an exception only when
    v3's independent spatial scene profile remains stable and the support
    change exceeds context change by the policy's full PRESENT/ABSENT change
    gap.  This band is search evidence only; it never emits ``ABSENT``.
    """
    advantage = round(change - background_change, policy.metric_decimal_places)
    required_advantage = round(
        policy.baseline_support_absent_change_minimum
        - policy.baseline_support_present_change_maximum,
        policy.metric_decimal_places,
    )
    scene_safe, ncc_advantage, registration_veto, registration_override = _localized_scene_safety(
        comparison, policy, ncc
    )
    localized_drop = (
        scene_safe
        and comparison.baseline_support_replacement_evidence is False
        and background_change > policy.baseline_support_background_change_maximum
        and ncc <= policy.baseline_support_absent_ncc_maximum
        and change >= policy.baseline_support_absent_change_minimum
        and foreground < policy.baseline_support_present_foreground_minimum
        and advantage >= required_advantage
    )
    return (
        localized_drop,
        advantage,
        ncc_advantage,
        registration_veto,
        localized_drop and registration_override,
    )


def _localized_scene_safety(
    comparison: RawComparison,
    policy: ObjectPresenceDecisionPolicy,
    support_ncc: float,
) -> tuple[bool, float | None, bool, bool]:
    """Require complete scene evidence and explicitly resolve registration vetoes.

    A registration veto may be overridden for internal S3 search evidence only
    when the unaligned whole ROI remains PRESENT-like while NCC on the immutable
    object support is ABSENT-like.  That independent separation demonstrates
    localized support loss rather than a coherent whole-ROI camera transform.
    Both bounds come from the existing policy; no new threshold is introduced.
    """
    registration_veto = _registration_stability_veto(comparison)
    if (
        comparison.comparison_mode != "baseline_support_v3"
        or comparison.baseline_support_scene_stable is not True
        or comparison.baseline_support_scene_stability_veto_reason is not None
        or not _complete_registration_evidence(comparison)
    ):
        return False, None, registration_veto, False
    roi_ncc = comparison.roi_luma_ncc
    if roi_ncc is None or not math.isfinite(roi_ncc):
        return False, None, registration_veto, False
    ncc_advantage = round(roi_ncc - support_ncc, policy.metric_decimal_places)
    if not registration_veto:
        return True, ncc_advantage, False, False
    required_ncc_advantage = round(
        policy.present_luma_ncc_minimum - policy.baseline_support_absent_ncc_maximum,
        policy.metric_decimal_places,
    )
    override = (
        roi_ncc >= policy.present_luma_ncc_minimum and ncc_advantage >= required_ncc_advantage
    )
    return override, ncc_advantage, True, override


def _complete_registration_evidence(comparison: RawComparison) -> bool:
    """Reject legacy, missing, or model-copy-corrupted alignment evidence."""
    integer_values = (
        comparison.baseline_support_alignment_dx,
        comparison.baseline_support_alignment_dy,
        comparison.baseline_support_alignment_rotation_degrees,
    )
    float_values = (
        comparison.baseline_support_alignment_overlap,
        comparison.baseline_support_alignment_score,
        comparison.baseline_support_alignment_margin,
    )
    return (
        comparison.baseline_support_alignment_state in {"aligned", "ambiguous"}
        and all(type(value) is int for value in integer_values)
        and all(type(value) is float and math.isfinite(value) for value in float_values)
    )


def _is_strong_reference(
    comparison: RawComparison,
    policy: ObjectPresenceDecisionPolicy,
    fast_present_hit: bool,
    scene_discontinuity: bool,
    similarity: float,
    ncc: float,
    edge: float,
    change: float,
    foreground: float,
) -> bool:
    return not scene_discontinuity and (
        fast_present_hit
        or comparison.baseline_support_present_gate_passed is True
        or (
            similarity >= policy.baseline_support_present_similarity_minimum
            and ncc >= policy.baseline_support_present_ncc_minimum
            and edge >= policy.baseline_support_present_edge_minimum
            and change <= policy.baseline_support_present_change_maximum
            and foreground >= policy.baseline_support_present_foreground_minimum
        )
    )


def _has_object_degradation(
    policy: ObjectPresenceDecisionPolicy,
    similarity: float,
    ncc: float,
    edge: float,
    change: float,
    foreground: float,
) -> bool:
    return foreground <= policy.baseline_support_absent_foreground_maximum and (
        similarity <= policy.baseline_support_absent_similarity_maximum
        or ncc <= policy.baseline_support_absent_ncc_maximum
        or edge < policy.baseline_support_present_edge_minimum
        or change >= policy.baseline_support_absent_change_minimum
    )


__all__ = ("SearchEvidence", "SearchEvidenceBand", "evaluate_search_evidence")
