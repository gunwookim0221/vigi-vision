"""Focused strict Phase 8 handoff and two-media lifecycle coverage."""

# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportImplicitOverride=false, reportUnannotatedClassAttribute=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnusedCallResult=false, reportUnusedParameter=false
# ruff: noqa: PLR0913

from __future__ import annotations

import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from vigi_vision.recording_search_7e_1c import MediaProbeFacts
from vigi_vision.recording_search_7e_models import StrictIdentityEnvelope
from vigi_vision.recording_search_7e_phase8 import (
    Phase8HandoffRepository,
    Phase8LifecycleError,
)
from vigi_vision.recording_search_7e_public import approved_phase8_media_policy

_COMMON_BYTES = b"strictly-admitted-common-session"
_CLIP_BYTES = b"bounded-derived-source-clip"


class _Probe:
    def probe(self, path: Path, timeout_seconds: float) -> MediaProbeFacts:
        assert timeout_seconds > 0
        return MediaProbeFacts(
            0,
            1,
            0,
            0,
            1,
            1,
            40,
            codec="h264",
            profile="High",
            pixel_format="yuv420p",
            width=32,
            height=32,
            average_frame_rate_num=1,
            average_frame_rate_den=1,
            level=41,
        )


class _Generator:
    def __init__(self, callback: Callable[[Path], None] | None = None) -> None:
        self.callback = callback
        self.calls = 0

    def generate(
        self,
        source: Path,
        destination: Path,
        *,
        stream_index: int,
        offset_seconds: int,
        duration_seconds: int,
        timeout_seconds: float,
    ) -> str:
        self.calls += 1
        assert source.read_bytes() == _COMMON_BYTES
        assert (stream_index, offset_seconds, duration_seconds) == (0, 0, 40)
        assert timeout_seconds == 120
        if self.callback is not None:
            self.callback(source)
        destination.write_bytes(_CLIP_BYTES)
        return "REENCODED"


class _ProbeMismatch(_Probe):
    def probe(self, path: Path, timeout_seconds: float) -> MediaProbeFacts:
        facts = super().probe(path, timeout_seconds)
        if path.parent.name != "clips" and path.name != ".candidate.mp4":
            return MediaProbeFacts(
                facts.selected_video_stream_index,
                facts.video_stream_count,
                facts.audio_stream_count,
                facts.container_start_pts,
                facts.time_base_num,
                facts.time_base_den,
                39,
                codec=facts.codec,
                profile=facts.profile,
                pixel_format=facts.pixel_format,
                width=facts.width,
                height=facts.height,
                average_frame_rate_num=facts.average_frame_rate_num,
                average_frame_rate_den=facts.average_frame_rate_den,
                level=facts.level,
            )
        return facts


class _Interrupt:
    def __init__(self, name: str, occurrence: int = 1) -> None:
        self.name = name
        self.occurrence = occurrence
        self.seen = 0

    def __call__(self, name: str) -> None:
        if name == self.name:
            self.seen += 1
            if self.seen == self.occurrence:
                raise KeyboardInterrupt(name)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _authority(tmp_path: Path) -> tuple[object, Path, Path]:
    repository_root = tmp_path / "R"
    media_root = repository_root / ".media"
    session = StrictIdentityEnvelope.from_payload(
        "common-session",
        {
            "investigation_id": "inv-01",
            "run_id": "run-01",
            "replay_operation_id": "rr-replay-operation-v1-" + "1" * 64,
            "policy_id": "rr-policy-v1-" + "2" * 64,
            "segment_id": "sdk-segment-01",
            "replay_start_requested_time_utc": "2026-07-20T03:00:00Z",
            "replay_end_requested_time_utc": "2026-07-20T03:00:40Z",
            "selected_video_stream_index": 0,
            "container_start_pts": 0,
            "time_base_num": 1,
            "time_base_den": 1,
            "duration_ticks": 40,
            "mp4_size_bytes": len(_COMMON_BYTES),
            "mp4_sha256": _sha(_COMMON_BYTES),
            "provenance_level": "REQUEST_RELATIVE_ESTIMATE",
            "physical_time_bias": "UNKNOWN_UNBOUNDED",
        },
    )
    snapshot = StrictIdentityEnvelope.from_payload(
        "evidence-snapshot",
        {
            "schema_version": 1,
            "investigation_id": "inv-01",
            "run_id": "run-01",
            "source_record_set_id": "rr-source-record-set-v1-" + "3" * 64,
            "policy_id": "rr-policy-v1-" + "2" * 64,
            "classifier_policy_id": "rr-classifier-policy-v1-" + "4" * 64,
            "selected_observation_ids": ["rr-observation-v1-" + "5" * 64],
            "selected_support_group_ids": ["rr-support-group-v1-" + "6" * 64],
            "narrowed_bracket_id": "rr-narrowed-bracket-v1-" + "7" * 64,
        },
    )
    terminal = StrictIdentityEnvelope.from_payload(
        "terminal-result",
        {
            "schema_version": 1,
            "investigation_id": "inv-01",
            "run_id": "run-01",
            "source_record_set_id": snapshot.payload["source_record_set_id"],
            "evidence_snapshot_id": snapshot.identity,
            "common_session_id": session.identity,
            "result_kind": "FOUND",
            "reason_code": "SUPPORTED_TRANSITION",
            "interval_start_requested_time_utc": "2026-07-20T03:00:10Z",
            "interval_end_requested_time_utc": "2026-07-20T03:00:11Z",
        },
    )
    run = SimpleNamespace(
        schema_version=7,
        result_kind="FOUND",
        investigation_id="inv-01",
        run_id="run-01",
        records=(session, snapshot, terminal),
    )
    common = media_root / "inv-01" / "run-01" / f"{session.identity}.mp4"
    common.parent.mkdir(parents=True)
    common.write_bytes(_COMMON_BYTES)
    return run, repository_root, common


def _repository(
    repository_root: Path,
    generator: _Generator | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> Phase8HandoffRepository:
    return Phase8HandoffRepository(
        repository_root / ".phase8",
        repository_root / ".media",
        _Probe(),
        generator or _Generator(),
        checkpoint or (lambda _name: None),
    )


def _create(repository: Phase8HandoffRepository, run: object) -> StrictIdentityEnvelope:
    return repository.create_or_reuse(run, approved_phase8_media_policy(), timeout_seconds=120)


def _snapshot_tree(root: Path) -> tuple[tuple[str, bytes, int], ...]:
    if not root.exists():
        return ()
    return tuple(
        (str(path.relative_to(root)), path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def test_ready_package_is_exact_derived_and_idempotent(tmp_path: Path) -> None:
    run, root, common = _authority(tmp_path)
    generator = _Generator()
    repository = _repository(root, generator)

    first = _create(repository, run)
    package_root = root / ".phase8" / "inv-01" / "run-01"
    clip = package_root / "clips" / f"{_sha(_CLIP_BYTES)}.mp4"
    assert generator.calls == 1
    assert common.read_bytes() == _COMMON_BYTES
    assert clip.read_bytes() == _CLIP_BYTES
    assert {path.name for path in package_root.iterdir()} == {
        "manifest.json",
        "source-clip.json",
        "phase8-request.json",
        "manifests",
        "clips",
    }
    assert {path.name for path in (package_root / "clips").iterdir()} == {clip.name}
    before = _snapshot_tree(package_root)
    second = _create(repository, run)
    assert first == second
    assert generator.calls == 1
    assert _snapshot_tree(package_root) == before
    assert repository.status(run, "inv-01", "run-01") == ("READY", None)


def test_missing_status_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "R"
    repository = _repository(root)
    assert repository.status(None, "inv-01", "run-01") == ("NOT_REQUESTED", None)
    assert not (root / ".phase8").exists()


@pytest.mark.parametrize("mutation", ["missing", "size", "digest"])
def test_unadmitted_common_media_never_creates_handoff(tmp_path: Path, mutation: str) -> None:
    run, root, common = _authority(tmp_path)
    if mutation == "missing":
        common.unlink()
    elif mutation == "size":
        common.write_bytes(_COMMON_BYTES + b"x")
    else:
        common.write_bytes(b"x" * len(_COMMON_BYTES))
    repository = _repository(root)
    with pytest.raises(Phase8LifecycleError) as error:
        _create(repository, run)
    expected = "phase8_media_unavailable" if mutation == "missing" else "phase8_media_corrupt"
    assert error.value.code == expected
    assert repository.status(run, "inv-01", "run-01") == ("NOT_REQUESTED", None)


def test_persisted_probe_mismatch_is_rejected(tmp_path: Path) -> None:
    run, root, _common = _authority(tmp_path)
    repository = Phase8HandoffRepository(
        root / ".phase8",
        root / ".media",
        _ProbeMismatch(),
        _Generator(),
    )
    with pytest.raises(Phase8LifecycleError) as error:
        _create(repository, run)
    assert error.value.code == "phase8_media_corrupt"


def test_conflicting_semantic_handoff_preserves_winner(tmp_path: Path) -> None:
    run, root, _common = _authority(tmp_path)
    repository = _repository(root)
    winner = _create(repository, run)
    terminal = next(item for item in run.records if item.family == "terminal-result")
    changed = StrictIdentityEnvelope.from_payload(
        "terminal-result",
        {
            **terminal.payload,
            "interval_start_requested_time_utc": "2026-07-20T03:00:09Z",
        },
    )
    conflicting = SimpleNamespace(
        **{
            **vars(run),
            "records": (
                *(item for item in run.records if item.family != "terminal-result"),
                changed,
            ),
        }
    )
    before = _snapshot_tree(root / ".phase8")
    with pytest.raises(Phase8LifecycleError) as error:
        _create(repository, conflicting)
    assert error.value.code == "phase8_conflict"
    assert _snapshot_tree(root / ".phase8") == before
    assert _create(repository, run) == winner


def test_concurrent_identical_handoff_has_one_durable_winner(tmp_path: Path) -> None:
    run, root, _common = _authority(tmp_path)
    generator = _Generator()
    repository = _repository(root, generator)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: _create(repository, run), range(2)))
    assert results[0] == results[1]
    assert repository.status(run, "inv-01", "run-01") == ("READY", None)
    assert len(list((root / ".phase8" / "inv-01" / "run-01" / "clips").iterdir())) == 1


@pytest.mark.parametrize(
    "checkpoint",
    [
        "after_clip_generation",
        "after_clip_validation",
        "after_staged_readback",
        "after_handoff_publication",
    ],
)
def test_handoff_interruptions_are_atomic_and_recoverable(tmp_path: Path, checkpoint: str) -> None:
    run, root, _common = _authority(tmp_path)
    repository = _repository(root, checkpoint=_Interrupt(checkpoint))
    with pytest.raises(KeyboardInterrupt):
        _create(repository, run)
    status_root = root / ".phase8"
    before = _snapshot_tree(root)
    status = repository.status(run, "inv-01", "run-01")
    assert status in {("NOT_REQUESTED", None), ("READY", None)}
    assert _snapshot_tree(root) == before
    repository.checkpoint = lambda _name: None
    _create(repository, run)
    assert repository.status(run, "inv-01", "run-01") == ("READY", None)
    assert status_root.is_dir()


def test_common_replacement_during_generation_fails_closed(tmp_path: Path) -> None:
    run, root, common = _authority(tmp_path)

    def replace(_source: Path) -> None:
        replacement = common.with_suffix(".replacement")
        replacement.write_bytes(_COMMON_BYTES)
        try:
            replacement.replace(common)
        except PermissionError:
            replacement.unlink()
            common.write_bytes(b"mutated")

    repository = _repository(root, _Generator(replace))
    with pytest.raises(Phase8LifecycleError) as error:
        _create(repository, run)
    assert error.value.code in {"phase8_media_corrupt", "phase8_clip_failed"}
    assert repository.status(run, "inv-01", "run-01") == ("NOT_REQUESTED", None)


def test_generated_clip_replacement_before_ready_fails_closed(tmp_path: Path) -> None:
    run, root, _common = _authority(tmp_path)

    def replace(name: str) -> None:
        if name != "after_clip_validation":
            return
        clip = next((root / ".phase8-staging").rglob(f"{_sha(_CLIP_BYTES)}.mp4"))
        clip.write_bytes(b"replacement")

    repository = _repository(root, checkpoint=replace)
    with pytest.raises(Phase8LifecycleError) as error:
        _create(repository, run)
    assert error.value.code == "phase8_media_corrupt"
    assert repository.status(run, "inv-01", "run-01") == ("NOT_REQUESTED", None)


def test_foreign_ready_entry_is_rejected_without_mutation(tmp_path: Path) -> None:
    run, root, _common = _authority(tmp_path)
    repository = _repository(root)
    _create(repository, run)
    foreign = root / ".phase8" / "inv-01" / "run-01" / "foreign.txt"
    foreign.write_text("foreign", encoding="utf-8")
    before = _snapshot_tree(root)
    assert repository.status(run, "inv-01", "run-01") == (
        "CORRUPT",
        "phase8_corrupt",
    )
    assert _snapshot_tree(root) == before


@pytest.mark.parametrize(
    ("checkpoint", "occurrence"),
    [
        ("after_transition_archive", 1),
        ("after_deleting_publication", 1),
        ("after_common_tombstone", 1),
        ("after_clip_tombstone", 1),
        ("after_common_unlink", 1),
        ("after_clip_unlink", 1),
        ("after_transition_archive", 2),
        ("after_deleted_publication", 1),
    ],
)
def test_deletion_interruptions_resume_to_exact_deleted(
    tmp_path: Path, checkpoint: str, occurrence: int
) -> None:
    run, root, common = _authority(tmp_path)
    repository = _repository(root)
    _create(repository, run)
    repository.checkpoint = _Interrupt(checkpoint, occurrence)
    with pytest.raises(KeyboardInterrupt):
        repository.delete(run)
    before = _snapshot_tree(root)
    state, _reason = repository.status(run, "inv-01", "run-01")
    assert state in {"READY", "DELETING", "DELETED"}
    assert _snapshot_tree(root) == before
    repository.checkpoint = lambda _name: None
    assert repository.delete(run) == "DELETED"
    assert repository.delete(run) == "DELETED"
    package_root = root / ".phase8" / "inv-01" / "run-01"
    assert not common.exists()
    assert not any((package_root / "clips").iterdir())
    assert not list(root.rglob(".delete-*.mp4"))
    assert repository.status(run, "inv-01", "run-01") == ("DELETED", None)


@pytest.mark.parametrize("target", ["common", "clip"])
def test_deletion_rejects_recorded_live_path_replacement(tmp_path: Path, target: str) -> None:
    run, root, common = _authority(tmp_path)
    repository = _repository(root)
    _create(repository, run)
    repository.checkpoint = _Interrupt("after_deleting_publication")
    with pytest.raises(KeyboardInterrupt):
        repository.delete(run)
    package_root = root / ".phase8" / "inv-01" / "run-01"
    live = common if target == "common" else next((package_root / "clips").glob("*.mp4"))
    replacement = live.with_suffix(".replacement")
    replacement.write_bytes(live.read_bytes())
    replacement.replace(live)
    repository.checkpoint = lambda _name: None
    with pytest.raises(Phase8LifecycleError) as error:
        repository.delete(run)
    assert error.value.code == "phase8_media_corrupt"
    assert live.exists()
    assert live.read_bytes() in {_COMMON_BYTES, _CLIP_BYTES}
    assert repository.status(run, "inv-01", "run-01")[0] != "READY"


def test_replaced_ready_media_is_not_deleted(tmp_path: Path) -> None:
    run, root, common = _authority(tmp_path)
    repository = _repository(root)
    _create(repository, run)
    common.write_bytes(b"foreign replacement")
    with pytest.raises(Phase8LifecycleError) as error:
        repository.delete(run)
    assert error.value.code == "phase8_media_corrupt"
    assert common.read_bytes() == b"foreign replacement"
    assert repository.status(run, "inv-01", "run-01") == (
        "MEDIA_CORRUPT",
        "phase8_media_corrupt",
    )


def test_recovery_after_first_rename_never_deletes_clip_replacement(tmp_path: Path) -> None:
    run, root, common = _authority(tmp_path)
    repository = _repository(root)
    _create(repository, run)
    repository.checkpoint = _Interrupt("after_common_tombstone")
    with pytest.raises(KeyboardInterrupt):
        repository.delete(run)
    package_root = root / ".phase8" / "inv-01" / "run-01"
    clip = next((package_root / "clips").glob("*.mp4"))
    replacement = clip.with_suffix(".replacement")
    replacement.write_bytes(clip.read_bytes())
    replacement.replace(clip)
    repository.checkpoint = lambda _name: None
    with pytest.raises(Phase8LifecycleError) as error:
        repository.delete(run)
    assert error.value.code == "phase8_media_corrupt"
    assert clip.read_bytes() == _CLIP_BYTES
    assert not common.exists()
    assert (common.parent / f".delete-{common.stem}.mp4").read_bytes() == _COMMON_BYTES
    assert repository.status(run, "inv-01", "run-01")[0] == "DELETING"
