"""Durable, secret-safe investigation artifacts from collected replay clips."""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final, final

from typing_extensions import override

from vigi_vision.investigation import InvestigationItem, InvestigationPlan
from vigi_vision.investigation_collection import (
    CollectionItemResult,
    CollectionResult,
    CollectionStatus,
)
from vigi_vision.investigation_manifest import (
    InvestigationManifest,
    ManifestItem,
    ManifestRecordingWindow,
)
from vigi_vision.investigation_progress import InvestigationStage, ProgressReporter
from vigi_vision.investigation_snapshot import AnchorSnapshotBoundary, AnchorSnapshotError
from vigi_vision.replay import ReplayClip

_INVALID_COLLECTION_RESULT: Final = "Successful collection items require a replay clip."
_ANCHOR_OUTSIDE_CLIP: Final = "The investigation anchor falls outside the collected replay clip."
_MANIFEST_FILENAME: Final = "manifest.json"


@final
@dataclass(frozen=True, slots=True)
class InvestigationArtifactError(RuntimeError):
    """Raised when an investigation artifact cannot be produced safely."""

    reason: str

    @override
    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class InvestigationResult:
    """Typed handoff containing the plan, collection, manifest, and package location."""

    investigation_plan: InvestigationPlan
    collection_result: CollectionResult
    manifest: InvestigationManifest
    artifact_directory: Path


@final
@dataclass(frozen=True, slots=True)
class InvestigationArtifactBuilder:
    """Preserve clips, create anchor snapshots, write a manifest, and clean replay clips."""

    artifact_root: Path
    snapshot_extractor: AnchorSnapshotBoundary
    progress: ProgressReporter | None = None

    def build(self, collection_result: CollectionResult) -> InvestigationResult:
        """Create one deterministic durable package from ordered collection outcomes."""
        plan = collection_result.investigation_plan
        investigation_id = _investigation_id(plan)
        artifact_directory = self.artifact_root / investigation_id
        created_directory = False
        try:
            artifact_directory.mkdir(parents=True, exist_ok=False)
            created_directory = True
            manifest_items = tuple(
                self._build_item(item, plan, artifact_directory) for item in collection_result.items
            )
            manifest = InvestigationManifest(
                investigation_id,
                plan.scenario_id,
                plan.anchor_time.anchor_utc,
                plan.anchor_time.source_timezone,
                manifest_items,
            )
            self._report(InvestigationStage.MANIFEST_WRITING)
            manifest.write(artifact_directory / _MANIFEST_FILENAME)
            return InvestigationResult(plan, collection_result, manifest, artifact_directory)
        except Exception:  # noqa: BROAD_EXCEPT_OK, BLE001 — cleanup covers every build boundary.
            _remove_replay_clips(collection_result)
            if created_directory:
                shutil.rmtree(artifact_directory)
            raise

    def _build_item(
        self,
        collection_item: CollectionItemResult,
        plan: InvestigationPlan,
        artifact_directory: Path,
    ) -> ManifestItem:
        item = _plan_item(plan, collection_item.item_id)
        window = ManifestRecordingWindow(
            item.recording_window.start_utc,
            item.recording_window.end_utc,
        )
        match collection_item.collection_status:  # noqa: RUF100  # noqa: MATCH_OK — CollectionStatus is closed.
            case CollectionStatus.SUCCESS:
                replay_clip = collection_item.replay_clip
                if replay_clip is None:
                    raise InvestigationArtifactError(_INVALID_COLLECTION_RESULT)
                video_filename = _video_filename(item)
                video_path = artifact_directory / video_filename
                self._report(InvestigationStage.MP4_PRESERVATION)
                _ = shutil.move(replay_clip.temporary_mp4_path, video_path)
                snapshot_filename = _snapshot_filename(item)
                snapshot_path = artifact_directory / snapshot_filename
                try:
                    self._report(InvestigationStage.ANCHOR_SNAPSHOT)
                    _ = self.snapshot_extractor.extract(
                        video_path,
                        _anchor_offset_seconds(replay_clip, plan),
                        snapshot_path,
                    )
                except AnchorSnapshotError:
                    _ = snapshot_path.unlink(missing_ok=True)
                    raise
                replay_clip.remove()
                return ManifestItem(
                    item.item_id,
                    item.channel_id,
                    item.role,
                    item.profile_id,
                    window,
                    collection_item.collection_status,
                    video_filename,
                    snapshot_filename,
                    None,
                )
            case (
                CollectionStatus.RECORDING_UNAVAILABLE
                | CollectionStatus.AUTHENTICATION_FAILED
                | CollectionStatus.EXTRACTION_FAILED
                | CollectionStatus.TIMEOUT
                | CollectionStatus.UNEXPECTED_ERROR
            ):
                return ManifestItem(
                    item.item_id,
                    item.channel_id,
                    item.role,
                    item.profile_id,
                    window,
                    collection_item.collection_status,
                    None,
                    None,
                    _safe_failure_reason(collection_item.collection_status),
                )

    def _report(self, stage: InvestigationStage) -> None:
        if self.progress is not None:
            self.progress(stage)


def _plan_item(plan: InvestigationPlan, item_id: str) -> InvestigationItem:
    for item in plan.items:
        if item.item_id == item_id:
            return item
    raise InvestigationArtifactError(_INVALID_COLLECTION_RESULT)


def _anchor_offset_seconds(replay_clip: ReplayClip, plan: InvestigationPlan) -> int:
    offset = plan.anchor_time.anchor_utc - replay_clip.requested_start_utc
    offset_seconds = int(offset.total_seconds())
    if offset_seconds < 0 or offset_seconds >= replay_clip.duration_seconds:
        raise InvestigationArtifactError(_ANCHOR_OUTSIDE_CLIP)
    return offset_seconds


def _investigation_id(plan: InvestigationPlan) -> str:
    return f"{plan.scenario_id}-{plan.anchor_time.anchor_utc.strftime('%Y%m%dT%H%M%SZ')}"


def _video_filename(item: InvestigationItem) -> str:
    return f"{item.role.value}-channel-{item.channel_id}.mp4"


def _snapshot_filename(item: InvestigationItem) -> str:
    return f"{item.role.value}-channel-{item.channel_id}-anchor.jpg"


def _safe_failure_reason(status: CollectionStatus) -> str:
    match status:  # noqa: RUF100  # noqa: MATCH_OK — CollectionStatus is closed.
        case CollectionStatus.SUCCESS:
            raise InvestigationArtifactError(_INVALID_COLLECTION_RESULT)
        case CollectionStatus.RECORDING_UNAVAILABLE:
            return "Recording unavailable."
        case CollectionStatus.AUTHENTICATION_FAILED:
            return "Recording authentication failed."
        case CollectionStatus.EXTRACTION_FAILED:
            return "Replay extraction failed."
        case CollectionStatus.TIMEOUT:
            return "Replay extraction timed out."
        case CollectionStatus.UNEXPECTED_ERROR:
            return "Collection failure details were redacted."


def _remove_replay_clips(collection_result: CollectionResult) -> None:
    for item in collection_result.items:
        if item.replay_clip is not None:
            item.replay_clip.remove()
