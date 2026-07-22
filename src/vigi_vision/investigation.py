"""Pure multi-camera investigation planning domain contract."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, final

from typing_extensions import override

from vigi_vision.profiles import get_profile
from vigi_vision.recording import RecordingWindow

_IDENTIFIER_PATTERN: Final = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_IANA_TIMEZONE_PATTERN: Final = re.compile(r"[A-Za-z][A-Za-z0-9_+.-]*(?:/[A-Za-z0-9_+.-]+)+")
_KST_INPUT_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
_KST_INPUT_PATTERN: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
_KST_TIMEZONE: Final = "Asia/Seoul"
_KST_UTC_OFFSET: Final = timedelta(hours=9)
_KST: Final = timezone(_KST_UTC_OFFSET, "KST")
_ITEM_TIME_FORMAT: Final = "%Y%m%dT%H%M%SZ"
_INVALID_ANCHOR_UTC: Final = "Anchor time must be a timezone-aware whole-second UTC instant."
_INVALID_ANCHOR_TIMEZONE: Final = "Anchor time must name a valid IANA source timezone."
_INVALID_KST_INPUT: Final = "Anchor time must use YYYY-MM-DD HH:MM:SS in Asia/Seoul."
_INVALID_CAMERA_ROLE: Final = "Camera roles must use lowercase kebab-case identifiers."
_INVALID_CHANNEL_ID: Final = "Camera channel IDs must be positive integers."
_INVALID_RELATIVE_WINDOW: Final = "Relative recording windows must start before they end."
_INVALID_PROFILE_ID: Final = "Scenario profile IDs must use lowercase kebab-case identifiers."
_INVALID_SCENARIO_ID: Final = "Scenario IDs must use lowercase kebab-case identifiers."
_EMPTY_SCENARIO: Final = "Scenarios must define at least one camera role rule."
_DUPLICATE_SCENARIO_ROLE: Final = "Scenarios cannot define a camera role more than once."
_CONFLICTING_ASSIGNMENT: Final = "Each camera channel must have one investigation role."


@final
@dataclass(frozen=True, slots=True)
class InvalidAnchorTimeError(ValueError):
    """Raised when an anchor cannot be represented as a whole-second UTC instant."""

    reason: str

    @override
    def __str__(self) -> str:
        """Return safe guidance for the anchor-time contract."""
        return self.reason


@final
@dataclass(frozen=True, slots=True)
class InvalidInvestigationDefinitionError(ValueError):
    """Raised when an investigation domain value violates its stable contract."""

    reason: str

    @override
    def __str__(self) -> str:
        """Return safe guidance for the invalid domain definition."""
        return self.reason


@final
@dataclass(frozen=True, slots=True)
class MissingRequiredCameraRoleError(ValueError):
    """Raised when a required scenario role has no assigned channel."""

    role: "CameraRole"

    @override
    def __str__(self) -> str:
        """Return the missing role without exposing deployment details."""
        return f"The required camera role '{self.role.value}' has no channel assignment."


@dataclass(frozen=True, slots=True)
class AnchorTime:
    """A whole-second UTC anchor with its user-input timezone for traceability."""

    anchor_utc: datetime
    source_timezone: str

    def __post_init__(self) -> None:
        """Reject noncanonical instants and malformed IANA timezone identifiers."""
        if (
            self.anchor_utc.tzinfo is None
            or self.anchor_utc.utcoffset() != timedelta(0)
            or self.anchor_utc.microsecond != 0
        ):
            raise InvalidAnchorTimeError(_INVALID_ANCHOR_UTC)
        if _IANA_TIMEZONE_PATTERN.fullmatch(self.source_timezone) is None:
            raise InvalidAnchorTimeError(_INVALID_ANCHOR_TIMEZONE)


@dataclass(frozen=True, slots=True)
class CameraRole:
    """A stable generic role assigned to one or more camera channels."""

    value: str

    def __post_init__(self) -> None:
        """Require a lowercase kebab-case semantic role."""
        if _IDENTIFIER_PATTERN.fullmatch(self.value) is None:
            raise InvalidInvestigationDefinitionError(_INVALID_CAMERA_ROLE)


@dataclass(frozen=True, slots=True)
class CameraAssignment:
    """One deployment-specific NVR channel mapped to a semantic role."""

    channel_id: int
    role: CameraRole

    def __post_init__(self) -> None:
        """Require the positive channel identifiers accepted by the NVR boundary."""
        if self.channel_id <= 0:
            raise InvalidInvestigationDefinitionError(_INVALID_CHANNEL_ID)


@dataclass(frozen=True, slots=True)
class RelativeWindow:
    """Whole-second recording offsets relative to an investigation anchor."""

    start_offset_seconds: int
    end_offset_seconds: int

    def __post_init__(self) -> None:
        """Require a nonempty relative recording interval."""
        if self.start_offset_seconds >= self.end_offset_seconds:
            raise InvalidInvestigationDefinitionError(_INVALID_RELATIVE_WINDOW)


@dataclass(frozen=True, slots=True)
class ScenarioCameraRule:
    """One role's profile and recording-window policy within a scenario."""

    role: CameraRole
    profile_id: str
    window_policy: RelativeWindow
    required: bool

    def __post_init__(self) -> None:
        """Require canonical profile identifiers before composition validation."""
        if _IDENTIFIER_PATTERN.fullmatch(self.profile_id) is None:
            raise InvalidInvestigationDefinitionError(_INVALID_PROFILE_ID)


@dataclass(frozen=True, slots=True)
class Scenario:
    """An ordered, data-only collection policy independent of camera deployment."""

    scenario_id: str
    rules: tuple[ScenarioCameraRule, ...]

    def __post_init__(self) -> None:
        """Reject empty scenarios, invalid IDs, and duplicate role rules."""
        if _IDENTIFIER_PATTERN.fullmatch(self.scenario_id) is None:
            raise InvalidInvestigationDefinitionError(_INVALID_SCENARIO_ID)
        if not self.rules:
            raise InvalidInvestigationDefinitionError(_EMPTY_SCENARIO)
        roles = tuple(rule.role.value for rule in self.rules)
        if len(set(roles)) != len(roles):
            raise InvalidInvestigationDefinitionError(_DUPLICATE_SCENARIO_ROLE)


@dataclass(frozen=True, slots=True)
class InvestigationItem:
    """One deterministic channel recording request produced by a scenario rule."""

    item_id: str
    channel_id: int
    role: CameraRole
    profile_id: str
    recording_window: RecordingWindow


@dataclass(frozen=True, slots=True)
class InvestigationPlan:
    """An ordered, complete plan that downstream collection may execute independently."""

    scenario_id: str
    anchor_time: AnchorTime
    items: tuple[InvestigationItem, ...]


@final
class InvestigationPlanner:
    """Expand validated scenario rules into deterministic UTC recording windows."""

    def plan(
        self,
        anchor_time: AnchorTime,
        scenario: Scenario,
        assignments: tuple[CameraAssignment, ...],
    ) -> InvestigationPlan:
        """Plan every matching channel without NVR, media, AI, or filesystem operations."""
        channel_ids = tuple(assignment.channel_id for assignment in assignments)
        if len(set(channel_ids)) != len(channel_ids):
            raise InvalidInvestigationDefinitionError(_CONFLICTING_ASSIGNMENT)
        items: list[InvestigationItem] = []
        for rule in scenario.rules:
            matching_assignments = tuple(
                sorted(
                    (assignment for assignment in assignments if assignment.role == rule.role),
                    key=lambda assignment: assignment.channel_id,
                )
            )
            if not matching_assignments and rule.required:
                raise MissingRequiredCameraRoleError(rule.role)
            start_utc = anchor_time.anchor_utc + timedelta(
                seconds=rule.window_policy.start_offset_seconds
            )
            end_utc = anchor_time.anchor_utc + timedelta(
                seconds=rule.window_policy.end_offset_seconds
            )
            for assignment in matching_assignments:
                window = RecordingWindow(assignment.channel_id, start_utc, end_utc)
                item_id = (
                    f"{scenario.scenario_id}-channel-{assignment.channel_id}-"
                    f"{rule.role.value}-{start_utc.strftime(_ITEM_TIME_FORMAT)}-"
                    f"{end_utc.strftime(_ITEM_TIME_FORMAT)}"
                )
                items.append(
                    InvestigationItem(
                        item_id=item_id,
                        channel_id=assignment.channel_id,
                        role=rule.role,
                        profile_id=rule.profile_id,
                        recording_window=window,
                    )
                )
        return InvestigationPlan(scenario.scenario_id, anchor_time, tuple(items))


def parse_kst_anchor(value: str) -> AnchorTime:
    """Parse the current product's strict Asia/Seoul timestamp input into UTC."""
    if _KST_INPUT_PATTERN.fullmatch(value) is None:
        raise InvalidAnchorTimeError(_INVALID_KST_INPUT)
    try:
        local_time = datetime.strptime(value, _KST_INPUT_FORMAT).replace(tzinfo=_KST)
    except ValueError as error:
        raise InvalidAnchorTimeError(_INVALID_KST_INPUT) from error
    anchor_utc = local_time.astimezone(timezone.utc)
    return AnchorTime(anchor_utc, _KST_TIMEZONE)


def validate_scenario_profiles(scenario: Scenario) -> Scenario:
    """Validate scenario profile references at composition before pure planning."""
    for rule in scenario.rules:
        _ = get_profile(rule.profile_id)
    return scenario
