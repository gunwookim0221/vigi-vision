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

from vigi_vision.config import CaptureSettings, NvrConnection, load_capture_settings
from vigi_vision.ffmpeg import resolve_ffmpeg
from vigi_vision.nvr import SdkNvrGateway
from vigi_vision.recording import RecordingPlanner
from vigi_vision.reference_frame_api_errors import (
    domain_error,
    safe_error_response,
)
from vigi_vision.reference_frame_api_models import (
    ReferenceFrameCreateBody,
    ReferenceFrameCreateResponse,
    ReferenceFrameErrorResponse,
    reference_frame_response,
)
from vigi_vision.reference_frame_artifacts import ReferenceFrameArtifactStore
from vigi_vision.reference_frame_decoder import FfmpegReferenceFrameDecoder
from vigi_vision.reference_frame_models import (
    ReferenceFrameError,
    ReferenceFrameOutcome,
    ReferenceFrameRequest,
    ReferenceFrameResolution,
    parse_reference_frame_request,
)
from vigi_vision.reference_frame_resources import (
    ReferenceFrameImageResource,
    ReferenceFrameResourceStore,
)
from vigi_vision.reference_frame_service import ReferenceFrameService
from vigi_vision.replay import ReplayExtractor
from vigi_vision.video import resolve_ffprobe

_ARTIFACT_ROOT: Final = Path("artifacts/reference-frames")
_IMAGE_HEADERS: Final = {
    "Cache-Control": "private, max-age=3600, immutable",
    "Content-Disposition": 'inline; filename="reference-frame.jpg"',
}
_OUTCOME_STATUS: Final = {
    ReferenceFrameOutcome.CREATED: status.HTTP_201_CREATED,
    ReferenceFrameOutcome.REUSED: status.HTTP_200_OK,
}


class ReferenceFrameExecutionBoundary(Protocol):
    """The existing synchronous service surface required by the HTTP creation route."""

    def execute_or_resolve(self, request: ReferenceFrameRequest) -> ReferenceFrameResolution:
        """Create or resolve a durable compatible frame."""
        ...


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


def create_reference_frame_app(
    service: ReferenceFrameExecutionBoundary,
    resources: ReferenceFrameImageBoundary,
    limiter: anyio.CapacityLimiter | None = None,
) -> FastAPI:
    """Create an injectable local API application without reading configuration in handlers."""
    dependencies = ReferenceFrameApiDependencies(
        service=service,
        resources=resources,
        limiter=anyio.CapacityLimiter(1) if limiter is None else limiter,
    )
    app = FastAPI(
        title="VIGI Vision Reference Frame API",
        version="1.0.0",
        description=(
            "Local trusted-network API for durable recorded reference frames. "
            "Timing is clip-relative and not an exact source-frame timestamp."
        ),
    )
    app.add_exception_handler(RequestValidationError, safe_error_response)
    app.add_exception_handler(StarletteHttpException, safe_error_response)
    app.add_exception_handler(Exception, safe_error_response)
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
        except Exception as error:  # noqa: BLE001 - required top-level HTTP redaction boundary.
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
        except Exception as error:  # noqa: BLE001 - required top-level HTTP redaction boundary.
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
    return app


def create_reference_frame_app_from_environment() -> FastAPI:
    """Compose the local NVR-only application once at ASGI startup."""
    try:
        settings = load_capture_settings(Path.cwd() / ".env")
        connection = _nvr_connection(settings)
        ffmpeg = resolve_ffmpeg(settings.ffmpeg_path)
        _ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        if _ARTIFACT_ROOT.is_symlink() or not _ARTIFACT_ROOT.is_dir():
            raise ReferenceFrameApiStartupError  # noqa: TRY301 - safe startup validation.
        planner = RecordingPlanner.connect(connection)
        artifacts = ReferenceFrameArtifactStore(_ARTIFACT_ROOT)
        resources = ReferenceFrameResourceStore(_ARTIFACT_ROOT)
        service = ReferenceFrameService(
            planner=planner,
            replay_extractor=ReplayExtractor(
                executable=ffmpeg,
                username=connection.username.get_secret_value(),
                password=connection.password,
            ),
            decoder=FfmpegReferenceFrameDecoder(ffmpeg, resolve_ffprobe(ffmpeg)),
            artifacts=artifacts,
            channel_inventory=SdkNvrGateway(connection),
            completed_resources=resources,
        )
        return create_reference_frame_app(service, resources)
    except ReferenceFrameApiStartupError:
        raise
    except Exception:  # noqa: BLE001 - fixed safe startup failure boundary.
        raise ReferenceFrameApiStartupError from None


def _nvr_connection(settings: CaptureSettings) -> NvrConnection:
    match settings.vigi_source:
        case "nvr":
            return settings.nvr_connection
        case "ipc":
            raise ReferenceFrameApiStartupError
