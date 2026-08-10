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

from vigi_vision.assisted_roi_service import (
    RoiSuggestionInferenceError,
    RoiSuggestionInvalidPointError,
    RoiSuggestionNoValidSuggestionError,
    RoiSuggestionTimeoutError,
    RoiSuggestionUnavailableError,
)
from vigi_vision.ffmpeg import FfmpegUnavailableError
from vigi_vision.investigation_confirmation_models import (
    ConfirmationArtifactError,
    ConfirmationCandidateMismatchError,
    ConfirmationConflictError,
    ConfirmationCorruptError,
    ConfirmationError,
    ConfirmationImageDimensionMismatchError,
    ConfirmationInProgressError,
    ConfirmationInvalidRoiError,
    ConfirmationRequestError,
    ConfirmedInputInvalidError,
    InvestigationConfirmationNotFoundError,
    LegacyInvestigationError,
)
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
_CONFIRMATION_INVALID_MESSAGE: Final = "The investigation confirmation request is invalid."
_CONFIRMATION_NOT_FOUND_MESSAGE: Final = "The confirmed investigation was not found."
_METHOD_NOT_ALLOWED_MESSAGE: Final = "The requested HTTP method is not allowed."
_CONFIRMATION_API_PREFIX: Final = "/api/v1/investigation-confirmations"
_RECORDING_SEARCH_API_PREFIX: Final = "/api/v1/recording-searches"


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
        (ConfirmationRequestError, ConfirmationInvalidRoiError),
        ReferenceFrameApiError(422, "invalid_confirmation", _CONFIRMATION_INVALID_MESSAGE),
    ),
    _DomainErrorMapping(
        (ConfirmationCandidateMismatchError, ConfirmationImageDimensionMismatchError),
        ReferenceFrameApiError(
            409,
            "stale_selection",
            "The selected reference frame no longer matches the confirmation.",
        ),
    ),
    _DomainErrorMapping(
        (ConfirmationConflictError,),
        ReferenceFrameApiError(
            409,
            "confirmation_conflict",
            "This investigation already has different confirmed conditions.",
        ),
    ),
    _DomainErrorMapping(
        (ConfirmationInProgressError,),
        ReferenceFrameApiError(
            409,
            "confirmation_in_progress",
            "Investigation confirmation is already in progress.",
        ),
    ),
    _DomainErrorMapping(
        (InvestigationConfirmationNotFoundError, LegacyInvestigationError),
        ReferenceFrameApiError(404, "investigation_not_found", _CONFIRMATION_NOT_FOUND_MESSAGE),
    ),
    _DomainErrorMapping(
        (ConfirmationCorruptError,),
        ReferenceFrameApiError(
            500,
            "confirmation_corrupt",
            "The stored investigation confirmation is corrupt.",
        ),
    ),
    _DomainErrorMapping(
        (ConfirmationArtifactError,),
        ReferenceFrameApiError(
            500,
            "artifact_failure",
            "Investigation confirmation could not be published safely.",
        ),
    ),
    _DomainErrorMapping(
        (ConfirmedInputInvalidError,),
        ReferenceFrameApiError(
            500,
            "resource_corrupt",
            "The stored reference-frame resource could not be read safely.",
        ),
    ),
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
        (RoiSuggestionInvalidPointError,),
        ReferenceFrameApiError(
            _UNPROCESSABLE_CONTENT,
            "invalid_point",
            "The assisted ROI point is outside the reference-frame image.",
        ),
    ),
    _DomainErrorMapping(
        (RoiSuggestionUnavailableError,),
        ReferenceFrameApiError(
            503,
            "suggestion_unavailable",
            "Assisted ROI suggestions are unavailable.",
        ),
    ),
    _DomainErrorMapping(
        (RoiSuggestionNoValidSuggestionError,),
        ReferenceFrameApiError(
            _UNPROCESSABLE_CONTENT,
            "no_valid_suggestion",
            "No valid assisted ROI suggestion is available for this point.",
        ),
    ),
    _DomainErrorMapping(
        (RoiSuggestionTimeoutError,),
        ReferenceFrameApiError(
            504,
            "suggestion_timeout",
            "Assisted ROI inference timed out safely.",
        ),
    ),
    _DomainErrorMapping(
        (RoiSuggestionInferenceError,),
        ReferenceFrameApiError(
            500,
            "suggestion_failure",
            "Assisted ROI inference failed safely.",
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


async def safe_error_response(request: Request, error: Exception) -> JSONResponse:
    """Return the stable safe envelope for all request and domain failures."""
    match error:
        case RequestValidationError():
            if request.url.path == _CONFIRMATION_API_PREFIX or request.url.path.startswith(
                f"{_CONFIRMATION_API_PREFIX}/"
            ):
                issues: Sequence[ErrorDetails] = error.errors()
                malformed_json = "json_invalid" in {issue["type"] for issue in issues}
                if malformed_json:
                    return ReferenceFrameApiError(
                        _BAD_REQUEST,
                        "invalid_request",
                        _CONFIRMATION_INVALID_MESSAGE,
                    ).response()
                return ReferenceFrameApiError(
                    _UNPROCESSABLE_CONTENT,
                    "invalid_confirmation",
                    _CONFIRMATION_INVALID_MESSAGE,
                ).response()
            if request.url.path == _RECORDING_SEARCH_API_PREFIX or request.url.path.startswith(
                f"{_RECORDING_SEARCH_API_PREFIX}/"
            ):
                return _recording_search_validation_error(error).response()
            return _validation_error(error).response()
        case _:
            return domain_error(error).response()


def domain_error(error: Exception) -> ReferenceFrameApiError:
    """Map known domain failures and unexpected exceptions without raw diagnostics."""
    match error:
        case StarletteHttpException(status_code=404):
            return ReferenceFrameApiError(404, "resource_not_found", _RESOURCE_NOT_FOUND_MESSAGE)
        case StarletteHttpException(status_code=405):
            return ReferenceFrameApiError(405, "method_not_allowed", _METHOD_NOT_ALLOWED_MESSAGE)
        case _:
            pass
    for mapping in _DOMAIN_ERROR_MAPPINGS:
        if isinstance(error, mapping.exception_types):
            return mapping.api_error
    return _INTERNAL_ERROR


def confirmation_domain_error(error: Exception) -> ReferenceFrameApiError:
    """Map confirmation failures while preserving the existing API mappings."""
    if isinstance(error, ReferenceFrameInputError):
        return ReferenceFrameApiError(422, "invalid_confirmation", _CONFIRMATION_INVALID_MESSAGE)
    if isinstance(error, ConfirmationError):
        return domain_error(error)
    return domain_error(error)


def _validation_error(error: RequestValidationError) -> ReferenceFrameApiError:
    """Classify FastAPI body failures without including rejected values or parser text."""
    issues: Sequence[ErrorDetails] = error.errors()
    issue_types = {issue["type"] for issue in issues}
    malformed_json = "json_invalid" in issue_types
    status_code = _BAD_REQUEST if malformed_json else _UNPROCESSABLE_CONTENT
    code = "malformed_json" if malformed_json else "invalid_request"
    return ReferenceFrameApiError(status_code, code, _INVALID_REQUEST_MESSAGE)


def _recording_search_validation_error(error: RequestValidationError) -> ReferenceFrameApiError:
    issues: Sequence[ErrorDetails] = error.errors()
    malformed_json = "json_invalid" in {issue["type"] for issue in issues}
    if malformed_json:
        return ReferenceFrameApiError(
            _BAD_REQUEST,
            "invalid_request",
            "The recording-search request is invalid.",
        )
    return ReferenceFrameApiError(
        _UNPROCESSABLE_CONTENT,
        "invalid_request",
        "The recording-search request is invalid.",
    )
