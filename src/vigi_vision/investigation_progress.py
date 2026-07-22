"""Typed progress stages shared by investigation orchestration boundaries."""

from collections.abc import Callable
from enum import Enum
from typing import final


@final
class InvestigationStage(str, Enum):
    """Safe stages reported while one investigation executes."""

    SETUP = "setup"
    PLANNING = "planning"
    COLLECTION = "recording collection"
    ARTIFACT_PACKAGE = "artifact package"
    MP4_PRESERVATION = "durable MP4 transfer"
    ANCHOR_SNAPSHOT = "anchor snapshot extraction"
    MANIFEST_WRITING = "manifest writing"


ProgressReporter = Callable[[InvestigationStage], None]
