# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportUnnecessaryIsInstance=false, reportUnreachable=false
# ruff: noqa: ANN401, E501, EM101, TRY003, UP012
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


def canonical_payload(payload: Mapping[str, Any], family: str | None = None) -> str:
    """Return strict canonical JSON for an allow-listed payload."""
    if not isinstance(payload, Mapping):
        raise IdentityValidationError("payload must be an object")
    if family is None and any(key in {"id", "identity", "expected_id"} for key in payload):
        raise IdentityValidationError("identity is self-referential")
    if family is not None:
        if family not in IDENTITY_DOMAINS:
            raise IdentityValidationError("unknown identity family")
        unknown = set(payload) - PAYLOAD_KEYS[family]
        if unknown:
            raise IdentityValidationError("unknown payload key")
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
