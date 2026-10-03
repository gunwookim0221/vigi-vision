"""Focused tests for the Windows development-server bootstrap."""

from __future__ import annotations

import hashlib
import importlib
import os
import re
import shutil
import subprocess
import sys
from importlib.machinery import PathFinder
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import start_server

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


def _make_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _ = (root / "pyproject.toml").write_text('[project]\nname = "test"\n', encoding="utf-8")
    return root


def _completed(returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def _toml_table(source: str, name: str) -> str:
    header = re.search(rf"(?m)^\[{re.escape(name)}\]\s*$", source)
    assert header is not None
    remainder = source[header.end() :]
    next_header = re.search(r"(?m)^\[", remainder)
    end = next_header.start() if next_header is not None else len(remainder)
    return remainder[:end]


def _toml_array(source: str, name: str) -> str:
    array = re.search(rf"(?ms)^{re.escape(name)}\s*=\s*\[(.*?)^\]", source)
    assert array is not None
    return array.group(1)


def test_assisted_roi_dependency_contract_is_pinned_for_supported_python_range() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "pyproject.toml").read_text(encoding="utf-8")
    project = _toml_table(source, "project")
    dependency_groups = _toml_table(source, "dependency-groups")
    build_dependencies = _toml_array(dependency_groups, "assisted-roi-build")
    assisted_dependencies = _toml_array(dependency_groups, "assisted-roi")
    uv_sources = _toml_table(source, "tool.uv.sources")
    uv = _toml_table(source, "tool.uv")

    assert 'requires-python = ">=3.10,<3.15"' in project
    assert '"setuptools==70.2.0"' in build_dependencies
    assert '"wheel==0.48.0"' in build_dependencies
    assert '"torch==2.10.0+cpu"' in assisted_dependencies
    assert '"torchvision==0.25.0+cpu"' in assisted_dependencies
    assert (
        "efficient-sam @ git+https://github.com/yformer/EfficientSAM.git"
        "@d525f622e6f640acf5a0fc37c7ca1f243da5bde0"
    ) in assisted_dependencies
    assert re.search(r"(?m)^\s*\"torch(?:vision)?(?:[<>=!~ ]|\")", project) is None
    assert 'torch = { index = "pytorch-cpu" }' in uv_sources
    assert 'torchvision = { index = "pytorch-cpu" }' in uv_sources
    assert 'no-build-isolation-package = ["efficient-sam"]' in uv


def test_repository_paths_are_derived_from_the_script_location(tmp_path: Path) -> None:
    script = tmp_path / "checkout" / "start_server.py"

    assert start_server.repository_root(script) == script.parent
    assert start_server.project_python(script.parent) == (
        script.parent / ".venv" / "Scripts" / "python.exe"
    )
    source = Path(start_server.__file__).read_text(encoding="utf-8")
    assert re.search(r"(?m)(?<![A-Za-z0-9])[A-Za-z]:\\", source) is None


def test_existing_venv_is_reused_without_sync_or_recreation(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    python = start_server.project_python(root)
    python.parent.mkdir(parents=True)
    _ = python.write_bytes(b"existing interpreter marker")
    calls: list[tuple[object, ...]] = []

    def unexpected_sync(
        command: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((tuple(command), kwargs))
        return _completed()

    actual_python, created = start_server.ensure_environment(
        root,
        "uv",
        {},
        runner=unexpected_sync,
    )

    assert actual_python == python
    assert not created
    assert python.read_bytes() == b"existing interpreter marker"
    assert calls == []


def test_missing_venv_uses_project_uv_sync_without_network_in_tests(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    calls: list[tuple[tuple[str, ...], Path, dict[str, str], bool]] = []
    parent_environment = {
        "PYTHONPATH": str(root / "src"),
        "VIRTUAL_ENV": str(root.parent / ".venv"),
    }

    def fake_sync(
        command: Sequence[str], *, cwd: Path, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((tuple(command), cwd, dict(env), check))
        if len(calls) == 1:
            python = start_server.project_python(root)
            python.parent.mkdir(parents=True)
            _ = python.write_bytes(b"mock interpreter")
        return _completed()

    python, created = start_server.ensure_environment(
        root,
        "uv",
        parent_environment,
        runner=fake_sync,
    )

    assert created
    assert python.is_file()
    assert calls[0][0] == start_server.build_dependency_sync_command("uv")
    assert calls[1][0] == start_server.dependency_sync_command(
        "uv", start_server.project_python(root)
    )
    assert calls[0][1] == root
    assert "VIRTUAL_ENV" not in calls[0][2]
    assert parent_environment["VIRTUAL_ENV"] == str(root.parent / ".venv")
    assert calls[0][3] is False
    assert calls[1][1] == root
    assert "VIRTUAL_ENV" not in calls[1][2]
    assert calls[1][3] is False
    assert not start_server.dependencies_need_sync(root)


def test_existing_venv_sync_pins_its_interpreter(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    python = start_server.project_python(root)

    assert start_server.dependency_sync_command("uv", python) == (
        "uv",
        "sync",
        "--no-install-project",
        "--locked",
        "--group",
        "assisted-roi-build",
        "--group",
        "assisted-roi",
        "--python",
        str(python),
    )


def test_build_sync_installs_only_locked_efficient_sam_build_tools() -> None:
    assert start_server.build_dependency_sync_command("uv") == (
        "uv",
        "sync",
        "--no-install-project",
        "--locked",
        "--inexact",
        "--only-group",
        "assisted-roi-build",
    )


def test_sync_command_keeps_uv_and_does_not_add_a_pip_fallback(tmp_path: Path) -> None:
    command = start_server.dependency_sync_command("uv", start_server.project_python(tmp_path))

    assert command[0] == "uv"
    assert "pip" not in command


def test_sync_marker_tracks_pyproject_and_lockfile(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _ = (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    venv = root / ".venv"
    venv.mkdir()
    start_server.write_sync_marker(root)

    assert not start_server.dependencies_need_sync(root)
    with (root / "pyproject.toml").open("a", encoding="utf-8") as project:
        _ = project.write("\n[tool.bootstrap-test]\nchanged = true\n")
    assert start_server.dependencies_need_sync(root)
    start_server.write_sync_marker(root)
    _ = (root / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    assert start_server.dependencies_need_sync(root)


def test_new_sync_policy_invalidates_a_preexisting_marker(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    lockfile = root / "uv.lock"
    _ = lockfile.write_text("version = 1\n", encoding="utf-8")
    marker = start_server.sync_marker(root)
    marker.parent.mkdir(parents=True)

    legacy_digest = hashlib.sha256()
    for path in start_server.dependency_inputs(root):
        legacy_digest.update(path.name.encode("utf-8"))
        legacy_digest.update(b"\0")
        legacy_digest.update(path.read_bytes())
        legacy_digest.update(b"\0")
    _ = marker.write_text(legacy_digest.hexdigest(), encoding="ascii")

    assert start_server.dependencies_need_sync(root)


def test_project_interpreter_comparison_and_self_reexec_arguments(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    python = start_server.project_python(root)
    arguments = start_server.reexec_arguments(root, ("--safe-argument",))

    assert start_server.is_project_interpreter(str(python), root)
    assert not start_server.is_project_interpreter(sys_executable(), root)
    assert arguments[0] == str(python)
    assert arguments[1] == str(root / "start_server.py")
    assert arguments[2:] == ("--safe-argument",)


def test_reexec_paths_with_spaces_are_not_duplicated() -> None:
    root = Path("D:/Python test/vigi-project/vigi-vision-clean").resolve()
    expected = (
        str(root / ".venv" / "Scripts" / "python.exe"),
        str(root / "start_server.py"),
    )

    assert start_server.reexec_arguments(root) == expected


def test_reexec_argv_passes_interpreter_then_script_as_a_list(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "checkout with spaces")
    expected = start_server.reexec_arguments(root, ("--child-option",))
    calls: list[tuple[list[str], Path, dict[str, str], bool]] = []

    def fake_runner(
        command: list[str], *, cwd: Path, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, cwd, dict(env), check))
        return _completed(23)

    result = start_server.run_project_interpreter(
        root,
        {"PYTHONPATH": str(root / "src")},
        ("--child-option",),
        runner=fake_runner,
    )

    assert result == 23
    assert calls == [(list(expected), root, {"PYTHONPATH": str(root / "src")}, False)]
    assert calls[0][0][0] == str(start_server.project_python(root))
    assert calls[0][0][1] == str(root / "start_server.py")
    assert calls[0][0][0] != calls[0][0][1]
    assert calls[0][0][2:] == ["--child-option"]


def test_server_environment_sets_repository_src_on_pythonpath(tmp_path: Path) -> None:
    root = _make_project(tmp_path)

    assert start_server.server_environment(root, {"OTHER": "kept", "PYTHONPATH": "old"}) == {
        "OTHER": "kept",
        "PYTHONPATH": str(root / "src"),
    }


def test_project_package_resolves_from_server_pythonpath_without_editable_install() -> None:
    root = Path(__file__).resolve().parents[1]
    environment = start_server.server_environment(root, {})

    spec = PathFinder.find_spec("vigi_vision", [environment["PYTHONPATH"]])

    assert spec is not None
    assert Path(spec.origin or "") == root / "src" / "vigi_vision" / "__init__.py"


def test_fixed_uvicorn_factory_command(tmp_path: Path) -> None:
    python = start_server.project_python(tmp_path)

    assert start_server.uvicorn_command(python) == (
        str(python),
        "-m",
        "uvicorn",
        "vigi_vision.reference_frame_api:create_reference_frame_app_from_environment",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    )


def test_dependency_sync_failure_prevents_runtime_or_server_start(
    tmp_path: Path,
) -> None:
    root = _make_project(tmp_path)
    calls: list[tuple[object, ...]] = []

    def failed_sync(
        command: Sequence[str], *, cwd: Path, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((tuple(command), cwd, env, check))
        return _completed(7)

    with pytest.raises(start_server.BootstrapError, match="uv sync"):
        _ = start_server.ensure_environment(root, "uv", {}, runner=failed_sync)

    assert calls[0][0] == start_server.build_dependency_sync_command("uv")
    assert not start_server.sync_marker(root).exists()
    assert start_server.dependencies_need_sync(root)


def test_runtime_sync_retries_transient_source_build_failure(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    python = start_server.project_python(root)
    calls: list[tuple[str, ...]] = []
    runtime_command = start_server.dependency_sync_command("uv", python)

    def flaky_sync(
        command: Sequence[str], *, cwd: Path, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(tuple(command))
        assert cwd == root
        assert env == {}
        assert not check
        if tuple(command) == start_server.build_dependency_sync_command("uv", python):
            return _completed()
        return _completed(7 if len(calls) < 5 else 0)

    start_server.run_sync(root, "uv", {}, existing_python=python, runner=flaky_sync)

    assert calls == [
        start_server.build_dependency_sync_command("uv", python),
        runtime_command,
        runtime_command,
        runtime_command,
        runtime_command,
    ]


def test_parent_virtualenv_cannot_redirect_sync_or_reexec(
    tmp_path: Path,
) -> None:
    root = _make_project(tmp_path)
    local_python = start_server.project_python(root)
    local_python.parent.mkdir(parents=True)
    _ = local_python.write_bytes(b"repo-local interpreter")
    parent_venv = tmp_path / "parent" / ".venv"
    parent_python = parent_venv / "Scripts" / "python.exe"
    parent_environment = {"VIRTUAL_ENV": str(parent_venv)}
    sync_commands: list[tuple[str, ...]] = []
    uv_environments: list[dict[str, str]] = []

    def unexpected_sync(command: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        sync_commands.append(tuple(command))
        return _completed()

    selected_python, created = start_server.ensure_environment(
        root,
        "uv",
        parent_environment,
        runner=unexpected_sync,
    )

    def fake_runner(
        command: Sequence[str], *, cwd: Path, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        _ = (cwd, check)
        sync_commands.append(tuple(command))
        uv_environments.append(dict(env))
        return _completed()

    start_server.run_sync(
        root,
        "uv",
        parent_environment,
        existing_python=selected_python,
        runner=fake_runner,
    )

    assert selected_python == local_python
    assert not created
    assert sync_commands == [
        start_server.build_dependency_sync_command("uv", local_python),
        start_server.dependency_sync_command("uv", local_python),
    ]
    assert "VIRTUAL_ENV" not in uv_environments[0]
    assert parent_environment["VIRTUAL_ENV"] == str(parent_venv)
    assert not start_server.is_project_interpreter(parent_python, root)
    assert start_server.is_project_interpreter(selected_python, root)
    assert start_server.reexec_arguments(root)[0] == str(local_python)


def test_runtime_import_failure_retries_sync_then_reports_failure(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    python = start_server.project_python(root)
    python.parent.mkdir(parents=True)
    _ = python.write_bytes(b"mock interpreter")
    runtime_calls = 0
    sync_commands: list[tuple[str, ...]] = []

    def runtime_check() -> start_server.RuntimeSummary:
        nonlocal runtime_calls
        runtime_calls += 1
        error_message = "missing"
        raise ModuleNotFoundError(error_message)

    def failed_sync(
        command: Sequence[str], *, cwd: Path, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        sync_commands.append(tuple(command))
        assert cwd == root
        assert env == {}
        assert not check
        return _completed(0 if len(sync_commands) == 1 else 9)

    with pytest.raises(start_server.BootstrapError, match="exit code 9"):
        _ = start_server.ensure_runtime(
            root,
            python,
            "uv",
            {},
            runtime_check=runtime_check,
            runner=failed_sync,
        )

    assert runtime_calls == 1
    assert sync_commands == [
        start_server.build_dependency_sync_command("uv", python),
        start_server.dependency_sync_command("uv", python),
        start_server.dependency_sync_command("uv", python),
        start_server.dependency_sync_command("uv", python),
        start_server.dependency_sync_command("uv", python),
    ]


def test_ctrl_c_is_a_clean_server_stop(tmp_path: Path) -> None:
    calls: list[tuple[object, ...]] = []

    def interrupt_server(
        command: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((tuple(command), kwargs.get("cwd")))
        raise KeyboardInterrupt

    result = start_server.launch_server(
        tmp_path,
        start_server.project_python(tmp_path),
        {},
        runner=interrupt_server,
    )

    assert result == 0
    assert calls[0][0] == start_server.uvicorn_command(start_server.project_python(tmp_path))


def test_main_does_not_launch_server_after_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_project(tmp_path)

    def failed_sync(
        sync_root: str | Path,
        uv_executable: str | Path,
        environment: Mapping[str, str],
        *,
        existing_python: str | Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        _ = (sync_root, uv_executable, environment, existing_python, runner)
        message = "sync failed"
        raise start_server.BootstrapError(message)

    def fake_root() -> Path:
        return root

    def fake_which(command: str) -> str:
        assert command == "uv"
        return "uv"

    def unexpected_launch(
        launch_root: str | Path,
        python: str | Path,
        environment: Mapping[str, str],
    ) -> int:
        _ = (launch_root, python, environment)
        message = "server launched"
        raise AssertionError(message)

    monkeypatch.setattr(start_server, "repository_root", fake_root)
    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(start_server, "run_sync", failed_sync)
    monkeypatch.setattr(start_server, "launch_server", unexpected_launch)

    assert start_server.main() == 1


def test_runtime_imports_require_assisted_roi_but_treat_cuda_as_optional(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = Path(__file__).resolve().parents[1]
    source_path = str(root / "src")
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != source_path])
    torch_module = ModuleType("torch")

    def unavailable_cuda() -> bool:
        raise RuntimeError

    torch_module.__dict__["cuda"] = SimpleNamespace(is_available=unavailable_cuda)
    modules = {
        name: torch_module if name == "torch" else ModuleType(name)
        for name in (
            "uvicorn",
            "vigi_vision",
            "vigi_vision.reference_frame_api",
            "PIL",
            *start_server.ASSISTED_ROI_IMPORTS,
        )
    }

    def fake_import(name: str, package: str | None = None) -> ModuleType:
        _ = package
        if name == "vigi_vision":
            assert source_path in sys.path
        return modules[name]

    monkeypatch.setattr(importlib, "import_module", fake_import)

    def fake_version(distribution: str) -> str:
        return {
            "uvicorn": "0.51.0",
            "torch": "2.10.0+cpu",
            "torchvision": "0.25.0+cpu",
            "efficient-sam": "1.0",
        }[distribution]

    monkeypatch.setattr(start_server, "version", fake_version)

    summary = start_server.check_runtime_imports()

    assert summary.uvicorn_version == "0.51.0"
    assert summary.torch_version == "2.10.0+cpu"
    assert summary.torchvision_version == "0.25.0+cpu"
    assert summary.efficient_sam_version == "1.0"
    assert not summary.cuda_available
    start_server.print_runtime_summary(
        root,
        start_server.project_python(root),
        summary,
    )
    output = capsys.readouterr().out
    assert "Torch      : 2.10.0+cpu" in output
    assert "CUDA       : unavailable" in output
    assert "EfficientSAM: available (1.0)" in output


def test_missing_required_efficient_sam_import_is_not_reported_as_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = {
        name: ModuleType(name)
        for name in (
            "uvicorn",
            "vigi_vision",
            "vigi_vision.reference_frame_api",
            "PIL",
            "torch",
            "torchvision.transforms.functional",
        )
    }

    def missing_efficient_sam(name: str, package: str | None = None) -> ModuleType:
        _ = package
        if name == "efficient_sam.efficient_sam":
            raise ModuleNotFoundError(name="efficient_sam")
        return modules[name]

    monkeypatch.setattr(importlib, "import_module", missing_efficient_sam)

    with pytest.raises(ModuleNotFoundError) as raised:
        _ = start_server.check_runtime_imports()
    assert raised.value.name == "efficient_sam"


def sys_executable() -> str:
    """Return a distinct interpreter path for self-reexec tests."""
    return os.fspath(Path(__file__).resolve().parents[1] / "global-python.exe")
