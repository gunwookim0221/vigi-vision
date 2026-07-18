"""OpenAI image-analysis boundary for one locally captured frame."""

import base64
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar, Final

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from pydantic import BaseModel, ConfigDict, ValidationError
from typing_extensions import override

_MODEL = "gpt-5.6-terra"
_PROMPT = (
    "Inspect this one current camera frame. Return a concise scene summary, whether a person "
    "is visible, visually notable observations, and limitations appropriate to a single image."
)
_QUOTA_ERROR_CODES: Final = frozenset({"billing_hard_limit_reached", "insufficient_quota"})
_SAFE_ERROR_FIELD: Final = re.compile(r"^[A-Za-z0-9_.:/-]{1,120}$")


class OpenAiFailureKind(Enum):
    """Safe categories for failures at the OpenAI image-analysis boundary."""

    MISSING_API_KEY = "Missing OpenAI API key"
    AUTHENTICATION = "OpenAI authentication failure"
    PERMISSION_OR_MODEL_ACCESS = "OpenAI permission or model-access failure"
    QUOTA_OR_BILLING = "OpenAI quota or billing failure"
    RATE_LIMIT = "OpenAI rate limit"
    TIMEOUT_OR_NETWORK = "OpenAI timeout or network failure"
    INVALID_REQUEST = "OpenAI invalid request"
    STRUCTURED_RESPONSE_PARSING = "OpenAI structured-response parsing failure"
    UNEXPECTED_API_FAILURE = "Unexpected OpenAI API failure"


_ACTIONS: Final = {
    OpenAiFailureKind.MISSING_API_KEY: "Set OPENAI_API_KEY.",
    OpenAiFailureKind.AUTHENTICATION: "Verify the configured API key.",
    OpenAiFailureKind.PERMISSION_OR_MODEL_ACCESS: "Verify account access to the configured model.",
    OpenAiFailureKind.QUOTA_OR_BILLING: "Verify account quota and billing status.",
    OpenAiFailureKind.RATE_LIMIT: "Wait briefly and retry.",
    OpenAiFailureKind.TIMEOUT_OR_NETWORK: "Verify network connectivity and retry.",
    OpenAiFailureKind.INVALID_REQUEST: "Verify the configured model and request contract.",
    OpenAiFailureKind.STRUCTURED_RESPONSE_PARSING: "Check the structured output contract.",
    OpenAiFailureKind.UNEXPECTED_API_FAILURE: "Retry and inspect the safe exception category.",
}


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
        """Return a redacted message."""
        return (
            f"{OpenAiFailureKind.STRUCTURED_RESPONSE_PARSING.value} "
            f"[{self.exception_type}]. Check the structured output contract."
        )


@dataclass(frozen=True, slots=True)
class OpenAiErrorMetadata:
    """Safe metadata exposed by an OpenAI SDK request exception."""

    status_code: int | None
    code: str | None
    param: str | None
    request_id: str | None

    def formatted_fields(self) -> str:
        """Return only pre-redacted diagnostic fields."""
        fields = (
            ("HTTP", self.status_code),
            ("code", self.code),
            ("param", self.param),
            ("request", self.request_id),
        )
        return "".join(f" [{name} {value}]" for name, value in fields if value is not None)


@dataclass(frozen=True, slots=True)
class AnalysisRequestError(RuntimeError):
    """Raised when the OpenAI request cannot complete without exposing details."""

    kind: OpenAiFailureKind
    exception_type: str
    metadata: OpenAiErrorMetadata

    @property
    def status_code(self) -> int | None:
        """Return the safe HTTP status code for compatibility with CLI consumers."""
        return self.metadata.status_code

    @override
    def __str__(self) -> str:
        """Return a redacted message."""
        return (
            f"{self.kind.value} [{self.exception_type}]{self.metadata.formatted_fields()}. "
            f"{_action(self.kind)}"
        )


def parse_scene_analysis(raw_response: str) -> SceneAnalysis:
    """Validate a JSON response at the external-model boundary."""
    try:
        return SceneAnalysis.model_validate_json(raw_response)
    except ValidationError as error:
        raise AnalysisResponseError(type(error).__name__) from error


@dataclass(frozen=True, slots=True)
class OpenAiAnalyzer:
    """Send one JPEG to the Responses API with a fixed vision result schema."""

    api_key: str

    def analyze(self, image_path: Path) -> SceneAnalysis:
        """Analyze one JPEG frame through the Responses API."""
        if not self.api_key:
            raise AnalysisRequestError(
                OpenAiFailureKind.MISSING_API_KEY,
                "ConfigurationError",
                OpenAiErrorMetadata(None, None, None, None),
            )
        image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
        try:
            response = OpenAI(api_key=self.api_key, timeout=20.0).responses.create(
                model=_MODEL,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": _PROMPT},
                            {
                                "type": "input_image",
                                "image_url": f"data:image/jpeg;base64,{image_data}",
                                "detail": "low",
                            },
                        ],
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "scene_analysis",
                        "strict": True,
                        "schema": SceneAnalysis.model_json_schema(),
                    }
                },
            )
        except OpenAIError as error:
            raise diagnose_openai_error(error) from error
        return parse_scene_analysis(response.output_text)


def diagnose_openai_error(error: OpenAIError) -> AnalysisRequestError:
    """Classify an SDK exception without reading or printing its message or body."""
    match error:
        case AuthenticationError():
            kind = OpenAiFailureKind.AUTHENTICATION
        case PermissionDeniedError() | NotFoundError():
            kind = OpenAiFailureKind.PERMISSION_OR_MODEL_ACCESS
        case RateLimitError() as rate_limit_error:
            kind = _rate_limit_kind(rate_limit_error)
        case APITimeoutError() | APIConnectionError():
            kind = OpenAiFailureKind.TIMEOUT_OR_NETWORK
        case BadRequestError() | UnprocessableEntityError():
            kind = OpenAiFailureKind.INVALID_REQUEST
        case APIStatusError():
            kind = OpenAiFailureKind.UNEXPECTED_API_FAILURE
        case _:
            kind = OpenAiFailureKind.UNEXPECTED_API_FAILURE
    return AnalysisRequestError(kind, type(error).__name__, _error_metadata(error))


def _rate_limit_kind(error: RateLimitError) -> OpenAiFailureKind:
    match error.body:
        case {"error": {"code": str() as code}} if code in _QUOTA_ERROR_CODES:
            return OpenAiFailureKind.QUOTA_OR_BILLING
        case {"code": str() as code} if code in _QUOTA_ERROR_CODES:
            return OpenAiFailureKind.QUOTA_OR_BILLING
        case _:
            return OpenAiFailureKind.RATE_LIMIT


def _error_metadata(error: OpenAIError) -> OpenAiErrorMetadata:
    match error:
        case APIStatusError() as status_error:
            return OpenAiErrorMetadata(
                status_error.status_code,
                _safe_error_field(status_error.code),
                _safe_error_field(status_error.param),
                _safe_error_field(status_error.request_id),
            )
        case _:
            return OpenAiErrorMetadata(None, None, None, None)


def _safe_error_field(value: str | None) -> str | None:
    if value is None:
        return None
    value_lower = value.lower()
    if (
        _SAFE_ERROR_FIELD.fullmatch(value)
        and "data:" not in value_lower
        and "sk-" not in value_lower
    ):
        return value
    return None


def _action(kind: OpenAiFailureKind) -> str:
    return _ACTIONS[kind]
