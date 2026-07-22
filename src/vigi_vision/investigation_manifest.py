"""Typed, credential-free JSON manifest for one investigation artifact package."""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vigi_vision.investigation import CameraRole
from vigi_vision.investigation_collection import CollectionStatus


@dataclass(frozen=True, slots=True)
class ManifestRecordingWindow:
    """Credential-free recording interval written into an investigation manifest."""

    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True, slots=True)
class ManifestItem:
    """One plan item with its safe collection and artifact outcome."""

    item_id: str
    channel_id: int
    role: CameraRole
    profile_id: str
    recording_window: ManifestRecordingWindow
    collection_status: CollectionStatus
    video_filename: str | None
    anchor_snapshot_filename: str | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class InvestigationManifest:
    """The typed, credential-free durable record of one investigation package."""

    investigation_id: str
    scenario_id: str
    anchor_time_utc: datetime
    source_timezone: str
    items: tuple[ManifestItem, ...]

    def write(self, output_path: Path) -> None:
        """Write stable JSON containing only selected safe metadata."""
        _ = output_path.write_text(
            json.dumps(
                {
                    "investigation_id": self.investigation_id,
                    "scenario_id": self.scenario_id,
                    "anchor_time_utc": _format_utc(self.anchor_time_utc),
                    "source_timezone": self.source_timezone,
                    "items": [
                        {
                            "item_id": item.item_id,
                            "channel_id": item.channel_id,
                            "role": item.role.value,
                            "profile_id": item.profile_id,
                            "recording_window": {
                                "start_utc": _format_utc(item.recording_window.start_utc),
                                "end_utc": _format_utc(item.recording_window.end_utc),
                            },
                            "collection_status": item.collection_status.value,
                            "video_filename": item.video_filename,
                            "anchor_snapshot_filename": item.anchor_snapshot_filename,
                            "failure_reason": item.failure_reason,
                        }
                        for item in self.items
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def _format_utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
