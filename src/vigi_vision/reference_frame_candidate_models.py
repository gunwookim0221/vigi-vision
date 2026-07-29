"""Typed bounded request values for recorded reference-frame candidate sets."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, final

from vigi_vision.reference_frame_models import (
    ReferenceFrameInputError,
    ReferenceFrameRequest,
)

DEFAULT_CANDIDATE_OFFSETS: Final[tuple[int, ...]] = (-60, -10, 0, 10, 60)
MAX_CANDIDATE_COUNT: Final = 5
MAX_ABSOLUTE_OFFSET_SECONDS: Final = 300
_INVALID_CANDIDATE_OFFSETS: Final = "invalid_candidate_offsets"


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameCandidateRequest:
    """One ordered candidate request derived from a normalized reference instant."""

    offset_seconds: int
    request: ReferenceFrameRequest


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameCandidateSetRequest:
    """Validated candidate-set input that preserves a single normalized anchor."""

    reference_time: ReferenceFrameRequest
    offsets_seconds: tuple[int, ...]
    comparison_now_utc: datetime = field(repr=False)

    def __post_init__(self) -> None:
        """Reject unbounded or duplicate offsets before candidate execution."""
        if (
            not self.offsets_seconds
            or len(self.offsets_seconds) > MAX_CANDIDATE_COUNT
            or len(set(self.offsets_seconds)) != len(self.offsets_seconds)
            or self.comparison_now_utc.tzinfo is None
        ):
            raise ReferenceFrameInputError(_INVALID_CANDIDATE_OFFSETS)
        for offset_seconds in self.offsets_seconds:
            if type(offset_seconds) is not int or abs(offset_seconds) > MAX_ABSOLUTE_OFFSET_SECONDS:
                raise ReferenceFrameInputError(_INVALID_CANDIDATE_OFFSETS)
            _ = self.reference_time.with_offset(offset_seconds)

    def candidates(self) -> tuple[ReferenceFrameCandidateRequest, ...]:
        """Derive complete ordered child requests from the normalized anchor."""
        return tuple(
            ReferenceFrameCandidateRequest(
                offset_seconds,
                self.reference_time.with_offset(offset_seconds),
            )
            for offset_seconds in self.offsets_seconds
        )
