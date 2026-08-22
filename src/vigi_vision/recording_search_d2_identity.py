"""Canonical evidence-snapshot serialization and digest functions."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
    from vigi_vision.recording_search_d2_evidence import D2EvidenceSnapshot


_AUTHORITATIVE_SOURCE_DOMAIN = "vigi-vision-recording-search-authoritative-source-v1"


def canonical_evidence_snapshot_json(snapshot: D2EvidenceSnapshot) -> str:
    """Serialize the allowlisted snapshot using the D2-0 canonical JSON rules."""
    return json.dumps(
        snapshot.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def evidence_snapshot_digest(snapshot: D2EvidenceSnapshot) -> str:
    """Return the lowercase SHA-256 of canonical snapshot UTF-8 bytes."""
    return hashlib.sha256(canonical_evidence_snapshot_json(snapshot).encode("utf-8")).hexdigest()


def authoritative_source_digest(
    root: Path, run_path: Path, predecessor: RecordingSearchManifestV3
) -> str:
    """Digest the complete strict schema-2/schema-3 source record set.

    The manifest indexes preserve authoritative order; child records are
    serialized in deterministic identifier order.  The domain prefix and
    explicit payload schema prevent this digest from being confused with any
    result or evidence-snapshot digest.
    """
    from vigi_vision.recording_search_a2_repository import (  # noqa: PLC0415
        read_schema2_children,
    )
    from vigi_vision.recording_search_b2_validation import (  # noqa: PLC0415
        read_schema3_children,
    )

    acquisition, frames, requests = read_schema2_children(root, run_path, predecessor.as_schema2())
    baseline, classification_operations, observations, aliases = read_schema3_children(
        root, run_path, predecessor
    )
    payload = {
        "identity_schema": _AUTHORITATIVE_SOURCE_DOMAIN,
        "manifest": predecessor.model_dump(mode="json"),
        "baseline": baseline.model_dump(mode="json"),
        "acquisition_operations": [
            acquisition[key].model_dump(mode="json") for key in sorted(acquisition)
        ],
        "probe_requests": [requests[key].model_dump(mode="json") for key in sorted(requests)],
        "canonical_frames": [frames[key].model_dump(mode="json") for key in sorted(frames)],
        "classification_operations": [
            classification_operations[key].model_dump(mode="json")
            for key in sorted(classification_operations)
        ],
        "observations": [observations[key].model_dump(mode="json") for key in sorted(observations)],
        "aliases": [aliases[key].model_dump(mode="json") for key in sorted(aliases)],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(
        f"{_AUTHORITATIVE_SOURCE_DOMAIN}\0{canonical}".encode("utf-8")  # noqa: UP012
    ).hexdigest()
