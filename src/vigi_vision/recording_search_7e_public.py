"""Public Phase 7E execution, status, and Phase 8 handoff boundaries."""

# The public boundary deliberately mirrors the closed, explicit Phase 7E
# contract (including its many typed media fields) and maps every failure to a
# fixed category.  Keep the implementation readable while exempting only
# complexity/style rules that describe those contract mechanics.
# ruff: noqa: D102, D107, EM101, PLR0913, PLR2004, PLC0415
# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportUnannotatedClassAttribute=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnnecessaryIsInstance=false, reportUnusedCallResult=false

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from secrets import token_hex
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, StrictStr

from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.recording_search_7e_1c import (
    CommonSessionAcquirer,
    CommonSessionPolicy,
    CommonSessionRequest,
    FfmpegLocalDecoder,
    FfprobeMediaProbe,
    Phase7E1CExecutor,
)
from vigi_vision.recording_search_7e_1d import (
    Phase7E1DService,
    Phase7EStatus,
    read_phase7_status,
)
from vigi_vision.recording_search_7e_models import StrictIdentityEnvelope
from vigi_vision.recording_search_7e_phase8 import (
    FfmpegSourceClipGenerator,
    Phase8HandoffRepository,
    Phase8LifecycleError,
)
from vigi_vision.recording_search_7e_repository import (
    Phase7ECorruptError,
    Phase7EInProgressError,
    Phase7ENotFoundError,
    RecordingSearch7ERepository,
)
from vigi_vision.recording_search_b3_media import InMemoryRgbDecoder
from vigi_vision.reference_frame_models import parse_reference_frame_request

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class Phase7EPublicError(RuntimeError):
    """Safe public failure category."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_MAX_SEARCH_SECONDS = 600


class StrictRequestModel(BaseModel):
    """Closed public input base."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Phase7EPublicRequest(StrictRequestModel):
    """Public command/API request with no server-owned fields."""

    investigation_id: StrictStr
    search_end_time_text: StrictStr
    source_timezone: StrictStr


@dataclass(frozen=True, slots=True)
class Phase7EPublicStatus:
    """Credential-free status projection used by CLI and HTTP."""

    phase7: Phase7EStatus
    phase8_status: str | None = None
    phase8_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "investigation_id": self.phase7.investigation_id,
            "run_id": self.phase7.run_id,
            "schema_version": self.phase7.schema_version,
            "status": self.phase7.status,
            "reason_code": self.phase7.reason_code,
            "terminal_result_id": self.phase7.terminal_result_id,
            "phase8_status": self.phase8_status,
            "phase8_reason": self.phase8_reason,
        }


@dataclass(frozen=True, slots=True)
class Phase7EPublicService:
    """Single public composition entry point for Phase 7E execution."""

    repository: RecordingSearch7ERepository
    executor: Phase7E1CExecutor
    confirmation_service: object
    classifier: object | None
    local_decoder: object | None
    policy: StrictIdentityEnvelope
    classifier_policy: StrictIdentityEnvelope
    object_policy: ObjectPresenceDecisionPolicy
    phase8_repository: Phase8HandoffRepository
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    media_probe: object | None = None

    def execute(
        self,
        request: Phase7EPublicRequest,
        *,
        create_phase8_handoff: bool = False,
    ) -> Phase7EPublicStatus:
        """Execute one bounded synchronous search and optionally persist handoff."""
        if self.classifier is None or self.local_decoder is None:
            raise Phase7EPublicError("recording_search_execution_unavailable")
        try:
            confirmed = self.confirmation_service.load_confirmed(request.investigation_id)
        except Exception as error:
            raise Phase7EPublicError("baseline_unavailable") from error
        if request.source_timezone != confirmed.source_timezone:
            raise Phase7EPublicError("invalid_request")
        try:
            end = parse_reference_frame_request(
                channel_id=confirmed.channel_id,
                requested_time_text=request.search_end_time_text,
                source_timezone=request.source_timezone,
                now_utc=self.now_utc(),
            ).requested_time_utc
            duration = int((end - confirmed.anchor_time_utc).total_seconds())
        except Exception as error:
            raise Phase7EPublicError("invalid_request") from error
        if duration <= 0 or duration > 600:
            raise Phase7EPublicError("invalid_request")
        run_id = f"search-run-{token_hex(16)}"
        request_domain = CommonSessionRequest(
            request.investigation_id,
            run_id,
            confirmed.channel_id,
            confirmed.anchor_time_utc,
            end,
            CommonSessionPolicy.from_payload(self.policy.payload),
        )
        from vigi_vision.recording_search_7e_1d import Phase7EC1PlannerAdapter

        bundle = Phase7EC1PlannerAdapter().build(request_domain, self.policy)
        schema5 = StrictIdentityEnvelope.from_payload(
            "schema5-manifest",
            {
                "schema_version": 5,
                "investigation_id": request_domain.investigation_id,
                "run_id": request_domain.run_id,
                "policy_id": self.policy.identity,
                "plan_id": bundle.plan.identity,
                "coarse_target_request_ids": [item.identity for item in bundle.coarse_targets],
            },
        )
        base_records = (self.policy, bundle.plan, *bundle.coarse_targets)
        with self.executor.invocation(request_domain) as invocation:
            admitted = self.executor.execute(
                request_domain,
                schema5,
                base_records,
                self.classifier_policy,
                bundle.coarse_targets,
                invocation=invocation,
            )
            _ = Phase7E1DService(
                self.repository,
                local_evidence=self._local_evidence(),
            ).execute(invocation, admitted.acquisition)
        phase8_status: tuple[str | None, str | None] = (None, None)
        if create_phase8_handoff:
            self.create_phase8_handoff(
                request_domain.investigation_id,
                request_domain.run_id,
            )
            phase8_status = ("READY", None)
        return Phase7EPublicStatus(
            read_phase7_status(self.repository, request_domain.investigation_id, run_id),
            phase8_status[0],
            phase8_status[1],
        )

    def status(self, investigation_id: str, run_id: str) -> Phase7EPublicStatus:
        phase7 = read_phase7_status(self.repository, investigation_id, run_id)
        run: object | None = None
        if phase7.schema_version == 7:
            try:
                run = self.repository.inspect_current_read_only(investigation_id, run_id)
            except (Phase7EInProgressError, Phase7ENotFoundError, Phase7ECorruptError):
                run = None
        phase8, reason = self.phase8_repository.status(run, investigation_id, run_id)
        return Phase7EPublicStatus(phase7, phase8, reason)

    def create_phase8_handoff(self, investigation_id: str, run_id: str) -> StrictIdentityEnvelope:
        """Create/reuse a request from strictly reopened terminal evidence."""
        try:
            with self.repository.invocation_ownership(investigation_id, run_id) as ownership:
                run = self.repository.reopen_current(
                    investigation_id,
                    run_id,
                    ownership=ownership,
                )
                return self._create_handoff(run)
        except Phase8LifecycleError as error:
            raise Phase7EPublicError(error.code) from error
        except Phase7EInProgressError as error:
            raise Phase7EPublicError("already_running") from error
        except Phase7ENotFoundError as error:
            raise Phase7EPublicError("run_not_found") from error
        except Phase7ECorruptError as error:
            raise Phase7EPublicError("phase8_corrupt") from error

    def delete_recording_search_media(self, investigation_id: str, run_id: str) -> str:
        """Delete only this run's retained common-session MP4 after explicit request."""
        try:
            with self.repository.invocation_ownership(investigation_id, run_id) as ownership:
                run = self.repository.reopen_current(
                    investigation_id,
                    run_id,
                    ownership=ownership,
                )
                return self.phase8_repository.delete(run)
        except Phase7EPublicError:
            raise
        except Phase8LifecycleError as error:
            raise Phase7EPublicError(error.code) from error
        except Phase7EInProgressError as error:
            raise Phase7EPublicError("already_running") from error
        except Phase7ENotFoundError as error:
            raise Phase7EPublicError("run_not_found") from error
        except Phase7ECorruptError as error:
            raise Phase7EPublicError("phase8_corrupt") from error

    def _local_evidence(self) -> object:
        from vigi_vision.recording_search_7e_1d import Phase7ELocalEvidenceAdapter

        return Phase7ELocalEvidenceAdapter(
            self.repository,
            self.local_decoder,  # type: ignore[arg-type]
            self.classifier,  # type: ignore[arg-type]
        )

    def _create_handoff(self, run: object) -> StrictIdentityEnvelope:
        return self.phase8_repository.create_or_reuse(
            run,
            approved_phase8_media_policy(),
            timeout_seconds=float(self.policy.payload["source_clip_timeout_seconds"]),
        )


def approved_phase7e_policy() -> tuple[
    StrictIdentityEnvelope, StrictIdentityEnvelope, ObjectPresenceDecisionPolicy
]:
    """Return the approved policy snapshots without reading configuration."""
    policy = StrictIdentityEnvelope.from_payload("policy", _policy_payload())
    classifier = StrictIdentityEnvelope.from_payload("classifier-policy", _classifier_payload())
    object_policy = ObjectPresenceDecisionPolicy(minimum_mask_overlap_for_comparison=0.1)
    return policy, classifier, object_policy


def build_phase7e_service(
    *,
    root: Path,
    confirmation_service: object,
    recording_planner: object,
    replay_extractor: object,
    ffmpeg: Path,
    ffprobe: Path,
    mask_predictor: object | None,
    now_utc: Callable[[], datetime] | None = None,
) -> Phase7EPublicService:
    """Compose the public service from existing capture/B4 boundaries."""
    policy, classifier_policy, object_policy = approved_phase7e_policy()
    repository = RecordingSearch7ERepository(root)
    repository.media_root = root / ".media"
    repository.media_probe = FfprobeMediaProbe(ffprobe)
    acquirer = CommonSessionAcquirer(
        recording_planner,
        replay_extractor,
        FfprobeMediaProbe(ffprobe),
    )
    executor = Phase7E1CExecutor(repository, acquirer)
    return Phase7EPublicService(
        repository,
        executor,
        confirmation_service,
        None
        if mask_predictor is None
        else __import__(
            "vigi_vision.recording_search_7e_b4", fromlist=["Phase7EProductionB4Adapter"]
        ).Phase7EProductionB4Adapter(
            confirmation_service.load_confirmed,
            InMemoryRgbDecoder(ffmpeg),
            mask_predictor,
            object_policy,
        ),
        FfmpegLocalDecoder(ffmpeg, ffprobe),
        policy,
        classifier_policy,
        object_policy,
        Phase8HandoffRepository(
            root / ".phase8",
            root / ".media",
            FfprobeMediaProbe(ffprobe),
            FfmpegSourceClipGenerator(ffmpeg),
        ),
        now_utc or (lambda: datetime.now(timezone.utc)),
        FfprobeMediaProbe(ffprobe),
    )


def approved_phase8_media_policy() -> StrictIdentityEnvelope:
    """Return the approved immutable Phase 8 media-generation policy."""
    return StrictIdentityEnvelope.from_payload(
        "media-generation-policy",
        {
            "container": "mp4",
            "stream_copy": {
                "eligible": True,
                "requires_single_video": True,
                "requires_no_audio": True,
                "requires_same_codec_parameters": True,
                "requires_interval_bounds": True,
                "requires_metadata_allowlist": True,
            },
            "reencode": {
                "codec": "h264",
                "encoder": "libx264",
                "profile": "High",
                "level": "4.1",
                "pixel_format": "yuv420p",
                "preset": "medium",
                "crf": 23,
                "frame_rate_source": "selected_stream_avg_frame_rate",
                "vfr_mode": "passthrough",
                "faststart": True,
            },
            "audio": "drop",
            "chapters": "drop",
            "copied_metadata": "drop",
            "interval_tolerance": "one_source_frame",
            "maximum_frame_rate": [60, 1],
            "maximum_duration_seconds": 41,
            "maximum_size_bytes": 536870912,
            "timeout_seconds": 120,
        },
    )


def _policy_payload() -> dict[str, object]:
    return {
        "schema_family": [5, 6, 7],
        "provenance_level": "REQUEST_RELATIVE_ESTIMATE",
        "default_search_duration_seconds": 300,
        "maximum_search_duration_seconds": 600,
        "coarse_interval_seconds": 300,
        "support_count": 3,
        "support_cadence_seconds": 1,
        "binary_stop_seconds": 1,
        "maximum_consecutive_indeterminate_targets": 3,
        "maximum_mp4_bytes": 4294967296,
        "maximum_process_memory_bytes": 2147483648,
        "maximum_selected_rgb24_frames": 12,
        "maximum_targets_per_decoder_pass": 32,
        "maximum_decoder_passes": 11,
        "maximum_classifications": 32,
        "replay_margin_seconds": 40,
        "ffprobe_timeout_seconds": 20,
        "decoder_timeout_seconds": 120,
        "classifier_timeout_seconds": 10,
        "classifier_total_budget_seconds": 320,
        "terminal_interpretation_seconds": 10,
        "publication_seconds": 10,
        "strict_readback_seconds": 20,
        "source_clip_timeout_seconds": 120,
        "cleanup_reserve_seconds": 60,
        "invocation_deadline_seconds": 2520,
        "phase8_retry_deadline_seconds": 180,
        "source_clip_pre_seconds": 10,
        "source_clip_post_seconds": 30,
        "maximum_found_interval_seconds": 1,
        "maximum_source_clip_seconds": 41,
        "maximum_source_clip_bytes": 536870912,
        "maximum_source_frame_rate": [60, 1],
    }


def _classifier_payload() -> dict[str, object]:
    return {
        "classifier_family": "efficient-sam-ti-roi-ncc",
        "implementation_version": 1,
        "implementation_source_commit": "d525f622e6f640acf5a0fc37c7ca1f243da5bde0",
        "checkpoint_logical_name": "efficient_sam_vitt.pt",
        "checkpoint_sha256": "dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a",
        "runtime": {
            "python": "3.11",
            "torch": "2.10.0+cpu",
            "torchvision": "0.25.0+cpu",
            "pillow": "12.3.0",
            "numpy": "2.4.6",
            "device": "cpu",
            "tensor_dtype": "float32",
            "comparison_dtype": "float64",
        },
        "input": {
            "color_space": "RGB",
            "channel_order": "RGB",
            "normalization": "torchvision.to_tensor uint8/255",
            "resize": "none before upstream model preprocessing",
            "interpolation": "upstream commit-owned",
            "positive_point_shape": [1, 1, 1, 2],
            "point_label_shape": [1, 1, 1],
            "positive_point_label": 1,
            "prompt": "confirmed_roi_center_v1",
        },
        "mask": {
            "logit_threshold": "0.000000",
            "candidate_selection": "highest predicted_iou among valid candidates",
            "must_contain_prompt": True,
            "minimum_width": 4,
            "minimum_height": 4,
            "minimum_pixel_count": 64,
            "maximum_source_coverage": "0.950000",
            "alignment": "source_pixel_grid",
        },
        "comparison": {
            "roi_preprocessing": "phase7b-roi-luma-v1",
            "luma_coefficients": [299, 587, 114],
            "luma_divisor": 1000,
            "luma_rounding": "add_500_then_floor",
            "ncc_area": "mask_intersection",
            "minimum_overlap_fraction": "0.100000",
            "minimum_effective_area_pixels": 64,
            "metric_rounding": "half_even",
            "decimal_places": 6,
        },
        "decision": {
            "present_min_iou": "0.500000",
            "present_min_ncc": "0.600000",
            "absent_max_iou": "0.100000",
            "absent_max_ncc": "0.200000",
            "otherwise": "INDETERMINATE",
        },
        "execution": {
            "timeout_seconds": 10,
            "maximum_attempts": 1,
            "maximum_concurrent_attempts": 1,
            "late_result": "revoked",
            "timeout_result": "OPERATIONAL",
            "unknown_result": "OPERATIONAL_INVALID",
            "retry": "new_run_only",
        },
    }


__all__ = [
    "Phase7EPublicError",
    "Phase7EPublicRequest",
    "Phase7EPublicService",
    "Phase7EPublicStatus",
    "Phase8HandoffRepository",
    "approved_phase7e_policy",
    "approved_phase8_media_policy",
    "build_phase7e_service",
]
