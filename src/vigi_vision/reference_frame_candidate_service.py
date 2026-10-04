"""Serial orchestration of existing single-frame work for candidate sets."""

from dataclasses import dataclass, field
from typing import Final, final

from vigi_vision.ffmpeg import FfmpegUnavailableError
from vigi_vision.nvr import NvrRequestError
from vigi_vision.recording import RecordingDataError, RecordingUnavailableError
from vigi_vision.reference_frame_api_errors import domain_error
from vigi_vision.reference_frame_candidate_models import (
    ReferenceFrameCandidateRequest,
    ReferenceFrameCandidateSetRequest,
)
from vigi_vision.reference_frame_models import (
    ReferenceFrameError,
    ReferenceFrameOutcome,
    ReferenceFrameResolution,
)
from vigi_vision.reference_frame_nvr_acquisition_diagnostics import (
    NvrAcquisitionDiagnosticCapture,
    NvrAcquisitionDiagnosticOperation,
    NvrAcquisitionDiagnosticStage,
    NvrAcquisitionDiagnosticWriter,
    candidate_nvr_acquisition_scope,
    record_nvr_acquisition_failure,
)
from vigi_vision.reference_frame_service import ReferenceFrameExecutionBoundary
from vigi_vision.replay import (
    ReplayAuthenticationError,
    ReplayExtractionError,
    ReplayTimeoutError,
    ReplayUnavailableError,
)

_INVALID_CANDIDATE_TIME_CODE: Final = "invalid_candidate_time"
_INVALID_CANDIDATE_TIME_MESSAGE: Final = "The candidate requested time is invalid."
_RECOVERABLE_CANDIDATE_ERRORS: Final = (
    ReferenceFrameError,
    FfmpegUnavailableError,
    NvrRequestError,
    RecordingDataError,
    RecordingUnavailableError,
    ReplayAuthenticationError,
    ReplayExtractionError,
    ReplayTimeoutError,
    ReplayUnavailableError,
)


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameCandidateSuccess:
    """One child result returned by the established single-frame execution boundary."""

    candidate: ReferenceFrameCandidateRequest
    resolution: ReferenceFrameResolution


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameCandidateFailure:
    """One fixed safe candidate failure that does not expose an exception."""

    candidate: ReferenceFrameCandidateRequest
    code: str
    message: str


ReferenceFrameCandidateResult = ReferenceFrameCandidateSuccess | ReferenceFrameCandidateFailure


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameCandidateSetSummary:
    """Created, reused, and failed totals for one ordered candidate set."""

    created: int
    reused: int
    failed: int


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameCandidateSetResult:
    """All candidate outcomes for one normalized anchor."""

    request: ReferenceFrameCandidateSetRequest
    items: tuple[ReferenceFrameCandidateResult, ...]
    summary: ReferenceFrameCandidateSetSummary


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameCandidateSetService:
    """Reuse one-frame execution serially without media or artifact implementation."""

    executor: ReferenceFrameExecutionBoundary = field(repr=False)
    diagnostic_store: NvrAcquisitionDiagnosticWriter | None = field(default=None, repr=False)

    def execute(
        self, request: ReferenceFrameCandidateSetRequest
    ) -> ReferenceFrameCandidateSetResult:
        """Execute every accepted candidate in order, isolating known media failures."""
        items: list[ReferenceFrameCandidateResult] = []
        for candidate in request.candidates():
            if candidate.request.requested_time_utc > request.comparison_now_utc:
                items.append(
                    ReferenceFrameCandidateFailure(
                        candidate,
                        _INVALID_CANDIDATE_TIME_CODE,
                        _INVALID_CANDIDATE_TIME_MESSAGE,
                    )
                )
                continue
            with candidate_nvr_acquisition_scope(
                channel_id=candidate.request.channel_id,
                requested_time_utc=request.reference_time.requested_time_utc,
                candidate_offset_seconds=candidate.offset_seconds,
                candidate_time_utc=candidate.request.requested_time_utc,
            ) as diagnostic_capture:
                try:
                    resolution = self.executor.execute_or_resolve(candidate.request)
                except _RECOVERABLE_CANDIDATE_ERRORS as error:
                    if isinstance(error, NvrRequestError):
                        record_nvr_acquisition_failure(
                            stage=NvrAcquisitionDiagnosticStage.UNKNOWN_NVR_REQUEST,
                            operation=NvrAcquisitionDiagnosticOperation.UNKNOWN_NVR_REQUEST,
                            error_kind=error.kind,
                            exception_class_name=error.exception_type,
                        )
                    _persist_diagnostic(self.diagnostic_store, diagnostic_capture)
                    safe_error = domain_error(error)
                    items.append(
                        ReferenceFrameCandidateFailure(
                            candidate, safe_error.code, safe_error.message
                        )
                    )
                except Exception:
                    _persist_diagnostic(self.diagnostic_store, diagnostic_capture)
                    raise
                else:
                    items.append(ReferenceFrameCandidateSuccess(candidate, resolution))
        return ReferenceFrameCandidateSetResult(request, tuple(items), _summary(tuple(items)))


def _summary(items: tuple[ReferenceFrameCandidateResult, ...]) -> ReferenceFrameCandidateSetSummary:
    successful_outcomes = tuple(
        item.resolution.outcome
        for item in items
        if isinstance(item, ReferenceFrameCandidateSuccess)
    )
    return ReferenceFrameCandidateSetSummary(
        created=successful_outcomes.count(ReferenceFrameOutcome.CREATED),
        reused=successful_outcomes.count(ReferenceFrameOutcome.REUSED),
        failed=sum(isinstance(item, ReferenceFrameCandidateFailure) for item in items),
    )


def _persist_diagnostic(
    store: NvrAcquisitionDiagnosticWriter | None,
    capture: NvrAcquisitionDiagnosticCapture | None,
) -> None:
    diagnostic = None if capture is None else capture.diagnostic
    if store is None or diagnostic is None:
        return
    try:
        store.write(diagnostic)
    except Exception:  # noqa: BLE001 - diagnostic persistence cannot replace the candidate error.
        return
