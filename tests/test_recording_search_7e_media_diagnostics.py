"""Regression coverage for bounded Phase 7E media-probe observability."""
# pyright: reportAny=false, reportArgumentType=false, reportCallIssue=false, reportImplicitOverride=false, reportOptionalMemberAccess=false, reportPrivateUsage=false, reportUnannotatedClassAttribute=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnusedCallResult=false, reportUnusedParameter=false

from __future__ import annotations

import json
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vigi_vision.recording import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.recording_search_7e_1c import (
    CommonSessionAcquirer,
    CommonSessionMediaError,
    CommonSessionMediaProbeTimeoutError,
    CommonSessionRequest,
    FfprobeMediaProbe,
    MediaProbeFacts,
)
from vigi_vision.recording_search_7e_media_diagnostics import (
    MEDIA_PROBE_DIAGNOSTIC_VERSION,
    MEDIA_PROBE_STAGES,
    Phase7EMediaProbeDiagnostic,
)
from vigi_vision.recording_search_7e_public import _execution_public_error
from vigi_vision.replay import ReplayClip


def _probe_document(**changes: object) -> dict[str, object]:
    stream: dict[str, object] = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "h264",
        "profile": "High",
        "pix_fmt": "yuv420p",
        "width": 8,
        "height": 8,
        "time_base": "1/1",
        "avg_frame_rate": "1/1",
        "duration_ts": "4",
        "start_pts": "0",
        "level": 41,
    }
    stream.update(changes)
    return {"streams": [stream], "format": {}}


def _probe(
    result: subprocess.CompletedProcess[str],
) -> FfprobeMediaProbe:
    return FfprobeMediaProbe(
        Path("ffprobe"),
        runner=lambda _arguments, _timeout: result,
    )


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(("ffprobe",), returncode, stdout, "secret stderr")


@pytest.mark.parametrize(
    ("result", "stage"),
    [
        (_completed("{}", returncode=1), "ffprobe_nonzero_exit"),
        (_completed("not-json"), "ffprobe_invalid_json"),
        (_completed("[]"), "ffprobe_invalid_shape"),
        (_completed(json.dumps({"streams": "bad", "format": {}})), "ffprobe_invalid_shape"),
        (
            _completed(json.dumps({"streams": [{"codec_type": "audio"}], "format": {}})),
            "video_stream_missing",
        ),
        (
            _completed(
                json.dumps(
                    {
                        "streams": [
                            {"codec_type": "video"},
                            {"codec_type": "video"},
                        ],
                        "format": {},
                    }
                )
            ),
            "unexpected_video_stream_count",
        ),
        (_completed(json.dumps(_probe_document(codec_name="vp9"))), "unsupported_video_codec"),
        (_completed(json.dumps(_probe_document(width=0))), "invalid_dimensions"),
        (_completed(json.dumps(_probe_document(time_base="bad"))), "invalid_time_base"),
        (_completed(json.dumps(_probe_document(duration_ts=None))), "missing_duration"),
        (_completed(json.dumps(_probe_document(duration_ts="nope"))), "invalid_duration"),
    ],
)
def test_ffprobe_reports_closed_stage_without_native_details(
    result: subprocess.CompletedProcess[str],
    stage: str,
) -> None:
    with pytest.raises(CommonSessionMediaError) as raised:
        _probe(result).probe(Path("C:/private/secret.mp4"), 1.0)

    diagnostic = raised.value.probe_diagnostic
    assert diagnostic is not None
    assert diagnostic.stage == stage
    assert diagnostic.as_dict()["version"] == MEDIA_PROBE_DIAGNOSTIC_VERSION
    rendered = json.dumps(diagnostic.as_dict(), sort_keys=True)
    assert "private" not in rendered
    assert "secret" not in rendered
    assert "stderr" not in rendered


def test_ffprobe_timeout_has_distinct_stage() -> None:
    def timeout(_arguments: tuple[str, ...], _timeout: float) -> subprocess.CompletedProcess[str]:
        executable = "ffprobe"
        raise subprocess.TimeoutExpired(executable, 1.0, stderr="token=secret")

    with pytest.raises(CommonSessionMediaProbeTimeoutError) as raised:
        FfprobeMediaProbe(Path("ffprobe"), runner=timeout).probe(Path("x.mp4"), 1.0)
    assert raised.value.code == "media_probe_timeout"
    assert raised.value.probe_diagnostic is not None
    assert raised.value.probe_diagnostic.stage == "ffprobe_timeout"


class _Planner:
    def __init__(self, root: Path) -> None:
        start = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
        self.segment = RecordingSegment(
            1,
            start.date(),
            int(start.timestamp()),
            int((start + timedelta(seconds=30)).timestamp()),
            start,
            start + timedelta(seconds=30),
        )
        self.root = root

    def find_segments_for_window(self, _window: object) -> tuple[RecordingSegment, ...]:
        return (self.segment,)

    def plan_for_segment(self, _segment: RecordingSegment, window: object) -> ReplayRequest:
        assert isinstance(window, RecordingWindow)
        return ReplayRequest(window, "rtsp://safe.invalid/replay")


class _Extractor:
    def __init__(self, path: object) -> None:
        self.path = path

    def extract(self, request: ReplayRequest) -> ReplayClip:
        if isinstance(self.path, Path):
            self.path.write_bytes(b"temporary")
        return ReplayClip(
            request.window.channel_id,
            request.window.start_utc,
            request.window.end_utc,
            request.replay_url,
            self.path,  # type: ignore[arg-type]
            request.window.duration_seconds,
        )


class _ProbeFacts:
    def __init__(self, *, duration_ticks: int = 4) -> None:
        self.duration_ticks = duration_ticks

    def probe(self, _path: Path, _timeout: float) -> MediaProbeFacts:
        return MediaProbeFacts(
            0,
            1,
            0,
            0,
            1,
            1,
            self.duration_ticks,
            "h264",
            "High",
            "yuv420p",
            8,
            8,
            1,
            1,
            41,
        )


def _request() -> CommonSessionRequest:
    return CommonSessionRequest.from_start_and_duration(
        "inv-safe", "run-safe", 1, datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc), 4
    )


def _acquirer(
    tmp_path: Path,
    path: object | None = None,
    *,
    duration_ticks: int = 4,
    sink: object | None = None,
) -> CommonSessionAcquirer:
    output = tmp_path / "replay.mp4" if path is None else path
    kwargs = {} if sink is None else {"diagnostic_sink": sink}
    return CommonSessionAcquirer(
        _Planner(tmp_path),
        _Extractor(output),
        _ProbeFacts(duration_ticks=duration_ticks),
        **kwargs,
    )  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "path_factory",
    [
        lambda tmp: tmp / "missing.mp4",
        lambda tmp: tmp / "empty.mp4",
    ],
)
def test_retained_file_failure_is_diagnosed_and_cleaned(
    tmp_path: Path, path_factory: object
) -> None:
    path = path_factory(tmp_path)  # type: ignore[operator]

    class FileStateExtractor(_Extractor):
        def extract(self, request: ReplayRequest) -> ReplayClip:
            if path.name == "empty.mp4":
                path.touch()
            return ReplayClip(
                1,
                request.window.start_utc,
                request.window.end_utc,
                request.replay_url,
                path,  # type: ignore[arg-type]
                4,
            )

    acquirer = CommonSessionAcquirer(_Planner(tmp_path), FileStateExtractor(path), _ProbeFacts())
    with pytest.raises(CommonSessionMediaError) as raised:
        acquirer.acquire(_request())
    assert raised.value.probe_diagnostic is not None
    assert raised.value.probe_diagnostic.stage in {"extracted_file_missing", "extracted_file_empty"}
    assert raised.value.probe_diagnostic.cleanup_outcome == "succeeded"
    assert not path.exists()


def test_duration_diagnostics_and_capture_failure_keep_primary(tmp_path: Path) -> None:
    captured: list[Phase7EMediaProbeDiagnostic] = []

    def sink(_investigation: str, _run: str, diagnostic: Phase7EMediaProbeDiagnostic) -> None:
        captured.append(diagnostic)
        failure = "path=C:/secret token=password"
        raise RuntimeError(failure)

    with pytest.raises(CommonSessionMediaError) as raised:
        _acquirer(tmp_path, duration_ticks=1, sink=sink).acquire(_request())
    assert raised.value.code == "media_probe_failed"
    assert raised.value.probe_diagnostic is not None
    assert raised.value.probe_diagnostic.stage == "duration_too_short"
    assert captured[0].cleanup_outcome == "succeeded"
    assert not (tmp_path / "replay.mp4").exists()


def test_duration_tolerance_boundary_is_preserved(tmp_path: Path) -> None:
    acquisition = _acquirer(tmp_path, duration_ticks=5).acquire(_request())
    acquisition.remove()
    assert not (tmp_path / "replay.mp4").exists()

    with pytest.raises(CommonSessionMediaError) as raised:
        _acquirer(tmp_path, duration_ticks=6).acquire(_request())
    assert raised.value.probe_diagnostic is not None
    assert raised.value.probe_diagnostic.stage == "duration_too_long"
    assert not (tmp_path / "replay.mp4").exists()


def test_nonregular_outside_and_unstable_files_have_closed_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "directory.mp4"
    directory.mkdir()

    class NoWriteExtractor(_Extractor):
        def extract(self, request: ReplayRequest) -> ReplayClip:
            return ReplayClip(
                1,
                request.window.start_utc,
                request.window.end_utc,
                request.replay_url,
                self.path,  # type: ignore[arg-type]
                4,
            )

    monkeypatch.setattr(ReplayClip, "remove", lambda _self: None)
    with pytest.raises(CommonSessionMediaError) as nonregular:
        CommonSessionAcquirer(
            _Planner(tmp_path), NoWriteExtractor(directory), _ProbeFacts()
        ).acquire(_request())
    assert nonregular.value.probe_diagnostic is not None
    assert nonregular.value.probe_diagnostic.stage == "extracted_file_not_regular"

    outside_actual = tmp_path.parent / "outside.mp4"
    outside_actual.write_bytes(b"temporary")
    outside_path = tmp_path / ".." / outside_actual.name
    with pytest.raises(CommonSessionMediaError) as outside:
        CommonSessionAcquirer(
            _Planner(tmp_path), NoWriteExtractor(outside_path), _ProbeFacts()
        ).acquire(_request())
    assert outside.value.probe_diagnostic is not None
    assert outside.value.probe_diagnostic.stage == "extracted_file_outside_confinement"

    unstable = tmp_path / "unstable.mp4"
    unstable.write_bytes(b"temporary")
    original_stat = Path.stat
    calls = [0]

    def unstable_stat(path: Path, *args: object, **kwargs: object) -> object:
        if path == unstable:
            calls[0] += 1
            return SimpleNamespace(
                st_size=8,
                st_mtime_ns=calls[0],
                st_nlink=1,
                st_mode=stat.S_IFREG,
            )
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", unstable_stat)
    with pytest.raises(CommonSessionMediaError) as unstable_error:
        CommonSessionAcquirer(
            _Planner(tmp_path), NoWriteExtractor(unstable), _ProbeFacts()
        ).acquire(_request())
    assert unstable_error.value.probe_diagnostic is not None
    assert unstable_error.value.probe_diagnostic.stage == "extracted_file_unstable"


def test_cleanup_failure_is_secondary_and_diagnostic_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquirer = _acquirer(tmp_path, duration_ticks=1)

    def fail_remove(self: ReplayClip) -> None:
        failure = "C:/secret/password"
        raise OSError(failure)

    monkeypatch.setattr(ReplayClip, "remove", fail_remove)
    with pytest.raises(CommonSessionMediaError) as raised:
        acquirer.acquire(_request())
    error = raised.value
    assert error.code == "media_probe_failed"
    assert error.cleanup_failure_code == "cleanup_failed"
    assert error.probe_diagnostic is not None
    assert error.probe_diagnostic.stage == "duration_too_short"
    assert error.probe_diagnostic.secondary_stage == "cleanup_failed"
    assert error.probe_diagnostic.cleanup_outcome == "failed"


def test_diagnostic_vocabulary_is_closed_and_public_projection_unchanged() -> None:
    diagnostic = Phase7EMediaProbeDiagnostic("probe_facts_mismatch")
    assert "cleanup_failed" in MEDIA_PROBE_STAGES
    assert set(diagnostic.as_dict()) == {
        "version",
        "stage",
        "retained_bytes",
        "ffprobe_exit",
        "json_status",
        "video_stream_count",
        "audio_stream_count",
        "codec",
        "width",
        "height",
        "requested_duration_ms",
        "observed_duration_ms",
        "duration_tolerance_ms",
        "cleanup_outcome",
        "secondary_stage",
    }
    with pytest.raises(ValueError, match=r"^$"):
        Phase7EMediaProbeDiagnostic("native secret path")


def test_process_projection_retains_media_facts_without_public_shape_change() -> None:
    error = CommonSessionMediaError(
        probe_diagnostic=Phase7EMediaProbeDiagnostic("ffprobe_invalid_json")
    )
    public = _execution_public_error(error)
    assert public.code == "media_probe_failed"
    assert public.diagnostic is not None
    assert public.diagnostic.media_probe is error.probe_diagnostic
    assert set(public.diagnostic.as_dict()) == {
        "boundary",
        "category",
        "exception_class",
        "cleanup_outcome",
    }
    assert public.diagnostic.as_process_dict()["media_probe"] == error.probe_diagnostic.as_dict()
