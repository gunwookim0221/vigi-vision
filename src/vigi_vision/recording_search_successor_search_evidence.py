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

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, cast

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
        ):
            raise ValueError
        if self.scene_only_suppressed and not self.scene_discontinuity:
            raise ValueError
        if self.band is SearchEvidenceBand.MATERIAL_DROP and (
            not self.object_degradation or self.scene_discontinuity
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
    scene_discontinuity = _scene_discontinuity(
        comparison, background_change, policy, scene_stable
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
        )
    object_degradation = _has_object_degradation(
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
        )
    if object_degradation:
        return SearchEvidence(
            SearchEvidenceBand.MATERIAL_DROP,
            "object_reference_support_drop",
            scene_stable=True,
            object_degradation=True,
            fast_present_hit=fast_present_hit,
        )
    return SearchEvidence(
        SearchEvidenceBand.USABLE_AMBIGUOUS,
        "usable_reference_evidence_not_directional",
        scene_stable=True,
        fast_present_hit=fast_present_hit,
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
    if any(value is None for value in values):
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
) -> bool:
    return (
        not scene_stable
        or comparison.baseline_support_scene_stability_veto_reason is not None
        or background_change > policy.baseline_support_background_change_maximum
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
