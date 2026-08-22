"""FastAPI transport for recording-search lifecycle operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from anyio.to_thread import run_sync
from fastapi import APIRouter, FastAPI, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from vigi_vision.recording_search_a2_models import RecordingSearchManifestV2
from vigi_vision.recording_search_d2_status import RecordingSearchStatusV4
from vigi_vision.recording_search_models import (
    ReconfirmationRequiredError,
    RecordingSearchArtifactError,
    RecordingSearchBaselineError,
    RecordingSearchError,
    RecordingSearchManifest,
    RecordingSearchManifestCorruptError,
    RecordingSearchNotFoundError,
    RecordingSearchOutcome,
    RecordingSearchRequest,
    RecordingSearchTerminalReopenError,
    RecordingSearchTransitionError,
)
from vigi_vision.reference_frame_api_errors import ReferenceFrameApiError
from vigi_vision.reference_frame_api_models import ReferenceFrameErrorResponse

if TYPE_CHECKING:
    import anyio

    from vigi_vision.recording_search_service import RecordingSearchService


class RecordingSearchRequestBody(BaseModel):
    """Strict start-request body."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    investigation_id: StrictStr = Field(min_length=1, max_length=128)
    search_end_time_text: StrictStr = Field(min_length=1, max_length=128)
    source_timezone: StrictStr = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_domain_fields(self) -> RecordingSearchRequestBody:
        """Reject identities that cannot form a domain request."""
        _ = RecordingSearchRequest(
            investigation_id=self.investigation_id,
            search_end_time_text=self.search_end_time_text,
            source_timezone=self.source_timezone,
        )
        return self

    def to_domain(self) -> RecordingSearchRequest:
        """Convert the transport body to the service request."""
        return RecordingSearchRequest(
            investigation_id=self.investigation_id,
            search_end_time_text=self.search_end_time_text,
            source_timezone=self.source_timezone,
        )


@dataclass(frozen=True, slots=True)
class RecordingSearchApiDependencies:
    """Route dependencies."""

    service: RecordingSearchService | None
    limiter: anyio.CapacityLimiter


def install_recording_search_routes(
    app: FastAPI,
    service: RecordingSearchService | None,
    limiter: anyio.CapacityLimiter,
) -> None:
    """Install the recording-search start and status routes."""
    dependencies = RecordingSearchApiDependencies(service, limiter)
    router = APIRouter(prefix="/api/v1/recording-searches", tags=["recording-searches"])

    async def start(
        body: RecordingSearchRequestBody, response: Response
    ) -> RecordingSearchManifest | RecordingSearchManifestV2 | JSONResponse:
        if dependencies.service is None:
            return _unavailable()
        try:
            result = await run_sync(
                dependencies.service.start,
                body.to_domain(),
                limiter=dependencies.limiter,
            )
        except RecordingSearchError as error:
            return _error_response(error)
        except Exception:  # noqa: BLE001 - HTTP boundary redacts unexpected failures.
            return _error_response(RecordingSearchError())
        if result.outcome is RecordingSearchOutcome.ALREADY_RUNNING:
            return _already_running(result.manifest.search_run_id)
        response.status_code = (
            status.HTTP_201_CREATED
            if result.outcome is RecordingSearchOutcome.STARTED
            else status.HTTP_409_CONFLICT
        )
        return result.manifest

    async def get_status(
        investigation_id: str, search_run_id: str
    ) -> (
        RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchStatusV4 | JSONResponse
    ):
        if dependencies.service is None:
            return _unavailable()
        try:
            return await run_sync(
                dependencies.service.status,
                investigation_id,
                search_run_id,
                limiter=dependencies.limiter,
            )
        except RecordingSearchError as error:
            return _error_response(error)
        except Exception:  # noqa: BLE001 - HTTP boundary redacts unexpected failures.
            return _error_response(RecordingSearchError())

    router.add_api_route(
        "",
        start,
        methods=["POST"],
        response_model=RecordingSearchManifest
        | RecordingSearchManifestV2
        | RecordingSearchStatusV4,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_400_BAD_REQUEST: {"model": ReferenceFrameErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ReferenceFrameErrorResponse},
        },
    )
    router.add_api_route(
        "/{investigation_id}/{search_run_id}",
        get_status,
        methods=["GET"],
        response_model=RecordingSearchManifest
        | RecordingSearchManifestV2
        | RecordingSearchStatusV4,
        status_code=status.HTTP_200_OK,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ReferenceFrameErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ReferenceFrameErrorResponse},
        },
    )
    app.include_router(router)


def _unavailable() -> JSONResponse:
    return ReferenceFrameApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "recording_search_unavailable",
        "Recording search is unavailable.",
    ).response()


def _already_running(search_run_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": {
                "code": "already_running",
                "message": "A recording-search run is already active for this investigation.",
                "details": [{"field": "search_run_id", "code": search_run_id}],
            }
        },
    )


def _error_response(error: RecordingSearchError) -> JSONResponse:
    if isinstance(error, ReconfirmationRequiredError):
        specification = (
            status.HTTP_409_CONFLICT,
            "reconfirmation_required",
            "Reconfirm this investigation before recording search.",
        )
    elif isinstance(error, RecordingSearchNotFoundError):
        specification = (
            status.HTTP_404_NOT_FOUND,
            "search_run_not_found",
            "The recording-search run was not found.",
        )
    elif isinstance(error, RecordingSearchBaselineError):
        specification = (
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "baseline_validation_failed",
            "The confirmed recording-search baseline could not be validated.",
        )
    elif isinstance(
        error, (RecordingSearchManifestCorruptError, RecordingSearchTerminalReopenError)
    ):
        specification = (
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "search_manifest_corrupt",
            "The recording-search manifest is invalid.",
        )
    elif isinstance(error, RecordingSearchArtifactError):
        specification = (
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "search_artifact_failure",
            "The recording-search run could not be stored safely.",
        )
    elif isinstance(error, RecordingSearchTransitionError):
        specification = (
            status.HTTP_409_CONFLICT,
            "invalid_search_transition",
            "The recording-search state transition is invalid.",
        )
    else:
        specification = (
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "The recording-search operation could not be completed safely.",
        )
    return ReferenceFrameApiError(*specification).response()
