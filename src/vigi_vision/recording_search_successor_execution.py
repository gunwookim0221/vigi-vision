"""Phase 7E successor Slice 5 orchestration and durable terminal state.

The successor path composes the already-reviewed planning, short replay,
classification, and narrowing boundaries.  It is deliberately stored beside
the legacy run tree (under ``.successor``) so Schema 5--7 readers never see a
foreign record family.
"""

# The orchestration is an explicit contract boundary; keep its state machine
# readable while suppressing only diagnostics for protocol-shaped adapters.
# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnannotatedClassAttribute=false, reportPrivateUsage=false, reportUnusedImport=false, reportUnusedCallResult=false, reportUnusedParameter=false, reportUnknownVariableType=false, reportDeprecated=false
# ruff: noqa: D102, D107, E501, EM101, FBT001, FBT003, PLR0913, PLC0415, PTH105, PTH108, RUF007, SIM105, TC001, TC003

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Protocol, cast

from vigi_vision.investigation_confirmation_models import ConfirmedInvestigationInput
from vigi_vision.object_presence_values import ClassificationOutcome, DecodedRgbImage
from vigi_vision.recording_search_7e_b4_process import (
    B4ProcessError,
    B4ProcessTimeout,
    EfficientSamWorkerSpec,
    StaticMaskWorkerSpec,
    run_b4_in_process,
)
from vigi_vision.recording_search_b3_models import ClassificationPreparationError
from vigi_vision.recording_search_successor import (
    CoarseTargetAssignment,
    MultiSegmentCoarsePlan,
    SuccessorPlanRequest,
    SuccessorPlanService,
    TargetAvailability,
    effective_search_start_utc,
)
from vigi_vision.recording_search_successor_acquisition import (
    SuccessorTargetAcquisitionService,
    SuccessorTargetStatus,
)
from vigi_vision.recording_search_successor_classification import (
    SuccessorClassificationAuthority,
    SuccessorClassificationContractError,
    SuccessorClassificationError,
    SuccessorClassifierResult,
    SuccessorCoarseClassificationResult,
    SuccessorCoarseClassificationService,
    SuccessorObservation,
    SuccessorObservationState,
    reidentify_observation,
)
from vigi_vision.recording_search_successor_narrowing import (
    SuccessorBinaryNarrowingResult,
    SuccessorBinaryNarrowingService,
    SuccessorNarrowingCompletion,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy


SUCCESSOR_SCHEMA_VERSION = 8
SUCCESSOR_RECORD_VERSION = "phase7e-successor-terminal-v1"
_RUNNING = "RUNNING"
_TERMINAL = frozenset({"FOUND", "NOT_FOUND", "INCONCLUSIVE", "FAILED", "INTERRUPTED"})
_MAX_RECOVERY_RECORDS = 1024
_SUCCESSOR_TERMINAL_REASONS = frozenset(
    {
        "disappearance_confirmed",
        "complete_present_coverage",
        "incomplete_coverage",
        "no_present_absent_bracket",
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
        "cancelled",
        "abandoned_after_restart",
        "internal_error",
    }
)
_PHASE8_NOT_REQUESTED_REASON = "successor_slice5_does_not_create_handoffs"


class SuccessorExecutionError(RuntimeError):
    """A safe successor orchestration or publication error."""


class _BaselineDecoder(Protocol):
    def decode(self, payload: bytes, width: int, height: int) -> object: ...


class _DecodedBaseline(Protocol):
    image: DecodedRgbImage


@dataclass(frozen=True, slots=True)
class SuccessorRequest:
    """The request facts accepted by the successor path."""

    investigation_id: str
    run_id: str
    channel_id: int
    anchor_time_utc: datetime
    end_utc: datetime
    source_timezone: str

    @property
    def duration_seconds(self) -> int:
        return int((self.end_utc - self.anchor_time_utc).total_seconds())


@dataclass(frozen=True, slots=True)
class SuccessorPreparedExecution:
    """All server-owned successor facts prepared before background admission."""

    request: SuccessorRequest
    plan: MultiSegmentCoarsePlan
    authority: SuccessorClassificationAuthority
    baseline_time_utc: datetime
    baseline_pts_seconds: float | None
    baseline_timing_precision_status: str
    baseline_warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SuccessorTerminal:
    """Published successor outcome and safe timing projection."""

    investigation_id: str
    run_id: str
    plan_id: str
    status: str
    reason_code: str
    terminal_result_id: str | None
    observed_start_time_utc: str
    observed_end_time_utc: str
    last_present_time_utc: str | None
    first_absent_time_utc: str | None
    coverage_complete: bool
    source_timezone: str
    narrowing_id: str | None
    phase8_status: str = "NOT_REQUESTED"
    phase8_reason: str = "successor_slice5_does_not_create_handoffs"
    policy_version: str = ""
    requested_end_time_utc: str = ""
    coverage: tuple[dict[str, str], ...] = ()
    gaps: tuple[dict[str, str], ...] = ()
    coarse_observation_ids: tuple[str, ...] = ()
    coarse_target_ids: tuple[str, ...] = ()
    target_statuses: tuple[str, ...] = ()
    coarse_observations: tuple[dict[str, object], ...] = ()

    def as_record(self) -> dict[str, object]:
        return {
            "record_version": SUCCESSOR_RECORD_VERSION,
            "schema_version": SUCCESSOR_SCHEMA_VERSION,
            "investigation_id": self.investigation_id,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "terminal_result_id": self.terminal_result_id,
            "observed_start_time_utc": self.observed_start_time_utc,
            "observed_end_time_utc": self.observed_end_time_utc,
            "last_present_time_utc": self.last_present_time_utc,
            "first_absent_time_utc": self.first_absent_time_utc,
            "coverage_complete": self.coverage_complete,
            "source_timezone": self.source_timezone,
            "narrowing_id": self.narrowing_id,
            "phase8_status": self.phase8_status,
            "phase8_reason": self.phase8_reason,
            "policy_version": self.policy_version,
            "requested_end_time_utc": self.requested_end_time_utc,
            "coverage": list(self.coverage),
            "gaps": list(self.gaps),
            "coarse_observation_ids": list(self.coarse_observation_ids),
            "coarse_target_ids": list(self.coarse_target_ids),
            "target_statuses": list(self.target_statuses),
            "coarse_observations": list(self.coarse_observations),
        }


class SuccessorTerminalRepository:
    """Atomic process-restartable terminal state for Schema 8 successor runs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = RLock()

    def _path(self, investigation_id: str, run_id: str) -> Path:
        return self.root / investigation_id / run_id / "terminal.json"

    def read(self, investigation_id: str, run_id: str) -> dict[str, object] | None:
        path = self._path(investigation_id, run_id)
        with self._lock:
            if not path.is_file():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise SuccessorExecutionError("successor_publication_corrupt") from error
            if not isinstance(value, dict) or value.get("investigation_id") != investigation_id:
                raise SuccessorExecutionError("successor_publication_corrupt")
            if (
                value.get("run_id") != run_id
                or value.get("schema_version") != SUCCESSOR_SCHEMA_VERSION
            ):
                raise SuccessorExecutionError("successor_publication_corrupt")
            if value.get("status") not in {_RUNNING, *_TERMINAL}:
                raise SuccessorExecutionError("successor_publication_corrupt")
            if value.get("status") in _TERMINAL:
                _validate_terminal_record(value)
            return value

    def publish_running(self, prepared: SuccessorPreparedExecution) -> None:
        payload = {
            "record_version": SUCCESSOR_RECORD_VERSION,
            "schema_version": SUCCESSOR_SCHEMA_VERSION,
            "investigation_id": prepared.request.investigation_id,
            "run_id": prepared.request.run_id,
            "plan_id": prepared.plan.plan_id,
            "status": _RUNNING,
            "request_end_utc": _timestamp(prepared.request.end_utc),
            "anchor_time_utc": _timestamp(prepared.request.anchor_time_utc),
            "source_timezone": prepared.request.source_timezone,
            "policy_version": prepared.plan.policy_version,
            "coverage": [
                {
                    "segment_id": item.segment_id,
                    "start_utc": _timestamp(item.start_utc),
                    "end_utc": _timestamp(item.end_utc),
                }
                for item in prepared.plan.segments
            ],
            "gaps": [
                {"start_utc": _timestamp(item.start_utc), "end_utc": _timestamp(item.end_utc)}
                for item in prepared.plan.gaps
            ],
        }
        self._atomic_write(prepared.request.investigation_id, prepared.request.run_id, payload)

    def publish_terminal(self, terminal: SuccessorTerminal) -> SuccessorTerminal:
        existing = self.read(terminal.investigation_id, terminal.run_id)
        if existing is not None and existing.get("status") in _TERMINAL:
            if _terminal_identity(existing) != terminal.terminal_result_id:
                raise SuccessorExecutionError("successor_request_conflict")
            return _terminal_from_record(existing)
        self._atomic_write(terminal.investigation_id, terminal.run_id, terminal.as_record())
        loaded = self.read(terminal.investigation_id, terminal.run_id)
        if loaded is None or _terminal_identity(loaded) != terminal.terminal_result_id:
            raise SuccessorExecutionError("successor_publication_readback_failed")
        return _terminal_from_record(loaded)

    def recover_abandoned(self) -> int:
        recovered = 0
        if not self.root.exists():
            return 0
        inspected = 0
        for path in self.root.glob("*/*/terminal.json"):
            inspected += 1
            if inspected > _MAX_RECOVERY_RECORDS:
                raise SuccessorExecutionError("successor_recovery_capacity_exceeded")
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(value, dict) and value.get("status") == _RUNNING:
                terminal = SuccessorTerminal(
                    str(value.get("investigation_id")),
                    str(value.get("run_id")),
                    str(value.get("plan_id")),
                    "INTERRUPTED",
                    "abandoned_after_restart",
                    _digest_terminal(
                        {**value, "status": "INTERRUPTED", "reason_code": "abandoned_after_restart"}
                    ),
                    str(value.get("anchor_time_utc")),
                    str(value.get("request_end_utc")),
                    None,
                    None,
                    False,
                    str(value.get("source_timezone")),
                    None,
                )
                terminal = replace(
                    terminal,
                    requested_end_time_utc=str(value.get("request_end_utc")),
                )
                self.publish_terminal(terminal)
                recovered += 1
        return recovered

    def _atomic_write(
        self, investigation_id: str, run_id: str, payload: Mapping[str, object]
    ) -> None:
        path = self._path(investigation_id, run_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".terminal-", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(
                        dict(payload),
                        handle,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


@dataclass(frozen=True, slots=True)
class SuccessorB4Classifier:
    """Process-isolated adapter over the existing B4 worker boundary."""

    policy: ObjectPresenceDecisionPolicy
    worker_spec: EfficientSamWorkerSpec | StaticMaskWorkerSpec
    timeout_seconds: float = 30.0
    startup_timeout_seconds: float = 30.0

    @property
    def policy_identity(self) -> str:
        return self.policy.identity

    def classify(
        self,
        baseline_image: DecodedRgbImage,
        probe_image: DecodedRgbImage,
        source_width: int,
        source_height: int,
        roi: object,
        correlation_id: str,
    ) -> SuccessorClassifierResult:
        try:
            result = run_b4_in_process(
                baseline_image=baseline_image,
                probe_image=probe_image,
                source_width=source_width,
                source_height=source_height,
                roi=roi,
                policy=self.policy,
                worker_spec=self.worker_spec,
                correlation_id=correlation_id,
                timeout_seconds=self.timeout_seconds,
                startup_timeout_seconds=self.startup_timeout_seconds,
            )
        except B4ProcessTimeout as error:
            raise SuccessorClassificationError("classifier_timeout") from error
        except (B4ProcessError, ClassificationPreparationError) as error:
            raise SuccessorClassificationError("classifier_failed") from error
        outcome = getattr(result, "outcome", None)
        if not isinstance(outcome, ClassificationOutcome):
            raise SuccessorClassificationContractError
        reason = getattr(result, "reason_code", None)
        return SuccessorClassifierResult(outcome, None if reason is None else str(reason.value))


@dataclass(slots=True)
class SuccessorExecutionService:
    """Compose the five bounded successor phases into one terminal run."""

    planning: SuccessorPlanService
    acquisition: SuccessorTargetAcquisitionService
    classification: SuccessorCoarseClassificationService
    narrowing: SuccessorBinaryNarrowingService
    baseline_decoder: object
    publisher: SuccessorTerminalRepository

    def prepare(
        self,
        confirmed: ConfirmedInvestigationInput,
        *,
        search_end_time_text: str,
        run_id: str,
        now_utc: datetime,
    ) -> SuccessorPreparedExecution:
        effective_start = effective_search_start_utc(
            confirmed.anchor_time_utc,
            confirmed.requested_time_utc,
        )
        request = SuccessorPlanRequest.from_text(
            channel_id=confirmed.channel_id,
            anchor_time_utc=effective_start,
            search_end_time_text=search_end_time_text,
            source_timezone=confirmed.source_timezone,
            now_utc=now_utc,
        )
        plan = self.planning.plan(request)
        try:
            baseline_payload = confirmed.jpeg_path.read_bytes()
            decoded = cast("_BaselineDecoder", self.baseline_decoder).decode(
                baseline_payload, confirmed.source_width, confirmed.source_height
            )
            baseline_image = cast("_DecodedBaseline", decoded).image
        except Exception as error:
            raise SuccessorExecutionError("confirmation_corrupt") from error
        authority = SuccessorClassificationAuthority.from_confirmed_input(
            confirmed,
            successor_plan_id=plan.plan_id,
            baseline_image=baseline_image,
        )
        return SuccessorPreparedExecution(
            SuccessorRequest(
                confirmed.investigation_id,
                run_id,
                confirmed.channel_id,
                effective_start,
                request.search_end_utc,
                confirmed.source_timezone,
            ),
            plan,
            authority,
            confirmed.requested_time_utc,
            confirmed.decoded_local_pts_seconds,
            confirmed.timing_precision_status,
            confirmed.warnings,
        )

    def execute(  # noqa: C901, PLR0911
        self,
        prepared: SuccessorPreparedExecution,
        *,
        cancellation: Callable[[], bool] | None = None,
    ) -> SuccessorTerminal:
        self.publisher.publish_running(prepared)
        try:
            if cancellation is not None and cancellation():
                return self._publish_interrupted(prepared)
            anchor_target = _anchor_target(prepared.plan)
            anchor_acquisition = self.acquisition.acquire_anchor(prepared.plan, anchor_target)
            anchor_observation = self.classification.classify_anchor_target(
                prepared.plan, anchor_target, anchor_acquisition, prepared.authority
            )
            coarse_observations: list[SuccessorObservation] = []
            coarse = _coarse_snapshot(prepared, coarse_observations)
            augmented = _with_anchor_observation(prepared, coarse, anchor_observation)
            if augmented.candidate_bracket is not None:
                return self._narrow_or_publish(prepared, augmented)
            if anchor_observation.state not in {
                SuccessorObservationState.PRESENT,
                SuccessorObservationState.ABSENT,
            }:
                return self._publish_inconclusive(
                    prepared,
                    _inconclusive_reason(prepared.plan, augmented.observations),
                    augmented,
                )
            for target in prepared.plan.targets:
                if cancellation is not None and cancellation():
                    return self._publish_interrupted(prepared)
                acquisition = self.acquisition.acquire(prepared.plan, target)
                observation = self.classification.classify_coarse_target(
                    prepared.plan, target, acquisition, prepared.authority
                )
                coarse_observations.append(observation)
                coarse = _coarse_snapshot(prepared, coarse_observations)
                augmented = _with_anchor_observation(prepared, coarse, anchor_observation)
                if augmented.candidate_bracket is not None:
                    return self._narrow_or_publish(prepared, augmented)
                if observation.state not in {
                    SuccessorObservationState.PRESENT,
                    SuccessorObservationState.ABSENT,
                }:
                    return self._publish_inconclusive(
                        prepared,
                        _inconclusive_reason(prepared.plan, augmented.observations),
                        augmented,
                    )
            if prepared.plan.gaps:
                return self._publish_inconclusive(prepared, "incomplete_coverage", augmented)
            if all(
                item.state is SuccessorObservationState.PRESENT for item in augmented.observations
            ):
                return self._publish_not_found(prepared, augmented)
            return self._publish_inconclusive(prepared, "no_present_absent_bracket", augmented)
        except SuccessorExecutionError:
            raise
        except Exception as error:
            failed = SuccessorTerminal(
                prepared.request.investigation_id,
                prepared.request.run_id,
                prepared.plan.plan_id,
                "FAILED",
                "internal_error",
                None,
                _timestamp(prepared.request.anchor_time_utc),
                _timestamp(prepared.request.end_utc),
                None,
                None,
                False,
                prepared.request.source_timezone,
                None,
            )
            failed = replace(failed, terminal_result_id=_digest_terminal(failed.as_record()))
            _ = self.publisher.publish_terminal(failed)
            raise SuccessorExecutionError("internal_error") from error

    def _narrow_or_publish(
        self,
        prepared: SuccessorPreparedExecution,
        augmented: SuccessorCoarseClassificationResult,
    ) -> SuccessorTerminal:
        """Narrow the first bracket or preserve its safe coverage boundary."""
        bracket = augmented.candidate_bracket
        if bracket is None:
            raise SuccessorExecutionError("internal_error")
        if _bracket_intersects_gap(prepared.plan, bracket):
            return self._publish_inconclusive(prepared, "incomplete_coverage", augmented)
        narrowed = self.narrowing.narrow(prepared.plan, augmented, prepared.authority)
        if narrowed.completion is not SuccessorNarrowingCompletion.NARROWED:
            return self._publish_inconclusive(prepared, narrowed.reason_code, augmented, narrowed)
        return self._publish_found(prepared, augmented, narrowed)

    def _publish_found(
        self,
        prepared: SuccessorPreparedExecution,
        coarse: SuccessorCoarseClassificationResult,
        narrowed: SuccessorBinaryNarrowingResult,
    ) -> SuccessorTerminal:
        terminal = self._base_terminal(
            prepared, "FOUND", "disappearance_confirmed", not prepared.plan.gaps
        )
        observations = _observation_bundle(coarse, narrowed)
        terminal = replace(
            terminal,
            last_present_time_utc=_timestamp(narrowed.last_present_frame_utc),
            first_absent_time_utc=_timestamp(narrowed.first_absent_frame_utc),
            observed_end_time_utc=_timestamp(narrowed.first_absent_frame_utc),
            narrowing_id=narrowed.narrowing_id,
            coarse_observation_ids=tuple(item.observation_id for item in observations),
            coarse_target_ids=tuple(item.target_id for item in observations),
            target_statuses=tuple(item.acquisition_status.value for item in observations),
            coarse_observations=tuple(_observation_record(item) for item in observations),
        )
        terminal = replace(
            terminal,
            observed_start_time_utc=_observed_start(observations, terminal.observed_start_time_utc),
            terminal_result_id=_digest_terminal(
                {**terminal.as_record(), "terminal_result_id": None}
            ),
        )
        return self.publisher.publish_terminal(terminal)

    def _publish_not_found(
        self, prepared: SuccessorPreparedExecution, coarse: SuccessorCoarseClassificationResult
    ) -> SuccessorTerminal:
        observations = _observation_bundle(coarse)
        observed = tuple(item.frame_utc for item in observations if item.frame_utc is not None)
        terminal = self._base_terminal(prepared, "NOT_FOUND", "complete_present_coverage", True)
        terminal = replace(
            terminal,
            terminal_result_id=_digest_terminal(
                {"plan": prepared.plan.plan_id, "status": "NOT_FOUND"}
            ),
            observed_end_time_utc=_timestamp(
                max(observed) if observed else prepared.request.end_utc
            ),
            coarse_observation_ids=tuple(item.observation_id for item in observations),
            coarse_target_ids=tuple(item.target_id for item in observations),
            target_statuses=tuple(item.acquisition_status.value for item in observations),
            coarse_observations=tuple(_observation_record(item) for item in observations),
        )
        terminal = replace(
            terminal,
            observed_start_time_utc=_observed_start(observations, terminal.observed_start_time_utc),
            terminal_result_id=_digest_terminal(
                {**terminal.as_record(), "terminal_result_id": None}
            ),
        )
        return self.publisher.publish_terminal(terminal)

    def _publish_inconclusive(
        self,
        prepared: SuccessorPreparedExecution,
        reason: str,
        coarse: SuccessorCoarseClassificationResult,
        narrowed: SuccessorBinaryNarrowingResult | None = None,
    ) -> SuccessorTerminal:
        terminal = self._base_terminal(prepared, "INCONCLUSIVE", reason, False)
        observations = _observation_bundle(coarse, narrowed)
        terminal = replace(
            terminal,
            terminal_result_id=_digest_terminal(
                {"plan": prepared.plan.plan_id, "status": "INCONCLUSIVE", "reason": reason}
            ),
            narrowing_id=None if narrowed is None else narrowed.narrowing_id,
            coarse_observation_ids=tuple(item.observation_id for item in observations),
            coarse_target_ids=tuple(item.target_id for item in observations),
            target_statuses=tuple(item.acquisition_status.value for item in observations),
            coarse_observations=tuple(_observation_record(item) for item in observations),
        )
        terminal = replace(
            terminal,
            terminal_result_id=_digest_terminal(
                {**terminal.as_record(), "terminal_result_id": None}
            ),
        )
        return self.publisher.publish_terminal(terminal)

    def _publish_interrupted(self, prepared: SuccessorPreparedExecution) -> SuccessorTerminal:
        terminal = self._base_terminal(prepared, "INTERRUPTED", "cancelled", False)
        terminal = replace(terminal, terminal_result_id=_digest_terminal(terminal.as_record()))
        return self.publisher.publish_terminal(terminal)

    @staticmethod
    def _base_terminal(
        prepared: SuccessorPreparedExecution, status: str, reason: str, complete: bool
    ) -> SuccessorTerminal:
        return SuccessorTerminal(
            prepared.request.investigation_id,
            prepared.request.run_id,
            prepared.plan.plan_id,
            status,
            reason,
            None,
            _timestamp(prepared.request.anchor_time_utc),
            _timestamp(prepared.request.end_utc),
            None,
            None,
            complete,
            prepared.request.source_timezone,
            None,
            policy_version=prepared.plan.policy_version,
            requested_end_time_utc=_timestamp(prepared.request.end_utc),
            coverage=tuple(
                {
                    "segment_id": item.segment_id,
                    "start_utc": _timestamp(item.start_utc),
                    "end_utc": _timestamp(item.end_utc),
                }
                for item in prepared.plan.segments
            ),
            gaps=tuple(
                {
                    "start_utc": _timestamp(item.start_utc),
                    "end_utc": _timestamp(item.end_utc),
                }
                for item in prepared.plan.gaps
            ),
        )


def _coarse_snapshot(
    prepared: SuccessorPreparedExecution,
    observations: list[SuccessorObservation],
) -> SuccessorCoarseClassificationResult:
    """Build the immutable coarse snapshot available at this point in time."""
    return SuccessorCoarseClassificationResult(
        prepared.plan.plan_id,
        prepared.authority.authority_identity,
        tuple(observations),
        None,
    )


def _with_anchor_observation(
    prepared: SuccessorPreparedExecution,
    coarse: SuccessorCoarseClassificationResult,
    anchor_observation: SuccessorObservation,
) -> SuccessorCoarseClassificationResult:
    """Bind the historical Phase 6 baseline and an actual anchor observation."""
    policy_identity = anchor_observation.classifier_policy_identity or (
        coarse.observations[0].classifier_policy_identity if coarse.observations else ""
    )
    baseline_time = prepared.baseline_time_utc
    baseline_pts = prepared.baseline_pts_seconds
    baseline_payload = {
        "version": "phase7e-successor-historical-baseline-v1",
        "plan_id": prepared.plan.plan_id,
        "target_id": f"successor-baseline-v1-{hashlib.sha256((prepared.plan.plan_id + ':' + _timestamp(baseline_time)).encode()).hexdigest()}",
        "requested_time_utc": _timestamp(baseline_time),
        "frame_utc": _timestamp(baseline_time),
        "frame_pts_seconds": baseline_pts,
        "authority_identity": prepared.authority.authority_identity,
        "reference_frame_resource_id": prepared.authority.reference_frame_resource_id,
        "reference_frame_jpeg_sha256": prepared.authority.reference_frame_jpeg_sha256,
        "roi_identity": prepared.authority.roi_identity,
        "policy_identity": policy_identity,
        "state": SuccessorObservationState.PRESENT.value,
    }
    baseline_id = (
        "successor-observation-v1-"
        + hashlib.sha256(
            json.dumps(baseline_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    baseline = SuccessorObservation(
        prepared.plan.plan_id,
        baseline_payload["target_id"],
        f"successor-baseline-acquisition-v1-{hashlib.sha256((prepared.plan.plan_id + ':' + _timestamp(baseline_time)).encode()).hexdigest()}",
        1,
        baseline_time,
        baseline_time,
        baseline_pts,
        None,
        prepared.authority.authority_identity,
        prepared.authority.reference_frame_resource_id,
        prepared.authority.roi_identity,
        policy_identity,
        SuccessorTargetStatus.FRAME_AVAILABLE,
        SuccessorObservationState.PRESENT,
        None,
        1,
        baseline_id,
        prepared.baseline_timing_precision_status,
        prepared.baseline_warnings,
        prepared.authority.reference_frame_jpeg_sha256,
    )
    anchor = reidentify_observation(anchor_observation, sequence=2, ordinal=2)
    shifted = tuple(
        reidentify_observation(item, sequence=item.sequence + 2, ordinal=item.ordinal + 2)
        for item in coarse.observations
    )
    observations = (baseline, anchor, *shifted)
    bracket = None
    for left, right in zip(observations, observations[1:], strict=False):
        if (
            left.state is SuccessorObservationState.PRESENT
            and right.state is SuccessorObservationState.ABSENT
            and left.frame_utc is not None
            and right.frame_utc is not None
        ):
            bracket = _bracket(left, right)
            break
    return SuccessorCoarseClassificationResult(
        coarse.plan_id, coarse.authority_identity, observations, bracket
    )


def _bracket(left: SuccessorObservation, right: SuccessorObservation) -> object:
    from vigi_vision.recording_search_successor_classification import SuccessorCandidateBracket

    if left.frame_utc is None or right.frame_utc is None:
        raise SuccessorExecutionError("internal_error")
    return SuccessorCandidateBracket(
        left.observation_id, right.observation_id, left.frame_utc, right.frame_utc
    )


def _anchor_target(plan: MultiSegmentCoarsePlan) -> CoarseTargetAssignment:
    for segment in plan.segments:
        if segment.start_utc <= plan.anchor_time_utc < segment.end_utc:
            return CoarseTargetAssignment(
                1,
                plan.anchor_time_utc,
                TargetAvailability.AVAILABLE,
                segment.segment_id,
                None,
            )
    for gap in plan.gaps:
        if gap.start_utc <= plan.anchor_time_utc <= gap.end_utc:
            return CoarseTargetAssignment(
                1,
                plan.anchor_time_utc,
                TargetAvailability.UNAVAILABLE,
                None,
                gap,
            )
    raise SuccessorExecutionError("incomplete_coverage")


def _inconclusive_reason(
    plan: MultiSegmentCoarsePlan, observations: tuple[SuccessorObservation, ...]
) -> str:
    if plan.gaps or any(
        item.state is SuccessorObservationState.UNAVAILABLE_GAP for item in observations
    ):
        return "incomplete_coverage"
    for item in observations:
        if item.state is SuccessorObservationState.INDETERMINATE:
            return item.reason_code or "indeterminate_observation"
        if item.state in {
            SuccessorObservationState.CLASSIFIER_TIMEOUT,
            SuccessorObservationState.CLASSIFIER_FAILED,
            SuccessorObservationState.RECORDING_UNAVAILABLE,
            SuccessorObservationState.REPLAY_TIMEOUT,
            SuccessorObservationState.REPLAY_FAILED,
            SuccessorObservationState.DECODE_TIMEOUT,
            SuccessorObservationState.DECODE_UNAVAILABLE,
        }:
            return item.reason_code or item.state.value.lower()
    return "indeterminate_observation"


def _bracket_intersects_gap(plan: MultiSegmentCoarsePlan, bracket: object) -> bool:
    start = getattr(bracket, "present_frame_utc", None)
    end = getattr(bracket, "absent_frame_utc", None)
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return True
    return any(gap.start_utc < end and gap.end_utc > start for gap in plan.gaps)


def _observation_record(item: SuccessorObservation) -> dict[str, object]:
    return {
        "observation_id": item.observation_id,
        "target_id": item.target_id,
        "acquisition_id": item.acquisition_id,
        "sequence": item.sequence,
        "ordinal": item.ordinal,
        "requested_time_utc": _timestamp(item.requested_time_utc),
        "frame_utc": None if item.frame_utc is None else _timestamp(item.frame_utc),
        "frame_pts_seconds": item.frame_pts_seconds,
        "frame_offset_seconds": item.frame_offset_seconds,
        "timing_precision_status": item.timing_precision_status,
        "timing_warnings": list(item.timing_warnings),
        "acquisition_status": item.acquisition_status.value,
        "state": item.state.value,
        "reason_code": item.reason_code,
        "frame_sha256": item.frame_sha256,
        "authority_identity": item.authority_identity,
        "reference_frame_resource_id": item.reference_frame_resource_id,
        "roi_identity": item.roi_identity,
        "classifier_policy_identity": item.classifier_policy_identity,
    }


def _observation_bundle(
    coarse: SuccessorCoarseClassificationResult,
    narrowed: SuccessorBinaryNarrowingResult | None = None,
) -> tuple[SuccessorObservation, ...]:
    if narrowed is None:
        return coarse.observations
    return (*coarse.observations, *narrowed.midpoint_observations)


def _observed_start(observations: tuple[SuccessorObservation, ...], fallback: str) -> str:
    frames = tuple(item.frame_utc for item in observations if item.frame_utc is not None)
    return fallback if not frames else _timestamp(min(frames))


def _terminal_identity(value: Mapping[str, object]) -> str | None:
    candidate = value.get("terminal_result_id")
    return candidate if isinstance(candidate, str) else None


def _terminal_from_record(value: Mapping[str, object]) -> SuccessorTerminal:
    _validate_terminal_record(value)
    coverage = _record_dicts(value.get("coverage"))
    gaps = _record_dicts(value.get("gaps"))
    observation_ids = _strings(value.get("coarse_observation_ids"))
    target_ids = _strings(value.get("coarse_target_ids"))
    target_statuses = _strings(value.get("target_statuses"))
    coarse_observations = _record_objects(value.get("coarse_observations"))
    return SuccessorTerminal(
        str(value["investigation_id"]),
        str(value["run_id"]),
        str(value["plan_id"]),
        str(value["status"]),
        str(value["reason_code"]),
        _terminal_identity(value),
        str(value["observed_start_time_utc"]),
        str(value["observed_end_time_utc"]),
        value.get("last_present_time_utc")
        if isinstance(value.get("last_present_time_utc"), str)
        else None,
        value.get("first_absent_time_utc")
        if isinstance(value.get("first_absent_time_utc"), str)
        else None,
        bool(value["coverage_complete"]),
        str(value["source_timezone"]),
        value.get("narrowing_id") if isinstance(value.get("narrowing_id"), str) else None,
        str(value.get("phase8_status", "NOT_REQUESTED")),
        str(value.get("phase8_reason", "")),
        str(value.get("policy_version", "")),
        str(value.get("requested_end_time_utc", "")),
        coverage,
        gaps,
        observation_ids,
        target_ids,
        target_statuses,
        coarse_observations,
    )


def _record_dicts(value: object) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SuccessorExecutionError("successor_publication_corrupt")
    return tuple(cast("dict[str, str]", item) for item in value)


def _record_objects(value: object) -> tuple[dict[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SuccessorExecutionError("successor_publication_corrupt")
    return tuple(cast("dict[str, object]", item) for item in value)


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SuccessorExecutionError("successor_publication_corrupt")
    return tuple(value)


def _validate_terminal_record(value: Mapping[str, object]) -> None:
    """Strictly validate one persisted Schema 8 terminal projection."""
    _validate_terminal_core(value)
    status, _ = _validate_terminal_status(value)
    _validate_terminal_timing(value, status)
    _validate_terminal_optional_fields(value)
    _validate_terminal_lists(value)


def _validate_terminal_core(value: Mapping[str, object]) -> None:
    required = {
        "record_version",
        "schema_version",
        "investigation_id",
        "run_id",
        "plan_id",
        "status",
        "reason_code",
        "terminal_result_id",
        "observed_start_time_utc",
        "observed_end_time_utc",
        "coverage_complete",
        "source_timezone",
    }
    if not required.issubset(value):
        raise SuccessorExecutionError("successor_publication_corrupt")
    if value.get("record_version") != SUCCESSOR_RECORD_VERSION:
        raise SuccessorExecutionError("successor_publication_corrupt")
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != SUCCESSOR_SCHEMA_VERSION
    ):
        raise SuccessorExecutionError("successor_publication_corrupt")
    for key in ("investigation_id", "run_id", "plan_id", "source_timezone"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise SuccessorExecutionError("successor_publication_corrupt")
    plan_id = cast("str", value["plan_id"])
    if not plan_id.startswith("successor-plan-v1-"):
        raise SuccessorExecutionError("successor_publication_corrupt")


def _validate_terminal_status(value: Mapping[str, object]) -> tuple[str, str]:
    status = value.get("status")
    reason = value.get("reason_code")
    if not isinstance(status, str) or status not in _TERMINAL:
        raise SuccessorExecutionError("successor_publication_corrupt")
    if not isinstance(reason, str) or reason not in _SUCCESSOR_TERMINAL_REASONS:
        raise SuccessorExecutionError("successor_publication_corrupt")
    expected_reasons = {
        "FOUND": {"disappearance_confirmed"},
        "NOT_FOUND": {"complete_present_coverage"},
        "INTERRUPTED": {"cancelled", "abandoned_after_restart"},
        "FAILED": {"internal_error"},
    }
    if status in expected_reasons and reason not in expected_reasons[status]:
        raise SuccessorExecutionError("successor_publication_corrupt")
    terminal_result_id = value.get("terminal_result_id")
    if not isinstance(terminal_result_id, str) or not terminal_result_id.startswith(
        "successor-terminal-v1-"
    ):
        raise SuccessorExecutionError("successor_publication_corrupt")
    return status, reason


def _validate_terminal_timing(value: Mapping[str, object], status: str) -> None:
    observed_start = _strict_utc_timestamp(value["observed_start_time_utc"])
    observed_end = _strict_utc_timestamp(value["observed_end_time_utc"])
    if observed_end < observed_start:
        raise SuccessorExecutionError("successor_publication_corrupt")
    for key in ("last_present_time_utc", "first_absent_time_utc"):
        candidate = value.get(key)
        if candidate is not None and not isinstance(candidate, str):
            raise SuccessorExecutionError("successor_publication_corrupt")
    last_present = value.get("last_present_time_utc")
    first_absent = value.get("first_absent_time_utc")
    if status == "FOUND":
        if not isinstance(last_present, str) or not isinstance(first_absent, str):
            raise SuccessorExecutionError("successor_publication_corrupt")
        present_time = _strict_utc_timestamp(last_present)
        absent_time = _strict_utc_timestamp(first_absent)
        if not observed_start <= present_time < absent_time <= observed_end:
            raise SuccessorExecutionError("successor_publication_corrupt")
    elif last_present is not None or first_absent is not None:
        raise SuccessorExecutionError("successor_publication_corrupt")
    if type(value.get("coverage_complete")) is not bool:
        raise SuccessorExecutionError("successor_publication_corrupt")


def _validate_terminal_optional_fields(value: Mapping[str, object]) -> None:
    _validate_optional_string(value, "narrowing_id")
    _validate_optional_string(value, "policy_version")
    _validate_optional_timestamp(value, "requested_end_time_utc")
    phase8_status = value.get("phase8_status", "NOT_REQUESTED")
    phase8_reason = value.get("phase8_reason")
    if not isinstance(phase8_status, str) or phase8_status != "NOT_REQUESTED":
        raise SuccessorExecutionError("successor_publication_corrupt")
    if phase8_reason is not None and (
        not isinstance(phase8_reason, str) or phase8_reason != _PHASE8_NOT_REQUESTED_REASON
    ):
        raise SuccessorExecutionError("successor_publication_corrupt")


def _validate_terminal_lists(value: Mapping[str, object]) -> None:
    _validate_list_field(value, "coverage", _validate_coverage_item)
    _validate_list_field(value, "gaps", _validate_gap_item)
    _validate_list_field(value, "coarse_observation_ids", _validate_string_item)
    _validate_list_field(value, "coarse_target_ids", _validate_string_item)
    _validate_list_field(value, "target_statuses", _validate_string_item)
    _validate_list_field(value, "coarse_observations", _validate_object_item)


def _validate_optional_string(value: Mapping[str, object], key: str) -> None:
    if key in value and value[key] is not None and (not isinstance(value[key], str)):
        raise SuccessorExecutionError("successor_publication_corrupt")


def _validate_optional_timestamp(value: Mapping[str, object], key: str) -> None:
    if key in value and value[key] is not None:
        if not isinstance(value[key], str):
            raise SuccessorExecutionError("successor_publication_corrupt")
        _strict_utc_timestamp(value[key])


def _strict_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise SuccessorExecutionError("successor_publication_corrupt")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SuccessorExecutionError("successor_publication_corrupt") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise SuccessorExecutionError("successor_publication_corrupt")
    return parsed


def _validate_list_field(
    value: Mapping[str, object],
    key: str,
    validator: Callable[[object], None],
) -> None:
    if key not in value:
        return
    items = value[key]
    if not isinstance(items, list):
        raise SuccessorExecutionError("successor_publication_corrupt")
    for item in items:
        validator(item)


def _validate_string_item(item: object) -> None:
    if not isinstance(item, str) or not item:
        raise SuccessorExecutionError("successor_publication_corrupt")


def _validate_object_item(item: object) -> None:
    if not isinstance(item, dict):
        raise SuccessorExecutionError("successor_publication_corrupt")


def _validate_coverage_item(item: object) -> None:
    if not isinstance(item, dict) or any(
        not isinstance(item.get(key), str) or not item[key]
        for key in ("segment_id", "start_utc", "end_utc")
    ):
        raise SuccessorExecutionError("successor_publication_corrupt")
    start = _strict_utc_timestamp(item["start_utc"])
    end = _strict_utc_timestamp(item["end_utc"])
    if end <= start:
        raise SuccessorExecutionError("successor_publication_corrupt")


def _validate_gap_item(item: object) -> None:
    if not isinstance(item, dict) or any(
        not isinstance(item.get(key), str) or not item[key] for key in ("start_utc", "end_utc")
    ):
        raise SuccessorExecutionError("successor_publication_corrupt")
    start = _strict_utc_timestamp(item["start_utc"])
    end = _strict_utc_timestamp(item["end_utc"])
    if end <= start:
        raise SuccessorExecutionError("successor_publication_corrupt")


def _digest_terminal(value: Mapping[str, object]) -> str:
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "successor-terminal-v1-" + hashlib.sha256(payload.encode()).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = (
    "SUCCESSOR_SCHEMA_VERSION",
    "SuccessorB4Classifier",
    "SuccessorExecutionError",
    "SuccessorExecutionService",
    "SuccessorPreparedExecution",
    "SuccessorRequest",
    "SuccessorTerminal",
    "SuccessorTerminalRepository",
)
