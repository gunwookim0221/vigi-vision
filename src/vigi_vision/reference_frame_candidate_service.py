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
            try:
                resolution = self.executor.execute_or_resolve(candidate.request)
            except _RECOVERABLE_CANDIDATE_ERRORS as error:
                safe_error = domain_error(error)
                items.append(
                    ReferenceFrameCandidateFailure(candidate, safe_error.code, safe_error.message)
                )
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
