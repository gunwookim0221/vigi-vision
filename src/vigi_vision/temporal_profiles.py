"""Strict temporal-analysis contracts and profile prompts."""

from collections.abc import Mapping
from dataclasses import dataclass
from math import isclose
from types import MappingProxyType
from typing import ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator
from typing_extensions import override

from vigi_vision.profiles import Confidence, UnknownProfileError
from vigi_vision.video import FrameRecord

TemporalProfileName = Literal["counter", "dining", "entrance"]
RecommendationBasis = Literal["evidence", "limitation"]
_PAIRED_EVIDENCE_MESSAGE = "evidence references must pair sampled frames and timestamps"
_CHRONOLOGICAL_CHANGE_MESSAGE = "an observed change must move forward through sampled frames"
_UNCERTAINTY_MESSAGE = "a possible event must explicitly express uncertainty"


class TemporalEvidence(BaseModel):
    """Visible observation tied to authoritative sampled frames and timestamps."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    frame_indices: tuple[int, ...]
    timestamps: tuple[float, ...]
    description: str

    @model_validator(mode="after")
    def references_are_paired(self) -> "TemporalEvidence":
        """Require a timestamp for every named sampled frame."""
        if not self.frame_indices or len(self.frame_indices) != len(self.timestamps):
            raise ValueError(_PAIRED_EVIDENCE_MESSAGE)
        return self


class ObservedChange(BaseModel):
    """A visible change recorded only between two ordered sampled frames."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    from_frame: int
    to_frame: int
    description: str

    @model_validator(mode="after")
    def frames_are_chronological(self) -> "ObservedChange":
        """Require that the change moves forward through the supplied samples."""
        if self.from_frame >= self.to_frame:
            raise ValueError(_CHRONOLOGICAL_CHANGE_MESSAGE)
        return self


class PossibleEvent(BaseModel):
    """An explicitly uncertain interpretation supported by sampled frames."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    description: str
    supporting_frames: tuple[int, ...]
    confidence_note: str

    @field_validator("confidence_note")
    @classmethod
    def confidence_note_expresses_uncertainty(cls, value: str) -> str:
        """Reject possible-event notes that present an inference as certain."""
        uncertainty_markers = ("possible", "may", "cannot", "uncertain", "unconfirmed")
        if not any(marker in value.lower() for marker in uncertainty_markers):
            raise ValueError(_UNCERTAINTY_MESSAGE)
        return value


class TemporalRecommendation(BaseModel):
    """A next step grounded in visible evidence or an explicit limitation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    basis: RecommendationBasis
    description: str


class TemporalProfileAnalysis(BaseModel):
    """The strict common report for sparse, ordered temporal samples."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    profile: TemporalProfileName
    summary: str
    confidence: Confidence
    evidence: tuple[TemporalEvidence, ...]
    observed_changes: tuple[ObservedChange, ...]
    possible_events: tuple[PossibleEvent, ...]
    unresolved_temporal_questions: tuple[str, ...]
    recommendations: tuple[TemporalRecommendation, ...]
    limitations: str
    video_duration_seconds: float
    sampled_frame_count: int
    sampled_timestamps: tuple[float, ...]

    def profile_findings(self) -> tuple[str, ...]:
        """Return concise profile-specific report lines for the terminal renderer."""
        raise NotImplementedError


class CounterTemporalAnalysis(TemporalProfileAnalysis):
    """Sparse counter-area state and potential service-sequence findings."""

    counter_occupancy_trend: Literal[
        "occupied_throughout",
        "becomes_occupied",
        "becomes_unoccupied",
        "no_change_visible",
        "uncertain",
    ]
    customer_counter_presence: Literal[
        "appears_near_counter", "appears_to_leave_counter", "not_visible", "uncertain"
    ]
    possible_service_interaction: bool
    possible_exchange_sequence: bool

    @override
    def profile_findings(self) -> tuple[str, ...]:
        service_interaction = "yes" if self.possible_service_interaction else "no"
        exchange_sequence = "yes" if self.possible_exchange_sequence else "no"
        return (
            f"Counter occupancy trend: {self.counter_occupancy_trend.replace('_', ' ')}.",
            f"Customer-counter presence: {self.customer_counter_presence.replace('_', ' ')}.",
            f"Possible service interaction: {service_interaction}.",
            f"Possible exchange sequence: {exchange_sequence}.",
        )


class DiningTemporalAnalysis(TemporalProfileAnalysis):
    """Sparse dining-area occupancy, movement, and crowd-trend findings."""

    estimated_people_count_trend: Literal["increases", "decreases", "stable", "uncertain"]
    occupied_table_trend: Literal["increases", "decreases", "stable", "uncertain"]
    empty_table_trend: Literal["increases", "decreases", "stable", "uncertain"]
    standing_person_change: Literal["appears", "leaves", "no_change_visible", "uncertain"]
    visible_movement: Literal["visible", "not_visible", "uncertain"]
    crowd_trend: Literal["increases", "decreases", "stable", "uncertain"]

    @override
    def profile_findings(self) -> tuple[str, ...]:
        return (
            f"Estimated people-count trend: {self.estimated_people_count_trend}.",
            f"Occupied-table trend: {self.occupied_table_trend}.",
            f"Empty-table trend: {self.empty_table_trend}.",
            f"Standing-person change: {self.standing_person_change.replace('_', ' ')}.",
            f"Visible movement: {self.visible_movement.replace('_', ' ')}.",
            f"Crowd trend: {self.crowd_trend}.",
        )


class EntranceTemporalAnalysis(TemporalProfileAnalysis):
    """Sparse entrance-state, person, and footwear-change findings."""

    entrance_state_trend: Literal["becomes_blocked", "becomes_clear", "unchanged", "uncertain"]
    person_entrance_change: Literal["approaches", "leaves", "not_visible", "uncertain"]
    footwear_count_trend: Literal["increases", "decreases", "stable", "uncertain"]
    footwear_change: Literal[
        "appears_added", "appears_removed", "scattered", "none_visible", "uncertain"
    ]

    @override
    def profile_findings(self) -> tuple[str, ...]:
        return (
            f"Entrance-state trend: {self.entrance_state_trend.replace('_', ' ')}.",
            f"Person-entrance change: {self.person_entrance_change.replace('_', ' ')}.",
            f"Footwear-count trend: {self.footwear_count_trend}.",
            f"Footwear change: {self.footwear_change.replace('_', ' ')}.",
        )


@dataclass(frozen=True, slots=True)
class TemporalProfileDefinition:
    """The prompt and strict response model selected for one temporal profile."""

    name: TemporalProfileName
    prompt: str
    response_model: type[TemporalProfileAnalysis]


@dataclass(slots=True)
class TemporalProfileResponseError(RuntimeError):
    """Report an unusable profile response without exposing raw model output."""

    profile_name: str
    exception_type: str
    validation_fields: tuple[str, ...] = ()

    @override
    def __str__(self) -> str:
        return (
            f"Structured temporal response parsing failure for profile '{self.profile_name}' "
            f"[{self.exception_type}]. Check the structured output contract."
        )


@dataclass(slots=True)
class TemporalReferenceError(RuntimeError):
    """Report a response reference that is absent from the supplied samples."""

    profile_name: str
    reference_type: str

    @override
    def __str__(self) -> str:
        return (
            f"Structured temporal response reference failure for profile '{self.profile_name}' "
            f"[{self.reference_type}]. References must use supplied sampled frames and timestamps."
        )


_TEMPORAL_SAFETY: Final = (
    "Use only the supplied ordered frame labels and timestamps as authoritative local references. "
    "Separate visible state, observed change, possible event, and unknown. Do not identify or "
    "continuously track people, infer activity in unsampled gaps, or conclude payment or a "
    "completed transaction. Possible events must explicitly state uncertainty. Recommendations "
    "must follow visible "
    "evidence or stated limitations."
)
_TEMPORAL_RESPONSE_CONTRACT: Final = (
    "Return exactly one JSON object matching the supplied schema. Never omit a required key. "
    "Every collection field must be a JSON array; use [] when no finding exists and never use a "
    "plain string in place of an array. Required common keys are profile, summary, confidence, "
    "evidence, observed_changes, possible_events, unresolved_temporal_questions, recommendations, "
    "limitations, video_duration_seconds, sampled_frame_count, and sampled_timestamps. Use these "
    'nested shapes: evidence=[{"frame_indices":[1],"timestamps":[0.0],"description":"visible '
    'state"}]; observed_changes=[{"from_frame":1,"to_frame":2,"description":"visible '
    'change"}]; possible_events=[{"description":"possible event","supporting_frames":[1,2],'
    '"confidence_note":"Possible only; sparse samples cannot confirm it."}]; '
    'recommendations=[{"basis":"evidence","description":"next step"}].'
)
_COUNTER_PROMPT: Final = (
    f"Analyze sparse counter-area samples. {_TEMPORAL_SAFETY} {_TEMPORAL_RESPONSE_CONTRACT} Set "
    "profile to counter. Required counter keys are counter_occupancy_trend, "
    "customer_counter_presence, possible_service_interaction, and possible_exchange_sequence. Use "
    "only these counter_occupancy_trend values: occupied_throughout, becomes_occupied, "
    "becomes_unoccupied, no_change_visible, uncertain. Use only these customer_counter_presence "
    "values: appears_near_counter, appears_to_leave_counter, not_visible, uncertain. The two "
    "possible fields are JSON booleans, never strings."
)
_DINING_PROMPT: Final = (
    f"Analyze sparse dining-area samples. {_TEMPORAL_SAFETY} {_TEMPORAL_RESPONSE_CONTRACT} Set "
    "profile to dining and provide estimated_people_count_trend, occupied_table_trend, "
    "empty_table_trend, standing_person_change, visible_movement, and crowd_trend using only the "
    "schema enum tokens."
)
_ENTRANCE_PROMPT: Final = (
    f"Analyze sparse entrance-area samples. {_TEMPORAL_SAFETY} {_TEMPORAL_RESPONSE_CONTRACT} Set "
    "profile to entrance and provide entrance_state_trend, person_entrance_change, "
    "footwear_count_trend, and footwear_change using only the schema enum tokens."
)

TEMPORAL_PROFILE_REGISTRY: Final[Mapping[str, TemporalProfileDefinition]] = MappingProxyType(
    {
        "counter": TemporalProfileDefinition("counter", _COUNTER_PROMPT, CounterTemporalAnalysis),
        "dining": TemporalProfileDefinition("dining", _DINING_PROMPT, DiningTemporalAnalysis),
        "entrance": TemporalProfileDefinition(
            "entrance", _ENTRANCE_PROMPT, EntranceTemporalAnalysis
        ),
    }
)


def get_temporal_profile(name: str) -> TemporalProfileDefinition:
    """Return the strict prompt and schema selected at the canonical profile boundary."""
    profile = TEMPORAL_PROFILE_REGISTRY.get(name)
    if profile is None:
        raise UnknownProfileError(name)
    return profile


def parse_temporal_profile_analysis(
    raw_response: str, profile: TemporalProfileDefinition, frames: tuple[FrameRecord, ...]
) -> TemporalProfileAnalysis:
    """Parse a selected response and prove every reference belongs to a supplied sample."""
    try:
        analysis = profile.response_model.model_validate_json(raw_response)
    except ValidationError as error:
        validation_fields = tuple(
            ".".join(str(location) for location in details["loc"]) for details in error.errors()
        )
        raise TemporalProfileResponseError(
            profile.name, type(error).__name__, validation_fields
        ) from error
    if analysis.profile != profile.name:
        raise TemporalProfileResponseError(profile.name, "ProfileMismatch")
    _validate_temporal_references(analysis, frames)
    return analysis


def _validate_temporal_references(
    analysis: TemporalProfileAnalysis, frames: tuple[FrameRecord, ...]
) -> None:
    timestamps_by_frame = {frame.index: frame.timestamp_ms / 1_000 for frame in frames}
    for evidence in analysis.evidence:
        for frame_index, timestamp in zip(evidence.frame_indices, evidence.timestamps, strict=True):
            sampled_timestamp = timestamps_by_frame.get(frame_index)
            if sampled_timestamp is None or not isclose(
                timestamp, sampled_timestamp, abs_tol=0.001
            ):
                raise TemporalReferenceError(analysis.profile, "EvidenceReference")
    for change in analysis.observed_changes:
        if (
            change.from_frame not in timestamps_by_frame
            or change.to_frame not in timestamps_by_frame
        ):
            raise TemporalReferenceError(analysis.profile, "ObservedChangeReference")
    for event in analysis.possible_events:
        if not event.supporting_frames or any(
            frame_index not in timestamps_by_frame for frame_index in event.supporting_frames
        ):
            raise TemporalReferenceError(analysis.profile, "PossibleEventReference")
