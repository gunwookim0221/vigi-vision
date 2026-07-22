"""Typed per-item replay collection for an existing investigation plan."""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias, final

from typing_extensions import override

from vigi_vision.investigation import CameraRole, InvestigationItem, InvestigationPlan
from vigi_vision.nvr import NvrErrorKind, NvrRequestError
from vigi_vision.recording import RecordingUnavailableError, RecordingWindow, ReplayRequest
from vigi_vision.replay import (
    ReplayAuthenticationError,
    ReplayClip,
    ReplayExtractionError,
    ReplayTimeoutError,
    ReplayUnavailableError,
)

_INVALID_SUCCESS_RESULT = "Successful results require a replay clip without a failure reason."
_INVALID_FAILURE_RESULT = "Failed collection results require a failure reason and no replay clip."
_UNEXPECTED_FAILURE_REASON = "Unexpected replay collection failure."

KnownCollectionError: TypeAlias = (
    NvrRequestError
    | RecordingUnavailableError
    | ReplayAuthenticationError
    | ReplayExtractionError
    | ReplayTimeoutError
    | ReplayUnavailableError
)


class RecordingPlanningBoundary(Protocol):
    """The existing recording-search operation used for one planned window."""

    def plan(self, window: RecordingWindow) -> ReplayRequest:
        """Return a credential-free replay request for the supplied window."""
        ...


class ReplayExtractionBoundary(Protocol):
    """The existing replay-extraction operation used for one replay request."""

    def extract(self, request: ReplayRequest) -> ReplayClip:
        """Return the caller-owned temporary replay clip for the request."""
        ...


class CollectionStatus(str, Enum):
    """The complete set of per-item collection outcomes."""

    SUCCESS = "success"
    RECORDING_UNAVAILABLE = "recording_unavailable"
    AUTHENTICATION_FAILED = "authentication_failed"
    EXTRACTION_FAILED = "extraction_failed"
    TIMEOUT = "timeout"
    UNEXPECTED_ERROR = "unexpected_error"


@final
@dataclass(frozen=True, slots=True)
class CollectionContractError(ValueError):
    """Raised when a collection result mixes success and failure fields."""

    reason: str

    @override
    def __str__(self) -> str:
        """Return the invalid result contract guidance."""
        return self.reason


@dataclass(frozen=True, slots=True)
class CollectionItemResult:
    """One item outcome with either a caller-owned clip or a safe failure reason."""

    item_id: str
    channel_id: int
    role: CameraRole
    collection_status: CollectionStatus
    replay_clip: ReplayClip | None
    failure_reason: str | None

    def __post_init__(self) -> None:
        """Keep successful clips and failed-item reasons mutually exclusive."""
        if self.collection_status is CollectionStatus.SUCCESS:
            if self.replay_clip is None or self.failure_reason is not None:
                raise CollectionContractError(_INVALID_SUCCESS_RESULT)
        elif self.replay_clip is not None or self.failure_reason is None:
            raise CollectionContractError(_INVALID_FAILURE_RESULT)

    @classmethod
    def success(cls, item: InvestigationItem, replay_clip: ReplayClip) -> "CollectionItemResult":
        """Create a successful result while preserving the plan item's identity."""
        return cls(
            item.item_id,
            item.channel_id,
            item.role,
            CollectionStatus.SUCCESS,
            replay_clip,
            None,
        )

    @classmethod
    def failure(
        cls,
        item: InvestigationItem,
        collection_status: CollectionStatus,
        failure_reason: str,
    ) -> "CollectionItemResult":
        """Create a failed result while retaining no temporary clip."""
        return cls(
            item.item_id,
            item.channel_id,
            item.role,
            collection_status,
            None,
            failure_reason,
        )


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Ordered independent collection outcomes for one immutable investigation plan."""

    investigation_plan: InvestigationPlan
    items: tuple[CollectionItemResult, ...]


@final
@dataclass(frozen=True, slots=True)
class InvestigationCollector:
    """Compose existing recording search and replay extraction per plan item."""

    recording_planner: RecordingPlanningBoundary
    replay_extractor: ReplayExtractionBoundary

    def collect(self, investigation_plan: InvestigationPlan) -> CollectionResult:
        """Collect every item independently while preserving investigation-plan order."""
        return CollectionResult(
            investigation_plan,
            tuple(self._collect_item(item) for item in investigation_plan.items),
        )

    def _collect_item(self, item: InvestigationItem) -> CollectionItemResult:
        """Collect one planned replay and translate safe existing retrieval errors."""
        try:
            request = self.recording_planner.plan(item.recording_window)
            return CollectionItemResult.success(item, self.replay_extractor.extract(request))
        except (
            NvrRequestError,
            RecordingUnavailableError,
            ReplayAuthenticationError,
            ReplayExtractionError,
            ReplayTimeoutError,
            ReplayUnavailableError,
        ) as error:
            return CollectionItemResult.failure(item, _collection_status(error), str(error))
        except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — isolate untrusted retrieval faults per item.
            return CollectionItemResult.failure(
                item,
                CollectionStatus.UNEXPECTED_ERROR,
                _UNEXPECTED_FAILURE_REASON,
            )


def _collection_status(error: KnownCollectionError) -> CollectionStatus:
    match error:  # noqa: RUF100  # noqa: MATCH_OK — KnownCollectionError is closed at this boundary.
        case RecordingUnavailableError() | ReplayUnavailableError():
            return CollectionStatus.RECORDING_UNAVAILABLE
        case ReplayAuthenticationError():
            return CollectionStatus.AUTHENTICATION_FAILED
        case ReplayTimeoutError():
            return CollectionStatus.TIMEOUT
        case ReplayExtractionError():
            return CollectionStatus.EXTRACTION_FAILED
        case NvrRequestError(kind=NvrErrorKind.AUTHENTICATION):
            return CollectionStatus.AUTHENTICATION_FAILED
        case NvrRequestError():
            return CollectionStatus.UNEXPECTED_ERROR
