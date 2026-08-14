"""Merged immutable acquisition and Phase 7B classifier policy snapshot."""

from __future__ import annotations

from typing import ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from vigi_vision.durable_io import CanonicalUtc  # noqa: TC001 - Pydantic runtime field type.
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.recording_search_models import RecordingSearchPolicy


class RecordingSearchPolicyV3(BaseModel):
    """The schema-3 policy preserving A2 fields and B1 determinism fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    search_start_utc: CanonicalUtc
    search_end_utc: CanonicalUtc
    maximum_requested_span_seconds: StrictInt = Field(gt=0)
    coarse_interval_seconds: StrictInt = Field(gt=0)
    binary_stop_resolution_seconds: StrictInt = Field(gt=0)
    absence_confirmation_frames: StrictInt = Field(gt=0)
    absence_cadence_seconds: StrictInt = Field(gt=0)
    maximum_consecutive_indeterminate_targets: StrictInt = Field(gt=0)
    acquisition_policy_version: StrictStr = Field(min_length=1, max_length=128)
    classifier_policy_version: StrictStr = Field(min_length=1, max_length=128)
    efficient_sam_source_commit: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    checkpoint_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_rule: StrictStr = Field(min_length=1, max_length=128)
    maximum_roi_mask_coverage_ratio: StrictFloat = Field(gt=0, lt=1)
    minimum_roi_pixels: StrictInt = Field(gt=0)
    minimum_clipped_mask_pixels: StrictInt = Field(gt=0)
    present_mask_iou_minimum: StrictFloat = Field(ge=0, le=1)
    present_luma_ncc_minimum: StrictFloat = Field(ge=-1, le=1)
    absent_mask_iou_maximum: StrictFloat = Field(ge=0, le=1)
    absent_luma_ncc_maximum: StrictFloat = Field(ge=-1, le=1)
    policy_version: StrictStr = Field(min_length=1, max_length=128)
    classifier_preprocessing_version: StrictStr = Field(min_length=1, max_length=128)
    mask_logit_threshold: StrictFloat
    luma_integer_coefficients: tuple[StrictInt, StrictInt, StrictInt]
    luma_integer_divisor: StrictInt = Field(gt=0)
    luma_rounding_rule: StrictStr
    comparison_dtype: StrictStr
    metric_decimal_places: StrictInt = Field(ge=0, le=6)
    metric_rounding_rule: StrictStr
    minimum_mask_overlap_for_comparison: StrictFloat = Field(ge=0, le=1)
    minimum_comparison_area: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def validate_policy(self) -> RecordingSearchPolicyV3:
        """Require the merged snapshot to agree with the pure B1 policy."""
        classifier = self.to_classifier_policy()
        if self.classifier_policy_version != classifier.classifier_policy_version:
            raise ValueError
        if self.policy_version == "":
            raise ValueError
        if self.search_start_utc >= self.search_end_utc:
            raise ValueError
        return self

    @classmethod
    def from_policies(
        cls, acquisition: RecordingSearchPolicy, classifier: ObjectPresenceDecisionPolicy
    ) -> RecordingSearchPolicyV3:
        """Build one strict schema-3 snapshot from A2 and B1 policies."""
        return cls(
            search_start_utc=acquisition.search_start_utc,
            search_end_utc=acquisition.search_end_utc,
            maximum_requested_span_seconds=acquisition.maximum_requested_span_seconds,
            coarse_interval_seconds=acquisition.coarse_interval_seconds,
            binary_stop_resolution_seconds=acquisition.binary_stop_resolution_seconds,
            absence_confirmation_frames=acquisition.absence_confirmation_frames,
            absence_cadence_seconds=acquisition.absence_cadence_seconds,
            maximum_consecutive_indeterminate_targets=acquisition.maximum_consecutive_indeterminate_targets,
            acquisition_policy_version=acquisition.acquisition_policy_version,
            classifier_policy_version=classifier.classifier_policy_version,
            efficient_sam_source_commit=classifier.efficient_sam_source_commit,
            checkpoint_sha256=classifier.checkpoint_sha256,
            prompt_rule=classifier.prompt_rule,
            maximum_roi_mask_coverage_ratio=classifier.maximum_roi_mask_coverage_ratio,
            minimum_roi_pixels=classifier.minimum_roi_pixels,
            minimum_clipped_mask_pixels=classifier.minimum_clipped_mask_pixels,
            present_mask_iou_minimum=classifier.present_mask_iou_minimum,
            present_luma_ncc_minimum=classifier.present_luma_ncc_minimum,
            absent_mask_iou_maximum=classifier.absent_mask_iou_maximum,
            absent_luma_ncc_maximum=classifier.absent_luma_ncc_maximum,
            policy_version=acquisition.policy_version,
            classifier_preprocessing_version=classifier.classifier_preprocessing_version,
            mask_logit_threshold=classifier.mask_logit_threshold,
            luma_integer_coefficients=classifier.luma_integer_coefficients,
            luma_integer_divisor=classifier.luma_integer_divisor,
            luma_rounding_rule=classifier.luma_rounding_rule,
            comparison_dtype=classifier.comparison_dtype,
            metric_decimal_places=classifier.metric_decimal_places,
            metric_rounding_rule=classifier.metric_rounding_rule,
            minimum_mask_overlap_for_comparison=classifier.minimum_mask_overlap_for_comparison,
            minimum_comparison_area=classifier.minimum_comparison_area,
        )

    def to_classifier_policy(self) -> ObjectPresenceDecisionPolicy:
        """Return the exact B1 policy represented by this snapshot."""
        values = {
            key: getattr(self, key)
            for key in ObjectPresenceDecisionPolicy.model_fields
            if hasattr(self, key)
        }
        return ObjectPresenceDecisionPolicy.model_validate(values, strict=True)

    def to_acquisition_policy(self) -> RecordingSearchPolicy:
        """Return the preserved A2 policy subset for existing readers."""
        names = RecordingSearchPolicy.model_fields
        return RecordingSearchPolicy.model_validate(
            {name: getattr(self, name) for name in names}, strict=True
        )
