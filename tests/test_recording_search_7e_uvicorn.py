# pyright: reportAny=false, reportExplicitAny=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""Real-Uvicorn proofs for the deterministic Phase 7E HTTP/background path."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

_INVESTIGATION_ID = "object-disappearance-v3-ch1-20260720T033428Z"
_SECRET_SENTINEL = "uvicorn-secret-sentinel"  # noqa: S105


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return cast("int", listener.getsockname()[1])


def _request_json(
    base_url: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(  # noqa: S310 - fixed loopback test origin.
        f"{base_url}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=3) as response:  # noqa: S310
            return response.status, cast("dict[str, Any]", json.load(response))
    except HTTPError as error:
        return error.code, cast("dict[str, Any]", json.load(error))


def _stop_server(
    process: subprocess.Popen[str],
    stdout_stream: TextIO,
    stderr_stream: TextIO,
) -> tuple[str, str]:
    forced = False
    if process.poll() is None:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
    try:
        _ = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        _ = process.wait(timeout=5)
        forced = True
    assert process.poll() is not None
    return_code = process.returncode
    stdout_stream.flush()
    stderr_stream.flush()
    _ = stdout_stream.seek(0)
    _ = stderr_stream.seek(0)
    stdout = stdout_stream.read()
    stderr = stderr_stream.read()
    stdout_stream.close()
    stderr_stream.close()
    if forced:
        pytest.fail("Uvicorn did not stop after the bounded interrupt")
    assert return_code in ({0, 3} if os.name == "nt" else {0})
    assert "Finished server process" in stderr
    return stdout, stderr


@pytest.fixture
def uvicorn_process(
    tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[tuple[str, Path, subprocess.Popen[str], TextIO, TextIO]]:
    scenario = cast("str", request.param)
    port = _free_port()
    root = tmp_path / scenario
    root.mkdir()
    environment = os.environ.copy()
    environment["VIGI_PHASE7E_UVICORN_TEST_ROOT"] = os.fspath(root)
    environment["VIGI_PHASE7E_UVICORN_TEST_SCENARIO"] = scenario
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "test_recording_search_7e_browser:create_uvicorn_phase7e_fixture_app",
        "--factory",
        "--app-dir",
        "tests",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    # The yielded fixture owns these streams across the server lifetime and
    # closes both in the bounded reap helper.
    stdout_stream = tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115
    stderr_stream = tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115
    process = subprocess.Popen(  # noqa: S603
        command,
        cwd=Path(__file__).parents[1],
        env=environment,
        stdout=stdout_stream,
        stderr=stderr_stream,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = _stop_server(process, stdout_stream, stderr_stream)
            pytest.fail(f"Uvicorn exited before readiness: {(stdout + stderr)[-1000:]}")
        try:
            status, _payload = _request_json(
                base_url,
                f"/api/v1/investigation-confirmations/{_INVESTIGATION_ID}",
            )
            if status == 200:
                break
        except (URLError, TimeoutError):
            time.sleep(0.05)
    else:
        _ = _stop_server(process, stdout_stream, stderr_stream)
        pytest.fail("Uvicorn did not become ready")
    yield base_url, root, process, stdout_stream, stderr_stream
    if not stdout_stream.closed:
        _ = _stop_server(process, stdout_stream, stderr_stream)


def _start_and_wait(
    base_url: str,
    *,
    request_id: str,
    search_end: str,
) -> tuple[dict[str, Any], bool]:
    accepted_status, accepted = _request_json(
        base_url,
        "/api/v1/recording-searches",
        body={
            "investigation_id": _INVESTIGATION_ID,
            "search_end": search_end,
            "request_id": request_id,
        },
    )
    assert accepted_status == 202, accepted
    assert accepted["run_id"] == f"search-run-{request_id.replace('-', '')}"
    saw_running = False
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        response_status, projected = _request_json(base_url, cast("str", accepted["status_url"]))
        assert response_status == 200, projected
        saw_running = saw_running or projected["status"] in {"ACCEPTED", "RUNNING"}
        if projected["status"] not in {"ACCEPTED", "RUNNING"}:
            return projected, saw_running
        time.sleep(0.03)
    pytest.fail("Phase 7E fixture did not reach a terminal status")


def _events(output: str) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for line in output.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            candidate = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if candidate.get("event") == "phase7e.media_probe_failure":
            parsed.append(cast("dict[str, Any]", candidate))
    return parsed


def _event_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if '"event":"phase7e.media_probe_failure"' in line]


@pytest.mark.parametrize(
    "uvicorn_process",
    ["duration_too_short", "ffprobe_invalid_json"],
    indirect=True,
)
def test_real_uvicorn_console_exposes_one_safe_media_failure_event(
    uvicorn_process: tuple[str, Path, subprocess.Popen[str], TextIO, TextIO],
) -> None:
    base_url, root, process, stdout_stream, stderr_stream = uvicorn_process
    stage = (
        "ffprobe_invalid_json"
        if "ffprobe_invalid_json" in os.fspath(root)
        else "duration_too_short"
    )
    suffix = "2" if stage == "ffprobe_invalid_json" else "1"
    request_id = f"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa{suffix}"
    projected, saw_running = _start_and_wait(
        base_url,
        request_id=request_id,
        search_end="2026-07-20T12:34:33",
    )
    assert saw_running
    assert projected["status"] == "FAILED"
    assert projected["reason_code"] == "media_probe_failed"
    assert not (root / "temporary-replay.mp4").exists()
    stdout, stderr = _stop_server(process, stdout_stream, stderr_stream)
    output = f"{stdout}\n{stderr}"
    events = _events(output)
    assert len(events) == 1, output
    assert len(_event_lines(output)) == 1
    assert _event_lines(output)[0].lstrip().startswith("WARNING:")
    event = events[0]
    assert event["investigation_id"] == _INVESTIGATION_ID
    assert event["run_id"] == f"search-run-{request_id.replace('-', '')}"
    assert event["diagnostic"]["version"] == 1
    assert event["diagnostic"]["stage"] == stage
    assert _SECRET_SENTINEL not in output
    assert "private.example" not in output


@pytest.mark.parametrize("uvicorn_process", ["schema7_jitter"], indirect=True)
def test_real_uvicorn_production_jitter_reaches_schema7(
    uvicorn_process: tuple[str, Path, subprocess.Popen[str], TextIO, TextIO],
) -> None:
    base_url, root, process, stdout_stream, stderr_stream = uvicorn_process
    request_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    projected, saw_running = _start_and_wait(
        base_url,
        request_id=request_id,
        search_end="2026-07-20T12:35:28",
    )
    assert saw_running
    assert projected["status"] == "FOUND"
    assert projected["schema_version"] == 7
    assert projected["run_id"] == f"search-run-{request_id.replace('-', '')}"
    assert not (root / "temporary-replay.mp4").exists()
    stdout, stderr = _stop_server(process, stdout_stream, stderr_stream)
    assert _events(f"{stdout}\n{stderr}") == []
