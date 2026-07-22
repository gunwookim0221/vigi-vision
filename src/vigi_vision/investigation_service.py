"""Single orchestration entry point for the existing investigation boundaries."""

from dataclasses import dataclass
from typing import Protocol, final

from vigi_vision.investigation import (
    AnchorTime,
    CameraAssignment,
    InvestigationPlan,
    Scenario,
)
from vigi_vision.investigation_artifacts import InvestigationResult
from vigi_vision.investigation_collection import CollectionResult
from vigi_vision.investigation_progress import InvestigationStage, ProgressReporter


@dataclass(frozen=True, slots=True)
class InvestigationRequest:
    """Validated investigation inputs required by the planning boundary."""

    anchor_time: AnchorTime
    scenario: Scenario
    camera_assignments: tuple[CameraAssignment, ...]


class InvestigationPlanningBoundary(Protocol):
    """Plan one validated investigation request without collection or artifact work."""

    def plan(
        self,
        anchor_time: AnchorTime,
        scenario: Scenario,
        assignments: tuple[CameraAssignment, ...],
    ) -> InvestigationPlan:
        """Return the ordered recording plan for the validated request."""
        ...


class InvestigationCollectionBoundary(Protocol):
    """Collect all replay outcomes for one investigation plan."""

    def collect(self, investigation_plan: InvestigationPlan) -> CollectionResult:
        """Return the ordered collection result without artifact generation."""
        ...


class InvestigationArtifactBoundary(Protocol):
    """Create durable artifacts for one collected investigation."""

    def build(self, collection_result: CollectionResult) -> InvestigationResult:
        """Return the complete durable investigation result."""
        ...


@final
@dataclass(frozen=True, slots=True)
class InvestigationService:
    """Compose existing planning, collection, and artifact boundaries exactly once."""

    planner: InvestigationPlanningBoundary
    collector: InvestigationCollectionBoundary
    artifact_builder: InvestigationArtifactBoundary
    progress: ProgressReporter | None = None

    def execute(self, request: InvestigationRequest) -> InvestigationResult:
        """Execute one validated request and propagate existing boundary failures."""
        self._report(InvestigationStage.PLANNING)
        plan = self.planner.plan(
            request.anchor_time,
            request.scenario,
            request.camera_assignments,
        )
        self._report(InvestigationStage.COLLECTION)
        collection = self.collector.collect(plan)
        self._report(InvestigationStage.ARTIFACT_PACKAGE)
        return self.artifact_builder.build(collection)

    def _report(self, stage: InvestigationStage) -> None:
        if self.progress is not None:
            self.progress(stage)
