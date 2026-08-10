"""Synchronous local FastAPI transport for durable recorded reference frames."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, final

import anyio
from anyio.to_thread import run_sync
from fastapi import APIRouter, FastAPI, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHttpException
from typing_extensions import override

from vigi_vision.assisted_roi_api_models import (
    RoiSuggestionBody,
    RoiSuggestionResponse,
    roi_suggestion_response,
)
from vigi_vision.assisted_roi_composition import build_assisted_roi_service
from vigi_vision.assisted_roi_geometry import Point
from vigi_vision.assisted_roi_service import (
    AssistedRoiSuggestionService,
    RoiSuggestionExecutionBoundary,
    RoiSuggestionUnavailableError,
)
from vigi_vision.channel_selection import select_channel, usable_channels
from vigi_vision.config import (
    AssistedRoiSettings,
    CaptureSettings,
    NvrConnection,
    load_assisted_roi_settings,
    load_capture_settings,
)
from vigi_vision.ffmpeg import resolve_ffmpeg
from vigi_vision.investigation_confirmation_api import (
    InvestigationConfirmationExecutionBoundary,
    install_investigation_confirmation_routes,
)
from vigi_vision.investigation_confirmation_integrity import FfmpegJpegDecoder
from vigi_vision.investigation_confirmation_repository import InvestigationConfirmationRepository
from vigi_vision.investigation_confirmation_service import InvestigationConfirmationService
from vigi_vision.nvr import SdkNvrGateway
from vigi_vision.recording import RecordingPlanner
from vigi_vision.recording_search_api import install_recording_search_routes
from vigi_vision.recording_search_repository import RecordingSearchRepository
from vigi_vision.recording_search_service import RecordingSearchService
from vigi_vision.reference_frame_api_errors import (
    domain_error,
    safe_error_response,
)
from vigi_vision.reference_frame_api_models import (
    ReferenceFrameChannelListResponse,
    ReferenceFrameChannelResponse,
    ReferenceFrameCreateBody,
    ReferenceFrameCreateResponse,
    ReferenceFrameErrorResponse,
    reference_frame_response,
)
from vigi_vision.reference_frame_artifacts import ReferenceFrameArtifactStore
from vigi_vision.reference_frame_candidate_api_models import (
    ReferenceFrameCandidateSetBody,
    ReferenceFrameCandidateSetResponse,
    parse_reference_frame_candidate_set_request,
    reference_frame_candidate_set_response,
)
from vigi_vision.reference_frame_candidate_service import ReferenceFrameCandidateSetService
from vigi_vision.reference_frame_decoder import FfmpegReferenceFrameDecoder
from vigi_vision.reference_frame_direct import FfmpegDirectReferenceFrameAcquirer
from vigi_vision.reference_frame_models import (
    ReferenceFrameError,
    ReferenceFrameOutcome,
    parse_reference_frame_request,
)
from vigi_vision.reference_frame_resources import (
    ReferenceFrameImageResource,
    ReferenceFrameResourceStore,
)
from vigi_vision.reference_frame_service import (
    ChannelInventoryBoundary,
    ReferenceFrameExecutionBoundary,
    ReferenceFrameService,
)
from vigi_vision.reference_frame_web_ui import install_reference_frame_web_ui
from vigi_vision.replay import ReplayExtractor
from vigi_vision.video import resolve_ffprobe

_ARTIFACT_ROOT: Final = Path("artifacts/reference-frames")
_CONFIRMATION_ARTIFACT_ROOT: Final = Path("artifacts/investigations")
_RECORDING_SEARCH_ARTIFACT_ROOT: Final = Path("artifacts/investigation-searches")
_IMAGE_HEADERS: Final = {
    "Cache-Control": "private, max-age=3600, immutable",
    "Content-Disposition": 'inline; filename="reference-frame.jpg"',
}
_OUTCOME_STATUS: Final = {
    ReferenceFrameOutcome.CREATED: status.HTTP_201_CREATED,
    ReferenceFrameOutcome.REUSED: status.HTTP_200_OK,
}


class ReferenceFrameImageBoundary(Protocol):
    """The artifact-only JPEG lookup surface required by the image route."""

    def resolve_image(self, resource_id: str) -> ReferenceFrameImageResource:
        """Return the fixed durable JPEG after completed-resource validation."""
        ...


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameApiDependencies:
    """Immutable collaborators and the local-first execution bound for one application."""

    service: ReferenceFrameExecutionBoundary = field(repr=False)
    resources: ReferenceFrameImageBoundary = field(repr=False)
    limiter: anyio.CapacityLimiter = field(repr=False)
    suggestion_service: RoiSuggestionExecutionBoundary | None = field(default=None, repr=False)
    channel_inventory: ChannelInventoryBoundary | None = field(default=None, repr=False)
    confirmation_service: InvestigationConfirmationExecutionBoundary | None = field(
        default=None, repr=False
    )
    recording_search_service: RecordingSearchService | None = field(default=None, repr=False)


@final
class ReferenceFrameApiStartupError(RuntimeError):
    """Raised when safe local reference-frame API composition cannot be completed."""

    @property
    def message(self) -> str:
        """Return fixed startup guidance without configuration contents."""
        return "The reference-frame API could not be configured safely."

    @override
    def __str__(self) -> str:
        """Return the fixed safe startup message."""
        return self.message


def create_reference_frame_app(  # noqa: PLR0913 — each argument is an independently injectable boundary.
    service: ReferenceFrameExecutionBoundary,
    resources: ReferenceFrameImageBoundary,
    limiter: anyio.CapacityLimiter | None = None,
    suggestion_service: RoiSuggestionExecutionBoundary | None = None,
    channel_inventory: ChannelInventoryBoundary | None = None,
    confirmation_service: InvestigationConfirmationExecutionBoundary | None = None,
    recording_search_service: RecordingSearchService | None = None,
) -> FastAPI:
    """Create an injectable local API application without reading configuration in handlers."""
    dependencies = ReferenceFrameApiDependencies(
        service=service,
        resources=resources,
        limiter=anyio.CapacityLimiter(1) if limiter is None else limiter,
        suggestion_service=suggestion_service,
        channel_inventory=channel_inventory,
        confirmation_service=confirmation_service,
        recording_search_service=recording_search_service,
    )
    app = FastAPI(
        title="VIGI Vision Reference Frame API",
        version="1.0.0",
        description=(
            "Local trusted-network API for durable recorded reference frames. "
            "Timing is clip-relative and not an exact source-frame timestamp."
        ),
    )
    for exception_type in (RequestValidationError, StarletteHttpException, Exception):
        app.add_exception_handler(exception_type, safe_error_response)
    install_reference_frame_web_ui(app)
    router = APIRouter(prefix="/api/v1/reference-frames", tags=["reference-frames"])

    async def create_frame(
        body: ReferenceFrameCreateBody, response: Response
    ) -> ReferenceFrameCreateResponse | JSONResponse:
        """Run the existing synchronous service off the event loop and serialize safe facts."""
        try:
            request = parse_reference_frame_request(
                channel_id=body.channel_id,
                requested_time_text=body.requested_time,
                source_timezone=body.source_timezone,
            )
            resolution = await run_sync(
                dependencies.service.execute_or_resolve,
                request,
                limiter=dependencies.limiter,
            )
            api_response = reference_frame_response(resolution)
            response.status_code = _OUTCOME_STATUS[resolution.outcome]
        except ReferenceFrameError as error:
            return domain_error(error).response()
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK - HTTP redaction boundary.
            return domain_error(error).response()
        else:
            return api_response

    async def get_image(resource_id: str) -> FileResponse | JSONResponse:
        """Resolve a fixed JPEG through the artifact boundary without client path handling."""
        try:
            image = await run_sync(
                dependencies.resources.resolve_image,
                resource_id,
                limiter=dependencies.limiter,
            )
            return FileResponse(image.jpeg_path, media_type="image/jpeg", headers=_IMAGE_HEADERS)
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK - HTTP redaction boundary.
            return domain_error(error).response()

    router.add_api_route(
        "",
        create_frame,
        methods=["POST"],
        response_model=ReferenceFrameCreateResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {"model": ReferenceFrameCreateResponse, "description": "Reused."},
            status.HTTP_400_BAD_REQUEST: {"model": ReferenceFrameErrorResponse},
            status.HTTP_404_NOT_FOUND: {"model": ReferenceFrameErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ReferenceFrameErrorResponse},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReferenceFrameErrorResponse},
            status.HTTP_504_GATEWAY_TIMEOUT: {"model": ReferenceFrameErrorResponse},
        },
        summary="Create or resolve a durable reference frame",
    )
    router.add_api_route(
        "/{resource_id}/image",
        get_image,
        methods=["GET"],
        response_model=None,
        response_class=FileResponse,
        responses={
            status.HTTP_200_OK: {
                "content": {"image/jpeg": {}},
                "description": "Durable reference-frame JPEG.",
            },
            status.HTTP_404_NOT_FOUND: {"model": ReferenceFrameErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ReferenceFrameErrorResponse},
        },
        summary="Retrieve a durable reference-frame JPEG",
    )
    app.include_router(router)
    _add_channel_route(app, dependencies)
    _add_candidate_route(app, dependencies)
    _add_roi_suggestion_route(app, dependencies)
    install_investigation_confirmation_routes(
        app, dependencies.confirmation_service, dependencies.limiter
    )
    install_recording_search_routes(
        app, dependencies.recording_search_service, dependencies.limiter
    )
    if isinstance(suggestion_service, AssistedRoiSuggestionService):
        app.router.add_event_handler("shutdown", suggestion_service.close)
    if recording_search_service is not None:
        app.router.add_event_handler("shutdown", recording_search_service.close)
    return app


def _add_channel_route(app: FastAPI, dependencies: ReferenceFrameApiDependencies) -> None:
    router = APIRouter(prefix="/api/v1/reference-frames", tags=["reference-frames"])

    async def get_channels() -> ReferenceFrameChannelListResponse | JSONResponse:
        inventory = dependencies.channel_inventory
        if inventory is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "error": {
                        "code": "channel_discovery_unavailable",
                        "message": "Channel discovery is unavailable.",
                        "details": None,
                    }
                },
            )
        try:
            channels = usable_channels(
                await run_sync(inventory.channels, limiter=dependencies.limiter)
            )
            default_channel = select_channel(channels, None) if channels else None
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK - HTTP redaction boundary.
            return domain_error(error).response()
        return ReferenceFrameChannelListResponse(
            channels=tuple(
                ReferenceFrameChannelResponse(
                    channel_id=channel.channel_id,
                    name=channel.name,
                    alias=channel.alias,
                    online=True,
                )
                for channel in channels
            ),
            default_channel_id=None if default_channel is None else default_channel.channel_id,
        )

    router.add_api_route(
        "/channels",
        get_channels,
        methods=["GET"],
        response_model=ReferenceFrameChannelListResponse,
        status_code=status.HTTP_200_OK,
        responses={
            status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReferenceFrameErrorResponse},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ReferenceFrameErrorResponse},
        },
        summary="List usable NVR channels",
    )
    app.include_router(router)


def _add_roi_suggestion_route(app: FastAPI, dependencies: ReferenceFrameApiDependencies) -> None:
    router = APIRouter(prefix="/api/v1/reference-frames", tags=["reference-frames"])

    async def create_roi_suggestion(
        resource_id: str, body: RoiSuggestionBody
    ) -> RoiSuggestionResponse | JSONResponse:
        suggestion_service = dependencies.suggestion_service
        if suggestion_service is None:
            return domain_error(RoiSuggestionUnavailableError()).response()
        try:
            result = await suggestion_service.suggest(
                resource_id,
                Point(body.point.x, body.point.y),
            )
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK - HTTP redaction boundary.
            return domain_error(error).response()
        return roi_suggestion_response(result)

    router.add_api_route(
        "/{resource_id}/roi-suggestions",
        create_roi_suggestion,
        methods=["POST"],
        response_model=RoiSuggestionResponse,
        status_code=status.HTTP_200_OK,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ReferenceFrameErrorResponse},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ReferenceFrameErrorResponse},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReferenceFrameErrorResponse},
            status.HTTP_504_GATEWAY_TIMEOUT: {"model": ReferenceFrameErrorResponse},
        },
        summary="Suggest a bounded source-pixel ROI from one point",
    )
    app.include_router(router)


def _add_candidate_route(app: FastAPI, dependencies: ReferenceFrameApiDependencies) -> None:
    candidate_router = APIRouter(
        prefix="/api/v1/reference-frame-candidate-sets", tags=["reference-frames"]
    )
    candidate_service = ReferenceFrameCandidateSetService(dependencies.service)

    async def create_candidate_set(
        body: ReferenceFrameCandidateSetBody,
    ) -> ReferenceFrameCandidateSetResponse | JSONResponse:
        try:
            request = parse_reference_frame_candidate_set_request(body=body)
            result = await run_sync(
                candidate_service.execute,
                request,
                limiter=dependencies.limiter,
            )
        except ReferenceFrameError as error:
            return domain_error(error).response()
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK - HTTP redaction boundary.
            return domain_error(error).response()
        else:
            return reference_frame_candidate_set_response(result)

    candidate_router.add_api_route(
        "",
        create_candidate_set,
        methods=["POST"],
        response_model=ReferenceFrameCandidateSetResponse,
        status_code=status.HTTP_200_OK,
        responses={
            status.HTTP_400_BAD_REQUEST: {"model": ReferenceFrameErrorResponse},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ReferenceFrameErrorResponse},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ReferenceFrameErrorResponse},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReferenceFrameErrorResponse},
            status.HTTP_504_GATEWAY_TIMEOUT: {"model": ReferenceFrameErrorResponse},
        },
        summary="Create or reuse bounded reference-frame candidates",
    )
    app.include_router(candidate_router)


def create_reference_frame_app_from_environment() -> FastAPI:
    """Compose the local NVR-only application once at ASGI startup."""
    try:
        settings = load_capture_settings(Path.cwd() / ".env")
        try:
            assisted_settings = load_assisted_roi_settings(Path.cwd() / ".env")
        except ValueError:
            assisted_settings = AssistedRoiSettings()
        connection = _nvr_connection(settings)
        ffmpeg = resolve_ffmpeg(settings.ffmpeg_path)
        _ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        if _ARTIFACT_ROOT.is_symlink() or not _ARTIFACT_ROOT.is_dir():
            raise ReferenceFrameApiStartupError  # noqa: TRY301 - safe startup validation.
        planner = RecordingPlanner.connect(connection)
        ffprobe = resolve_ffprobe(ffmpeg)
        artifacts = ReferenceFrameArtifactStore(_ARTIFACT_ROOT)
        resources = ReferenceFrameResourceStore(_ARTIFACT_ROOT)
        jpeg_decoder = FfmpegJpegDecoder(ffmpeg)
        confirmation_repository = InvestigationConfirmationRepository(
            _CONFIRMATION_ARTIFACT_ROOT, resources, jpeg_decoder=jpeg_decoder
        )
        confirmation_service = InvestigationConfirmationService(
            resources,
            confirmation_repository,
            jpeg_decoder=jpeg_decoder,
        )
        channel_inventory = SdkNvrGateway(connection)
        recording_search_service = RecordingSearchService(
            confirmation_service=confirmation_service,
            repository=RecordingSearchRepository(_RECORDING_SEARCH_ARTIFACT_ROOT),
            channel_inventory=channel_inventory,
            artifact_root=Path("artifacts"),
            jpeg_decoder=jpeg_decoder,
        )
        service = ReferenceFrameService(
            planner=planner,
            replay_extractor=ReplayExtractor(
                executable=ffmpeg,
                username=connection.username.get_secret_value(),
                password=connection.password,
                timeout_diagnostic_directory=settings.replay_timeout_diagnostic_directory,
                progress_diagnostics=settings.replay_progress_diagnostics,
            ),
            decoder=FfmpegReferenceFrameDecoder(ffmpeg, ffprobe),
            artifacts=artifacts,
            direct_acquirer=FfmpegDirectReferenceFrameAcquirer(
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                username=connection.username.get_secret_value(),
                password=connection.password,
            ),
            channel_inventory=channel_inventory,
            completed_resources=resources,
        )
        suggestion_service = build_assisted_roi_service(assisted_settings, resources)
        return create_reference_frame_app(
            service,
            resources,
            suggestion_service=suggestion_service,
            channel_inventory=channel_inventory,
            confirmation_service=confirmation_service,
            recording_search_service=recording_search_service,
        )
    except ReferenceFrameApiStartupError:
        raise
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK - safe startup redaction boundary.
        raise ReferenceFrameApiStartupError from None


def _nvr_connection(settings: CaptureSettings) -> NvrConnection:
    match settings.vigi_source:
        case "nvr":
            return settings.nvr_connection
        case "ipc":
            raise ReferenceFrameApiStartupError
