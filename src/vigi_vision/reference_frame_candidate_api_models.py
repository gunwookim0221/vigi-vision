"""Pydantic HTTP schemas for bounded reference-frame candidate sets."""

from datetime import datetime, timezone
from typing import Annotated, ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from typing_extensions import Self

from vigi_vision.reference_frame_api_models import (
    ReferenceFrameCreateResponse,
    reference_frame_response,
)
from vigi_vision.reference_frame_candidate_models import (
    DEFAULT_CANDIDATE_OFFSETS,
    ReferenceFrameCandidateSetRequest,
)
from vigi_vision.reference_frame_candidate_service import (
    ReferenceFrameCandidateSetResult,
    ReferenceFrameCandidateSuccess,
)
from vigi_vision.reference_frame_models import (
    ReferenceFrameOutcome,
    parse_reference_frame_request,
)

_MINIMUM_OFFSET_SECONDS: Final = -300
_MAXIMUM_OFFSET_SECONDS: Final = 300
_MINIMUM_CANDIDATE_COUNT: Final = 1
_MAXIMUM_CANDIDATE_COUNT: Final = 5
_DUPLICATE_OFFSETS_MESSAGE: Final = "offsets_seconds must not contain duplicates"

CandidateOffsetSeconds = Annotated[
    StrictInt,
    Field(ge=_MINIMUM_OFFSET_SECONDS, le=_MAXIMUM_OFFSET_SECONDS),
]


class ReferenceFrameCandidateSetBody(BaseModel):
    """The bounded user-controlled input for one synchronous candidate-set request."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    channel_id: int = Field(gt=0, description="Positive NVR channel identifier.")
    reference_time: str = Field(
        description="Whole-second ISO 8601/RFC 3339 anchor; naïve input defaults to Asia/Seoul."
    )
    source_timezone: str | None = Field(
        default=None,
        description="Optional IANA source timezone, checked against aware input when supplied.",
    )
    offsets_seconds: tuple[CandidateOffsetSeconds, ...] | None = Field(
        default=None,
        min_length=_MINIMUM_CANDIDATE_COUNT,
        max_length=_MAXIMUM_CANDIDATE_COUNT,
        description="Ordered unique requested offsets in seconds; omission uses the default five.",
    )

    @model_validator(mode="after")
    def require_unique_offsets(self) -> Self:
        """Reject duplicate candidate positions without reordering the request."""
        if self.offsets_seconds is not None and len(set(self.offsets_seconds)) != len(
            self.offsets_seconds
        ):
            raise ValueError(_DUPLICATE_OFFSETS_MESSAGE)
        return self


class ReferenceFrameCandidateFailureResponse(BaseModel):
    """One fixed safe failure for a candidate request position."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


class ReferenceFrameCandidateSuccessResponse(BaseModel):
    """One successful candidate represented by the existing frame response shape."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    offset_seconds: int
    candidate_requested_time_utc: str
    status: Literal["succeeded"]
    outcome: ReferenceFrameOutcome
    reference_frame: ReferenceFrameCreateResponse
    warnings: tuple[str, ...]


class ReferenceFrameCandidateFailureItemResponse(BaseModel):
    """One safe candidate failure without a durable reference-frame resource."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    offset_seconds: int
    candidate_requested_time_utc: str
    status: Literal["failed"]
    failure: ReferenceFrameCandidateFailureResponse
    warnings: tuple[str, ...]


ReferenceFrameCandidateItemResponse = Annotated[
    ReferenceFrameCandidateSuccessResponse | ReferenceFrameCandidateFailureItemResponse,
    Field(discriminator="status"),
]


class ReferenceFrameCandidateSetSummaryResponse(BaseModel):
    """Counts that summarize ordered candidate outcomes."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    created: int = Field(ge=0)
    reused: int = Field(ge=0)
    failed: int = Field(ge=0)


class ReferenceFrameCandidateSetResponse(BaseModel):
    """A complete ordered candidate-set response around one normalized anchor."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    reference_time_utc: str
    source_timezone: str
    offsets_seconds: tuple[int, ...]
    candidates: tuple[ReferenceFrameCandidateItemResponse, ...]
    summary: ReferenceFrameCandidateSetSummaryResponse


def reference_frame_candidate_set_response(
    result: ReferenceFrameCandidateSetResult,
) -> ReferenceFrameCandidateSetResponse:
    """Serialize ordered candidate outcomes without exposing internal paths or failures."""
    candidates: list[ReferenceFrameCandidateItemResponse] = []
    for item in result.items:
        if isinstance(item, ReferenceFrameCandidateSuccess):
            candidate = item.candidate
            resolution = item.resolution
            candidates.append(
                ReferenceFrameCandidateSuccessResponse(
                    offset_seconds=candidate.offset_seconds,
                    candidate_requested_time_utc=candidate.request.requested_time_utc.isoformat(),
                    status="succeeded",
                    outcome=resolution.outcome,
                    reference_frame=reference_frame_response(resolution),
                    warnings=(),
                )
            )
        else:
            candidate = item.candidate
            candidates.append(
                ReferenceFrameCandidateFailureItemResponse(
                    offset_seconds=candidate.offset_seconds,
                    candidate_requested_time_utc=candidate.request.requested_time_utc.isoformat(),
                    status="failed",
                    failure=ReferenceFrameCandidateFailureResponse(
                        code=item.code,
                        message=item.message,
                    ),
                    warnings=(),
                )
            )
    return ReferenceFrameCandidateSetResponse(
        reference_time_utc=result.request.reference_time.requested_time_utc.isoformat(),
        source_timezone=result.request.reference_time.source_timezone,
        offsets_seconds=result.request.offsets_seconds,
        candidates=tuple(candidates),
        summary=ReferenceFrameCandidateSetSummaryResponse(
            created=result.summary.created,
            reused=result.summary.reused,
            failed=result.summary.failed,
        ),
    )


def parse_reference_frame_candidate_set_request(
    *,
    body: ReferenceFrameCandidateSetBody,
    now_utc: datetime | None = None,
) -> ReferenceFrameCandidateSetRequest:
    """Normalize one anchor once and attach accepted bounded offsets."""
    comparison_now_utc = datetime.now(timezone.utc) if now_utc is None else now_utc
    reference_time = parse_reference_frame_request(
        channel_id=body.channel_id,
        requested_time_text=body.reference_time,
        source_timezone=body.source_timezone,
        now_utc=comparison_now_utc,
    )
    offsets_seconds = (
        DEFAULT_CANDIDATE_OFFSETS if body.offsets_seconds is None else body.offsets_seconds
    )
    return ReferenceFrameCandidateSetRequest(reference_time, offsets_seconds, comparison_now_utc)
