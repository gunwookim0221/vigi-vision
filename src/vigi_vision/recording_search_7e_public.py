"""Public Phase 7E execution, status, and Phase 8 handoff boundaries."""

# The public boundary deliberately mirrors the closed, explicit Phase 7E
# contract (including its many typed media fields) and maps every failure to a
# fixed category.  Keep the implementation readable while exempting only
# complexity/style rules that describe those contract mechanics.
# ruff: noqa: D102, D107, EM101, PLR0913, PLR2004, PLC0415
# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportUnannotatedClassAttribute=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnnecessaryIsInstance=false, reportUnusedCallResult=false, reportOptionalMemberAccess=false, reportPrivateUsage=false

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, StrictStr

from vigi_vision.investigation_confirmation_models import (
    ConfirmationArtifactError,
    ConfirmationCorruptError,
    ConfirmedInputInvalidError,
    InvestigationConfirmationNotFoundError,
    LegacyInvestigationError,
)
from vigi_vision.nvr import NvrRequestError
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.recording_search_7e_1c import (
    CommonSessionAcquirer,
    CommonSessionError,
    CommonSessionPolicy,
    CommonSessionRequest,
    FfmpegLocalDecoder,
    FfprobeMediaProbe,
    Phase7E1CExecutor,
)
from vigi_vision.recording_search_7e_1d import (
    Phase7E1DError,
    Phase7E1DService,
    Phase7EAdapterError,
    Phase7EIncompleteEvidenceError,
    Phase7EOperationalEvidenceError,
    Phase7EStatus,
    read_phase7_status,
)
from vigi_vision.recording_search_7e_models import StrictIdentityEnvelope
from vigi_vision.recording_search_7e_phase8 import (
    FfmpegSourceClipGenerator,
    Phase8HandoffRepository,
    Phase8LifecycleError,
)
from vigi_vision.recording_search_7e_repository import (
    Phase7EConflictError,
    Phase7ECorruptError,
    Phase7EInProgressError,
    Phase7ENotFoundError,
    Phase7EReadbackError,
    Phase7ERepositoryError,
    Phase7ERun,
    RecordingSearch7ERepository,
)
from vigi_vision.recording_search_7e_validation import Phase7EValidationError
from vigi_vision.recording_search_b3_media import InMemoryRgbDecoder
from vigi_vision.recording_search_successor import (
    SuccessorPlanError,
    SuccessorPlanRequest,
    SuccessorPlanService,
)
from vigi_vision.recording_search_successor_execution import (
    SuccessorB4Classifier,
    SuccessorExecutionError,
    SuccessorExecutionService,
    SuccessorPreparedExecution,
    SuccessorRequest,
    SuccessorTerminalRepository,
)
from vigi_vision.reference_frame_decoder import FfmpegReferenceFrameDecoder
from vigi_vision.reference_frame_models import parse_reference_frame_request

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vigi_vision.recording_search_7e_media_diagnostics import Phase7EMediaProbeDiagnostic


_FAILURE_BOUNDARY_BY_CATEGORY: Final = {
    "invalid_request": "invocation_input",
    "already_running": "invocation_input",
    "request_conflict": "invocation_input",
    "interrupted": "invocation_input",
    "invocation_deadline_exhausted": "invocation_input",
    "capacity_exhausted": "invocation_input",
    "acquisition_failed": "recording_discovery",
    "recording_unavailable": "recording_discovery",
    "replay_timeout": "replay_acquisition",
    "replay_authentication_failed": "replay_acquisition",
    "replay_failed": "replay_acquisition",
    "cleanup_failed": "replay_acquisition",
    "media_probe_timeout": "media_validation",
    "media_probe_failed": "media_validation",
    "invalid_time_base": "decoder",
    "missing_pts": "decoder",
    "nonmonotonic_pts": "decoder",
    "timestamp_reset": "decoder",
    "recording_gap": "decoder",
    "segment_boundary": "decoder",
    "decoder_timeout": "decoder",
    "decoder_failed": "decoder",
    "classifier_timeout": "classifier",
    "classification_failed": "classifier",
    "invalid_classifier_result": "classifier",
    "incomplete_evidence": "classifier",
    "adapter_unknown_result": "classifier",
    "publication_failed": "publication",
    "readback_failed": "publication",
    "search_run_corrupt": "publication",
    "internal_error": "internal",
}
_SAFE_EXCEPTION_CLASSES: Final = frozenset(
    {
        "NvrRequestError",
        "CommonSessionError",
        "CommonSessionInternalError",
        "CommonSessionValidationError",
        "CommonSessionRecordingUnavailableError",
        "CommonSessionReplayTimeoutError",
        "CommonSessionReplayError",
        "CommonSessionReplayAuthenticationError",
        "CommonSessionMediaError",
        "CommonSessionMediaProbeTimeoutError",
        "CommonSessionMissingPtsError",
        "CommonSessionInvalidTimeBaseError",
        "CommonSessionNonmonotonicPtsError",
        "CommonSessionTimestampResetError",
        "CommonSessionRecordingGapError",
        "CommonSessionSegmentBoundaryError",
        "CommonSessionDecoderTimeoutError",
        "CommonSessionDecoderError",
        "CommonSessionDeadlineError",
        "CommonSessionCapacityError",
        "CommonSessionCleanupError",
        "CommonSessionCancelledError",
        "CommonSessionPublicationError",
        "CommonSessionReadbackError",
        "Phase7E1DError",
        "Phase7EIncompleteEvidenceError",
        "Phase7EOperationalEvidenceError",
        "Phase7EAdapterError",
        "Phase7ERepositoryError",
        "Phase7EReadbackError",
        "Phase7ECorruptError",
        "Phase7EValidationError",
        "Phase7EPublicError",
        "unexpected_exception",
    }
)
_CLEANUP_OUTCOMES: Final = frozenset({"not_required", "no_failure_reported", "failed", "unknown"})
_INVALID_FAILURE_DIAGNOSTIC: Final = "invalid failure diagnostic vocabulary"


@dataclass(frozen=True, slots=True)
class Phase7EFailureDiagnostic:
    """Closed, credential-free process-local failure context."""

    boundary: str
    category: str
    exception_class: str
    cleanup_outcome: str
    media_probe: Phase7EMediaProbeDiagnostic | None = None

    def __post_init__(self) -> None:
        """Reject any value outside the fixed diagnostic vocabulary."""
        if (
            _FAILURE_BOUNDARY_BY_CATEGORY.get(self.category) != self.boundary
            or self.exception_class not in _SAFE_EXCEPTION_CLASSES
            or self.cleanup_outcome not in _CLEANUP_OUTCOMES
        ):
            raise ValueError(_INVALID_FAILURE_DIAGNOSTIC)

    def as_dict(self) -> dict[str, str]:
        """Return only the four approved non-secret fields."""
        return {
            "boundary": self.boundary,
            "category": self.category,
            "exception_class": self.exception_class,
            "cleanup_outcome": self.cleanup_outcome,
        }

    def as_process_dict(self) -> dict[str, object]:
        """Return the bounded process-local projection, including media facts."""
        result: dict[str, object] = {**self.as_dict()}
        if self.media_probe is not None:
            result["media_probe"] = self.media_probe.as_dict()
        return result


class Phase7EPublicError(RuntimeError):
    """Safe public failure category."""

    def __init__(
        self,
        code: str,
        *,
        diagnostic: Phase7EFailureDiagnostic | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostic = diagnostic


_MAX_SEARCH_SECONDS = 600
_MAX_STARTUP_RECOVERY_RUNS = 1024
_SUCCESSOR_CONFIGURED = "configured"
_SUCCESSOR_UNAVAILABLE = "unavailable"
_LOGGER = logging.getLogger("uvicorn.error.vigi_vision.phase7e")


def _execution_public_error(
    error: BaseException,
    *,
    category: str | None = None,
) -> Phase7EPublicError:
    """Map one known execution failure to closed public and diagnostic facts."""
    safe_category = category or _execution_failure_category(error)
    diagnostic = Phase7EFailureDiagnostic(
        _failure_boundary(safe_category),
        safe_category,
        _safe_exception_class(error),
        _cleanup_outcome(error, safe_category),
        getattr(error, "probe_diagnostic", None),
    )
    return Phase7EPublicError(safe_category, diagnostic=diagnostic)


def _execution_failure_category(error: BaseException) -> str:
    if isinstance(error, NvrRequestError):
        category = "acquisition_failed"
    elif isinstance(error, CommonSessionError):
        category = error.code if error.code in _FAILURE_BOUNDARY_BY_CATEGORY else "internal_error"
    elif isinstance(error, Phase7EIncompleteEvidenceError):
        category = "incomplete_evidence"
    elif isinstance(error, Phase7EOperationalEvidenceError):
        category = "classification_failed"
    elif isinstance(error, Phase7EAdapterError):
        category = "adapter_unknown_result"
    elif isinstance(error, Phase7EReadbackError):
        category = "readback_failed"
    elif isinstance(error, (Phase7ERepositoryError, Phase7EValidationError)):
        category = "publication_failed"
    else:
        category = "internal_error"
    return category


def _failure_boundary(category: str) -> str:
    return _FAILURE_BOUNDARY_BY_CATEGORY.get(category, "internal")


def _safe_exception_class(error: BaseException) -> str:
    candidate = type(error).__name__
    if candidate in _SAFE_EXCEPTION_CLASSES:
        return candidate
    if isinstance(error, CommonSessionError):
        return "CommonSessionError"
    if isinstance(error, Phase7E1DError):
        return "Phase7E1DError"
    if isinstance(error, Phase7ERepositoryError):
        return "Phase7ERepositoryError"
    return "unexpected_exception"


def _cleanup_outcome(error: BaseException, category: str) -> str:
    if isinstance(error, NvrRequestError):
        return "not_required"
    if isinstance(error, CommonSessionError):
        if error.cleanup_failure_code is not None:
            return "failed"
        if _failure_boundary(category) in {
            "replay_acquisition",
            "media_validation",
            "decoder",
            "classifier",
        }:
            return "no_failure_reported"
        return "not_required"
    if isinstance(error, (Phase7E1DError, Phase7ERepositoryError, Phase7EValidationError)):
        return "not_required"
    return "unknown"


def _selected_transition_times(
    records: tuple[StrictIdentityEnvelope, ...],
    terminal: StrictIdentityEnvelope,
    session_start: datetime,
) -> tuple[str | None, str | None]:
    """Resolve a FOUND interval from the selected frames' actual PTS."""
    if terminal.payload["result_kind"] != "FOUND":
        return None, None
    by_identity = {item.identity: item for item in records}
    snapshot = by_identity[str(terminal.payload["evidence_snapshot_id"])]
    narrowed = by_identity[str(snapshot.payload["narrowed_bracket_id"])]

    def observed_time(observation_id: object) -> str:
        observation = by_identity[str(observation_id)]
        frame = by_identity[str(observation.payload["frame_id"])]
        microseconds = (
            (int(frame.payload["raw_pts"]) - int(frame.payload["container_start_pts"]))
            * int(frame.payload["time_base_num"])
            * 1_000_000
            // int(frame.payload["time_base_den"])
        )
        return (
            (session_start + timedelta(microseconds=microseconds))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    return (
        observed_time(narrowed.payload["lower_observation_id"]),
        observed_time(narrowed.payload["upper_observation_id"]),
    )


def _bounded_recovery_candidates(root: Path) -> list[tuple[str, str]]:
    """Collect run identities without allowing an unbounded startup walk."""
    candidates: list[tuple[str, str]] = []
    investigation_entries = 0
    run_entries = 0
    for investigation in root.iterdir():
        investigation_entries += 1
        if investigation_entries > _MAX_STARTUP_RECOVERY_RUNS:
            raise Phase7EPublicError("recovery_capacity_exceeded")
        if (
            investigation.name.startswith(".")
            or investigation.is_symlink()
            or not investigation.is_dir()
        ):
            continue
        for run in investigation.iterdir():
            run_entries += 1
            if run_entries > _MAX_STARTUP_RECOVERY_RUNS:
                raise Phase7EPublicError("recovery_capacity_exceeded")
            if not run.is_symlink() and run.is_dir():
                candidates.append((investigation.name, run.name))
    return candidates


class StrictRequestModel(BaseModel):
    """Closed public input base."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Phase7EPublicRequest(StrictRequestModel):
    """Public command/API request with no server-owned fields."""

    investigation_id: StrictStr
    search_end_time_text: StrictStr
    source_timezone: StrictStr


@dataclass(frozen=True, slots=True)
class Phase7EPreparedRequest:
    """Server-reconstructed execution facts for one strict public request."""

    request: CommonSessionRequest | SuccessorRequest
    schema5: StrictIdentityEnvelope | None
    base_records: tuple[StrictIdentityEnvelope, ...]
    coarse_targets: tuple[StrictIdentityEnvelope, ...]
    successor: SuccessorPreparedExecution | None = None


@dataclass(frozen=True, slots=True)
class Phase7ETerminalDetails:
    """Safe user-facing timing facts reconstructed from immutable evidence."""

    last_present_time_utc: str | None
    first_absent_time_utc: str | None
    observed_start_time_utc: str
    observed_end_time_utc: str
    coverage_complete: bool
    source_timezone: str

    def as_dict(self) -> dict[str, object]:
        return {
            "last_present_time_utc": self.last_present_time_utc,
            "first_absent_time_utc": self.first_absent_time_utc,
            "observed_start_time_utc": self.observed_start_time_utc,
            "observed_end_time_utc": self.observed_end_time_utc,
            "coverage_complete": self.coverage_complete,
            "source_timezone": self.source_timezone,
        }


@dataclass(frozen=True, slots=True)
class Phase7EPublicStatus:
    """Credential-free status projection used by CLI and HTTP."""

    phase7: Phase7EStatus
    phase8_status: str | None = None
    phase8_reason: str | None = None
    terminal_details: Phase7ETerminalDetails | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "investigation_id": self.phase7.investigation_id,
            "run_id": self.phase7.run_id,
            "schema_version": self.phase7.schema_version,
            "status": self.phase7.status,
            "reason_code": self.phase7.reason_code,
            "terminal_result_id": self.phase7.terminal_result_id,
            "phase8_status": self.phase8_status,
            "phase8_reason": self.phase8_reason,
            "terminal_details": (
                None if self.terminal_details is None else self.terminal_details.as_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class Phase7EPublicService:
    """Single public composition entry point for Phase 7E execution."""

    repository: RecordingSearch7ERepository
    executor: Phase7E1CExecutor
    confirmation_service: object
    classifier: object | None
    local_decoder: object | None
    policy: StrictIdentityEnvelope
    classifier_policy: StrictIdentityEnvelope
    object_policy: ObjectPresenceDecisionPolicy
    phase8_repository: Phase8HandoffRepository
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    media_probe: object | None = None
    successor_execution: SuccessorExecutionService | None = None
    successor_readiness: str = _SUCCESSOR_UNAVAILABLE

    def execute(
        self,
        request: Phase7EPublicRequest,
        *,
        create_phase8_handoff: bool = False,
    ) -> Phase7EPublicStatus:
        """Execute one bounded synchronous search and optionally persist handoff."""
        try:
            prepared = self.prepare(
                request.investigation_id,
                request.search_end_time_text,
                request.source_timezone,
                run_id=f"search-run-{token_hex(16)}",
            )
        except Phase7EPublicError as error:
            if error.code in {
                "investigation_not_found",
                "reconfirmation_required",
                "confirmation_corrupt",
                "confirmation_unavailable",
            }:
                raise Phase7EPublicError("baseline_unavailable") from error
            raise
        return self.execute_prepared(
            prepared,
            create_phase8_handoff=create_phase8_handoff,
        )

    def prepare_http(
        self,
        investigation_id: str,
        search_end: str,
        request_id: str,
    ) -> Phase7EPreparedRequest:
        """Reconstruct all server-owned facts for one browser start request."""
        return self.prepare(
            investigation_id,
            search_end,
            None,
            run_id=f"search-run-{request_id.replace('-', '')}",
        )

    def prepare(  # noqa: C901, PLR0912, PLR0915 - strict boundary validation is intentionally explicit.
        self,
        investigation_id: str,
        search_end_time_text: str,
        source_timezone: str | None,
        *,
        run_id: str,
    ) -> Phase7EPreparedRequest:
        """Strictly reopen Phase 6 and build the deterministic Phase 7E plan."""
        try:
            confirmed = self.confirmation_service.load_confirmed(investigation_id)
        except InvestigationConfirmationNotFoundError as error:
            raise Phase7EPublicError("investigation_not_found") from error
        except LegacyInvestigationError as error:
            raise Phase7EPublicError("reconfirmation_required") from error
        except (
            ConfirmationCorruptError,
            ConfirmationArtifactError,
            ConfirmedInputInvalidError,
        ) as error:
            raise Phase7EPublicError("confirmation_corrupt") from error
        except Exception as error:
            raise Phase7EPublicError("confirmation_unavailable") from error
        if source_timezone is not None and source_timezone != confirmed.source_timezone:
            raise Phase7EPublicError("invalid_request")
        now_utc = self.now_utc()
        try:
            end = parse_reference_frame_request(
                channel_id=confirmed.channel_id,
                requested_time_text=search_end_time_text,
                source_timezone=confirmed.source_timezone,
                now_utc=now_utc,
            ).requested_time_utc
            duration_seconds = (end - confirmed.anchor_time_utc).total_seconds()
        except Exception as error:
            raise Phase7EPublicError("invalid_request") from error
        if duration_seconds != int(duration_seconds) or duration_seconds <= 0:
            raise Phase7EPublicError("invalid_request")
        if duration_seconds >= _MAX_SEARCH_SECONDS:
            successor_now_utc = now_utc.astimezone(timezone.utc).replace(microsecond=0)
            try:
                successor_request = SuccessorPlanRequest.from_text(
                    channel_id=confirmed.channel_id,
                    anchor_time_utc=confirmed.anchor_time_utc,
                    search_end_time_text=search_end_time_text,
                    source_timezone=confirmed.source_timezone,
                    now_utc=successor_now_utc,
                )
            except (SuccessorPlanError, TypeError, ValueError) as error:
                raise Phase7EPublicError("invalid_request") from error
            if self.successor_execution is None:
                raise Phase7EPublicError("successor_unavailable")
            try:
                successor = self.successor_execution.prepare(
                    confirmed,
                    search_end_time_text=search_end_time_text,
                    run_id=run_id,
                    now_utc=successor_now_utc,
                )
            except SuccessorExecutionError as error:
                code = str(error)
                raise Phase7EPublicError(
                    code
                    if code in {"confirmation_corrupt", "internal_error"}
                    else "recording_search_execution_unavailable"
                ) from error
            except NvrRequestError as error:
                raise Phase7EPublicError("acquisition_failed") from error
            except Exception as error:
                raise Phase7EPublicError("successor_unavailable") from error
            if successor.plan.search_end_utc != successor_request.search_end_utc:
                raise Phase7EPublicError("request_conflict")
            return Phase7EPreparedRequest(
                successor.request,
                None,
                (),
                (),
                successor,
            )
        if self.classifier is None or self.local_decoder is None:
            raise Phase7EPublicError("recording_search_execution_unavailable")
        readiness_check = getattr(self.classifier, "readiness_error", None)
        if callable(readiness_check):
            readiness_error = readiness_check()
            if readiness_error is not None:
                raise Phase7EPublicError(readiness_error)
        request_domain = CommonSessionRequest(
            investigation_id,
            run_id,
            confirmed.channel_id,
            confirmed.anchor_time_utc,
            end,
            CommonSessionPolicy.from_payload(self.policy.payload),
        )
        from vigi_vision.recording_search_7e_1d import Phase7EC1PlannerAdapter

        bundle = Phase7EC1PlannerAdapter().build(request_domain, self.policy)
        schema5 = StrictIdentityEnvelope.from_payload(
            "schema5-manifest",
            {
                "schema_version": 5,
                "investigation_id": request_domain.investigation_id,
                "run_id": request_domain.run_id,
                "policy_id": self.policy.identity,
                "plan_id": bundle.plan.identity,
                "coarse_target_request_ids": [item.identity for item in bundle.coarse_targets],
            },
        )
        base_records = (self.policy, bundle.plan, *bundle.coarse_targets)
        return Phase7EPreparedRequest(
            request_domain,
            schema5,
            base_records,
            tuple(bundle.coarse_targets),
        )

    def resolve_existing(  # noqa: C901
        self, prepared: Phase7EPreparedRequest
    ) -> Phase7EPublicStatus | None:
        """Resolve a durable retry, interrupting only an unowned active predecessor."""
        if prepared.successor is not None:
            if self.successor_execution is None:
                raise Phase7EPublicError("recording_search_execution_unavailable")
            existing = self.successor_execution.publisher.read(
                prepared.successor.request.investigation_id,
                prepared.successor.request.run_id,
            )
            if existing is None:
                return None
            if existing.get("plan_id") != prepared.successor.plan.plan_id:
                raise Phase7EPublicError("request_conflict")
            if existing.get("status") == "RUNNING":
                raise Phase7EPublicError("already_running")
            return self.status(
                prepared.successor.request.investigation_id,
                prepared.successor.request.run_id,
            )
        request = prepared.request
        try:
            self.repository.ensure_root()
            run = self.repository.inspect_current_read_only(
                request.investigation_id,
                request.run_id,
            )
        except Phase7ENotFoundError:
            return None
        except Phase7EInProgressError as error:
            raise Phase7EPublicError("already_running") from error
        except Phase7ECorruptError as error:
            raise Phase7EPublicError("search_run_corrupt") from error
        schema5 = (
            run.manifest
            if run.is_schema5
            else next(
                (record for record in run.records if record.family == "schema5-manifest"),
                None,
            )
        )
        if (
            prepared.schema5 is None
            or schema5 is None
            or schema5.identity != prepared.schema5.identity
        ):
            raise Phase7EPublicError("request_conflict")
        if run.state.run_state == "RUNNING":
            try:
                _ = self.repository.recover_active(request.investigation_id, request.run_id)
            except Phase7EInProgressError as error:
                raise Phase7EPublicError("already_running") from error
            except Phase7ECorruptError as error:
                raise Phase7EPublicError("search_run_corrupt") from error
        return self.status(request.investigation_id, request.run_id)

    def execute_prepared(  # noqa: C901
        self,
        prepared: Phase7EPreparedRequest,
        *,
        cancellation: Callable[[], bool] | None = None,
        create_phase8_handoff: bool = False,
    ) -> Phase7EPublicStatus:
        """Execute one already validated request under one cancellable invocation."""
        if prepared.successor is not None:
            if self.successor_execution is None:
                raise Phase7EPublicError("recording_search_execution_unavailable")
            try:
                self.successor_execution.execute(prepared.successor, cancellation=cancellation)
            except SuccessorExecutionError as error:
                raise Phase7EPublicError(str(error)) from error
            return self.status(
                prepared.successor.request.investigation_id,
                prepared.successor.request.run_id,
            )
        request_domain = prepared.request
        try:
            with self.executor.invocation(
                request_domain,
                cancellation=cancellation,
            ) as invocation:
                try:
                    admitted = self.executor.execute(
                        request_domain,
                        prepared.schema5,
                        prepared.base_records,
                        self.classifier_policy,
                        prepared.coarse_targets,
                        invocation=invocation,
                    )
                    _ = Phase7E1DService(
                        self.repository,
                        local_evidence=self._local_evidence(),
                    ).execute(invocation, admitted.acquisition)
                except Phase7EInProgressError as error:
                    raise Phase7EPublicError("already_running") from error
                except Phase7EConflictError as error:
                    raise Phase7EPublicError("request_conflict") from error
                except Phase7ECorruptError as error:
                    raise _execution_public_error(
                        error,
                        category="search_run_corrupt",
                    ) from error
                except (
                    NvrRequestError,
                    CommonSessionError,
                    Phase7E1DError,
                    Phase7ERepositoryError,
                    Phase7EValidationError,
                ) as error:
                    raise _execution_public_error(error) from error
        except Phase7EInProgressError as error:
            raise Phase7EPublicError("already_running") from error
        except Phase7EConflictError as error:
            raise Phase7EPublicError("request_conflict") from error
        except Phase7ECorruptError as error:
            raise _execution_public_error(error, category="search_run_corrupt") from error
        except (
            NvrRequestError,
            CommonSessionError,
            Phase7E1DError,
            Phase7ERepositoryError,
            Phase7EValidationError,
        ) as error:
            raise _execution_public_error(error) from error
        if create_phase8_handoff:
            self.create_phase8_handoff(
                request_domain.investigation_id,
                request_domain.run_id,
            )
        return self.status(request_domain.investigation_id, request_domain.run_id)

    def status(self, investigation_id: str, run_id: str) -> Phase7EPublicStatus:
        if self.successor_execution is not None:
            successor_record = self.successor_execution.publisher.read(investigation_id, run_id)
            if successor_record is not None:
                terminal = _successor_public_status(successor_record)
                if terminal is not None:
                    return terminal
        phase7 = read_phase7_status(self.repository, investigation_id, run_id)
        run: Phase7ERun | None = None
        if phase7.schema_version == 7:
            try:
                run = self.repository.inspect_current_read_only(investigation_id, run_id)
            except (Phase7EInProgressError, Phase7ENotFoundError, Phase7ECorruptError):
                run = None
        phase8, reason = self.phase8_repository.status(run, investigation_id, run_id)
        details = None if run is None else self._terminal_details(run)
        return Phase7EPublicStatus(phase7, phase8, reason, details)

    def _terminal_details(self, run: Phase7ERun) -> Phase7ETerminalDetails:
        records = tuple(run.records)
        terminal = next(item for item in records if item.family == "terminal-result")
        session = next(item for item in records if item.family == "common-session")
        start_text = str(session.payload["replay_start_requested_time_utc"])
        end_text = str(session.payload["replay_end_requested_time_utc"])
        start = datetime.strptime(start_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        requested_end = datetime.strptime(end_text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        ticks = int(session.payload["duration_ticks"])
        numerator = ticks * int(session.payload["time_base_num"])
        denominator = int(session.payload["time_base_den"])
        requested_microseconds = int((requested_end - start).total_seconds()) * 1_000_000
        frames = tuple(item for item in records if item.family == "frame")
        maximum_frame_microseconds = max(
            (
                (int(item.payload["raw_pts"]) - int(item.payload["container_start_pts"]))
                * int(item.payload["time_base_num"])
                * 1_000_000
                // int(item.payload["time_base_den"])
            )
            for item in frames
        )
        frame_observed_microseconds = (maximum_frame_microseconds // 1_000_000 + 1) * 1_000_000
        decision_observed_microseconds = min(
            requested_microseconds,
            numerator * 1_000_000 // denominator,
            frame_observed_microseconds,
        )
        observed_microseconds = min(
            requested_microseconds,
            numerator * 1_000_000 // denominator,
            maximum_frame_microseconds,
        )
        observed_end = start + timedelta(microseconds=observed_microseconds)
        observable_seconds = (decision_observed_microseconds + 999_999) // 1_000_000
        confirmation = self.confirmation_service.load_confirmed(run.investigation_id)
        last_present, first_absent = _selected_transition_times(records, terminal, start)
        return Phase7ETerminalDetails(
            last_present,
            first_absent,
            start_text,
            observed_end.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            observable_seconds >= int((requested_end - start).total_seconds()),
            confirmation.source_timezone,
        )

    def recover_abandoned(self) -> int:
        """Interrupt bounded, strictly reopened active runs left by a prior process."""
        recovered = 0
        if self.successor_execution is not None:
            recovered += self.successor_execution.publisher.recover_abandoned()
        try:
            self.repository.ensure_root()
            candidates = _bounded_recovery_candidates(self.repository.root)
        except OSError as error:
            raise Phase7EPublicError("recovery_unavailable") from error
        for investigation_id, run_id in candidates:
            try:
                run = self.repository.inspect_current_read_only(investigation_id, run_id)
                if run.is_schema7 or run.state.run_state != "RUNNING":
                    continue
                interrupted = self.repository.recover_active(investigation_id, run_id)
                if interrupted.state.run_state == "INTERRUPTED":
                    recovered += 1
            except (Phase7ENotFoundError, Phase7EInProgressError, Phase7ECorruptError):
                continue
        return recovered

    def create_phase8_handoff(self, investigation_id: str, run_id: str) -> StrictIdentityEnvelope:
        """Create/reuse a request from strictly reopened terminal evidence."""
        try:
            with self.repository.invocation_ownership(investigation_id, run_id) as ownership:
                run = self.repository.reopen_current(
                    investigation_id,
                    run_id,
                    ownership=ownership,
                )
                return self._create_handoff(run)
        except Phase8LifecycleError as error:
            raise Phase7EPublicError(error.code) from error
        except Phase7EInProgressError as error:
            raise Phase7EPublicError("already_running") from error
        except Phase7ENotFoundError as error:
            raise Phase7EPublicError("run_not_found") from error
        except Phase7ECorruptError as error:
            raise Phase7EPublicError("phase8_corrupt") from error

    def delete_recording_search_media(self, investigation_id: str, run_id: str) -> str:
        """Delete only this run's retained common-session MP4 after explicit request."""
        try:
            with self.repository.invocation_ownership(investigation_id, run_id) as ownership:
                run = self.repository.reopen_current(
                    investigation_id,
                    run_id,
                    ownership=ownership,
                )
                return self.phase8_repository.delete(run)
        except Phase7EPublicError:
            raise
        except Phase8LifecycleError as error:
            raise Phase7EPublicError(error.code) from error
        except Phase7EInProgressError as error:
            raise Phase7EPublicError("already_running") from error
        except Phase7ENotFoundError as error:
            raise Phase7EPublicError("run_not_found") from error
        except Phase7ECorruptError as error:
            raise Phase7EPublicError("phase8_corrupt") from error

    def _local_evidence(self) -> object:
        from vigi_vision.recording_search_7e_1d import Phase7ELocalEvidenceAdapter

        return Phase7ELocalEvidenceAdapter(
            self.repository,
            self.local_decoder,  # type: ignore[arg-type]
            self.classifier,  # type: ignore[arg-type]
        )

    def _create_handoff(self, run: object) -> StrictIdentityEnvelope:
        return self.phase8_repository.create_or_reuse(
            run,
            approved_phase8_media_policy(),
            timeout_seconds=float(self.policy.payload["source_clip_timeout_seconds"]),
        )


def _successor_public_status(record: dict[str, object]) -> Phase7EPublicStatus | None:
    """Project one successor record through the existing public JSON shape."""
    from vigi_vision.recording_search_7e_1d import Phase7EStatus

    status = record.get("status")
    if not isinstance(status, str):
        return None
    phase7 = Phase7EStatus(
        str(record["investigation_id"]),
        str(record["run_id"]),
        int(record["schema_version"]),
        status,
        str(record["reason_code"]) if record.get("reason_code") is not None else None,
        str(record["terminal_result_id"]) if record.get("terminal_result_id") is not None else None,
    )
    if status == "RUNNING":
        return Phase7EPublicStatus(phase7)
    details = Phase7ETerminalDetails(
        record.get("last_present_time_utc")
        if isinstance(record.get("last_present_time_utc"), str)
        else None,
        record.get("first_absent_time_utc")
        if isinstance(record.get("first_absent_time_utc"), str)
        else None,
        str(record["observed_start_time_utc"]),
        str(record["observed_end_time_utc"]),
        bool(record.get("coverage_complete", False)),
        str(record["source_timezone"]),
    )
    return Phase7EPublicStatus(
        phase7,
        str(record.get("phase8_status", "NOT_REQUESTED")),
        str(record.get("phase8_reason", "")),
        details,
    )


def approved_phase7e_policy() -> tuple[
    StrictIdentityEnvelope, StrictIdentityEnvelope, ObjectPresenceDecisionPolicy
]:
    """Return the approved policy snapshots without reading configuration."""
    policy = StrictIdentityEnvelope.from_payload("policy", _policy_payload())
    classifier = StrictIdentityEnvelope.from_payload("classifier-policy", _classifier_payload())
    object_policy = ObjectPresenceDecisionPolicy(minimum_mask_overlap_for_comparison=0.1)
    return policy, classifier, object_policy


def build_phase7e_service(
    *,
    root: Path,
    confirmation_service: object,
    recording_planner: object,
    replay_extractor: object,
    ffmpeg: Path,
    ffprobe: Path,
    mask_predictor: object | None,
    now_utc: Callable[[], datetime] | None = None,
) -> Phase7EPublicService:
    """Compose the public service from existing capture/B4 boundaries."""
    policy, classifier_policy, object_policy = approved_phase7e_policy()
    repository = RecordingSearch7ERepository(root)
    repository.media_root = root / ".media"
    repository.media_probe = FfprobeMediaProbe(ffprobe)
    acquirer = CommonSessionAcquirer(
        recording_planner,
        replay_extractor,
        FfprobeMediaProbe(ffprobe),
    )
    executor = Phase7E1CExecutor(repository, acquirer)
    successor_execution = None
    successor_readiness = _SUCCESSOR_UNAVAILABLE
    successor_failure_stage = "classifier_wiring"
    try:
        if mask_predictor is not None:
            from vigi_vision.recording_search_7e_b4 import _worker_spec
            from vigi_vision.recording_search_successor_acquisition import (
                SuccessorTargetAcquisitionService,
            )
            from vigi_vision.recording_search_successor_classification import (
                SuccessorCoarseClassificationService,
            )
            from vigi_vision.recording_search_successor_narrowing import (
                SuccessorBinaryNarrowingService,
            )

            worker_spec = _worker_spec(mask_predictor)
            successor_classifier = SuccessorB4Classifier(object_policy, worker_spec)
            successor_execution = SuccessorExecutionService(
                SuccessorPlanService(recording_planner),
                SuccessorTargetAcquisitionService(
                    recording_planner,
                    replay_extractor,
                    FfmpegReferenceFrameDecoder(ffmpeg, ffprobe),
                    temporary_directory=root / ".successor-tmp",
                ),
                SuccessorCoarseClassificationService(
                    successor_classifier,
                    InMemoryRgbDecoder(ffmpeg),
                ),
                SuccessorBinaryNarrowingService(
                    SuccessorTargetAcquisitionService(
                        recording_planner,
                        replay_extractor,
                        FfmpegReferenceFrameDecoder(ffmpeg, ffprobe),
                        temporary_directory=root / ".successor-tmp",
                    ),
                    SuccessorCoarseClassificationService(
                        successor_classifier,
                        InMemoryRgbDecoder(ffmpeg),
                    ),
                ),
                InMemoryRgbDecoder(ffmpeg),
                SuccessorTerminalRepository(root / ".successor"),
            )
            successor_readiness = _SUCCESSOR_CONFIGURED
            successor_failure_stage = "none"
    except (TypeError, ValueError):
        successor_execution = None
        successor_readiness = _SUCCESSOR_UNAVAILABLE
        successor_failure_stage = "successor_wiring"
    if successor_execution is None:
        _LOGGER.warning(
            "%s",
            json.dumps(
                {
                    "event": "phase7e.successor_unavailable",
                    "readiness": successor_readiness,
                    "stage": successor_failure_stage,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    return Phase7EPublicService(
        repository,
        executor,
        confirmation_service,
        None
        if mask_predictor is None
        else __import__(
            "vigi_vision.recording_search_7e_b4", fromlist=["Phase7EProductionB4Adapter"]
        ).Phase7EProductionB4Adapter(
            confirmation_service.load_confirmed,
            InMemoryRgbDecoder(ffmpeg),
            mask_predictor,
            object_policy,
        ),
        FfmpegLocalDecoder(ffmpeg, ffprobe),
        policy,
        classifier_policy,
        object_policy,
        Phase8HandoffRepository(
            root / ".phase8",
            root / ".media",
            FfprobeMediaProbe(ffprobe),
            FfmpegSourceClipGenerator(ffmpeg),
        ),
        now_utc or (lambda: datetime.now(timezone.utc)),
        FfprobeMediaProbe(ffprobe),
        successor_execution,
        successor_readiness,
    )


def approved_phase8_media_policy() -> StrictIdentityEnvelope:
    """Return the approved immutable Phase 8 media-generation policy."""
    return StrictIdentityEnvelope.from_payload(
        "media-generation-policy",
        {
            "container": "mp4",
            "stream_copy": {
                "eligible": True,
                "requires_single_video": True,
                "requires_no_audio": True,
                "requires_same_codec_parameters": True,
                "requires_interval_bounds": True,
                "requires_metadata_allowlist": True,
            },
            "reencode": {
                "codec": "h264",
                "encoder": "libx264",
                "profile": "High",
                "level": "4.1",
                "pixel_format": "yuv420p",
                "preset": "medium",
                "crf": 23,
                "frame_rate_source": "selected_stream_avg_frame_rate",
                "vfr_mode": "passthrough",
                "faststart": True,
            },
            "audio": "drop",
            "chapters": "drop",
            "copied_metadata": "drop",
            "interval_tolerance": "one_source_frame",
            "maximum_frame_rate": [60, 1],
            "maximum_duration_seconds": 41,
            "maximum_size_bytes": 536870912,
            "timeout_seconds": 120,
        },
    )


def _policy_payload() -> dict[str, object]:
    return {
        "schema_family": [5, 6, 7],
        "provenance_level": "REQUEST_RELATIVE_ESTIMATE",
        "default_search_duration_seconds": 300,
        "maximum_search_duration_seconds": 600,
        "coarse_interval_seconds": 300,
        "support_count": 3,
        "support_cadence_seconds": 1,
        "binary_stop_seconds": 1,
        "maximum_consecutive_indeterminate_targets": 3,
        "maximum_mp4_bytes": 4294967296,
        "maximum_process_memory_bytes": 2147483648,
        "maximum_selected_rgb24_frames": 12,
        "maximum_targets_per_decoder_pass": 32,
        "maximum_decoder_passes": 11,
        "maximum_classifications": 32,
        "replay_margin_seconds": 40,
        "ffprobe_timeout_seconds": 20,
        "decoder_timeout_seconds": 120,
        "classifier_timeout_seconds": 30,
        "classifier_total_budget_seconds": 320,
        "terminal_interpretation_seconds": 10,
        "publication_seconds": 10,
        "strict_readback_seconds": 20,
        "source_clip_timeout_seconds": 120,
        "cleanup_reserve_seconds": 60,
        "invocation_deadline_seconds": 2520,
        "phase8_retry_deadline_seconds": 180,
        "source_clip_pre_seconds": 10,
        "source_clip_post_seconds": 30,
        "maximum_found_interval_seconds": 1,
        "maximum_source_clip_seconds": 41,
        "maximum_source_clip_bytes": 536870912,
        "maximum_source_frame_rate": [60, 1],
    }


def _classifier_payload() -> dict[str, object]:
    return {
        "classifier_family": "efficient-sam-ti-roi-ncc",
        "implementation_version": 1,
        "implementation_source_commit": "d525f622e6f640acf5a0fc37c7ca1f243da5bde0",
        "checkpoint_logical_name": "efficient_sam_vitt.pt",
        "checkpoint_sha256": "dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a",
        "runtime": {
            "python": "3.11",
            "torch": "2.10.0+cpu",
            "torchvision": "0.25.0+cpu",
            "pillow": "12.3.0",
            "numpy": "2.4.6",
            "device": "cpu",
            "tensor_dtype": "float32",
            "comparison_dtype": "float64",
        },
        "input": {
            "color_space": "RGB",
            "channel_order": "RGB",
            "normalization": "torchvision.to_tensor uint8/255",
            "resize": "none before upstream model preprocessing",
            "interpolation": "upstream commit-owned",
            "positive_point_shape": [1, 1, 1, 2],
            "point_label_shape": [1, 1, 1],
            "positive_point_label": 1,
            "prompt": "confirmed_roi_center_v1",
        },
        "mask": {
            "logit_threshold": "0.000000",
            "candidate_selection": "highest predicted_iou among valid candidates",
            "must_contain_prompt": True,
            "minimum_width": 4,
            "minimum_height": 4,
            "minimum_pixel_count": 64,
            "maximum_source_coverage": "0.950000",
            "alignment": "source_pixel_grid",
        },
        "comparison": {
            "roi_preprocessing": "phase7b-roi-luma-v1",
            "luma_coefficients": [299, 587, 114],
            "luma_divisor": 1000,
            "luma_rounding": "add_500_then_floor",
            "ncc_area": "mask_intersection",
            "minimum_overlap_fraction": "0.100000",
            "minimum_effective_area_pixels": 64,
            "metric_rounding": "half_even",
            "decimal_places": 6,
        },
        "decision": {
            "present_min_iou": "0.500000",
            "present_min_ncc": "0.600000",
            "absent_max_iou": "0.100000",
            "absent_max_ncc": "0.200000",
            "otherwise": "INDETERMINATE",
        },
        "execution": {
            "timeout_seconds": 30,
            "maximum_attempts": 1,
            "maximum_concurrent_attempts": 1,
            "late_result": "revoked",
            "timeout_result": "OPERATIONAL",
            "unknown_result": "OPERATIONAL_INVALID",
            "retry": "new_run_only",
        },
    }


__all__ = [
    "Phase7EPreparedRequest",
    "Phase7EPublicError",
    "Phase7EPublicRequest",
    "Phase7EPublicService",
    "Phase7EPublicStatus",
    "Phase7ETerminalDetails",
    "Phase8HandoffRepository",
    "approved_phase7e_policy",
    "approved_phase8_media_policy",
    "build_phase7e_service",
]
