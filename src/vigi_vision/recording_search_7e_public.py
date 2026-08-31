"""Public Phase 7E execution, status, and Phase 8 handoff boundaries."""

# The public boundary deliberately mirrors the closed, explicit Phase 7E
# contract (including its many typed media fields) and maps every failure to a
# fixed category.  Keep the implementation readable while exempting only
# complexity/style rules that describe those contract mechanics.
# ruff: noqa: C901, D102, D107, EM101, PERF203, PLR0912, PLR0913, PLR0915, PLR2004, PLC0415, TRY300, TRY301
# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportUnannotatedClassAttribute=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnnecessaryIsInstance=false, reportUnusedCallResult=false

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, StrictStr

from vigi_vision.durable_io import is_safe_contained_path, is_safe_path, load_durable_json_object
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


class Phase7EPublicError(RuntimeError):
    """Safe public failure category."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_PHASE7_SCHEMA7 = 7
_MAX_SEARCH_SECONDS = 600
_PHASE8_REQUEST_FILE = "phase8-request.json"
_PHASE8_SOURCE_CLIP_FILE = "source-clip.json"
_PHASE8_MANIFEST_FILE = "manifest.json"


def _safe_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\0" not in value
    )


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


class Phase8HandoffRepository:
    """Minimal separate request repository; it never generates review media."""

    def __init__(self, root: Path, media_root: Path | None = None) -> None:
        self.root = root
        self.media_root = media_root

    def _directory(self, investigation_id: str, run_id: str, *, create: bool = True) -> Path:
        if not _safe_component(investigation_id) or not _safe_component(run_id):
            raise Phase7EPublicError("invalid_request")
        if not is_safe_path(self.root):
            raise Phase7EPublicError("phase8_corrupt")
        try:
            if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
                raise Phase7EPublicError("phase8_corrupt")
            if create:
                self.root.mkdir(parents=True, exist_ok=True)
            elif not self.root.exists():
                return self.root / investigation_id / run_id
        except OSError as error:
            raise Phase7EPublicError("phase8_corrupt") from error
        directory = self.root / investigation_id / run_id
        if not is_safe_contained_path(self.root, directory):
            raise Phase7EPublicError("phase8_corrupt")
        return directory

    @staticmethod
    def _read_envelope(path: Path) -> StrictIdentityEnvelope:
        try:
            raw = path.read_text(encoding="utf-8")
            document = load_durable_json_object(raw)
            return StrictIdentityEnvelope.model_validate(document, strict=True)
        except Exception as error:
            raise Phase7EPublicError("phase8_corrupt") from error

    @staticmethod
    def _document(envelope: StrictIdentityEnvelope) -> str:
        return json.dumps(
            {
                "family": envelope.family,
                "identity": envelope.identity,
                "payload": envelope.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _write_pair(
        self,
        directory: Path,
        source_clip: StrictIdentityEnvelope,
        request: StrictIdentityEnvelope,
        manifest: StrictIdentityEnvelope,
    ) -> None:
        directory_was_present = directory.exists()
        try:
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                raise Phase7EPublicError("phase8_corrupt")
            directory.mkdir(parents=True, exist_ok=True)
            if not is_safe_contained_path(self.root, directory, require_target=True):
                raise Phase7EPublicError("phase8_corrupt")
            targets = (
                (directory / _PHASE8_SOURCE_CLIP_FILE, source_clip),
                (directory / _PHASE8_REQUEST_FILE, request),
                (directory / _PHASE8_MANIFEST_FILE, manifest),
            )
            if any(path.exists() or path.is_symlink() for path, _ in targets):
                existing = tuple(self._read_envelope(path) for path, _ in targets)
                if all(
                    left == right
                    for (left, right) in zip(
                        existing, (source_clip, request, manifest), strict=True
                    )
                ):
                    return
                raise Phase7EPublicError("phase8_conflict")
            created: list[Path] = []
            try:
                for target, envelope in targets:
                    temporary_path: Path | None = None
                    try:
                        descriptor, name = tempfile.mkstemp(
                            prefix=f".{target.name}.", suffix=".tmp", dir=directory
                        )
                        temporary_path = Path(name)
                        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                            stream.write(self._document(envelope))
                            stream.flush()
                            os.fsync(stream.fileno())
                        temporary_path.rename(target)
                        created.append(target)
                    finally:
                        if temporary_path is not None and temporary_path.exists():
                            temporary_path.unlink(missing_ok=True)
                _ = self._read_envelope(directory / _PHASE8_SOURCE_CLIP_FILE)
                _ = self._read_envelope(directory / _PHASE8_REQUEST_FILE)
                _ = self._read_envelope(directory / _PHASE8_MANIFEST_FILE)
            except Phase7EPublicError:
                for path in reversed(created):
                    try:
                        if path.is_file() and not path.is_symlink():
                            path.unlink()
                    except OSError:
                        pass
                raise
            except (OSError, ValueError, TypeError) as error:
                for path in reversed(created):
                    try:
                        if path.is_file() and not path.is_symlink():
                            path.unlink()
                    except OSError:
                        pass
                raise Phase7EPublicError("phase8_corrupt") from error
        except Phase7EPublicError:
            if not directory_was_present and directory.exists():
                try:
                    if not any(directory.iterdir()):
                        directory.rmdir()
                except OSError:
                    pass
            raise

    def create_or_reuse(
        self,
        run: object,
        source_media: Path,
        *,
        terminal_result_id: str,
        common_session_id: str,
        selected_observation_ids: list[str],
        selected_support_group_ids: list[str],
        stream_index: int,
        width: int,
        height: int,
        duration_ticks: int,
        time_base_num: int,
        time_base_den: int,
        frame_rate_num: int,
        frame_rate_den: int,
        level: int,
        codec: str,
        profile: str,
        pixel_format: str,
        audio_stream_count: int,
        interval_start: str,
        interval_end: str,
    ) -> StrictIdentityEnvelope:
        """Persist one immutable Phase 8 request from approved terminal media."""
        if (
            getattr(run, "schema_version", None) != _PHASE7_SCHEMA7
            or getattr(run, "result_kind", None) != "FOUND"
            or not _safe_component(str(getattr(run, "investigation_id", "")))
            or not _safe_component(str(getattr(run, "run_id", "")))
        ):
            raise Phase7EPublicError("phase8_not_eligible")
        if not source_media.is_file() or source_media.is_symlink():
            raise Phase7EPublicError("phase8_media_unavailable")
        media_root = self.media_root or source_media.parent.parent.parent
        if not is_safe_path(media_root) or not is_safe_contained_path(
            media_root, source_media, require_target=True
        ):
            raise Phase7EPublicError("phase8_media_unavailable")
        raw = source_media.read_bytes()
        if not raw:
            raise Phase7EPublicError("phase8_media_corrupt")
        media_policy = _media_policy()
        clip_integrity = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "observed_duration_ticks": duration_ticks,
            "observed_time_base_num": time_base_num,
            "observed_time_base_den": time_base_den,
            "video_stream_index": stream_index,
            "codec": codec,
            "profile": profile,
            "level": level,
            "pixel_format": pixel_format,
            "width": width,
            "height": height,
            "average_frame_rate_num": frame_rate_num,
            "average_frame_rate_den": frame_rate_den,
            "audio_stream_count": audio_stream_count,
            "generation_outcome": "SOURCE_SESSION",
        }
        investigation_id = str(run.investigation_id)
        run_id = str(run.run_id)
        source_clip = StrictIdentityEnvelope.from_payload(
            "source-clip",
            {
                "schema_version": 1,
                "investigation_id": investigation_id,
                "run_id": run_id,
                "terminal_result_id": terminal_result_id,
                "common_session_id": common_session_id,
                "input_stream_index": stream_index,
                "media_generation_policy_id": media_policy.identity,
                "requested_interval_start_requested_time_utc": interval_start,
                "requested_interval_end_requested_time_utc": interval_end,
                "clipped_interval_start_requested_time_utc": interval_start,
                "clipped_interval_end_requested_time_utc": interval_end,
            },
        )
        request = StrictIdentityEnvelope.from_payload(
            "phase8-request",
            {
                "schema_version": 1,
                "investigation_id": investigation_id,
                "run_id": run_id,
                "terminal_result_id": terminal_result_id,
                "source_clip_id": source_clip.identity,
                "selected_observation_ids": selected_observation_ids,
                "selected_support_group_ids": selected_support_group_ids,
                "clip_integrity": clip_integrity,
            },
        )
        manifest = StrictIdentityEnvelope.from_payload(
            "phase8-manifest",
            {
                "schema_version": 1,
                "state": "READY",
                "investigation_id": investigation_id,
                "run_id": run_id,
                "terminal_result_id": terminal_result_id,
                "common_session_id": common_session_id,
                "previous_phase8_manifest_id": None,
                "source_clip_id": source_clip.identity,
                "clip_integrity": clip_integrity,
                "phase8_request_id": request.identity,
            },
        )
        directory = self._directory(investigation_id, run_id)
        self._write_pair(directory, source_clip, request, manifest)
        return request

    def status(self, investigation_id: str, run_id: str) -> tuple[str | None, str | None]:
        directory = self._directory(investigation_id, run_id, create=False)
        target = directory / _PHASE8_REQUEST_FILE
        source = directory / _PHASE8_SOURCE_CLIP_FILE
        manifest = directory / _PHASE8_MANIFEST_FILE
        if not target.exists() and not source.exists() and not manifest.exists():
            return None, None
        if (
            target.is_symlink()
            or source.is_symlink()
            or manifest.is_symlink()
            or not target.is_file()
            or not source.is_file()
            or not manifest.is_file()
        ):
            return "CORRUPT", "phase8_corrupt"
        try:
            request = self._read_envelope(target)
            source_clip = self._read_envelope(source)
            phase8_manifest = self._read_envelope(manifest)
            if (
                request.family != "phase8-request"
                or source_clip.family != "source-clip"
                or phase8_manifest.family != "phase8-manifest"
                or request.payload.get("investigation_id") != investigation_id
                or request.payload.get("run_id") != run_id
                or source_clip.payload.get("investigation_id") != investigation_id
                or source_clip.payload.get("run_id") != run_id
                or phase8_manifest.payload.get("investigation_id") != investigation_id
                or phase8_manifest.payload.get("run_id") != run_id
                or request.payload.get("source_clip_id") != source_clip.identity
                or phase8_manifest.payload.get("source_clip_id") != source_clip.identity
                or phase8_manifest.payload.get("phase8_request_id") != request.identity
            ):
                return "CORRUPT", "phase8_corrupt"
            return "READY", None
        except Phase7EPublicError:
            return "CORRUPT", "phase8_corrupt"


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
            phase8 = self.create_phase8_handoff(
                request_domain.investigation_id,
                request_domain.run_id,
            )
            phase8_status = ("READY", phase8.identity)
        return Phase7EPublicStatus(
            read_phase7_status(self.repository, request_domain.investigation_id, run_id),
            phase8_status[0],
            phase8_status[1],
        )

    def status(self, investigation_id: str, run_id: str) -> Phase7EPublicStatus:
        phase7 = read_phase7_status(self.repository, investigation_id, run_id)
        phase8, reason = self.phase8_repository.status(investigation_id, run_id)
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
                if not run.is_schema7 or run.result_kind != "FOUND":
                    raise Phase7EPublicError("phase8_not_eligible")
                phase8_status, _ = self.phase8_repository.status(investigation_id, run_id)
                if phase8_status != "READY":
                    raise Phase7EPublicError("phase8_not_eligible")
                media_root = self.repository.media_root
                if media_root is None:
                    raise Phase7EPublicError("phase8_media_unavailable")
                common_session_id = run.manifest.payload.get("common_session_id")
                if not isinstance(common_session_id, str):
                    raise Phase7EPublicError("phase8_media_corrupt")
                path = media_root / investigation_id / run_id / f"{common_session_id}.mp4"
                if (
                    path.is_symlink()
                    or not is_safe_contained_path(media_root, path, require_target=False)
                    or (path.exists() and not path.is_file())
                ):
                    raise Phase7EPublicError("phase8_media_corrupt")
                if path.exists():
                    path.unlink()
                return "DELETED"
        except Phase7EPublicError:
            raise
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
        terminal = next(item for item in run.records if item.family == "terminal-result")
        session = next(item for item in run.records if item.family == "common-session")
        snapshot = next(item for item in run.records if item.family == "evidence-snapshot")
        payload = session.payload
        media_root = self.repository.media_root
        if media_root is None:
            raise Phase7EPublicError("phase8_media_unavailable")
        if self.media_probe is None:
            raise Phase7EPublicError("phase8_media_unavailable")
        try:
            media = self.media_probe.probe(
                media_root
                / run.investigation_id
                / run.run_id
                / f"{payload['common_session_id']}.mp4",
                float(self.policy.payload["source_clip_timeout_seconds"]),
            )
        except Exception as error:
            raise Phase7EPublicError("phase8_media_corrupt") from error
        return self.phase8_repository.create_or_reuse(
            run,
            media_root / run.investigation_id / run.run_id / f"{payload['common_session_id']}.mp4",
            terminal_result_id=terminal.identity,
            common_session_id=str(payload["common_session_id"]),
            selected_observation_ids=list(snapshot.payload["selected_observation_ids"]),
            selected_support_group_ids=list(snapshot.payload["selected_support_group_ids"]),
            stream_index=media.selected_video_stream_index,
            width=media.width,
            height=media.height,
            duration_ticks=media.duration_ticks,
            time_base_num=media.time_base_num,
            time_base_den=media.time_base_den,
            frame_rate_num=media.average_frame_rate_num,
            frame_rate_den=media.average_frame_rate_den,
            codec=media.codec,
            profile=media.profile,
            pixel_format=media.pixel_format,
            audio_stream_count=media.audio_stream_count,
            level=media.level,
            interval_start=str(payload["replay_start_requested_time_utc"]),
            interval_end=str(payload["replay_end_requested_time_utc"]),
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
        Phase8HandoffRepository(root.parent / "phase8-handoffs", media_root=root / ".media"),
        now_utc or (lambda: datetime.now(timezone.utc)),
        FfprobeMediaProbe(ffprobe),
    )


def _media_policy() -> StrictIdentityEnvelope:
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
    "build_phase7e_service",
]
