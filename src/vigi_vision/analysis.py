"""OpenAI image and sparse temporal-analysis boundary."""

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from openai import OpenAI, OpenAIError
from openai.types.responses.response_input_content_param import ResponseInputContentParam
from openai.types.responses.response_input_image_param import ResponseInputImageParam
from openai.types.responses.response_input_text_param import ResponseInputTextParam
from pydantic import BaseModel, ConfigDict, ValidationError
from typing_extensions import override

from vigi_vision.openai_errors import (
    AnalysisRequestError,
    OpenAiErrorMetadata,
    OpenAiFailureKind,
    diagnose_openai_error,
    missing_api_key_error,
)
from vigi_vision.profiles import ProfileAnalysis, ProfileDefinition, parse_profile_analysis
from vigi_vision.temporal_profiles import (
    TemporalProfileAnalysis,
    TemporalProfileDefinition,
    parse_temporal_profile_analysis,
)
from vigi_vision.video import FrameRecord, VideoMetadata

if TYPE_CHECKING:
    from openai.types.responses.response_input_item_param import Message, ResponseInputItemParam

_MODEL = "gpt-5.6-terra"
_PROMPT = (
    "Inspect this one current camera frame. Return a concise scene summary, whether a person "
    "is visible, visually notable observations, and limitations appropriate to a single image."
)
__all__ = (
    "AnalysisRequestError",
    "AnalysisResponseError",
    "OpenAiAnalyzer",
    "OpenAiErrorMetadata",
    "OpenAiFailureKind",
    "SceneAnalysis",
    "TemporalAnalysisRequest",
    "diagnose_openai_error",
    "parse_scene_analysis",
)


class SceneAnalysis(BaseModel):
    """The validated structured result returned from the image-analysis boundary."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    summary: str
    person_visible: bool
    notable_observations: tuple[str, ...]
    limitations: str


@dataclass(frozen=True, slots=True)
class AnalysisResponseError(RuntimeError):
    """Raised when OpenAI returns an unusable structured analysis response."""

    exception_type: str

    @override
    def __str__(self) -> str:
        return (
            f"{OpenAiFailureKind.STRUCTURED_RESPONSE_PARSING.value} "
            f"[{self.exception_type}]. Check the structured output contract."
        )


@dataclass(frozen=True, slots=True)
class TemporalAnalysisRequest:
    """All locally authoritative facts and temporary images for one temporal request."""

    profile: TemporalProfileDefinition
    metadata: VideoMetadata
    frames: tuple[FrameRecord, ...]


def parse_scene_analysis(raw_response: str) -> SceneAnalysis:
    """Validate a JSON response at the external-model boundary."""
    try:
        return SceneAnalysis.model_validate_json(raw_response)
    except ValidationError as error:
        raise AnalysisResponseError(type(error).__name__) from error


@dataclass(frozen=True, slots=True)
class OpenAiAnalyzer:
    """Send one or more JPEGs to the Responses API with strict schemas."""

    api_key: str

    def analyze(self, image_path: Path) -> SceneAnalysis:
        """Analyze one JPEG frame through the Responses API."""
        raw_response = self._request_analysis(image_path, _PROMPT, SceneAnalysis, "scene_analysis")
        return parse_scene_analysis(raw_response)

    def analyze_profile(self, image_path: Path, profile: ProfileDefinition) -> ProfileAnalysis:
        """Analyze one existing image with a registered business-task profile."""
        raw_response = self._request_analysis(
            image_path,
            profile.prompt,
            profile.response_model,
            f"{profile.name}_analysis",
        )
        return parse_profile_analysis(raw_response, profile)

    def analyze_temporal(self, request: TemporalAnalysisRequest) -> TemporalProfileAnalysis:
        """Analyze all ordered temporary frames through exactly one request."""
        raw_response = self._request_temporal_analysis(request)
        analysis = parse_temporal_profile_analysis(raw_response, request.profile, request.frames)
        return analysis.model_copy(
            update={
                "video_duration_seconds": request.metadata.duration_seconds,
                "sampled_frame_count": len(request.frames),
                "sampled_timestamps": tuple(frame.timestamp_ms / 1_000 for frame in request.frames),
            }
        )

    def _request_analysis(
        self,
        image_path: Path,
        prompt: str,
        response_model: type[BaseModel],
        response_name: str,
    ) -> str:
        """Send one image using the supplied prompt and strict response model."""
        self._require_api_key()
        return self._request_content(
            [_text_content(prompt), _image_content(image_path)],
            response_model,
            response_name,
        )

    def _request_temporal_analysis(self, request: TemporalAnalysisRequest) -> str:
        """Assemble the ordered label/image pairs for one temporal model request."""
        self._require_api_key()
        content: list[ResponseInputContentParam] = [
            _text_content(f"{request.profile.prompt}\n\n{_temporal_metadata_prompt(request)}")
        ]
        for frame in request.frames:
            content.extend(
                (
                    {"type": "input_text", "text": frame.display_label},
                    _image_content(frame.temporary_path),
                )
            )
        return self._request_content(
            content,
            request.profile.response_model,
            f"{request.profile.name}_temporal_analysis",
        )

    def _request_content(
        self,
        content: list[ResponseInputContentParam],
        response_model: type[BaseModel],
        response_name: str,
    ) -> str:
        """Send one strict multimodal request through the shared OpenAI client path."""
        try:
            message: Message = {"role": "user", "content": content}
            input_messages: list[ResponseInputItemParam] = [message]
            response = OpenAI(api_key=self.api_key, timeout=20.0).responses.create(
                model=_MODEL,
                input=input_messages,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": response_name,
                        "strict": True,
                        "schema": response_model.model_json_schema(),
                    }
                },
            )
        except OpenAIError as error:
            raise diagnose_openai_error(error) from error
        return response.output_text

    def _require_api_key(self) -> None:
        """Reject a missing API key before reading any local image bytes."""
        if not self.api_key:
            raise missing_api_key_error()


def _text_content(value: str) -> ResponseInputTextParam:
    return {"type": "input_text", "text": value}


def _image_content(image_path: Path) -> ResponseInputImageParam:
    image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return {
        "type": "input_image",
        "image_url": f"data:image/jpeg;base64,{image_data}",
        "detail": "low",
    }


def _temporal_metadata_prompt(request: TemporalAnalysisRequest) -> str:
    timestamps = tuple(frame.timestamp_ms / 1_000 for frame in request.frames)
    return (
        "For the required local metadata fields, return these exact values: "
        f"video_duration_seconds={request.metadata.duration_seconds}; "
        f"sampled_frame_count={len(request.frames)}; sampled_timestamps={list(timestamps)}."
    )
