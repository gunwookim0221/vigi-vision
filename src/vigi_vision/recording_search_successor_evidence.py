"""Durable, identity-bound visual evidence for Schema 8 successor runs."""

# This module is a deliberately explicit persistence boundary.
# ruff: noqa: C901, D102, D107, EM101, PLR0912, PLR0915, PLR2004, PLC0415, PTH105, PTH108, SIM105, TC003
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false, reportUnannotatedClassAttribute=false, reportImportCycles=false, reportUnusedCallResult=false, reportMissingImports=false, reportUnnecessaryIsInstance=false, reportUnnecessaryComparison=false, reportOperatorIssue=false

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from vigi_vision.recording_search_successor_classification import (
        SuccessorObservation,
    )

    SuccessorPreparedExecution = Any
    SuccessorTerminal = Any

EVIDENCE_VERSION = "phase7e-successor-evidence-v1"
MAX_EVIDENCE_RECORDS = 64
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
_SHA256 = frozenset("0123456789abcdef")
_INVESTIGATION_ID = re.compile(r"^object-disappearance-v3-ch[1-9][0-9]*-[0-9]{8}T[0-9]{6}Z$")
_RUN_ID = re.compile(r"^search-run-[0-9a-f]{32}$")
_ENTRY_ROLES = frozenset({"baseline", "baseline_link", "anchor", "observation"})
_ACQUISITION_STATUSES = frozenset(
    {
        "FRAME_AVAILABLE",
        "UNAVAILABLE_GAP",
        "RECORDING_UNAVAILABLE",
        "REPLAY_TIMEOUT",
        "REPLAY_FAILED",
        "DECODE_TIMEOUT",
        "DECODE_UNAVAILABLE",
    }
)
_OBSERVATION_STATES = frozenset(
    {
        "PRESENT",
        "ABSENT",
        "INDETERMINATE",
        "UNAVAILABLE_GAP",
        "RECORDING_UNAVAILABLE",
        "REPLAY_TIMEOUT",
        "REPLAY_FAILED",
        "DECODE_TIMEOUT",
        "DECODE_UNAVAILABLE",
        "CLASSIFIER_TIMEOUT",
        "CLASSIFIER_FAILED",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "version",
        "investigation_id",
        "run_id",
        "plan_id",
        "authority_identity",
        "roi_identity",
        "roi",
        "source_width",
        "source_height",
        "terminal_status",
        "terminal_reason",
        "last_present_observation_id",
        "first_absent_observation_id",
        "review_clip",
        "entries",
    }
)
_ENTRY_KEYS = frozenset(
    {
        "role",
        "plan_id",
        "observation_id",
        "target_id",
        "acquisition_id",
        "assigned_segment_id",
        "requested_time_utc",
        "frame_utc",
        "frame_pts_seconds",
        "frame_ordinal",
        "frame_offset_seconds",
        "digest",
        "width",
        "height",
        "authority_identity",
        "reference_frame_resource_id",
        "roi_identity",
        "classifier_policy_identity",
        "acquisition_status",
        "state",
        "reason_code",
        "comparison",
        "classifier_stage",
        "classifier_elapsed_ms",
        "path",
        "roi_path",
        "roi_digest",
        "acquisition_mode",
        "target_delta_ms",
        "cadence_source",
        "cadence_ms",
        "tolerance_ms",
        "raw_segment_end_utc",
        "media_validation_outcome",
    }
)
_EXTENDED_ENTRY_KEYS = _ENTRY_KEYS | frozenset(
    {"fallback_used", "fallback_reason", "observability"}
)
_LEGACY_ENTRY_KEYS = _ENTRY_KEYS - {
    "acquisition_mode",
    "target_delta_ms",
    "cadence_source",
    "cadence_ms",
    "tolerance_ms",
    "raw_segment_end_utc",
    "media_validation_outcome",
}
_COMPARISON_KEYS = frozenset(
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
        "comparison_mode",
        "baseline_support_pixel_count",
        "baseline_support_luma_similarity",
        "baseline_support_luma_ncc",
        "baseline_support_edge_similarity",
        "baseline_support_change_ratio",
        "baseline_support_foreground_retention",
        "baseline_support_background_change_ratio",
        "baseline_support_alignment_dx",
        "baseline_support_alignment_dy",
        "baseline_support_alignment_rotation_degrees",
        "baseline_support_alignment_overlap",
        "baseline_support_alignment_score",
        "baseline_support_alignment_margin",
        "baseline_support_stability_pixel_count",
        "baseline_support_stability_changed_pixel_count",
        "baseline_support_stability_valid_pixel_count",
        "baseline_support_stability_excluded_pixel_count",
        "baseline_support_alignment_candidates_generated",
        "baseline_support_alignment_candidates_evaluated",
        "baseline_support_alignment_valid_candidates",
        "baseline_support_alignment_state",
        "baseline_support_scene_stable",
        "baseline_support_scene_stability_veto_reason",
        "baseline_support_present_gate_passed",
        "baseline_support_absent_gate_passed",
        "baseline_support_empty_background_evidence",
        "baseline_support_replacement_evidence",
        "baseline_support_occlusion_evidence",
        "baseline_support_decision_path",
        "baseline_support_decision_reason",
        "visual_status",
        "unusable_reason",
    }
)
_VISUAL_STATUSES = frozenset({"comparable", "unusable"})
_UNUSABLE_REASONS = frozenset(
    {
        "invalid_mask",
        "background_dominant",
        "insufficient_mask_overlap",
        "insufficient_comparison_area",
        "zero_luma_variance",
    }
)
_REASONS = frozenset(
    {
        "disappearance_confirmed",
        "complete_present_coverage",
        "incomplete_coverage",
        "indeterminate_observation",
        "insufficient_visual_evidence",
        "invalid_frame_or_roi",
        "frame_decode_failed",
        "frame_resolution_mismatch",
        "target_unavailable_gap",
        "target_recording_unavailable",
        "target_replay_timeout",
        "target_replay_failed",
        "target_decode_timeout",
        "target_decode_unavailable",
        "classifier_timeout",
        "classifier_failed",
        "midpoint_gap",
        "midpoint_acquisition_unavailable",
        "midpoint_indeterminate",
        "midpoint_classification_unavailable",
        "no_progress",
        "no_present_absent_bracket",
        "cancelled",
        "internal_error",
        "execution_deadline_exhausted",
        "roi_occluded",
    }
)


class SuccessorEvidenceError(RuntimeError):
    """Evidence could not be published or safely reopened."""


class SuccessorEvidenceRepository:
    """Atomic evidence publication and strict read-only reopen boundary."""

    root: Path
    _lock: RLock
    _staged_payloads: dict[tuple[str, str], dict[str, object]]

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = RLock()
        self._staged_payloads = {}

    def _directory(self, investigation_id: str, run_id: str) -> Path:
        if (
            _INVESTIGATION_ID.fullmatch(investigation_id) is None
            or _RUN_ID.fullmatch(run_id) is None
        ):
            raise SuccessorEvidenceError("evidence_unavailable")
        return self.root / investigation_id / run_id / "evidence"

    def _manifest(self, investigation_id: str, run_id: str) -> Path:
        return self._directory(investigation_id, run_id) / "manifest.json"

    def _staged_manifest(self, investigation_id: str, run_id: str) -> Path:
        return self._directory(investigation_id, run_id) / "manifest.json.pending"

    def publish(
        self,
        prepared: SuccessorPreparedExecution,
        observations: tuple[SuccessorObservation, ...],
        terminal: SuccessorTerminal,
    ) -> dict[str, object]:
        """Publish bounded JPEGs and a manifest, without overwriting a run."""
        return self._publish_at_path(
            prepared,
            observations,
            terminal,
            self._manifest(prepared.request.investigation_id, prepared.request.run_id),
        )

    def stage(
        self,
        prepared: SuccessorPreparedExecution,
        observations: tuple[SuccessorObservation, ...],
        terminal: SuccessorTerminal,
    ) -> dict[str, object]:
        """Materialize evidence without making its normal result authoritative."""
        investigation_id = prepared.request.investigation_id
        run_id = prepared.request.run_id
        key = (investigation_id, run_id)
        staged_path = self._staged_manifest(
            investigation_id,
            run_id,
        )
        with self._lock:
            try:
                # Clean up the pre-finalization marker used by older runs.  A
                # new execution must never reuse stale staging state.
                staged_path.unlink(missing_ok=True)
            except OSError as error:
                raise SuccessorEvidenceError("evidence_cleanup_failed") from error
            self._staged_payloads.pop(key, None)
            payload = self._publish_at_path(
                prepared,
                observations,
                terminal,
                staged_path,
                authoritative=False,
            )
            self._staged_payloads[key] = payload
            return payload

    def commit_staged(
        self,
        prepared: SuccessorPreparedExecution,
    ) -> dict[str, object]:
        """Make staged evidence authoritative with one atomic manifest write."""
        investigation_id = prepared.request.investigation_id
        run_id = prepared.request.run_id
        manifest_path = self._manifest(investigation_id, run_id)
        with self._lock:
            if manifest_path.is_file():
                self._staged_payloads.pop((investigation_id, run_id), None)
                existing = self.read(investigation_id, run_id)
                if existing is None:
                    raise SuccessorEvidenceError("evidence_corrupt")
                return existing
            payload = self._staged_payloads.pop((investigation_id, run_id), None)
            if payload is None:
                raise SuccessorEvidenceError("evidence_unavailable")
            try:
                _atomic_json(manifest_path, payload)
            except OSError as error:
                raise SuccessorEvidenceError("evidence_publish_failed") from error
            loaded = self.read(investigation_id, run_id)
            if loaded is None:
                raise SuccessorEvidenceError("evidence_readback_failed")
            return loaded

    def discard_staged(self, prepared: SuccessorPreparedExecution) -> None:
        """Remove staged metadata; materialized frame artifacts may remain."""
        staged_path = self._staged_manifest(
            prepared.request.investigation_id, prepared.request.run_id
        )
        with self._lock:
            self._staged_payloads.pop(
                (prepared.request.investigation_id, prepared.request.run_id), None
            )
            try:
                staged_path.unlink(missing_ok=True)
            except OSError as error:
                raise SuccessorEvidenceError("evidence_cleanup_failed") from error

    def _publish_at_path(
        self,
        prepared: SuccessorPreparedExecution,
        observations: tuple[SuccessorObservation, ...],
        terminal: SuccessorTerminal,
        manifest_path: Path,
        *,
        authoritative: bool = True,
    ) -> dict[str, object]:
        if not observations or len(observations) > MAX_EVIDENCE_RECORDS:
            raise SuccessorEvidenceError("evidence_capacity_exceeded")
        investigation_id = prepared.request.investigation_id
        run_id = prepared.request.run_id
        directory = self._directory(investigation_id, run_id)
        with self._lock:
            if authoritative and manifest_path.is_file():
                existing = self._read_manifest_path(manifest_path, investigation_id, run_id)
                if existing is None:
                    raise SuccessorEvidenceError("evidence_corrupt")
                return existing
            directory.mkdir(parents=True, exist_ok=True)
            frames_dir = directory / "frames"
            frames_dir.mkdir(exist_ok=True)
            total = 0
            entries: list[dict[str, object]] = []
            baseline = _baseline_entry(prepared)
            if baseline is None:
                raise SuccessorEvidenceError("evidence_unavailable")
            baseline_path = frames_dir / f"{baseline['digest']}.jpg"
            _atomic_bytes(baseline_path, prepared.baseline_jpeg_bytes or b"")
            total += baseline_path.stat().st_size
            total += _publish_roi_crop(
                frames_dir,
                baseline,
                prepared.baseline_jpeg_bytes or b"",
                prepared.authority.roi,
            )
            entries.append(baseline)
            for item in observations:
                digest: str | None = None
                is_baseline_link = item.target_id.startswith("successor-baseline-v1-")
                frame_fields = (
                    item.frame_bytes,
                    item.frame_sha256,
                    item.frame_width,
                    item.frame_height,
                )
                if (
                    not is_baseline_link
                    and any(field is not None for field in frame_fields)
                    and not all(field is not None for field in frame_fields)
                ):
                    raise SuccessorEvidenceError("evidence_digest_mismatch")
                if (
                    item.frame_bytes is not None
                    and item.frame_sha256 is not None
                    and item.frame_width is not None
                    and item.frame_height is not None
                ):
                    digest = hashlib.sha256(item.frame_bytes).hexdigest()
                    if digest != item.frame_sha256:
                        raise SuccessorEvidenceError("evidence_digest_mismatch")
                    if len(item.frame_bytes) > 16 * 1024 * 1024:
                        raise SuccessorEvidenceError("evidence_capacity_exceeded")
                    frame_path = frames_dir / f"{digest}.jpg"
                    if not frame_path.exists():
                        _atomic_bytes(frame_path, item.frame_bytes)
                    total += len(item.frame_bytes)
                elif is_baseline_link:
                    # The synthetic baseline observation is an identity link to
                    # the immutable Phase 6 JPEG, not a second in-memory frame.
                    # Persist the authority digest so strict reopen can resolve
                    # it without pretending that a replay frame was acquired.
                    if item.frame_sha256 != prepared.authority.reference_frame_jpeg_sha256:
                        raise SuccessorEvidenceError("evidence_digest_mismatch")
                    digest = item.frame_sha256
                entry = _observation_entry(
                    item,
                    digest,
                    source_width=prepared.authority.source_width,
                    source_height=prepared.authority.source_height,
                )
                if digest is not None and item.frame_bytes is not None:
                    total += _publish_roi_crop(
                        frames_dir, entry, item.frame_bytes, prepared.authority.roi
                    )
                entries.append(entry)
                if total > MAX_EVIDENCE_BYTES:
                    raise SuccessorEvidenceError("evidence_capacity_exceeded")
            payload: dict[str, object] = {
                "version": EVIDENCE_VERSION,
                "investigation_id": investigation_id,
                "run_id": run_id,
                "plan_id": prepared.plan.plan_id,
                "authority_identity": prepared.authority.authority_identity,
                "roi_identity": prepared.authority.roi_identity,
                "roi": prepared.authority.roi.model_dump(mode="json"),
                "source_width": prepared.authority.source_width,
                "source_height": prepared.authority.source_height,
                "terminal_status": terminal.status,
                "terminal_reason": terminal.reason_code,
                "last_present_observation_id": _observation_for_time(
                    observations, terminal.last_present_time_utc, "PRESENT"
                ),
                "first_absent_observation_id": _observation_for_time(
                    observations, terminal.first_absent_time_utc, "ABSENT"
                ),
                "review_clip": {"status": "UNAVAILABLE", "reason": "phase8_not_requested"},
                "entries": entries,
            }
            if not authoritative:
                return payload
            _atomic_json(manifest_path, payload)
            loaded = self._read_manifest_path(manifest_path, investigation_id, run_id)
            if loaded is None:
                raise SuccessorEvidenceError("evidence_readback_failed")
            return loaded

    def read(self, investigation_id: str, run_id: str) -> dict[str, object] | None:
        path = self._manifest(investigation_id, run_id)
        with self._lock:
            return self._read_manifest_path(path, investigation_id, run_id)

    @staticmethod
    def _read_manifest_path(
        path: Path,
        investigation_id: str,
        run_id: str,
    ) -> dict[str, object] | None:
        if not path.is_file():
            return None
        try:
            value: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SuccessorEvidenceError("evidence_corrupt") from error
        _validate_manifest(value, investigation_id, run_id)
        return cast("dict[str, object]", value)

    def read_frame(self, investigation_id: str, run_id: str, digest: str) -> bytes | None:
        if len(digest) != 64 or any(char not in _SHA256 for char in digest):
            raise SuccessorEvidenceError("evidence_corrupt")
        manifest = self.read(investigation_id, run_id)
        if manifest is None:
            return None
        entries = cast("list[dict[str, object]]", manifest["entries"])
        if not any(
            item.get("digest") == digest or item.get("roi_digest") == digest for item in entries
        ):
            raise SuccessorEvidenceError("evidence_unavailable")
        path = self._directory(investigation_id, run_id) / "frames" / f"{digest}.jpg"
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise SuccessorEvidenceError("evidence_unavailable") from error
        if hashlib.sha256(payload).hexdigest() != digest:
            raise SuccessorEvidenceError("evidence_digest_mismatch")
        return payload


def _baseline_entry(prepared: SuccessorPreparedExecution) -> dict[str, object] | None:
    payload = prepared.baseline_jpeg_bytes
    if payload is None:
        return None
    digest = hashlib.sha256(payload).hexdigest()
    if (
        digest != prepared.authority.reference_frame_jpeg_sha256
        or len(payload) != prepared.authority.reference_frame_jpeg_size_bytes
    ):
        raise SuccessorEvidenceError("evidence_digest_mismatch")
    return {
        "role": "baseline",
        "observation_id": None,
        "target_id": "historical-baseline",
        "acquisition_id": None,
        "assigned_segment_id": None,
        "plan_id": prepared.plan.plan_id,
        "authority_identity": prepared.authority.authority_identity,
        "reference_frame_resource_id": prepared.authority.reference_frame_resource_id,
        "roi_identity": prepared.authority.roi_identity,
        "classifier_policy_identity": None,
        "requested_time_utc": _timestamp(prepared.baseline_time_utc),
        "frame_utc": _timestamp(prepared.baseline_time_utc),
        "frame_pts_seconds": prepared.baseline_pts_seconds,
        "frame_ordinal": 1,
        "frame_offset_seconds": None,
        "digest": digest,
        "width": prepared.authority.source_width,
        "height": prepared.authority.source_height,
        "acquisition_status": "FRAME_AVAILABLE",
        "state": "PRESENT",
        "reason_code": None,
        "comparison": None,
        "classifier_stage": None,
        "classifier_elapsed_ms": None,
        "path": f"frames/{digest}.jpg",
        "roi_path": None,
        "roi_digest": None,
        "acquisition_mode": "normal",
        "target_delta_ms": None,
        "cadence_source": None,
        "cadence_ms": None,
        "tolerance_ms": None,
        "raw_segment_end_utc": None,
        "media_validation_outcome": "validated",
        "fallback_used": False,
        "fallback_reason": None,
        "observability": "USABLE",
    }


def _observation_entry(
    item: SuccessorObservation,
    digest: str | None,
    *,
    source_width: int,
    source_height: int,
) -> dict[str, object]:
    if item.target_id.startswith("successor-baseline-v1-"):
        role = "baseline_link"
    elif item.target_id.startswith("successor-anchor-target-v1-"):
        role = "anchor"
    else:
        role = "observation"
    width = item.frame_width
    height = item.frame_height
    if role == "baseline_link":
        width = source_width
        height = source_height
    return {
        "role": role,
        "plan_id": item.plan_id,
        "observation_id": item.observation_id,
        "target_id": item.target_id,
        "acquisition_id": item.acquisition_id,
        "assigned_segment_id": item.assigned_segment_id,
        "requested_time_utc": _timestamp(item.requested_time_utc),
        "frame_utc": _timestamp(item.frame_utc) if item.frame_utc else None,
        "frame_pts_seconds": item.frame_pts_seconds,
        "frame_ordinal": item.ordinal,
        "frame_offset_seconds": item.frame_offset_seconds,
        "digest": digest,
        "width": width,
        "height": height,
        "authority_identity": item.authority_identity,
        "reference_frame_resource_id": item.reference_frame_resource_id,
        "roi_identity": item.roi_identity,
        "classifier_policy_identity": item.classifier_policy_identity,
        "acquisition_status": item.acquisition_status.value,
        "state": item.state.value,
        "reason_code": item.reason_code,
        "comparison": item.comparison,
        "classifier_stage": item.classifier_stage,
        "classifier_elapsed_ms": item.classifier_elapsed_ms,
        "path": None if digest is None else f"frames/{digest}.jpg",
        "roi_path": None,
        "roi_digest": None,
        "acquisition_mode": item.acquisition_mode,
        "target_delta_ms": item.target_delta_ms,
        "cadence_source": item.cadence_source,
        "cadence_ms": item.cadence_ms,
        "tolerance_ms": item.tolerance_ms,
        "raw_segment_end_utc": (
            None if item.raw_segment_end_utc is None else _timestamp(item.raw_segment_end_utc)
        ),
        "media_validation_outcome": item.media_validation_outcome,
        "fallback_used": item.fallback_used,
        "fallback_reason": item.fallback_reason,
        "observability": item.observability,
    }


def _publish_roi_crop(
    frames_dir: Path, entry: dict[str, object], payload: bytes, roi: object
) -> int:
    """Create an invocation-owned, content-addressed ROI JPEG when decodable."""
    try:
        from io import BytesIO

        from PIL import Image

        coords = roi.x, roi.y, roi.width, roi.height
        with Image.open(BytesIO(payload)) as image:
            source = image.convert("RGB")
            x, y, width, height = coords
            if x < 0 or y < 0 or x + width > source.width or y + height > source.height:
                return 0
            cropped = source.crop((x, y, x + width, y + height))
            output = BytesIO()
            cropped.save(output, format="JPEG", quality=95, optimize=True)
            crop_bytes = output.getvalue()
        if len(crop_bytes) > 16 * 1024 * 1024:
            return 0
        digest = hashlib.sha256(crop_bytes).hexdigest()
        _atomic_bytes(frames_dir / f"{digest}.jpg", crop_bytes)
        entry["roi_digest"] = digest
        entry["roi_path"] = f"frames/{digest}.jpg"
        return len(crop_bytes)
    except (OSError, ValueError, TypeError):
        return 0


def _observation_for_time(
    observations: tuple[SuccessorObservation, ...], value: str | None, state: str
) -> str | None:
    if value is None or not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        target = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if target.tzinfo is None:
        return None
    target = target.astimezone(timezone.utc).replace(microsecond=0)
    matches: list[tuple[int, int, str]] = []
    for index, item in enumerate(observations):
        if (
            item.observation_id is None
            or item.frame_utc is None
            or item.state.value != state
            or item.frame_utc.astimezone(timezone.utc).replace(microsecond=0) != target
        ):
            continue
        # Prefer a real search observation over the anchor/baseline link when
        # timestamp precision is reduced to whole seconds at publication.
        target_id = item.target_id
        role_rank = (
            2
            if target_id.startswith("successor-baseline-v1-")
            else 1
            if target_id.startswith("successor-anchor-target-v1-")
            else 0
        )
        matches.append((role_rank, index, item.observation_id))
    if not matches:
        return None
    best_rank = min(item[0] for item in matches)
    best = [item for item in matches if item[0] == best_rank]
    # Whole-second terminal timing cannot safely distinguish two real search
    # observations in the same second; refuse to invent a bracket identity.
    if len(best) != 1:
        return None
    return best[0][2]


def _validate_manifest(value: object, investigation_id: str, run_id: str) -> None:
    if not isinstance(value, dict):
        raise SuccessorEvidenceError("evidence_corrupt")
    if (
        value.get("version") != EVIDENCE_VERSION
        or value.get("investigation_id") != investigation_id
        or value.get("run_id") != run_id
    ):
        raise SuccessorEvidenceError("evidence_corrupt")
    typed = cast("dict[str, object]", value)
    if set(typed) != _MANIFEST_KEYS:
        raise SuccessorEvidenceError("evidence_corrupt")
    plan_id = typed.get("plan_id")
    authority_identity = typed.get("authority_identity")
    roi_identity = typed.get("roi_identity")
    source_width = typed.get("source_width")
    source_height = typed.get("source_height")
    roi = typed.get("roi")
    terminal_status = typed.get("terminal_status")
    terminal_reason = typed.get("terminal_reason")
    review_clip = typed.get("review_clip")
    if (
        not isinstance(plan_id, str)
        or not plan_id
        or not isinstance(authority_identity, str)
        or not authority_identity
        or not isinstance(roi_identity, str)
        or not roi_identity
        or type(source_width) is not int
        or source_width <= 0
        or type(source_height) is not int
        or source_height <= 0
        or not isinstance(roi, dict)
        or not _valid_roi(roi, source_width, source_height)
        or terminal_status not in {"FOUND", "NOT_FOUND", "INCONCLUSIVE", "FAILED"}
        or terminal_reason not in _REASONS
        or not isinstance(review_clip, dict)
        or set(review_clip) != {"status", "reason"}
        or review_clip.get("status") not in {"UNAVAILABLE", "READY"}
        or not isinstance(review_clip.get("reason"), str)
    ):
        raise SuccessorEvidenceError("evidence_corrupt")
    entries = typed.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_EVIDENCE_RECORDS:
        raise SuccessorEvidenceError("evidence_corrupt")
    baseline_entries: list[dict[str, object]] = []
    for item in cast("list[object]", entries):
        if not isinstance(item, dict):
            raise SuccessorEvidenceError("evidence_corrupt")
        item_keys = set(item)
        if not (
            item_keys == set(_ENTRY_KEYS)
            or item_keys == set(_LEGACY_ENTRY_KEYS)
            or item_keys == set(_EXTENDED_ENTRY_KEYS)
        ):
            raise SuccessorEvidenceError("evidence_corrupt")
        if item.get("role") not in _ENTRY_ROLES or item.get("plan_id") != plan_id:
            raise SuccessorEvidenceError("evidence_corrupt")
        if item.get("role") == "baseline":
            baseline_entries.append(item)
        if (
            item.get("authority_identity") != authority_identity
            or item.get("roi_identity") != roi_identity
        ):
            raise SuccessorEvidenceError("evidence_corrupt")
        if item_keys == _ENTRY_KEYS:
            _validate_transport_metadata(item)
        if item_keys == _EXTENDED_ENTRY_KEYS:
            _validate_transport_metadata(item)
            _validate_fallback_metadata(item)
        acquisition_status = item.get("acquisition_status")
        state = item.get("state")
        if acquisition_status not in _ACQUISITION_STATUSES or state not in _OBSERVATION_STATES:
            raise SuccessorEvidenceError("evidence_corrupt")
        width = item.get("width")
        height = item.get("height")
        if acquisition_status == "FRAME_AVAILABLE" and (
            type(width) is not int
            or width != source_width
            or type(height) is not int
            or height != source_height
        ):
            raise SuccessorEvidenceError("evidence_corrupt")
        if acquisition_status != "FRAME_AVAILABLE" and (width is not None or height is not None):
            raise SuccessorEvidenceError("evidence_corrupt")
        digest = item.get("digest")
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in _SHA256 for char in digest)
        ):
            raise SuccessorEvidenceError("evidence_corrupt")
        roi_digest = item.get("roi_digest")
        if roi_digest is not None and (
            not isinstance(roi_digest, str)
            or len(roi_digest) != 64
            or any(char not in _SHA256 for char in roi_digest)
        ):
            raise SuccessorEvidenceError("evidence_corrupt")
        comparison = item.get("comparison")
        if comparison is not None and (
            not isinstance(comparison, dict) or set(comparison) - _COMPARISON_KEYS
        ):
            raise SuccessorEvidenceError("evidence_corrupt")
        if not _valid_comparison(comparison) or not _valid_timestamp(
            item.get("requested_time_utc")
        ):
            raise SuccessorEvidenceError("evidence_corrupt")
        frame_utc = item.get("frame_utc")
        if frame_utc is not None and not _valid_timestamp(frame_utc):
            raise SuccessorEvidenceError("evidence_corrupt")
        reason_code = item.get("reason_code")
        if (
            reason_code is not None
            and reason_code not in _REASONS
            and reason_code not in _UNUSABLE_REASONS
        ):
            raise SuccessorEvidenceError("evidence_corrupt")
        for field_name, digest_value in (("digest", digest), ("roi_digest", roi_digest)):
            path_value = item.get("path" if field_name == "digest" else "roi_path")
            if digest_value is None and path_value is not None:
                raise SuccessorEvidenceError("evidence_corrupt")
            if digest_value is not None and path_value != f"frames/{digest_value}.jpg":
                raise SuccessorEvidenceError("evidence_corrupt")
    if len(baseline_entries) != 1:
        raise SuccessorEvidenceError("evidence_corrupt")
    baseline_digest = baseline_entries[0].get("digest")
    for item in cast("list[dict[str, object]]", entries):
        if item.get("role") == "baseline_link" and item.get("digest") != baseline_digest:
            raise SuccessorEvidenceError("evidence_corrupt")


def _valid_roi(value: dict[object, object], source_width: int, source_height: int) -> bool:
    return (
        type(value.get("x")) is int
        and type(value.get("y")) is int
        and type(value.get("width")) is int
        and type(value.get("height")) is int
        and value["x"] >= 0
        and value["y"] >= 0
        and value["width"] > 0
        and value["height"] > 0
        and value["x"] + value["width"] <= source_width
        and value["y"] + value["height"] <= source_height
        and value.get("coordinate_space") == "source_pixels"
    )


def _validate_transport_metadata(item: dict[str, object]) -> None:
    mode = item.get("acquisition_mode")
    if mode not in {"normal", "segment_end_fallback"}:
        raise SuccessorEvidenceError("evidence_corrupt")
    delta = item.get("target_delta_ms")
    if delta is not None and (type(delta) is not int or delta < 0):
        raise SuccessorEvidenceError("evidence_corrupt")
    source = item.get("cadence_source")
    if source is not None and source != "adjacent_pts":
        raise SuccessorEvidenceError("evidence_corrupt")
    for key in ("cadence_ms", "tolerance_ms"):
        value = item.get(key)
        if value is not None and (type(value) is not int or value <= 0):
            raise SuccessorEvidenceError("evidence_corrupt")
    raw_end = item.get("raw_segment_end_utc")
    if raw_end is not None and not _valid_timestamp(raw_end):
        raise SuccessorEvidenceError("evidence_corrupt")
    if item.get("media_validation_outcome") not in {"not_attempted", "validated", "failed"}:
        raise SuccessorEvidenceError("evidence_corrupt")
    if mode == "segment_end_fallback" and raw_end is None:
        raise SuccessorEvidenceError("evidence_corrupt")
    if (
        mode == "segment_end_fallback"
        and item.get("acquisition_status") == "FRAME_AVAILABLE"
        and (
            delta is None
            or item.get("cadence_source") != "adjacent_pts"
            or item.get("cadence_ms") is None
            or item.get("tolerance_ms") is None
            or delta > item["tolerance_ms"]
        )
    ):
        raise SuccessorEvidenceError("evidence_corrupt")


def _validate_fallback_metadata(item: dict[str, object]) -> None:
    fallback_used = item.get("fallback_used")
    fallback_reason = item.get("fallback_reason")
    observability = item.get("observability")
    if type(fallback_used) is not bool:
        raise SuccessorEvidenceError("evidence_corrupt")
    if fallback_reason not in {None, "ROI_OCCLUDED", "DECODE_UNAVAILABLE"}:
        raise SuccessorEvidenceError("evidence_corrupt")
    if observability not in {"USABLE", "OCCLUDED", "DECODE_UNAVAILABLE"}:
        raise SuccessorEvidenceError("evidence_corrupt")
    if fallback_used and fallback_reason not in {"ROI_OCCLUDED", "DECODE_UNAVAILABLE"}:
        raise SuccessorEvidenceError("evidence_corrupt")


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _valid_comparison(value: object) -> bool:  # noqa: PLR0911
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    payload = value
    visual_status = payload.get("visual_status")
    unusable_reason = payload.get("unusable_reason")
    comparison_mode = payload.get("comparison_mode")
    alignment_state = payload.get("baseline_support_alignment_state")
    decision_path = payload.get("baseline_support_decision_path")
    decision_reason = payload.get("baseline_support_decision_reason")
    if alignment_state is not None and alignment_state not in {
        "aligned",
        "ambiguous",
        "no_valid_candidate",
        "not_required",
    }:
        return False
    if visual_status is not None and visual_status not in _VISUAL_STATUSES:
        return False
    if unusable_reason is not None and unusable_reason not in _UNUSABLE_REASONS:
        return False
    if comparison_mode not in {
        None,
        "baseline_support_v1",
        "baseline_support_v2",
        "baseline_support_v3",
    }:
        return False
    decision_values = (
        payload.get("baseline_support_present_gate_passed"),
        payload.get("baseline_support_absent_gate_passed"),
        payload.get("baseline_support_empty_background_evidence"),
        payload.get("baseline_support_replacement_evidence"),
        payload.get("baseline_support_occlusion_evidence"),
        decision_path,
        decision_reason,
    )
    if any(item is not None for item in decision_values):
        if any(item is None for item in decision_values):
            return False
        if any(type(item) is not bool for item in decision_values[:5]):
            return False
        if decision_path not in {"present", "absent", "indeterminate"}:
            return False
        if decision_reason not in {
            "present_identity_retained",
            "absent_empty_background",
            "replacement_candidate",
            "roi_occluded",
            "unstable_scene",
            "conflicting_visual_evidence",
            "insufficient_visual_evidence",
        }:
            return False
    alignment_keys = {
        "baseline_support_alignment_dx",
        "baseline_support_alignment_dy",
        "baseline_support_alignment_rotation_degrees",
        "baseline_support_alignment_overlap",
        "baseline_support_alignment_score",
        "baseline_support_alignment_margin",
    }
    if comparison_mode != "baseline_support_v3" and any(
        item is not None for key, item in payload.items() if key in alignment_keys
    ):
        return False
    if alignment_state in {"no_valid_candidate", "not_required"}:
        alignment_values = [
            payload.get(key)
            for key in (
                "baseline_support_alignment_dx",
                "baseline_support_alignment_dy",
                "baseline_support_alignment_rotation_degrees",
                "baseline_support_alignment_overlap",
                "baseline_support_alignment_score",
                "baseline_support_alignment_margin",
            )
        ]
        if any(item is None for item in alignment_values) and any(
            item is not None for item in alignment_values
        ):
            return False
    total = payload.get("baseline_support_stability_pixel_count")
    changed = payload.get("baseline_support_stability_changed_pixel_count")
    valid = payload.get("baseline_support_stability_valid_pixel_count")
    excluded = payload.get("baseline_support_stability_excluded_pixel_count")
    stable = payload.get("baseline_support_scene_stable")
    veto = payload.get("baseline_support_scene_stability_veto_reason")
    if any(item is not None for item in (total, changed, valid, excluded, stable, veto)) and (
        type(total) is not int
        or type(changed) is not int
        or type(valid) is not int
        or type(excluded) is not int
        or type(stable) is not bool
        or total <= 0
        or changed < 0
        or valid <= 0
        or excluded < 0
        or total != payload.get("roi_pixel_count")
        or changed > valid
        or valid + excluded != total
        or (stable and veto is not None)
        or (
            not stable
            and veto
            not in {
                "global_scene_change",
                "insufficient_stability_area",
                "registration_failed",
            }
        )
    ):
        return False
    generated = payload.get("baseline_support_alignment_candidates_generated")
    evaluated = payload.get("baseline_support_alignment_candidates_evaluated")
    valid_candidates = payload.get("baseline_support_alignment_valid_candidates")
    if any(item is not None for item in (generated, evaluated, valid_candidates)) and (
        type(generated) is not int
        or type(evaluated) is not int
        or type(valid_candidates) is not int
        or generated <= 0
        or evaluated < 0
        or valid_candidates < 0
        or evaluated > generated
        or valid_candidates > evaluated
    ):
        return False
    for key, item in payload.items():
        if key in {
            "visual_status",
            "unusable_reason",
            "comparison_mode",
            "baseline_support_alignment_state",
            "baseline_support_scene_stability_veto_reason",
            "baseline_support_scene_stable",
            "baseline_support_decision_path",
            "baseline_support_decision_reason",
            "baseline_support_present_gate_passed",
            "baseline_support_absent_gate_passed",
            "baseline_support_empty_background_evidence",
            "baseline_support_replacement_evidence",
            "baseline_support_occlusion_evidence",
        }:
            continue
        if item is None:
            continue
        if type(item) not in {int, float} or not math.isfinite(item):
            return False
    if comparison_mode in {
        "baseline_support_v1",
        "baseline_support_v2",
        "baseline_support_v3",
    }:
        required = {
            "baseline_mask_pixel_count",
            "baseline_support_pixel_count",
            "baseline_support_luma_similarity",
            "baseline_support_luma_ncc",
            "baseline_support_change_ratio",
            "baseline_support_foreground_retention",
            "baseline_support_background_change_ratio",
        }
        if comparison_mode == "baseline_support_v3":
            required.update(
                {
                    "baseline_support_alignment_dx",
                    "baseline_support_alignment_dy",
                    "baseline_support_alignment_rotation_degrees",
                    "baseline_support_alignment_overlap",
                    "baseline_support_alignment_score",
                    "baseline_support_alignment_margin",
                }
            )
            if alignment_state in {"no_valid_candidate", "not_required"} and all(
                payload.get(key) is None for key in alignment_keys
            ):
                required.difference_update(alignment_keys)
        if not required.issubset(payload) or payload.get("visual_status") != "comparable":
            return False
        for key in required:
            item = payload.get(key)
            if (
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(item)
            ):
                return False
        if comparison_mode == "baseline_support_v3":
            dx = payload.get("baseline_support_alignment_dx")
            dy = payload.get("baseline_support_alignment_dy")
            rotation = payload.get("baseline_support_alignment_rotation_degrees")
            overlap = payload.get("baseline_support_alignment_overlap")
            score = payload.get("baseline_support_alignment_score")
            margin = payload.get("baseline_support_alignment_margin")
            alignment_values = (dx, dy, rotation, overlap, score, margin)
            if alignment_state in {"no_valid_candidate", "not_required"}:
                has_null = any(item is None for item in alignment_values)
                has_value = any(item is not None for item in alignment_values)
                if has_null and has_value:
                    return False
                if has_null:
                    return True
            if (
                type(dx) is not int
                or type(dy) is not int
                or type(rotation) is not int
                or abs(dx) > 32
                or abs(dy) > 32
                or abs(rotation) > 15
                or not isinstance(overlap, (int, float))
                or not 0.0 < overlap <= 1.0
                or not isinstance(score, (int, float))
                or not -1.0 <= score <= 1.0
                or not isinstance(margin, (int, float))
                or not 0.0 <= margin <= 1.0
            ):
                return False
    return True


def _atomic_bytes(path: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".evidence-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    fd, temporary = tempfile.mkstemp(prefix=".evidence-manifest-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ("EVIDENCE_VERSION", "SuccessorEvidenceError", "SuccessorEvidenceRepository")
