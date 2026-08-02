"""FastAPI transport for immutable investigation confirmations."""

from dataclasses import dataclass
from typing import Final, Protocol, final

import anyio
from anyio.to_thread import run_sync
from fastapi import APIRouter, FastAPI, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from vigi_vision.investigation_confirmation_api_models import (
    InvestigationConfirmationCreateBody,
    InvestigationConfirmationResponse,
    confirmation_response,
    loaded_confirmation_response,
)
from vigi_vision.investigation_confirmation_models import (
    ConfirmationError,
    ConfirmationManifest,
    ConfirmationOutcome,
    ConfirmationRequest,
    ConfirmationResult,
)
from vigi_vision.reference_frame_api_errors import (
    ReferenceFrameApiError,
    confirmation_domain_error,
)
from vigi_vision.reference_frame_api_models import ReferenceFrameErrorResponse
from vigi_vision.reference_frame_models import ReferenceFrameError

_OUTCOME_STATUS: Final = {
    ConfirmationOutcome.CREATED: status.HTTP_201_CREATED,
    ConfirmationOutcome.REUSED: status.HTTP_200_OK,
}


class InvestigationConfirmationExecutionBoundary(Protocol):
    """The existing confirmation service methods required by HTTP transport."""

    def confirm(self, request: ConfirmationRequest) -> ConfirmationResult:
        """Create or reuse one immutable confirmation."""
        ...

    def load_confirmation_manifest(self, investigation_id: str) -> ConfirmationManifest:
        """Load one strictly validated confirmation manifest."""
        ...


@final
@dataclass(frozen=True, slots=True)
class _ConfirmationRoutes:
    """Immutable route collaborators shared by the create and read handlers."""

    service: InvestigationConfirmationExecutionBoundary | None
    limiter: anyio.CapacityLimiter


def install_investigation_confirmation_routes(
    app: FastAPI,
    service: InvestigationConfirmationExecutionBoundary | None,
    limiter: anyio.CapacityLimiter,
) -> None:
    """Install the designed confirmation POST and GET endpoints on an app."""
    dependencies = _ConfirmationRoutes(service, limiter)
    router = APIRouter(prefix="/api/v1/investigation-confirmations", tags=["investigations"])

    async def create_confirmation(
        body: InvestigationConfirmationCreateBody, response: Response
    ) -> InvestigationConfirmationResponse | JSONResponse:
        if dependencies.service is None:
            return _unavailable().response()
        try:
            result = await run_sync(
                dependencies.service.confirm,
                body.to_domain(),
                limiter=dependencies.limiter,
            )
        except (ConfirmationError, ReferenceFrameError, ValidationError) as error:
            return confirmation_domain_error(error).response()
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK - HTTP redaction boundary.
            return confirmation_domain_error(error).response()
        response.status_code = _status_for(result.outcome)
        return confirmation_response(result)

    async def get_confirmation(
        investigation_id: str,
    ) -> InvestigationConfirmationResponse | JSONResponse:
        if dependencies.service is None:
            return _unavailable().response()
        try:
            loaded = await run_sync(
                dependencies.service.load_confirmation_manifest,
                investigation_id,
                limiter=dependencies.limiter,
            )
        except (ConfirmationError, ReferenceFrameError, ValidationError) as error:
            return confirmation_domain_error(error).response()
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK - HTTP redaction boundary.
            return confirmation_domain_error(error).response()
        return loaded_confirmation_response(loaded)

    router.add_api_route(
        "",
        create_confirmation,
        methods=["POST"],
        response_model=InvestigationConfirmationResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {"model": InvestigationConfirmationResponse},
            status.HTTP_400_BAD_REQUEST: {"model": ReferenceFrameErrorResponse},
            status.HTTP_404_NOT_FOUND: {"model": ReferenceFrameErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ReferenceFrameErrorResponse},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReferenceFrameErrorResponse},
        },
        summary="Create or resolve an immutable investigation confirmation",
    )
    router.add_api_route(
        "/{investigation_id}",
        get_confirmation,
        methods=["GET"],
        response_model=InvestigationConfirmationResponse,
        status_code=status.HTTP_200_OK,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ReferenceFrameErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ReferenceFrameErrorResponse},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReferenceFrameErrorResponse},
        },
        summary="Retrieve an immutable investigation confirmation",
    )
    app.include_router(router)


def _status_for(outcome: ConfirmationOutcome) -> int:
    return _OUTCOME_STATUS[outcome]


def _unavailable() -> ReferenceFrameApiError:
    return ReferenceFrameApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "confirmation_unavailable",
        "Investigation confirmation is unavailable.",
    )
