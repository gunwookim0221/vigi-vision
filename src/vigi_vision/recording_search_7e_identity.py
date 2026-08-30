# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportUnnecessaryIsInstance=false, reportUnreachable=false
# ruff: noqa: ANN401, C901, DTZ007, E501, EM101, PLR0912, PLR0915, PLR2004, TRY003, UP012
"""Pure identity helpers for the request-relative Phase 7E contract.

The Phase 7E identities deliberately live beside (rather than inside) the
legacy Phase 7D identity helpers.  The payload is an allow-listed value object;
the envelope identity is computed from that payload and is never included in
the bytes being hashed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any


class IdentityValidationError(ValueError):
    """Raised when a Phase 7E identity or canonical payload is invalid."""


# The domain strings are normative and are intentionally not derived from a
# caller supplied value.  Keeping this table closed prevents family
# substitution and accidentally creating identities in a new namespace.
IDENTITY_DOMAINS: dict[str, str] = {
    "policy": "vigi.recording-search.request-relative.policy.v1",
    "classifier-policy": "vigi.recording-search.request-relative.classifier-policy.v1",
    "media-generation-policy": "vigi.recording-search.request-relative.media-generation-policy.v1",
    "coarse-plan": "vigi.recording-search.request-relative.coarse-plan.v1",
    "replay-operation": "vigi.recording-search.request-relative.replay-operation.v1",
    "target-request": "vigi.recording-search.request-relative.target-request.v1",
    "schema5-manifest": "vigi.recording-search.request-relative.schema5-manifest.v1",
    "common-session": "vigi.recording-search.request-relative.common-session.v1",
    "decoder-operation": "vigi.recording-search.request-relative.decoder-operation.v1",
    "frame": "vigi.recording-search.request-relative.frame.v1",
    "classification-operation": "vigi.recording-search.request-relative.classification-operation.v1",
    "observation": "vigi.recording-search.request-relative.observation.v1",
    "alias": "vigi.recording-search.request-relative.alias.v1",
    "support-group": "vigi.recording-search.request-relative.support-group.v1",
    "c2-bracket": "vigi.recording-search.request-relative.c2-bracket.v1",
    "d1-input": "vigi.recording-search.request-relative.d1-input.v1",
    "d1-history": "vigi.recording-search.request-relative.d1-history.v1",
    "narrowed-bracket": "vigi.recording-search.request-relative.narrowed-bracket.v1",
    "schema6-manifest": "vigi.recording-search.request-relative.schema6-manifest.v1",
    "source-record-set": "vigi.recording-search.request-relative.source-record-set.v1",
    "evidence-snapshot": "vigi.recording-search.request-relative.evidence-snapshot.v1",
    "terminal-result": "vigi.recording-search.request-relative.terminal-result.v1",
    "schema7-manifest": "vigi.recording-search.request-relative.schema7-manifest.v1",
    "source-clip": "vigi.recording-search.request-relative.source-clip.v1",
    "phase8-request": "vigi.recording-search.request-relative.phase8-request.v1",
    "phase8-manifest": "vigi.recording-search.request-relative.phase8-manifest.v1",
}

IDENTITY_PREFIXES: dict[str, str] = {
    **{family: f"rr-{family}-v1" for family in IDENTITY_DOMAINS},
    "media-generation-policy": "rr-media-policy-v1",
}

# Union of the closed payload keys in the approved base and supplemental
# vectors.  Nested policy/evidence keys are checked by their own models.
PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "policy": frozenset(
        {
            "schema_family",
            "provenance_level",
            "default_search_duration_seconds",
            "maximum_search_duration_seconds",
            "coarse_interval_seconds",
            "support_count",
            "support_cadence_seconds",
            "binary_stop_seconds",
            "maximum_consecutive_indeterminate_targets",
            "maximum_mp4_bytes",
            "maximum_process_memory_bytes",
            "maximum_selected_rgb24_frames",
            "maximum_targets_per_decoder_pass",
            "maximum_decoder_passes",
            "maximum_classifications",
            "replay_margin_seconds",
            "ffprobe_timeout_seconds",
            "decoder_timeout_seconds",
            "classifier_timeout_seconds",
            "classifier_total_budget_seconds",
            "terminal_interpretation_seconds",
            "publication_seconds",
            "strict_readback_seconds",
            "source_clip_timeout_seconds",
            "cleanup_reserve_seconds",
            "invocation_deadline_seconds",
            "phase8_retry_deadline_seconds",
            "source_clip_pre_seconds",
            "source_clip_post_seconds",
            "maximum_found_interval_seconds",
            "maximum_source_clip_seconds",
            "maximum_source_clip_bytes",
            "maximum_source_frame_rate",
        }
    ),
    "classifier-policy": frozenset(
        {
            "classifier_family",
            "implementation_version",
            "implementation_source_commit",
            "checkpoint_logical_name",
            "checkpoint_sha256",
            "runtime",
            "input",
            "mask",
            "comparison",
            "decision",
            "execution",
        }
    ),
    "media-generation-policy": frozenset(
        {
            "container",
            "stream_copy",
            "reencode",
            "audio",
            "chapters",
            "copied_metadata",
            "interval_tolerance",
            "maximum_frame_rate",
            "maximum_duration_seconds",
            "maximum_size_bytes",
            "timeout_seconds",
        }
    ),
    "coarse-plan": frozenset(
        {
            "investigation_id",
            "run_id",
            "channel_id",
            "policy_id",
            "start_requested_time_utc",
            "end_requested_time_utc",
            "target_requested_times_utc",
        }
    ),
    "replay-operation": frozenset(
        {
            "investigation_id",
            "run_id",
            "policy_id",
            "plan_id",
            "channel_id",
            "segment_id",
            "replay_start_requested_time_utc",
            "replay_end_requested_time_utc",
        }
    ),
    "target-request": frozenset(
        {
            "investigation_id",
            "run_id",
            "plan_id",
            "sequence",
            "kind",
            "requested_time_utc",
            "selection_rule",
            "origin_target_request_id",
        }
    ),
    "schema5-manifest": frozenset(
        {
            "schema_version",
            "investigation_id",
            "run_id",
            "policy_id",
            "plan_id",
            "coarse_target_request_ids",
        }
    ),
    "common-session": frozenset(
        {
            "investigation_id",
            "run_id",
            "replay_operation_id",
            "policy_id",
            "segment_id",
            "replay_start_requested_time_utc",
            "replay_end_requested_time_utc",
            "selected_video_stream_index",
            "container_start_pts",
            "time_base_num",
            "time_base_den",
            "duration_ticks",
            "mp4_size_bytes",
            "mp4_sha256",
            "provenance_level",
            "physical_time_bias",
        }
    ),
    "decoder-operation": frozenset(
        {"investigation_id", "run_id", "common_session_id", "pass_number", "target_request_ids"}
    ),
    "frame": frozenset(
        {
            "investigation_id",
            "run_id",
            "common_session_id",
            "decoder_operation_id",
            "selected_video_stream_index",
            "target_request_id",
            "raw_pts",
            "container_start_pts",
            "time_base_num",
            "time_base_den",
            "estimated_requested_time_utc",
            "ordinal",
            "width",
            "height",
            "jpeg_size_bytes",
            "jpeg_sha256",
            "rgb24_sha256",
        }
    ),
    "classification-operation": frozenset(
        {
            "investigation_id",
            "run_id",
            "frame_id",
            "target_request_id",
            "baseline_identity",
            "classifier_policy_id",
            "attempt",
            "result_kind",
            "outcome",
            "reason_code",
            "classifier_evidence",
            "operational_reason",
        }
    ),
    "observation": frozenset(
        {
            "investigation_id",
            "run_id",
            "common_session_id",
            "classification_operation_id",
            "frame_id",
            "target_request_id",
            "classifier_policy_id",
            "outcome",
            "reason_code",
            "classifier_evidence",
        }
    ),
    "alias": frozenset(
        {
            "investigation_id",
            "run_id",
            "target_request_id",
            "frame_id",
            "alias_of_target_request_id",
        }
    ),
    "support-group": frozenset(
        {
            "investigation_id",
            "run_id",
            "origin_target_request_id",
            "member_target_request_ids",
            "member_frame_ids",
            "member_observation_ids",
            "outcome",
        }
    ),
    "c2-bracket": frozenset(
        {
            "investigation_id",
            "run_id",
            "lower_observation_id",
            "upper_observation_id",
            "upper_support_group_id",
            "status",
        }
    ),
    "d1-input": frozenset({"investigation_id", "run_id", "c2_bracket_id", "policy_id"}),
    "d1-history": frozenset({"investigation_id", "run_id", "d1_input_id", "steps"}),
    "narrowed-bracket": frozenset(
        {
            "investigation_id",
            "run_id",
            "d1_input_id",
            "d1_history_id",
            "lower_observation_id",
            "upper_observation_id",
            "upper_support_group_id",
            "interval_start_requested_time_utc",
            "interval_end_requested_time_utc",
            "stop_reason",
        }
    ),
    "schema6-manifest": frozenset(
        {
            "schema_version",
            "investigation_id",
            "run_id",
            "schema5_predecessor_manifest_id",
            "policy_id",
            "classifier_policy_id",
            "plan_id",
            "replay_operation_id",
            "common_session_id",
            "indexes",
        }
    ),
    "source-record-set": frozenset(
        {
            "schema_version",
            "investigation_id",
            "run_id",
            "schema6_manifest_id",
            "record_count",
            "record_groups",
        }
    ),
    "evidence-snapshot": frozenset(
        {
            "schema_version",
            "investigation_id",
            "run_id",
            "policy_id",
            "classifier_policy_id",
            "narrowed_bracket_id",
            "selected_observation_ids",
            "selected_support_group_ids",
            "source_record_set_id",
        }
    ),
    "terminal-result": frozenset(
        {
            "schema_version",
            "investigation_id",
            "run_id",
            "result_kind",
            "reason_code",
            "interval_start_requested_time_utc",
            "interval_end_requested_time_utc",
            "source_record_set_id",
            "evidence_snapshot_id",
            "common_session_id",
        }
    ),
    "schema7-manifest": frozenset(
        {
            "schema_version",
            "investigation_id",
            "run_id",
            "schema6_predecessor_manifest_id",
            "source_record_set_id",
            "evidence_snapshot_id",
            "terminal_result_id",
        }
    ),
    "source-clip": frozenset(
        {
            "schema_version",
            "investigation_id",
            "run_id",
            "terminal_result_id",
            "common_session_id",
            "input_stream_index",
            "media_generation_policy_id",
            "requested_interval_start_requested_time_utc",
            "requested_interval_end_requested_time_utc",
            "clipped_interval_start_requested_time_utc",
            "clipped_interval_end_requested_time_utc",
        }
    ),
    "phase8-request": frozenset(
        {
            "schema_version",
            "investigation_id",
            "run_id",
            "terminal_result_id",
            "source_clip_id",
            "selected_observation_ids",
            "selected_support_group_ids",
            "clip_integrity",
        }
    ),
    "phase8-manifest": frozenset(
        {
            "schema_version",
            "state",
            "investigation_id",
            "run_id",
            "terminal_result_id",
            "common_session_id",
            "previous_phase8_manifest_id",
            "source_clip_id",
            "clip_integrity",
            "phase8_request_id",
            "failure_reason",
            "common_media_tombstone_name",
            "source_clip_tombstone_name",
            "deletion_result",
        }
    ),
}

_OPTIONAL_PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "target-request": frozenset({"origin_target_request_id"}),
}

_PHASE8_STATE_KEYS: dict[str, frozenset[str]] = {
    "RETRYABLE": frozenset(
        {
            "schema_version",
            "state",
            "investigation_id",
            "run_id",
            "terminal_result_id",
            "common_session_id",
            "previous_phase8_manifest_id",
            "source_clip_id",
            "clip_integrity",
            "phase8_request_id",
            "failure_reason",
        }
    ),
    "CLIP_READY": frozenset(
        {
            "schema_version",
            "state",
            "investigation_id",
            "run_id",
            "terminal_result_id",
            "common_session_id",
            "previous_phase8_manifest_id",
            "source_clip_id",
            "clip_integrity",
        }
    ),
    "READY": frozenset(
        {
            "schema_version",
            "state",
            "investigation_id",
            "run_id",
            "terminal_result_id",
            "common_session_id",
            "previous_phase8_manifest_id",
            "source_clip_id",
            "clip_integrity",
            "phase8_request_id",
        }
    ),
    "DELETING": frozenset(
        {
            "schema_version",
            "state",
            "investigation_id",
            "run_id",
            "terminal_result_id",
            "common_session_id",
            "previous_phase8_manifest_id",
            "source_clip_id",
            "clip_integrity",
            "phase8_request_id",
            "common_media_tombstone_name",
            "source_clip_tombstone_name",
        }
    ),
    "DELETED": frozenset(
        {
            "schema_version",
            "state",
            "investigation_id",
            "run_id",
            "terminal_result_id",
            "common_session_id",
            "previous_phase8_manifest_id",
            "source_clip_id",
            "clip_integrity",
            "phase8_request_id",
            "common_media_tombstone_name",
            "source_clip_tombstone_name",
            "deletion_result",
        }
    ),
}

_NESTED_KEYS: dict[str, dict[str, frozenset[str]]] = {
    "classifier-policy": {
        "runtime": frozenset(
            {
                "python",
                "torch",
                "torchvision",
                "pillow",
                "numpy",
                "device",
                "tensor_dtype",
                "comparison_dtype",
            }
        ),
        "input": frozenset(
            {
                "color_space",
                "channel_order",
                "normalization",
                "resize",
                "interpolation",
                "positive_point_shape",
                "point_label_shape",
                "positive_point_label",
                "prompt",
            }
        ),
        "mask": frozenset(
            {
                "logit_threshold",
                "candidate_selection",
                "must_contain_prompt",
                "minimum_width",
                "minimum_height",
                "minimum_pixel_count",
                "maximum_source_coverage",
                "alignment",
            }
        ),
        "comparison": frozenset(
            {
                "roi_preprocessing",
                "luma_coefficients",
                "luma_divisor",
                "luma_rounding",
                "ncc_area",
                "minimum_overlap_fraction",
                "minimum_effective_area_pixels",
                "metric_rounding",
                "decimal_places",
            }
        ),
        "decision": frozenset(
            {"present_min_iou", "present_min_ncc", "absent_max_iou", "absent_max_ncc", "otherwise"}
        ),
        "execution": frozenset(
            {
                "timeout_seconds",
                "maximum_attempts",
                "maximum_concurrent_attempts",
                "late_result",
                "timeout_result",
                "unknown_result",
                "retry",
            }
        ),
    },
    "media-generation-policy": {
        "stream_copy": frozenset(
            {
                "eligible",
                "requires_single_video",
                "requires_no_audio",
                "requires_same_codec_parameters",
                "requires_interval_bounds",
                "requires_metadata_allowlist",
            }
        ),
        "reencode": frozenset(
            {
                "codec",
                "encoder",
                "profile",
                "level",
                "pixel_format",
                "preset",
                "crf",
                "frame_rate_source",
                "vfr_mode",
                "faststart",
            }
        ),
    },
    "classification-operation": {
        "classifier_evidence": frozenset(
            {
                "baseline_mask_pixel_count",
                "probe_mask_pixel_count",
                "roi_pixel_count",
                "mask_intersection_pixel_count",
                "mask_union_pixel_count",
                "baseline_mask_coverage",
                "probe_mask_coverage",
                "mask_iou",
                "effective_comparison_area",
                "roi_luma_ncc",
                "visual_status",
                "unusable_reason",
            }
        ),
    },
    "observation": {
        "classifier_evidence": frozenset(
            {
                "baseline_mask_pixel_count",
                "probe_mask_pixel_count",
                "roi_pixel_count",
                "mask_intersection_pixel_count",
                "mask_union_pixel_count",
                "baseline_mask_coverage",
                "probe_mask_coverage",
                "mask_iou",
                "effective_comparison_area",
                "roi_luma_ncc",
                "visual_status",
                "unusable_reason",
            }
        ),
    },
    "phase8-manifest": {
        "clip_integrity": frozenset(
            {
                "sha256",
                "size_bytes",
                "observed_duration_ticks",
                "observed_time_base_num",
                "observed_time_base_den",
                "video_stream_index",
                "codec",
                "profile",
                "level",
                "pixel_format",
                "width",
                "height",
                "average_frame_rate_num",
                "average_frame_rate_den",
                "audio_stream_count",
                "generation_outcome",
            }
        ),
    },
    "phase8-request": {
        "clip_integrity": frozenset(
            {
                "sha256",
                "size_bytes",
                "observed_duration_ticks",
                "observed_time_base_num",
                "observed_time_base_den",
                "video_stream_index",
                "codec",
                "profile",
                "level",
                "pixel_format",
                "width",
                "height",
                "average_frame_rate_num",
                "average_frame_rate_den",
                "audio_stream_count",
                "generation_outcome",
            }
        ),
    },
}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^rr-[a-z0-9-]+-v1-[0-9a-f]{64}$")
_UTC_SECOND = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DECIMAL6 = re.compile(r"^-?(?:0|[1-9]\d*)\.\d{6}$")

_INTEGER_FIELDS = frozenset(
    {
        "schema_version",
        "channel_id",
        "sequence",
        "implementation_version",
        "positive_point_label",
        "minimum_width",
        "minimum_height",
        "minimum_pixel_count",
        "minimum_effective_area_pixels",
        "luma_divisor",
        "decimal_places",
        "timeout_seconds",
        "maximum_attempts",
        "maximum_concurrent_attempts",
        "crf",
        "maximum_duration_seconds",
        "maximum_size_bytes",
        "selected_video_stream_index",
        "container_start_pts",
        "time_base_num",
        "time_base_den",
        "duration_ticks",
        "mp4_size_bytes",
        "pass_number",
        "raw_pts",
        "ordinal",
        "width",
        "height",
        "jpeg_size_bytes",
        "attempt",
        "record_count",
        "input_stream_index",
        "size_bytes",
        "observed_duration_ticks",
        "observed_time_base_num",
        "observed_time_base_den",
        "video_stream_index",
        "level",
        "average_frame_rate_num",
        "average_frame_rate_den",
        "audio_stream_count",
        "iteration",
        "decoded_pts",
        "decoded_ordinal",
        "baseline_mask_pixel_count",
        "probe_mask_pixel_count",
        "roi_pixel_count",
        "mask_intersection_pixel_count",
        "mask_union_pixel_count",
        "effective_comparison_area",
        "default_search_duration_seconds",
        "maximum_search_duration_seconds",
        "coarse_interval_seconds",
        "support_count",
        "support_cadence_seconds",
        "binary_stop_seconds",
        "maximum_consecutive_indeterminate_targets",
        "maximum_mp4_bytes",
        "maximum_process_memory_bytes",
        "maximum_selected_rgb24_frames",
        "maximum_targets_per_decoder_pass",
        "maximum_decoder_passes",
        "maximum_classifications",
        "replay_margin_seconds",
        "ffprobe_timeout_seconds",
        "decoder_timeout_seconds",
        "classifier_timeout_seconds",
        "classifier_total_budget_seconds",
        "terminal_interpretation_seconds",
        "publication_seconds",
        "strict_readback_seconds",
        "source_clip_timeout_seconds",
        "cleanup_reserve_seconds",
        "invocation_deadline_seconds",
        "phase8_retry_deadline_seconds",
        "source_clip_pre_seconds",
        "source_clip_post_seconds",
        "maximum_found_interval_seconds",
        "maximum_source_clip_seconds",
        "maximum_source_clip_bytes",
    }
)

_NONNEGATIVE_INTEGER_FIELDS = frozenset(
    {
        "sequence",
        "selected_video_stream_index",
        "container_start_pts",
        "raw_pts",
        "ordinal",
        "input_stream_index",
        "video_stream_index",
        "audio_stream_count",
        "iteration",
        "decoded_pts",
        "decoded_ordinal",
        "observed_duration_ticks",
        "baseline_mask_pixel_count",
        "probe_mask_pixel_count",
        "mask_intersection_pixel_count",
        "mask_union_pixel_count",
        "effective_comparison_area",
    }
)

_NULLABLE_INTEGER_FIELDS = frozenset(
    {
        "baseline_mask_pixel_count",
        "probe_mask_pixel_count",
        "mask_intersection_pixel_count",
        "mask_union_pixel_count",
        "effective_comparison_area",
    }
)

_UTC_FIELDS = frozenset(
    {
        "start_requested_time_utc",
        "end_requested_time_utc",
        "requested_time_utc",
        "replay_start_requested_time_utc",
        "replay_end_requested_time_utc",
        "estimated_requested_time_utc",
        "interval_start_requested_time_utc",
        "interval_end_requested_time_utc",
        "requested_interval_start_requested_time_utc",
        "requested_interval_end_requested_time_utc",
        "clipped_interval_start_requested_time_utc",
        "clipped_interval_end_requested_time_utc",
    }
)

_NULLABLE_UTC_FIELDS = frozenset(
    {
        "interval_start_requested_time_utc",
        "interval_end_requested_time_utc",
    }
)

_DIGEST_FIELDS = frozenset(
    {"checkpoint_sha256", "mp4_sha256", "jpeg_sha256", "rgb24_sha256", "sha256"}
)

_DECIMAL_FIELDS = frozenset(
    {
        "logit_threshold",
        "maximum_source_coverage",
        "minimum_overlap_fraction",
        "present_min_iou",
        "present_min_ncc",
        "absent_max_iou",
        "absent_max_ncc",
        "baseline_mask_coverage",
        "probe_mask_coverage",
        "mask_iou",
        "roi_luma_ncc",
    }
)

_BOOL_FIELDS = frozenset(
    {
        "must_contain_prompt",
        "eligible",
        "requires_single_video",
        "requires_no_audio",
        "requires_same_codec_parameters",
        "requires_interval_bounds",
        "requires_metadata_allowlist",
        "faststart",
    }
)

_STRING_LIST_FIELDS = frozenset(
    {
        "ids",
        "target_requested_times_utc",
        "coarse_target_request_ids",
        "target_request_ids",
        "member_target_request_ids",
        "member_frame_ids",
        "member_observation_ids",
        "selected_observation_ids",
        "selected_support_group_ids",
        "classification_operation_ids",
        "decoder_operation_ids",
        "frame_ids",
        "observation_ids",
        "alias_ids",
        "support_group_ids",
        "c2_bracket_ids",
        "d1_input_ids",
        "d1_history_ids",
        "narrowed_bracket_ids",
    }
)

_POLICY_MAXIMUMS: dict[str, int] = {
    "default_search_duration_seconds": 600,
    "maximum_search_duration_seconds": 600,
    "coarse_interval_seconds": 600,
    "support_count": 32,
    "support_cadence_seconds": 600,
    "binary_stop_seconds": 600,
    "maximum_consecutive_indeterminate_targets": 32,
    "maximum_mp4_bytes": 4_294_967_296,
    "maximum_process_memory_bytes": 2_147_483_648,
    "maximum_selected_rgb24_frames": 12,
    "maximum_targets_per_decoder_pass": 32,
    "maximum_decoder_passes": 11,
    "maximum_classifications": 32,
    "replay_margin_seconds": 40,
    "ffprobe_timeout_seconds": 20,
    "decoder_timeout_seconds": 120,
    "classifier_timeout_seconds": 10,
    "classifier_total_budget_seconds": 320,
    "terminal_interpretation_seconds": 10,
    "publication_seconds": 10,
    "strict_readback_seconds": 20,
    "source_clip_timeout_seconds": 120,
    "cleanup_reserve_seconds": 60,
    "invocation_deadline_seconds": 2_520,
    "phase8_retry_deadline_seconds": 180,
    "source_clip_pre_seconds": 10,
    "source_clip_post_seconds": 30,
    "maximum_found_interval_seconds": 1,
    "maximum_source_clip_seconds": 41,
    "maximum_source_clip_bytes": 536_870_912,
}


def _required_payload_keys(family: str, payload: Mapping[str, Any]) -> frozenset[str]:
    if family == "phase8-manifest":
        state = payload.get("state")
        if type(state) is not str or state not in _PHASE8_STATE_KEYS:
            raise IdentityValidationError("invalid phase8 state")
        return _PHASE8_STATE_KEYS[state]
    return PAYLOAD_KEYS[family] - _OPTIONAL_PAYLOAD_KEYS.get(family, frozenset())


def _validate_payload_contract(payload: Mapping[str, Any], family: str) -> None:
    required = _required_payload_keys(family, payload)
    present = frozenset(payload)
    optional = _OPTIONAL_PAYLOAD_KEYS.get(family, frozenset())
    if not required.issubset(present) or not present.issubset(required | optional):
        raise IdentityValidationError("invalid payload key set")
    _validate_value_types(payload)
    _validate_family_contract(payload, family)


def _validate_value_types(value: Mapping[str, Any]) -> None:
    for key, child in value.items():
        if key in _INTEGER_FIELDS:
            if key == "level" and type(child) is str and child == "4.1":
                continue
            if child is None and key in _NULLABLE_INTEGER_FIELDS:
                continue
            if type(child) is not int:
                raise IdentityValidationError("invalid integer field")
            if key in _NONNEGATIVE_INTEGER_FIELDS:
                if child < 0:
                    raise IdentityValidationError("negative integer field")
            elif child <= 0:
                raise IdentityValidationError("non-positive integer field")
        elif key in _BOOL_FIELDS:
            if type(child) is not bool:
                raise IdentityValidationError("invalid boolean field")
        elif key in _UTC_FIELDS:
            if child is None and key in _NULLABLE_UTC_FIELDS:
                continue
            _validate_utc_second(child)
        elif key in _DIGEST_FIELDS:
            if type(child) is not str or _HEX64.fullmatch(child) is None:
                raise IdentityValidationError("invalid digest field")
        elif key in _DECIMAL_FIELDS:
            if child is not None and (type(child) is not str or _DECIMAL6.fullmatch(child) is None):
                raise IdentityValidationError("invalid decimal field")
        elif key in _STRING_LIST_FIELDS:
            if type(child) is not list or any(type(item) is not str or not item for item in child):
                raise IdentityValidationError("invalid ordered string list")
            if len(child) != len(set(child)):
                raise IdentityValidationError("duplicate ordered member")
        elif isinstance(child, Mapping):
            _validate_value_types(child)
        elif type(child) is list:
            _validate_list_value(key, child)
        elif child is not None and type(child) is not str:
            raise IdentityValidationError("invalid payload field type")
        elif type(child) is str and not child:
            raise IdentityValidationError("empty payload field")


def _validate_list_value(key: str, value: list[Any]) -> None:
    if key == "schema_family":
        if value != [5, 6, 7] or any(type(item) is not int for item in value):
            raise IdentityValidationError("invalid schema family")
    elif key in {"maximum_source_frame_rate", "maximum_frame_rate", "luma_coefficients"}:
        if not value or any(type(item) is not int or item <= 0 for item in value):
            raise IdentityValidationError("invalid integer list")
        if key != "luma_coefficients" and (len(value) != 2 or value[1] == 0):
            raise IdentityValidationError("invalid rational")
    elif key in {"positive_point_shape", "point_label_shape"}:
        if not value or any(type(item) is not int or item <= 0 for item in value):
            raise IdentityValidationError("invalid tensor shape")
    elif key == "record_groups":
        if not value:
            raise IdentityValidationError("empty source record groups")
        for item in value:
            if not isinstance(item, Mapping) or set(item) != {"type", "ids"}:
                raise IdentityValidationError("invalid source record group")
            _validate_value_types(item)
    elif key == "steps":
        if any(not isinstance(item, Mapping) for item in value):
            raise IdentityValidationError("invalid D1 history step")
        for item in value:
            _validate_value_types(item)
    elif key == "evidence":
        if any(not isinstance(item, Mapping) for item in value):
            raise IdentityValidationError("invalid D1 history evidence")
        for item in value:
            _validate_value_types(item)
    elif key == "support_indexes":
        if any(type(item) is not int or item < 0 for item in value):
            raise IdentityValidationError("invalid D1 support indexes")
        if len(value) != len(set(value)):
            raise IdentityValidationError("duplicate D1 support index")
    else:
        raise IdentityValidationError("unsupported list field")


def _validate_utc_second(value: Any) -> None:
    if type(value) is not str or _UTC_SECOND.fullmatch(value) is None:
        raise IdentityValidationError("invalid UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise IdentityValidationError("invalid UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise IdentityValidationError("non-canonical UTC timestamp")


def _validate_family_contract(payload: Mapping[str, Any], family: str) -> None:
    expected_versions = {
        "schema5-manifest": 5,
        "schema6-manifest": 6,
        "schema7-manifest": 7,
        "source-record-set": 1,
        "evidence-snapshot": 1,
        "terminal-result": 1,
        "source-clip": 1,
        "phase8-request": 1,
        "phase8-manifest": 1,
    }
    if family in expected_versions and payload["schema_version"] != expected_versions[family]:
        raise IdentityValidationError("invalid schema version")
    if family == "policy":
        _validate_policy(payload)
    elif family == "classifier-policy":
        _validate_classifier_policy(payload)
    elif family == "media-generation-policy":
        _validate_media_policy(payload)
    elif family == "coarse-plan":
        _validate_time_interval(payload, "start_requested_time_utc", "end_requested_time_utc")
        targets = payload["target_requested_times_utc"]
        if not targets or targets != sorted(targets):
            raise IdentityValidationError("invalid coarse targets")
        if (
            targets[0] < payload["start_requested_time_utc"]
            or targets[-1] > payload["end_requested_time_utc"]
        ):
            raise IdentityValidationError("coarse target outside interval")
    elif family == "replay-operation":
        _validate_time_interval(
            payload, "replay_start_requested_time_utc", "replay_end_requested_time_utc"
        )
    elif family == "target-request":
        _validate_target_request(payload)
    elif family == "common-session":
        _validate_time_interval(
            payload, "replay_start_requested_time_utc", "replay_end_requested_time_utc"
        )
        if (
            payload["provenance_level"] != "REQUEST_RELATIVE_ESTIMATE"
            or payload["physical_time_bias"] != "UNKNOWN_UNBOUNDED"
        ):
            raise IdentityValidationError("invalid request-relative provenance")
    elif family == "frame":
        if payload["time_base_num"] <= 0 or payload["time_base_den"] <= 0:
            raise IdentityValidationError("invalid frame time base")
        if (
            payload["width"] > 16_384
            or payload["height"] > 16_384
            or payload["jpeg_size_bytes"] > 268_435_456
        ):
            raise IdentityValidationError("frame resource limit exceeded")
    elif family in {"classification-operation", "observation"}:
        _validate_classification_payload(payload, family)
    elif family == "support-group":
        lengths = {
            len(payload[name])
            for name in ("member_target_request_ids", "member_frame_ids", "member_observation_ids")
        }
        if lengths != {3} or payload["outcome"] != "SUPPORTED_ABSENT":
            raise IdentityValidationError("invalid support group")
    elif family == "c2-bracket" and payload["status"] != "BRACKET_READY":
        raise IdentityValidationError("invalid C2 bracket")
    elif family == "narrowed-bracket":
        _validate_time_interval(
            payload, "interval_start_requested_time_utc", "interval_end_requested_time_utc"
        )
        if payload["stop_reason"] not in {
            "TARGET_PRECISION_REACHED",
            "NO_DISTINCT_MIDPOINT",
            "MAXIMUM_ITERATIONS",
        }:
            raise IdentityValidationError("invalid narrowing stop reason")
    elif family == "terminal-result":
        _validate_terminal_result(payload)
    elif family == "schema6-manifest":
        expected_indexes = {
            "target_request_ids",
            "decoder_operation_ids",
            "frame_ids",
            "classification_operation_ids",
            "observation_ids",
            "alias_ids",
            "support_group_ids",
            "c2_bracket_ids",
            "d1_input_ids",
            "d1_history_ids",
            "narrowed_bracket_ids",
        }
        if set(payload["indexes"]) != expected_indexes:
            raise IdentityValidationError("invalid schema6 indexes")
    elif family == "source-record-set":
        groups = payload["record_groups"]
        if payload["record_count"] != sum(len(group["ids"]) for group in groups):
            raise IdentityValidationError("source record count mismatch")
    elif family == "source-clip":
        _validate_time_interval(
            payload,
            "requested_interval_start_requested_time_utc",
            "requested_interval_end_requested_time_utc",
        )
        _validate_time_interval(
            payload,
            "clipped_interval_start_requested_time_utc",
            "clipped_interval_end_requested_time_utc",
        )
    elif family in {"phase8-request", "phase8-manifest"}:
        _validate_clip_integrity(payload.get("clip_integrity"))


def _validate_time_interval(payload: Mapping[str, Any], start_key: str, end_key: str) -> None:
    if payload[start_key] >= payload[end_key]:
        raise IdentityValidationError("invalid half-open interval")


def _validate_policy(payload: Mapping[str, Any]) -> None:
    if (
        payload["schema_family"] != [5, 6, 7]
        or payload["provenance_level"] != "REQUEST_RELATIVE_ESTIMATE"
    ):
        raise IdentityValidationError("invalid policy family")
    for key, maximum in _POLICY_MAXIMUMS.items():
        value = payload[key]
        if type(value) is not int or value <= 0 or value > maximum:
            raise IdentityValidationError("policy limit exceeds approved ceiling")
    if payload["maximum_search_duration_seconds"] != 600:
        raise IdentityValidationError("maximum search duration must be 600 seconds")
    if payload["default_search_duration_seconds"] > payload["maximum_search_duration_seconds"]:
        raise IdentityValidationError("default search duration exceeds maximum")
    if payload["maximum_selected_rgb24_frames"] > payload["maximum_targets_per_decoder_pass"]:
        raise IdentityValidationError("frame retention exceeds decoder target ceiling")
    if (
        payload["classifier_total_budget_seconds"]
        > payload["maximum_classifications"] * payload["classifier_timeout_seconds"]
    ):
        raise IdentityValidationError("classifier budget exceeds per-call ceiling")
    if payload["invocation_deadline_seconds"] <= payload["cleanup_reserve_seconds"]:
        raise IdentityValidationError("invocation deadline lacks cleanup reserve")
    if payload["maximum_source_frame_rate"] != [60, 1]:
        raise IdentityValidationError("invalid source frame-rate ceiling")


def _validate_classifier_policy(payload: Mapping[str, Any]) -> None:
    if payload["implementation_version"] != 1:
        raise IdentityValidationError("invalid classifier implementation version")
    if payload["input"]["positive_point_shape"] != [1, 1, 1, 2] or payload["input"][
        "point_label_shape"
    ] != [1, 1, 1]:
        raise IdentityValidationError("invalid classifier prompt shape")
    if (
        payload["execution"]["maximum_attempts"] != 1
        or payload["execution"]["maximum_concurrent_attempts"] != 1
    ):
        raise IdentityValidationError("invalid classifier attempt policy")
    if payload["execution"]["timeout_seconds"] > 10:
        raise IdentityValidationError("classifier timeout exceeds approved ceiling")
    if payload["mask"]["maximum_source_coverage"] != "0.950000":
        raise IdentityValidationError("invalid background-dominant ceiling")


def _validate_media_policy(payload: Mapping[str, Any]) -> None:
    if payload["container"] != "mp4" or payload["maximum_frame_rate"] != [60, 1]:
        raise IdentityValidationError("invalid media policy")
    if (
        payload["maximum_duration_seconds"] > 41
        or payload["maximum_size_bytes"] > 536_870_912
        or payload["timeout_seconds"] > 120
    ):
        raise IdentityValidationError("media policy exceeds approved ceiling")


def _validate_target_request(payload: Mapping[str, Any]) -> None:
    kind = payload["kind"]
    rule = payload["selection_rule"]
    if kind not in {"COARSE", "SUPPORT", "BINARY"} or rule not in {
        "NEAREST_IN_HALF_OPEN_SESSION",
        "FINAL_STRICTLY_BEFORE_END",
    }:
        raise IdentityValidationError("invalid target request")
    origin_present = "origin_target_request_id" in payload
    if (kind == "SUPPORT") is not origin_present:
        raise IdentityValidationError("invalid support-origin binding")
    if origin_present and (
        type(payload["origin_target_request_id"]) is not str
        or not payload["origin_target_request_id"]
    ):
        raise IdentityValidationError("invalid support-origin identity")


def _validate_classification_payload(payload: Mapping[str, Any], family: str) -> None:
    if family == "classification-operation":
        kind = payload["result_kind"]
        if kind == "OPERATIONAL":
            if any(
                payload[key] is not None
                for key in ("outcome", "reason_code", "classifier_evidence")
            ) or payload["operational_reason"] not in {
                "classifier_timeout",
                "classification_failed",
                "invalid_classifier_result",
            }:
                raise IdentityValidationError("invalid operational classification")
            return
        if kind != "VISUAL" or payload["operational_reason"] is not None:
            raise IdentityValidationError("invalid classification result kind")
    evidence = payload["classifier_evidence"]
    if not isinstance(evidence, Mapping):
        raise IdentityValidationError("missing classifier evidence")
    _validate_classifier_evidence(evidence)
    outcome = payload["outcome"]
    reason = payload["reason_code"]
    if outcome not in {"PRESENT", "ABSENT", "INDETERMINATE"}:
        raise IdentityValidationError("invalid visual outcome")
    if outcome in {"PRESENT", "ABSENT"} and reason is not None:
        raise IdentityValidationError("terminal visual state has a reason")
    if outcome == "INDETERMINATE" and type(reason) is not str:
        raise IdentityValidationError("indeterminate state lacks reason")
    status = evidence["visual_status"]
    if outcome in {"PRESENT", "ABSENT"} and status != "comparable":
        raise IdentityValidationError("terminal visual state is not comparable")
    if outcome == "INDETERMINATE":
        expected_reason = (
            "insufficient_visual_evidence"
            if status == "comparable"
            else evidence["unusable_reason"]
        )
        if reason != expected_reason:
            raise IdentityValidationError("indeterminate reason does not match evidence")


def _validate_classifier_evidence(evidence: Mapping[str, Any]) -> None:
    expected = _NESTED_KEYS["classification-operation"]["classifier_evidence"]
    if set(evidence) != set(expected):
        raise IdentityValidationError("invalid classifier evidence key set")
    _validate_value_types(evidence)
    status = evidence["visual_status"]
    reason = evidence["unusable_reason"]
    metric_keys = expected - {"roi_pixel_count", "visual_status", "unusable_reason"}
    if status == "comparable":
        if reason is not None or any(evidence[key] is None for key in metric_keys):
            raise IdentityValidationError("invalid comparable evidence")
    elif status == "unusable":
        allowed = {
            "invalid_mask",
            "background_dominant",
            "insufficient_mask_overlap",
            "insufficient_comparison_area",
            "zero_luma_variance",
        }
        if reason not in allowed:
            raise IdentityValidationError("invalid unusable reason")
        fields = {
            "baseline_mask_pixel_count",
            "probe_mask_pixel_count",
            "mask_intersection_pixel_count",
            "mask_union_pixel_count",
            "baseline_mask_coverage",
            "probe_mask_coverage",
            "mask_iou",
            "effective_comparison_area",
            "roi_luma_ncc",
        }
        allowed_fields = {
            "invalid_mask": set(),
            "background_dominant": {
                "baseline_mask_pixel_count",
                "probe_mask_pixel_count",
                "baseline_mask_coverage",
                "probe_mask_coverage",
            },
            "insufficient_mask_overlap": fields - {"effective_comparison_area", "roi_luma_ncc"},
            "insufficient_comparison_area": fields - {"roi_luma_ncc"},
            "zero_luma_variance": fields - {"roi_luma_ncc"},
        }[reason]
        if any((key in allowed_fields) != (evidence[key] is not None) for key in fields):
            raise IdentityValidationError("invalid unusable evidence row")
    else:
        raise IdentityValidationError("invalid visual status")


def _validate_terminal_result(payload: Mapping[str, Any]) -> None:
    kind = payload["result_kind"]
    if kind not in {"FOUND", "NOT_FOUND", "INCONCLUSIVE"}:
        raise IdentityValidationError("invalid terminal result")
    start = payload["interval_start_requested_time_utc"]
    end = payload["interval_end_requested_time_utc"]
    if (start is None) != (end is None):
        raise IdentityValidationError("partial terminal interval")
    if start is not None:
        _validate_utc_second(start)
        _validate_utc_second(end)
        if start >= end:
            raise IdentityValidationError("invalid terminal interval")


def _validate_clip_integrity(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or set(value) != set(
        _NESTED_KEYS["phase8-request"]["clip_integrity"]
    ):
        raise IdentityValidationError("invalid clip integrity")
    _validate_value_types(value)
    if type(value["level"]) is not int:
        raise IdentityValidationError("invalid clip level")


def canonical_payload(payload: Mapping[str, Any], family: str | None = None) -> str:
    """Return strict canonical JSON for an allow-listed payload."""
    if not isinstance(payload, Mapping):
        raise IdentityValidationError("payload must be an object")
    if family is None and any(key in {"id", "identity", "expected_id"} for key in payload):
        raise IdentityValidationError("identity is self-referential")
    if family is not None:
        if family not in IDENTITY_DOMAINS:
            raise IdentityValidationError("unknown identity family")
        _validate_payload_contract(payload, family)
        if _contains_self_field(payload, family):
            raise IdentityValidationError("identity is self-referential")
        _validate_nested_keys(payload, family)
    _reject_noncanonical_numbers(payload)
    try:
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise IdentityValidationError("payload is not canonical JSON") from exc


def identity_for(family: str, payload: Mapping[str, Any]) -> str:
    """Construct a domain-separated, lowercase SHA-256 Phase 7E identity."""
    domain = IDENTITY_DOMAINS.get(family)
    prefix = IDENTITY_PREFIXES.get(family)
    if domain is None or prefix is None:
        raise IdentityValidationError("unknown identity family")
    canonical = canonical_payload(payload, family)
    digest = hashlib.sha256(f"{domain}\0{canonical}".encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}"


def validate_identity(family: str, identity: str, payload: Mapping[str, Any]) -> str:
    """Validate an envelope identity against its exact payload."""
    expected_prefix = IDENTITY_PREFIXES.get(family)
    if (
        expected_prefix is None
        or not isinstance(identity, str)
        or not identity.startswith(expected_prefix + "-")
    ):
        raise IdentityValidationError("identity family mismatch")
    if not _ID.fullmatch(identity) or not _HEX64.fullmatch(identity.rsplit("-", 1)[1]):
        raise IdentityValidationError("invalid identity")
    expected = identity_for(family, payload)
    if expected != identity:
        raise IdentityValidationError("identity digest mismatch")
    return identity


def family_from_identity(identity: str) -> str:
    """Return the unique family represented by an identity string."""
    for family, prefix in IDENTITY_PREFIXES.items():
        if identity.startswith(prefix + "-"):
            return family
    raise IdentityValidationError("unknown identity")


def _contains_self_field(payload: Mapping[str, Any], family: str) -> bool:
    self_names = {
        "identity",
        "expected_id",
        "id",
        f"{family.replace('-', '_')}_id",
    }
    if family == "media-generation-policy":
        self_names.add("media_policy_id")
    return any(key in self_names for key in payload)


def _validate_nested_keys(payload: Mapping[str, Any], family: str) -> None:
    for key, allowed in _NESTED_KEYS.get(family, {}).items():
        child = payload.get(key)
        if child is None:
            continue
        if not isinstance(child, Mapping) or set(child) != set(allowed):
            raise IdentityValidationError("unknown nested payload key")


def _reject_noncanonical_numbers(value: Any) -> None:
    if isinstance(value, float):
        raise IdentityValidationError("floating point values are not canonical")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise IdentityValidationError("object keys must be strings")
            _reject_noncanonical_numbers(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_noncanonical_numbers(child)


def strict_json_loads(text: str) -> dict[str, Any]:
    """Parse one JSON object while rejecting duplicate keys."""
    if type(text) is not str:
        raise IdentityValidationError("JSON input must be text")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise IdentityValidationError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IdentityValidationError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise IdentityValidationError("JSON root must be an object")
    return value


__all__ = [
    "IDENTITY_DOMAINS",
    "IDENTITY_PREFIXES",
    "PAYLOAD_KEYS",
    "IdentityValidationError",
    "canonical_payload",
    "family_from_identity",
    "identity_for",
    "strict_json_loads",
    "validate_identity",
]
