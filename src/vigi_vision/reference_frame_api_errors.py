"""Centralized credential-safe translation from domain failures to HTTP responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, final

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHttpException

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import Request
    from pydantic_core import ErrorDetails

from vigi_vision.ffmpeg import FfmpegUnavailableError
from vigi_vision.nvr import NvrRequestError
from vigi_vision.recording import RecordingDataError, RecordingUnavailableError
from vigi_vision.reference_frame_api_models import (
    ReferenceFrameErrorBody,
    ReferenceFrameErrorResponse,
)
from vigi_vision.reference_frame_decoder import ReferenceFrameDecodeTimeoutError
from vigi_vision.reference_frame_models import (
    ReferenceFrameArtifactConflictError,
    ReferenceFrameArtifactError,
    ReferenceFrameChannelNotFoundError,
    ReferenceFrameCleanupError,
    ReferenceFrameDecodeError,
    ReferenceFrameInputError,
    ReferenceFrameNoCandidateError,
    ReferenceFrameResourceCorruptError,
    ReferenceFrameResourceIncompatibleError,
    ReferenceFrameResourceNotFoundError,
    ReferenceFrameSegmentMismatchError,
    UnsupportedReferenceFrameSourceError,
)
from vigi_vision.replay import (
    ReplayAuthenticationError,
    ReplayExtractionError,
    ReplayTimeoutError,
    ReplayUnavailableError,
)

_BAD_REQUEST: Final = 400
_UNPROCESSABLE_CONTENT: Final = 422
_INVALID_REQUEST_MESSAGE: Final = "The reference-frame request is invalid."
_RECORDING_UNAVAILABLE_MESSAGE: Final = "No recording is available for the requested time."
_RESOURCE_NOT_FOUND_MESSAGE: Final = "The requested reference-frame resource was not found."
_REPLAY_FAILURE_MESSAGE: Final = "The recording replay could not be processed safely."
_DECODE_FAILURE_MESSAGE: Final = "The replay clip could not be decoded into a reference frame."
_INTERNAL_ERROR_MESSAGE: Final = "The reference-frame operation could not be completed safely."


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameApiError:
    """A complete fixed HTTP error chosen without serializing an exception."""

    status_code: int
    code: str
    message: str

    def response(self) -> JSONResponse:
        """Build the documented safe error envelope."""
        body = ReferenceFrameErrorResponse(
            error=ReferenceFrameErrorBody(code=self.code, message=self.message)
        )
        return JSONResponse(status_code=self.status_code, content=body.model_dump(mode="json"))


@final
@dataclass(frozen=True, slots=True)
class _DomainErrorMapping:
    exception_types: tuple[type[Exception], ...]
    api_error: ReferenceFrameApiError


_DOMAIN_ERROR_MAPPINGS: Final[tuple[_DomainErrorMapping, ...]] = (
    _DomainErrorMapping(
        (ReferenceFrameInputError,),
        ReferenceFrameApiError(_UNPROCESSABLE_CONTENT, "invalid_request", _INVALID_REQUEST_MESSAGE),
    ),
    _DomainErrorMapping(
        (UnsupportedReferenceFrameSourceError,),
        ReferenceFrameApiError(
            _BAD_REQUEST,
            "unsupported_source",
            "Reference-frame extraction is available only for NVR recordings.",
        ),
    ),
    _DomainErrorMapping(
        (ReferenceFrameChannelNotFoundError,),
        ReferenceFrameApiError(
            404,
            "channel_not_found",
            "The requested NVR channel was not found.",
        ),
    ),
    _DomainErrorMapping(
        (RecordingUnavailableError, ReplayUnavailableError),
        ReferenceFrameApiError(404, "recording_unavailable", _RECORDING_UNAVAILABLE_MESSAGE),
    ),
    _DomainErrorMapping(
        (ReferenceFrameResourceNotFoundError,),
        ReferenceFrameApiError(404, "resource_not_found", _RESOURCE_NOT_FOUND_MESSAGE),
    ),
    _DomainErrorMapping(
        (ReferenceFrameArtifactConflictError,),
        ReferenceFrameApiError(
            409,
            "artifact_conflict",
            "A completed reference-frame resource could not be resolved safely.",
        ),
    ),
    _DomainErrorMapping(
        (ReferenceFrameResourceIncompatibleError,),
        ReferenceFrameApiError(
            409,
            "incompatible_resource",
            "The existing reference-frame resource is not compatible with this request.",
        ),
    ),
    _DomainErrorMapping(
        (ReferenceFrameResourceCorruptError,),
        ReferenceFrameApiError(
            500,
            "resource_corrupt",
            "The stored reference-frame resource could not be read safely.",
        ),
    ),
    _DomainErrorMapping(
        (ReferenceFrameNoCandidateError,),
        ReferenceFrameApiError(
            _UNPROCESSABLE_CONTENT,
            "no_acceptable_frame",
            "No acceptable decoded frame is available for the requested recording time.",
        ),
    ),
    _DomainErrorMapping(
        (ReplayTimeoutError,),
        ReferenceFrameApiError(504, "replay_timeout", "Replay extraction timed out safely."),
    ),
    _DomainErrorMapping(
        (ReferenceFrameDecodeTimeoutError,),
        ReferenceFrameApiError(504, "decode_timeout", "Reference-frame decoding timed out safely."),
    ),
    _DomainErrorMapping(
        (FfmpegUnavailableError,),
        ReferenceFrameApiError(
            503,
            "media_tool_unavailable",
            "The required media tool is unavailable.",
        ),
    ),
    _DomainErrorMapping(
        (NvrRequestError,),
        ReferenceFrameApiError(503, "nvr_unavailable", "The NVR could not be reached safely."),
    ),
    _DomainErrorMapping(
        (
            RecordingDataError,
            ReplayAuthenticationError,
            ReplayExtractionError,
            ReferenceFrameSegmentMismatchError,
        ),
        ReferenceFrameApiError(503, "replay_failure", _REPLAY_FAILURE_MESSAGE),
    ),
    _DomainErrorMapping(
        (ReferenceFrameDecodeError,),
        ReferenceFrameApiError(503, "decode_failure", _DECODE_FAILURE_MESSAGE),
    ),
    _DomainErrorMapping(
        (ReferenceFrameArtifactError, ReferenceFrameCleanupError),
        ReferenceFrameApiError(500, "artifact_failure", _INTERNAL_ERROR_MESSAGE),
    ),
)
_INTERNAL_ERROR: Final = ReferenceFrameApiError(500, "internal_error", _INTERNAL_ERROR_MESSAGE)


async def safe_error_response(_: Request, error: Exception) -> JSONResponse:
    """Return the stable safe envelope for all request and domain failures."""
    match error:
        case RequestValidationError():
            return _validation_error(error).response()
        case _:
            return domain_error(error).response()


def domain_error(error: Exception) -> ReferenceFrameApiError:
    """Map known domain failures and unexpected exceptions without raw diagnostics."""
    match error:
        case StarletteHttpException(status_code=404):
            return ReferenceFrameApiError(404, "resource_not_found", _RESOURCE_NOT_FOUND_MESSAGE)
        case _:
            pass
    for mapping in _DOMAIN_ERROR_MAPPINGS:
        if isinstance(error, mapping.exception_types):
            return mapping.api_error
    return _INTERNAL_ERROR


def _validation_error(error: RequestValidationError) -> ReferenceFrameApiError:
    """Classify FastAPI body failures without including rejected values or parser text."""
    issues: Sequence[ErrorDetails] = error.errors()
    issue_types = {issue["type"] for issue in issues}
    malformed_json = "json_invalid" in issue_types
    status_code = _BAD_REQUEST if malformed_json else _UNPROCESSABLE_CONTENT
    code = "malformed_json" if malformed_json else "invalid_request"
    return ReferenceFrameApiError(status_code, code, _INVALID_REQUEST_MESSAGE)
