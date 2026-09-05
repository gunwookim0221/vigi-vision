"""FastAPI transport for recording-search lifecycle operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal, cast

from anyio.to_thread import run_sync
from fastapi import APIRouter, FastAPI, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from vigi_vision.recording_search_7e_background import (
    Phase7EBackgroundManager,
    Phase7EStartReceipt,
)
from vigi_vision.recording_search_7e_public import Phase7EPublicError
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

    from vigi_vision.recording_search_7e_public import Phase7EPublicService
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


class Phase7EStatusResponse(BaseModel):
    """Credential-free request-relative status projection."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    investigation_id: str
    run_id: str
    schema_version: int
    status: str
    reason_code: str | None
    terminal_result_id: str | None
    phase8_status: str | None
    phase8_reason: str | None


class Phase7EStartRequestBody(BaseModel):
    """Closed browser start body containing no server-owned execution facts."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    investigation_id: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
    )
    search_end: StrictStr = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
    request_id: StrictStr = Field(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )


Phase7EStartStatus = Literal[
    "ACCEPTED",
    "RUNNING",
    "FOUND",
    "NOT_FOUND",
    "INCONCLUSIVE",
    "FAILED",
    "INTERRUPTED",
]


class Phase7EStartResponse(BaseModel):
    """Credential-free accepted or idempotently resolved start receipt."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    request_id: str
    investigation_id: str
    run_id: str
    status: Phase7EStartStatus
    status_url: str


@dataclass(frozen=True, slots=True)
class RecordingSearchApiDependencies:
    """Route dependencies."""

    service: RecordingSearchService | None
    limiter: anyio.CapacityLimiter
    phase7e_manager: Phase7EBackgroundManager | None


def install_recording_search_routes(  # noqa: C901 - explicit legacy/Phase 7E dispatch.
    app: FastAPI,
    service: RecordingSearchService | None,
    limiter: anyio.CapacityLimiter,
    *,
    phase7e_service: Phase7EPublicService | None = None,
) -> None:
    """Install the recording-search start and status routes."""
    phase7e_manager = None if phase7e_service is None else Phase7EBackgroundManager(phase7e_service)
    dependencies = RecordingSearchApiDependencies(
        service,
        limiter,
        phase7e_manager,
    )
    app.state.phase7e_recording_search = phase7e_service is not None
    app.state.phase7e_background_manager = phase7e_manager
    router = APIRouter(prefix="/api/v1/recording-searches", tags=["recording-searches"])

    async def start_legacy(
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

    async def start_phase7e(
        body: Phase7EStartRequestBody,
    ) -> Phase7EStartResponse | JSONResponse:
        manager = dependencies.phase7e_manager
        if manager is None:
            return _unavailable()
        try:
            receipt = await run_sync(
                manager.start,
                body.investigation_id,
                body.search_end,
                body.request_id,
                limiter=dependencies.limiter,
            )
        except Phase7EPublicError as error:
            return _phase7e_error_response(error)
        except Exception:  # noqa: BLE001 - HTTP boundary redacts unexpected failures.
            return _phase7e_error_response(Phase7EPublicError("internal_error"))
        return _phase7e_start_response(receipt)

    async def get_status(  # noqa: PLR0911 - explicit legacy/Phase 7E status dispatch.
        investigation_id: str, search_run_id: str
    ) -> (
        RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchStatusV4 | JSONResponse
    ):
        if dependencies.phase7e_manager is not None:
            try:
                projected = await run_sync(
                    dependencies.phase7e_manager.status,
                    investigation_id,
                    search_run_id,
                    limiter=dependencies.limiter,
                )
                if projected.phase7.status == "UNAVAILABLE":
                    return ReferenceFrameApiError(
                        status.HTTP_404_NOT_FOUND,
                        "search_run_not_found",
                        "The recording-search run was not found.",
                    ).response()
                return JSONResponse(status_code=status.HTTP_200_OK, content=projected.as_dict())
            except Exception:  # noqa: BLE001 - HTTP boundary redacts unexpected failures.
                return ReferenceFrameApiError(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "The recording-search operation could not be completed safely.",
                ).response()
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
        start_phase7e if phase7e_manager is not None else start_legacy,
        methods=["POST"],
        response_model=(
            Phase7EStartResponse
            if phase7e_manager is not None
            else RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchStatusV4
        ),
        status_code=(
            status.HTTP_202_ACCEPTED if phase7e_manager is not None else status.HTTP_201_CREATED
        ),
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
        response_model=(
            Phase7EStatusResponse
            if phase7e_manager is not None
            else RecordingSearchManifest | RecordingSearchManifestV2 | RecordingSearchStatusV4
        ),
        status_code=status.HTTP_200_OK,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ReferenceFrameErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ReferenceFrameErrorResponse},
        },
    )
    app.include_router(router)
    if phase7e_manager is not None:
        app.router.add_event_handler("startup", phase7e_manager.recover_startup)
        app.router.add_event_handler("shutdown", phase7e_manager.close)


def _unavailable() -> JSONResponse:
    return ReferenceFrameApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "recording_search_unavailable",
        "Recording search is unavailable.",
    ).response()


def _phase7e_start_response(receipt: Phase7EStartReceipt) -> Phase7EStartResponse:
    return Phase7EStartResponse(
        request_id=receipt.request_id,
        investigation_id=receipt.investigation_id,
        run_id=receipt.run_id,
        status=cast("Phase7EStartStatus", receipt.status),
        status_url=receipt.status_url,
    )


def _phase7e_error_response(error: Phase7EPublicError) -> JSONResponse:
    specification = {
        "invalid_request": (
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_recording_search_request",
            "The recording-search request is invalid.",
        ),
        "investigation_not_found": (
            status.HTTP_404_NOT_FOUND,
            "investigation_not_found",
            "The confirmed investigation was not found.",
        ),
        "reconfirmation_required": (
            status.HTTP_409_CONFLICT,
            "reconfirmation_required",
            "Reconfirm this investigation before recording search.",
        ),
        "already_running": (
            status.HTTP_409_CONFLICT,
            "already_running",
            "A recording-search run is already active.",
        ),
        "request_conflict": (
            status.HTTP_409_CONFLICT,
            "request_conflict",
            "The request identifier is already bound to different search input.",
        ),
        "recording_search_execution_unavailable": (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "recording_search_unavailable",
            "Recording search is unavailable.",
        ),
        "confirmation_unavailable": (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "confirmation_unavailable",
            "The confirmed investigation could not be loaded.",
        ),
        "confirmation_corrupt": (
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "confirmation_corrupt",
            "The confirmed investigation is invalid.",
        ),
        "search_run_corrupt": (
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "search_run_corrupt",
            "The recording-search run is invalid.",
        ),
        "recovery_unavailable": (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "recording_search_unavailable",
            "Recording-search recovery is unavailable.",
        ),
        "recovery_capacity_exceeded": (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "recording_search_unavailable",
            "Recording-search recovery capacity was exceeded.",
        ),
    }.get(
        error.code,
        (
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "The recording-search operation could not be completed safely.",
        ),
    )
    return ReferenceFrameApiError(*specification).response()


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
