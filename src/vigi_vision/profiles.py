"""Profile-specific prompts and strict structured scene-analysis schemas."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from typing_extensions import override

ProfileValue: TypeAlias = bool | int | str
Confidence: TypeAlias = Literal["high", "moderate", "low"]
MAX_SUMMARY_SENTENCES: Final = 2


@dataclass(frozen=True, slots=True)
class SummarySentenceLimitError(ValueError):
    """Report that a profile summary exceeds its two-sentence limit."""

    sentence_count: int

    @override
    def __str__(self) -> str:
        return "A profile summary may contain at most two sentences."


class ProfileAnalysis(BaseModel):
    """Common fields returned by every registered camera-analysis profile."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    summary: str
    confidence: Confidence
    evidence: tuple[str, ...]
    recommendations: tuple[str, ...]
    notable_observations: tuple[str, ...]
    limitations: str

    @field_validator("summary")
    @classmethod
    def require_at_most_two_sentences(cls, value: str) -> str:
        """Keep business summaries concise and suitable for the CLI report."""
        sentence_count = sum(value.count(terminator) for terminator in ".!?")
        if sentence_count > MAX_SUMMARY_SENTENCES:
            raise SummarySentenceLimitError(sentence_count)
        return value

    def display_values(self) -> tuple[ProfileValue, ...]:
        """Return scalar values in the registered report-field order."""
        raise NotImplementedError


class CounterAnalysis(ProfileAnalysis):
    """Structured observations for a counter-facing camera frame."""

    profile: Literal["counter"]
    staff_visible: bool
    customer_visible: bool
    customer_at_counter: bool
    counter_occupied: bool
    possible_payment_interaction: bool

    @override
    def display_values(self) -> tuple[ProfileValue, ...]:
        return (
            self.staff_visible,
            self.customer_visible,
            self.customer_at_counter,
            self.counter_occupied,
            self.possible_payment_interaction,
        )


class DiningAnalysis(ProfileAnalysis):
    """Structured estimates for a dining-area camera frame."""

    profile: Literal["dining"]
    estimated_people_count: int
    occupied_tables: int
    empty_tables: int
    standing_people: int
    crowd_level: str

    @override
    def display_values(self) -> tuple[ProfileValue, ...]:
        return (
            self.estimated_people_count,
            self.occupied_tables,
            self.empty_tables,
            self.standing_people,
            self.crowd_level,
        )


class EntranceAnalysis(ProfileAnalysis):
    """Structured estimates for an entrance-facing camera frame."""

    profile: Literal["entrance"]
    estimated_shoe_pairs_on_rack: int
    estimated_shoe_pairs_on_floor: int
    person_near_entrance: bool
    entrance_clear: bool
    scattered_footwear: bool

    @override
    def display_values(self) -> tuple[ProfileValue, ...]:
        return (
            self.estimated_shoe_pairs_on_rack,
            self.estimated_shoe_pairs_on_floor,
            self.person_near_entrance,
            self.entrance_clear,
            self.scattered_footwear,
        )


@dataclass(frozen=True, slots=True)
class ReportField:
    """One scalar schema field rendered in the shared CLI report."""

    name: str
    label: str


@dataclass(frozen=True, slots=True)
class ProfileDefinition:
    """A complete task definition consumed by the shared image analyzer."""

    name: str
    prompt: str
    response_model: type[ProfileAnalysis]
    report_fields: tuple[ReportField, ...]


@dataclass(frozen=True, slots=True)
class UnknownProfileError(RuntimeError):
    """Raised when CLI input does not name a registered camera profile."""

    name: str

    @override
    def __str__(self) -> str:
        """Return stable profile-selection guidance."""
        return f"Unknown profile '{self.name}'. Choose one of: counter, dining, entrance."


@dataclass(frozen=True, slots=True)
class ProfileResponseError(RuntimeError):
    """Raised when a profile response does not match its strict schema."""

    profile_name: str
    exception_type: str

    @override
    def __str__(self) -> str:
        """Return a redacted structured-response failure."""
        return (
            f"Structured response parsing failure for profile '{self.profile_name}' "
            f"[{self.exception_type}]. Check the structured output contract."
        )


_COUNTER_PROFILE: Final = ProfileDefinition(
    name="counter",
    prompt=(
        "Inspect this one counter-area camera frame. Determine whether staff and customers are "
        "visible, whether a customer is at the counter, whether the counter is occupied, and "
        "whether a possible payment interaction is visible. Never conclude payment definitely "
        "happened from a single frame; use cautious wording such as 'possible payment interaction' "
        "or 'payment activity may be occurring'. Return a business summary of at most two "
        "sentences, qualitative confidence of high, moderate, or low, the canonical profile "
        "identifier 'counter', observable evidence, "
        "recommendations only when appropriate, concise notable observations, and the limitations "
        "of one still image."
    ),
    response_model=CounterAnalysis,
    report_fields=(
        ReportField("staff_visible", "Staff Visible"),
        ReportField("customer_visible", "Customer Visible"),
        ReportField("customer_at_counter", "Customer at Counter"),
        ReportField("counter_occupied", "Counter Occupied"),
        ReportField("possible_payment_interaction", "Possible Payment Interaction"),
    ),
)

_DINING_PROFILE: Final = ProfileDefinition(
    name="dining",
    prompt=(
        "Inspect this one dining-area camera frame. Estimate people, occupied tables, empty "
        "tables, and standing people, then classify the crowd level. Treat every count as an "
        "estimate from one still image. Return a business summary of at most two sentences, "
        "qualitative confidence of high, moderate, or low, the canonical profile identifier "
        "'dining', observable evidence, recommendations "
        "only when appropriate, concise notable observations, and limitations."
    ),
    response_model=DiningAnalysis,
    report_fields=(
        ReportField("estimated_people_count", "Estimated People Count"),
        ReportField("occupied_tables", "Occupied Tables"),
        ReportField("empty_tables", "Empty Tables"),
        ReportField("standing_people", "Standing People"),
        ReportField("crowd_level", "Crowd Level"),
    ),
)

_ENTRANCE_PROFILE: Final = ProfileDefinition(
    name="entrance",
    prompt=(
        "Inspect this one entrance-area camera frame. Estimate shoe pairs on a rack and on the "
        "floor; determine whether a person is near the entrance, whether the entrance is clear, "
        "and whether footwear is scattered. Treat every count as an estimate from one still image. "
        "Return a business summary of at most two sentences, qualitative confidence of high, "
        "moderate, or low, the canonical profile identifier 'entrance', observable evidence, "
        "recommendations only when appropriate, concise "
        "notable observations, and limitations."
    ),
    response_model=EntranceAnalysis,
    report_fields=(
        ReportField("estimated_shoe_pairs_on_rack", "Estimated Shoe Pairs on Rack"),
        ReportField("estimated_shoe_pairs_on_floor", "Estimated Shoe Pairs on Floor"),
        ReportField("person_near_entrance", "Person Near Entrance"),
        ReportField("entrance_clear", "Entrance Clear"),
        ReportField("scattered_footwear", "Scattered Footwear"),
    ),
)

PROFILE_REGISTRY: Final[Mapping[str, ProfileDefinition]] = MappingProxyType(
    {profile.name: profile for profile in (_COUNTER_PROFILE, _DINING_PROFILE, _ENTRANCE_PROFILE)}
)
_PROFILE_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "카운터": "counter",
        "홀": "dining",
        "식사공간": "dining",
        "입구": "entrance",
        "신발장": "entrance",
    }
)


def resolve_profile_alias(name: str) -> str:
    """Resolve a documented profile alias to its canonical English identifier."""
    return _PROFILE_ALIASES.get(name, name)


def get_profile(name: str) -> ProfileDefinition:
    """Return one registered profile selected at the CLI boundary."""
    profile = PROFILE_REGISTRY.get(name)
    if profile is None:
        raise UnknownProfileError(name)
    return profile


def parse_profile_analysis(raw_response: str, profile: ProfileDefinition) -> ProfileAnalysis:
    """Validate an external model response against one selected profile schema."""
    try:
        return profile.response_model.model_validate_json(raw_response)
    except ValidationError as error:
        raise ProfileResponseError(profile.name, type(error).__name__) from error
