"""Deterministic Phase 7B schema-3 identities and path keys."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Final

_BASELINE_PREFIX: Final = "baseline-"
_OBSERVATION_PREFIX: Final = "observation-"
_ALIAS_PREFIX: Final = "observation-alias-"
_OPERATION_PATTERN: Final = re.compile(r"^classification-op-[a-z0-9-]{1,96}$")
_BASELINE_PATTERN: Final = re.compile(r"^baseline-[0-9a-f]{64}$")
_OBSERVATION_PATTERN: Final = re.compile(r"^observation-[0-9a-f]{64}$")
_ALIAS_PATTERN: Final = re.compile(r"^observation-alias-[0-9a-f]{64}$")


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def baseline_observation_id_for(  # noqa: PLR0913
    *,
    investigation_id: str,
    search_run_id: str,
    channel_id: int,
    reference_frame_resource_id: str,
    reference_requested_time_utc: datetime,
    source_width: int,
    source_height: int,
    roi: dict[str, object],
    jpeg_sha256: str,
    jpeg_size_bytes: int,
) -> str:
    """Return the approved baseline identity over its ordered semantic fields."""
    payload = {
        "record_type": "confirmed_reference_baseline",
        "investigation_id": investigation_id,
        "search_run_id": search_run_id,
        "channel_id": channel_id,
        "reference_frame_resource_id": reference_frame_resource_id,
        "reference_requested_time_utc": _utc_text(reference_requested_time_utc),
        "source_width": source_width,
        "source_height": source_height,
        "roi": roi,
        "jpeg_sha256": jpeg_sha256,
        "jpeg_size_bytes": jpeg_size_bytes,
    }
    return _BASELINE_PREFIX + _digest(payload)


def observation_id_for(  # noqa: PLR0913
    *,
    investigation_id: str,
    search_run_id: str,
    channel_id: int,
    baseline_observation_id: str,
    canonical_frame_id: str,
    classifier_policy_version: str,
) -> str:
    """Return the approved semantic canonical-observation identity."""
    payload = {
        "record_type": "recording_probe",
        "investigation_id": investigation_id,
        "search_run_id": search_run_id,
        "channel_id": channel_id,
        "baseline_observation_id": baseline_observation_id,
        "canonical_frame_id": canonical_frame_id,
        "classifier_policy_version": classifier_policy_version,
    }
    return _OBSERVATION_PREFIX + _digest(payload)


def alias_id_for(search_run_id: str, probe_request_id: str, canonical_observation_id: str) -> str:
    """Return the approved request-to-observation alias identity."""
    return _ALIAS_PREFIX + _digest([search_run_id, probe_request_id, canonical_observation_id])


def is_baseline_id(value: str) -> bool:
    """Return whether value has the closed baseline identity shape."""
    return _BASELINE_PATTERN.fullmatch(value) is not None


def is_observation_id(value: str) -> bool:
    """Return whether value has the closed canonical observation shape."""
    return _OBSERVATION_PATTERN.fullmatch(value) is not None


def is_alias_id(value: str) -> bool:
    """Return whether value has the closed observation-alias shape."""
    return _ALIAS_PATTERN.fullmatch(value) is not None


def is_classification_operation_id(value: str) -> bool:
    """Return whether value has the closed classification-operation shape."""
    return _OPERATION_PATTERN.fullmatch(value) is not None
