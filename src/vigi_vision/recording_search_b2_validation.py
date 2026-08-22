"""Strict reopening of schema-3 manifests and indexed child records."""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from vigi_vision.durable_io import (
    DurableJsonError,
    is_safe_contained_path,
    load_durable_json_object,
)
from vigi_vision.investigation_confirmation_models import (
    ConfirmationError,
    ConfirmedInvestigationInput,
)
from vigi_vision.object_presence_values import VisualReason
from vigi_vision.recording_search_a2_models import (  # noqa: TC001
    AcquisitionOperationRecord,
    CanonicalProbeFrameRecord,
    ProbeFrameRequestRecord,
)
from vigi_vision.recording_search_a2_repository import (
    read_schema2_children,
    read_schema2_children_read_only_for_schema3,
    validate_schema2_tree,
    validate_schema2_tree_read_only_for_schema3,
)
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b2_records import (
    ClassificationOperationRecord,
    ConfirmedReferenceBaselineRecord,
    RecordingProbeObservationRecord,
    TargetAliasRecord,
)
from vigi_vision.recording_search_models import RecordingSearchManifestCorruptError

_ModelT = TypeVar("_ModelT", bound=BaseModel)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vigi_vision.object_presence_evidence import RawComparison
    from vigi_vision.recording_search_a2_models import RecordingSearchManifestV2


def parse_schema3_manifest(raw: str) -> RecordingSearchManifestV3:
    """Parse a strict schema-3 manifest JSON object."""
    try:
        _ = load_durable_json_object(raw)
        return RecordingSearchManifestV3.model_validate_json(raw, strict=True)
    except (DurableJsonError, ValidationError, ValueError, TypeError):
        raise RecordingSearchManifestCorruptError from None


class ConfirmedBaselineLoader(Protocol):
    """Resolve one strictly revalidated Phase 6 confirmation resource."""

    def load_confirmed(self, investigation_id: str) -> ConfirmedInvestigationInput:
        """Return authoritative confirmation facts and validated JPEG identity."""
        ...


def validate_schema3_tree(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV3
) -> ConfirmedReferenceBaselineRecord:
    """Validate every schema-2 and Phase 7B indexed relationship."""
    return _validate_schema3_tree(
        root,
        run_path,
        manifest,
        schema2_validator=validate_schema2_tree,
        schema2_reader=read_schema2_children,
    )


def validate_schema3_tree_read_only(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV3
) -> ConfirmedReferenceBaselineRecord:
    """Validate schema 3 without invoking active A2 recovery."""
    return _validate_schema3_tree(
        root,
        run_path,
        manifest,
        schema2_validator=validate_schema2_tree_read_only_for_schema3,
        schema2_reader=read_schema2_children_read_only_for_schema3,
    )


def _validate_schema3_tree(
    root: Path,
    run_path: Path,
    manifest: RecordingSearchManifestV3,
    *,
    schema2_validator: Callable[[Path, Path, RecordingSearchManifestV2], None],
    schema2_reader: Callable[
        [Path, Path, RecordingSearchManifestV2],
        tuple[
            dict[str, AcquisitionOperationRecord],
            dict[str, CanonicalProbeFrameRecord],
            dict[str, ProbeFrameRequestRecord],
        ],
    ],
) -> ConfirmedReferenceBaselineRecord:
    """Shared schema-3 relationship validation with an explicit reader boundary."""
    try:
        if not is_safe_contained_path(root, run_path, require_target=True) or run_path.is_symlink():
            _raise_corrupt()
        schema2_validator(root, run_path, manifest.as_schema2())
        operations, frames, requests = schema2_reader(root, run_path, manifest.as_schema2())
        _validate_directories(root, run_path)
        baseline = _read_child(
            root,
            run_path / "observations" / f"{manifest.baseline_observation_id}.json",
            ConfirmedReferenceBaselineRecord,
        )
        _validate_baseline(manifest, baseline)
        classification_operations = {
            operation_id: _read_child(
                root,
                run_path / "classification-operations" / f"{operation_id}.json",
                ClassificationOperationRecord,
            )
            for operation_id in manifest.classification_operation_ids
        }
        observations = {
            observation_id: _read_child(
                root,
                run_path / "observations" / f"{observation_id}.json",
                RecordingProbeObservationRecord,
            )
            for observation_id in manifest.canonical_observation_ids
        }
        aliases = {
            alias_id: _read_child(
                root,
                run_path / "observations" / f"{alias_id}.json",
                TargetAliasRecord,
            )
            for alias_id in manifest.target_alias_ids
        }
        _reject_unindexed_files(run_path, manifest)
        _validate_operations(manifest, classification_operations, operations, requests, frames)
        _validate_observations(
            manifest, baseline, observations, classification_operations, requests, frames
        )
        _validate_aliases(manifest, aliases, observations, requests)
    except RecordingSearchManifestCorruptError:
        raise
    except (OSError, ValueError, ValidationError, DurableJsonError, TypeError):
        raise RecordingSearchManifestCorruptError from None
    return baseline


def validate_authoritative_baseline(
    loader: ConfirmedBaselineLoader | None,
    manifest: RecordingSearchManifestV3,
    baseline: ConfirmedReferenceBaselineRecord,
) -> None:
    """Bind a committed schema-3 baseline to the authoritative Phase 6 JPEG."""
    if loader is None:
        raise RecordingSearchManifestCorruptError
    try:
        loaded = loader.load_confirmed(manifest.investigation_id)
    except ConfirmationError:
        raise RecordingSearchManifestCorruptError from None
    confirmation = manifest.confirmation
    if (
        loaded.investigation_id != manifest.investigation_id
        or loaded.channel_id != confirmation.channel_id
        or loaded.anchor_time_utc != confirmation.anchor_time_utc
        or loaded.source_timezone != confirmation.source_timezone
        or loaded.candidate_offset_seconds != confirmation.candidate_offset_seconds
        or loaded.reference_frame_resource_id != confirmation.reference_frame_resource_id
        or loaded.requested_time_utc != confirmation.reference_requested_time_utc
        or loaded.generation_policy_version != confirmation.generation_policy_version
        or loaded.frame_selection_policy != confirmation.frame_selection_policy
        or loaded.estimated_source_time_utc != confirmation.estimated_source_time_utc
        or loaded.decoded_local_pts_seconds != confirmation.decoded_local_pts_seconds
        or loaded.timing_precision_status != confirmation.timing_precision_status
        or loaded.warnings != confirmation.warnings
        or loaded.source_width != confirmation.source_width
        or loaded.source_height != confirmation.source_height
        or loaded.roi != confirmation.roi
        or loaded.jpeg_sha256 != confirmation.jpeg_sha256
        or loaded.jpeg_size_bytes != confirmation.jpeg_size_bytes
        or baseline.reference_frame_resource_id != loaded.reference_frame_resource_id
        or baseline.source_width != loaded.source_width
        or baseline.source_height != loaded.source_height
        or baseline.roi != loaded.roi
        or baseline.jpeg_sha256 != loaded.jpeg_sha256
        or baseline.jpeg_size_bytes != loaded.jpeg_size_bytes
    ):
        raise RecordingSearchManifestCorruptError


def read_schema3_children(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV3
) -> tuple[
    ConfirmedReferenceBaselineRecord,
    dict[str, ClassificationOperationRecord],
    dict[str, RecordingProbeObservationRecord],
    dict[str, TargetAliasRecord],
]:
    """Return the strictly reopened Phase 7B children for duplicate lookup."""
    _ = validate_schema3_tree(root, run_path, manifest)
    return _read_schema3_children(root, run_path, manifest)


def read_schema3_children_read_only(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV3
) -> tuple[
    ConfirmedReferenceBaselineRecord,
    dict[str, ClassificationOperationRecord],
    dict[str, RecordingProbeObservationRecord],
    dict[str, TargetAliasRecord],
]:
    """Read schema-3 children without invoking active A2 recovery."""
    _ = validate_schema3_tree_read_only(root, run_path, manifest)
    return _read_schema3_children(root, run_path, manifest)


def _read_schema3_children(
    root: Path, run_path: Path, manifest: RecordingSearchManifestV3
) -> tuple[
    ConfirmedReferenceBaselineRecord,
    dict[str, ClassificationOperationRecord],
    dict[str, RecordingProbeObservationRecord],
    dict[str, TargetAliasRecord],
]:
    baseline = _read_child(
        root,
        run_path / "observations" / f"{manifest.baseline_observation_id}.json",
        ConfirmedReferenceBaselineRecord,
    )
    operations = {
        operation_id: _read_child(
            root,
            run_path / "classification-operations" / f"{operation_id}.json",
            ClassificationOperationRecord,
        )
        for operation_id in manifest.classification_operation_ids
    }
    observations = {
        observation_id: _read_child(
            root,
            run_path / "observations" / f"{observation_id}.json",
            RecordingProbeObservationRecord,
        )
        for observation_id in manifest.canonical_observation_ids
    }
    aliases = {
        alias_id: _read_child(
            root,
            run_path / "observations" / f"{alias_id}.json",
            TargetAliasRecord,
        )
        for alias_id in manifest.target_alias_ids
    }
    return baseline, operations, observations, aliases


def _validate_directories(root: Path, run_path: Path) -> None:
    for relative in ("classification-operations", "observations"):
        path = run_path / relative
        if (
            not is_safe_contained_path(root, path, require_target=True)
            or path.is_symlink()
            or not path.is_dir()
        ):
            _raise_corrupt()


def _validate_baseline(
    manifest: RecordingSearchManifestV3, baseline: ConfirmedReferenceBaselineRecord
) -> None:
    confirmation = manifest.confirmation
    if (
        baseline.observation_id != manifest.baseline_observation_id
        or baseline.investigation_id != manifest.investigation_id
        or baseline.search_run_id != manifest.search_run_id
        or baseline.channel_id != confirmation.channel_id
        or baseline.reference_frame_resource_id != confirmation.reference_frame_resource_id
        or baseline.reference_requested_time_utc != confirmation.reference_requested_time_utc
        or baseline.source_width != confirmation.source_width
        or baseline.source_height != confirmation.source_height
        or baseline.roi != confirmation.roi
        or baseline.jpeg_sha256 != confirmation.jpeg_sha256
        or baseline.jpeg_size_bytes != confirmation.jpeg_size_bytes
    ):
        raise RecordingSearchManifestCorruptError


def _validate_operations(
    manifest: RecordingSearchManifestV3,
    records: dict[str, ClassificationOperationRecord],
    acquisition: dict[str, AcquisitionOperationRecord],
    requests: dict[str, ProbeFrameRequestRecord],
    frames: dict[str, CanonicalProbeFrameRecord],
) -> None:
    for operation_id, operation in records.items():
        if (
            operation_id not in manifest.classification_operation_ids
            or operation.investigation_id != manifest.investigation_id
            or operation.search_run_id != manifest.search_run_id
            or operation.baseline_observation_id != manifest.baseline_observation_id
            or operation.classification_operation_id != operation_id
            or operation.probe_request_id not in requests
            or operation.canonical_frame_id not in frames
            or operation.classifier_policy_version != manifest.policy.classifier_policy_version
        ):
            raise RecordingSearchManifestCorruptError
        request = requests[operation.probe_request_id]
        frame = frames[operation.canonical_frame_id]
        if (
            request.status.value != "SUCCEEDED"
            or request.canonical_frame_id != operation.canonical_frame_id
            or request.operation_id not in acquisition
            or frame.operation_id not in acquisition
        ):
            raise RecordingSearchManifestCorruptError


def _validate_observations(  # noqa: PLR0913
    manifest: RecordingSearchManifestV3,
    baseline: ConfirmedReferenceBaselineRecord,
    records: dict[str, RecordingProbeObservationRecord],
    operations: dict[str, ClassificationOperationRecord],
    requests: dict[str, ProbeFrameRequestRecord],
    frames: dict[str, CanonicalProbeFrameRecord],
) -> None:
    classifier = manifest.policy.to_classifier_policy()
    for observation_id, observation in records.items():
        operation = operations.get(observation.classification_operation_id)
        if (
            observation_id not in manifest.canonical_observation_ids
            or operation is None
            or observation.investigation_id != manifest.investigation_id
            or observation.search_run_id != manifest.search_run_id
            or observation.channel_id != manifest.confirmation.channel_id
            or observation.baseline_observation_id != baseline.observation_id
            or observation.classifier_policy_version != manifest.policy.classifier_policy_version
            or observation.primary_probe_request_id != operation.probe_request_id
            or observation.canonical_frame_id != operation.canonical_frame_id
        ):
            _raise_corrupt()
        request = requests.get(observation.primary_probe_request_id)
        frame = frames.get(observation.canonical_frame_id)
        if (
            request is None
            or frame is None
            or request.requested_time_utc != observation.primary_requested_time_utc
        ):
            raise RecordingSearchManifestCorruptError
        try:
            result = classifier.decide(observation.classifier_evidence)
        except (ValueError, TypeError):
            raise RecordingSearchManifestCorruptError from None
        _validate_policy_terminal(manifest, observation.classifier_evidence)
        if (
            result.outcome is not observation.state
            or result.reason_code is not observation.reason_code
        ):
            raise RecordingSearchManifestCorruptError


def _validate_policy_terminal(
    manifest: RecordingSearchManifestV3, comparison: RawComparison
) -> None:
    reason = comparison.unusable_reason
    if reason is VisualReason.BACKGROUND_DOMINANT and (
        comparison.baseline_mask_coverage is None
        or comparison.probe_mask_coverage is None
        or (
            comparison.baseline_mask_coverage < manifest.policy.maximum_roi_mask_coverage_ratio
            and comparison.probe_mask_coverage < manifest.policy.maximum_roi_mask_coverage_ratio
        )
    ):
        _raise_corrupt()
    if reason is VisualReason.INSUFFICIENT_COMPARISON_AREA:
        if comparison.effective_comparison_area is None:
            _raise_corrupt()
        if (
            comparison.roi_pixel_count >= manifest.policy.minimum_roi_pixels
            and comparison.effective_comparison_area >= manifest.policy.minimum_comparison_area
        ):
            _raise_corrupt()


def _validate_aliases(
    manifest: RecordingSearchManifestV3,
    aliases: dict[str, TargetAliasRecord],
    observations: dict[str, RecordingProbeObservationRecord],
    requests: dict[str, ProbeFrameRequestRecord],
) -> None:
    for alias_id, alias in aliases.items():
        request = requests.get(alias.probe_request_id)
        if (
            alias_id not in manifest.target_alias_ids
            or alias.investigation_id != manifest.investigation_id
            or alias.search_run_id != manifest.search_run_id
            or alias.channel_id != manifest.confirmation.channel_id
            or alias.canonical_observation_id not in observations
            or request is None
            or request.requested_time_utc != alias.requested_time_utc
            or any(
                request_id == alias.probe_request_id
                for request_id in (
                    observation.primary_probe_request_id for observation in observations.values()
                )
            )
        ):
            raise RecordingSearchManifestCorruptError


def _reject_unindexed_files(run_path: Path, manifest: RecordingSearchManifestV3) -> None:
    expected_operations = {f"{value}.json" for value in manifest.classification_operation_ids}
    expected_observations = {
        f"{manifest.baseline_observation_id}.json",
        *(f"{value}.json" for value in manifest.canonical_observation_ids),
        *(f"{value}.json" for value in manifest.target_alias_ids),
    }
    for directory, expected in (
        (run_path / "classification-operations", expected_operations),
        (run_path / "observations", expected_observations),
    ):
        if {path.name for path in directory.iterdir()} != expected:
            raise RecordingSearchManifestCorruptError


def _read_child(root: Path, path: Path, model: type[_ModelT]) -> _ModelT:
    try:
        if (
            not is_safe_contained_path(root, path, require_target=True)
            or path.is_symlink()
            or not path.is_file()
        ):
            _raise_corrupt()
        raw = path.read_text(encoding="utf-8")
        _ = load_durable_json_object(raw)
        return model.model_validate_json(raw, strict=True)
    except RecordingSearchManifestCorruptError:
        raise
    except (OSError, UnicodeError, DurableJsonError, ValidationError, ValueError):
        raise RecordingSearchManifestCorruptError from None


def _raise_corrupt() -> NoReturn:
    raise RecordingSearchManifestCorruptError
