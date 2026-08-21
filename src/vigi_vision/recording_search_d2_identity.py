"""Canonical evidence-snapshot serialization and digest functions."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vigi_vision.recording_search_d2_evidence import D2EvidenceSnapshot


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
