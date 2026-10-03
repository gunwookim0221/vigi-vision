# ruff: noqa: EM101, EM102, PLR0913, T201, TRY003, TRY301
"""Bootstrap and run the local VIGI Vision development server on Windows."""

from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

SERVER_APP = "vigi_vision.reference_frame_api:create_reference_frame_app_from_environment"
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
SYNC_MARKER_NAME = ".vigi-vision-uv-sync"
RUNTIME_SYNC_ATTEMPTS = 4
ASSISTED_ROI_BUILD_GROUP = "assisted-roi-build"
ASSISTED_ROI_GROUP = "assisted-roi"
ASSISTED_ROI_IMPORTS = (
    "torch",
    "torchvision.transforms.functional",
    "efficient_sam.efficient_sam",
)


class BootstrapError(RuntimeError):
    """A safe, user-facing startup failure."""


@dataclass(frozen=True, slots=True)
class RuntimeSummary:
    """Required server and assisted ROI versions discovered before startup."""

    uvicorn_version: str
    torch_version: str
    torchvision_version: str
    efficient_sam_version: str
    cuda_available: bool


class TorchCuda(Protocol):
    """Narrow type for the only CUDA API used by the bootstrap."""

    def is_available(self) -> bool:
        """Return whether this Torch build can use CUDA."""
        ...


class TorchRuntime(Protocol):
    """Narrow type for the Torch runtime values shown at startup."""

    __version__: str
    cuda: TorchCuda


def repository_root(script_path: str | Path = __file__) -> Path:
    """Resolve the checkout root from this bootstrap script's location."""
    return Path(script_path).resolve().parent


def project_python(root: str | Path) -> Path:
    """Return the Windows interpreter inside this checkout's virtualenv."""
    return Path(root).resolve() / ".venv" / "Scripts" / "python.exe"


def is_project_interpreter(executable: str | Path, root: str | Path) -> bool:
    """Compare interpreter paths without depending on the caller's working directory."""
    actual = os.path.normcase(str(Path(executable).resolve()))
    expected = os.path.normcase(str(project_python(root).resolve()))
    return actual == expected


def reexec_arguments(root: str | Path, arguments: Sequence[str] = ()) -> tuple[str, ...]:
    """Construct argv for the repository-local Python and this checkout's script."""
    checkout = Path(root).resolve()
    return (
        os.fspath(project_python(root)),
        os.fspath(checkout / Path(__file__).name),
        *arguments,
    )


def run_project_interpreter(
    root: str | Path,
    environment: Mapping[str, str],
    arguments: Sequence[str] = (),
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> int:
    """Run this script under the repo-local Python and return its exit code."""
    command = list(reexec_arguments(root, arguments))
    result = runner(command, cwd=Path(root).resolve(), env=dict(environment), check=False)
    return result.returncode


def server_environment(
    root: str | Path, base_environment: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build a child environment with imports rooted at this checkout's src directory."""
    environment = dict(os.environ if base_environment is None else base_environment)
    environment["PYTHONPATH"] = os.fspath(Path(root) / "src")
    return environment


def uvicorn_command(python: str | Path) -> tuple[str, ...]:
    """Return the fixed, loopback-only development server command."""
    return (
        os.fspath(python),
        "-m",
        "uvicorn",
        SERVER_APP,
        "--factory",
        "--host",
        SERVER_HOST,
        "--port",
        str(SERVER_PORT),
    )


def dependency_inputs(root: str | Path) -> tuple[Path, ...]:
    """Find the dependency declarations used by the existing uv workflow."""
    checkout = Path(root)
    inputs = [checkout / "pyproject.toml"]
    lockfile = checkout / "uv.lock"
    if lockfile.is_file():
        inputs.append(lockfile)
    inputs.extend(sorted(checkout.glob("requirements*.txt")))
    return tuple(path for path in inputs if path.is_file())


def dependency_fingerprint(root: str | Path) -> str:
    """Hash declared dependency inputs to avoid syncing an unchanged environment."""
    digest = hashlib.sha256()
    digest.update(b"uv-sync-locked-assisted-roi-v2\0")
    for path in dependency_inputs(root):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def sync_marker(root: str | Path) -> Path:
    """Return the bootstrap's dependency-state marker inside the ignored venv."""
    return Path(root) / ".venv" / SYNC_MARKER_NAME


def build_dependency_sync_command(
    uv_executable: str | Path, existing_python: str | Path | None = None
) -> tuple[str, ...]:
    """Install EfficientSAM's pinned build tools before its source package is built."""
    command = [
        os.fspath(uv_executable),
        "sync",
        "--no-install-project",
        "--locked",
        "--inexact",
        "--only-group",
        ASSISTED_ROI_BUILD_GROUP,
    ]
    if existing_python is not None:
        command.extend(("--python", os.fspath(existing_python)))
    return tuple(command)


def dependency_sync_command(
    uv_executable: str | Path, existing_python: str | Path | None = None
) -> tuple[str, ...]:
    """Build the final locked environment with the ROI runtime and its build tools."""
    command = [
        os.fspath(uv_executable),
        "sync",
        "--no-install-project",
        "--locked",
        "--group",
        ASSISTED_ROI_BUILD_GROUP,
        "--group",
        ASSISTED_ROI_GROUP,
    ]
    if existing_python is not None:
        command.extend(("--python", os.fspath(existing_python)))
    return tuple(command)


def uv_subprocess_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Copy the parent environment without an unrelated active virtualenv hint."""
    child_environment = dict(environment)
    _ = child_environment.pop("VIRTUAL_ENV", None)
    return child_environment


def _display_command(command: Sequence[str]) -> str:
    """Format a command as Windows displays it."""
    return subprocess.list2cmdline(list(command))


def run_sync(
    root: str | Path,
    uv_executable: str | Path,
    environment: Mapping[str, str],
    *,
    existing_python: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> None:
    """Run the locked build-tool and runtime sync phases in order."""
    checkout = Path(root)
    build_command = build_dependency_sync_command(uv_executable, existing_python)
    commands = (
        build_command,
        dependency_sync_command(
            uv_executable,
            existing_python if existing_python is not None else project_python(checkout),
        ),
    )
    for index, command in enumerate(commands):
        attempts = 1 if index == 0 else RUNTIME_SYNC_ATTEMPTS
        for attempt in range(attempts):
            print(f"[VIGI] Synchronizing dependencies: {_display_command(command)}", flush=True)
            try:
                result = runner(
                    command,
                    cwd=checkout,
                    env=uv_subprocess_environment(environment),
                    check=False,
                )
            except OSError as error:
                raise BootstrapError(
                    f"Could not run {_display_command(command)}: {error}"
                ) from error
            if result.returncode == 0:
                break
            if attempt + 1 < attempts:
                print(
                    "[VIGI] Locked runtime sync failed; retrying the source build.",
                    flush=True,
                )
                continue
            raise BootstrapError(
                f"Command failed with exit code {result.returncode}: {_display_command(command)}"
            )


def ensure_environment(
    root: str | Path,
    uv_executable: str | Path | None,
    environment: Mapping[str, str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> tuple[Path, bool]:
    """Create a missing .venv through uv sync, or reuse its existing interpreter."""
    checkout = Path(root)
    virtualenv = checkout / ".venv"
    python = project_python(checkout)
    if virtualenv.exists():
        if not python.is_file():
            raise BootstrapError(
                f"Existing .venv is missing {python}; repair it manually (it is never replaced)."
            )
        print(f"[VIGI] Reusing virtual environment: {virtualenv}", flush=True)
        return python, False
    if uv_executable is None:
        raise BootstrapError("uv is required to create .venv; install uv and retry.")
    print("[VIGI] Creating the project virtual environment...", flush=True)
    run_sync(checkout, uv_executable, environment, runner=runner)
    if not python.is_file():
        raise BootstrapError(f"uv sync completed but did not create {python}.")
    write_sync_marker(checkout)
    return python, True


def dependencies_need_sync(root: str | Path) -> bool:
    """Check whether the ignored venv has synchronized with current declarations."""
    marker = sync_marker(root)
    try:
        recorded = marker.read_text(encoding="ascii").strip()
    except OSError:
        return True
    try:
        current = dependency_fingerprint(root)
    except OSError:
        return True
    return recorded != current


def write_sync_marker(root: str | Path) -> None:
    """Record the declaration fingerprint after a successful uv sync."""
    try:
        _ = sync_marker(root).write_text(dependency_fingerprint(root), encoding="ascii")
    except OSError as error:
        print(f"[VIGI] WARNING: Could not save dependency state: {error}", flush=True)


def check_runtime_imports() -> RuntimeSummary:
    """Import the app and required assisted ROI runtime without invoking the app factory."""
    source_path = os.fspath(repository_root() / "src")
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    _ = importlib.import_module("uvicorn")
    _ = importlib.import_module("vigi_vision")
    _ = importlib.import_module("vigi_vision.reference_frame_api")
    _ = importlib.import_module("PIL")
    for module in ASSISTED_ROI_IMPORTS:
        _ = importlib.import_module(module)
    torch = cast("TorchRuntime", cast("object", importlib.import_module("torch")))
    try:
        cuda_available = torch.cuda.is_available()
    except Exception:  # noqa: BLE001 - CUDA availability is informational only.
        cuda_available = False

    return RuntimeSummary(
        uvicorn_version=version("uvicorn"),
        torch_version=version("torch"),
        torchvision_version=version("torchvision"),
        efficient_sam_version=version("efficient-sam"),
        cuda_available=cuda_available,
    )


def ensure_runtime(
    root: str | Path,
    python: str | Path,
    uv_executable: str | Path | None,
    environment: Mapping[str, str],
    *,
    runtime_check: Callable[[], RuntimeSummary] = check_runtime_imports,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> RuntimeSummary:
    """Check app imports and synchronize once if this venv lacks declared dependencies."""
    try:
        return runtime_check()
    except (ImportError, OSError) as initial_error:
        if uv_executable is None:
            message = f"Runtime imports failed: {initial_error}."
            action = (
                "Install uv and rerun start.bat to synchronize the locked assisted ROI runtime."
            )
            raise BootstrapError(f"{message} {action}") from initial_error
        try:
            run_sync(root, uv_executable, environment, existing_python=python, runner=runner)
            write_sync_marker(root)
            return runtime_check()
        except Exception as retry_error:
            message = (
                "Required server or assisted ROI imports still fail after the locked ROI sync."
            )
            raise BootstrapError(f"{message} {retry_error}") from retry_error
    except Exception as error:
        raise BootstrapError(f"Runtime check failed: {error}") from error


def print_runtime_summary(root: str | Path, python: str | Path, info: RuntimeSummary) -> None:
    """Print concise interpreter, server, and required assisted ROI runtime facts."""
    print(f"[VIGI] Repository : {Path(root)}")
    print(f"[VIGI] Python     : {python}")
    print(f"[VIGI] Python ver : {sys.version.split()[0]}")
    print(f"[VIGI] Server     : http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"[VIGI] Torch      : {info.torch_version}")
    print(f"[VIGI] TorchVision: {info.torchvision_version}")
    print(f"[VIGI] CUDA       : {'available' if info.cuda_available else 'unavailable'}")
    print(f"[VIGI] EfficientSAM: available ({info.efficient_sam_version})")


def launch_server(
    root: str | Path,
    python: str | Path,
    environment: Mapping[str, str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> int:
    """Run the fixed development app, treating Ctrl+C as a normal stop."""
    command = uvicorn_command(python)
    print("[VIGI] Starting server...", flush=True)
    try:
        result = runner(command, cwd=Path(root), env=dict(environment), check=False)
    except KeyboardInterrupt:
        print("\n[VIGI] Server stopped.", flush=True)
        return 0
    except OSError as error:
        raise BootstrapError(f"Could not start {_display_command(command)}: {error}") from error
    if result.returncode != 0:
        print(f"[VIGI] ERROR: Uvicorn exited with status {result.returncode}.", flush=True)
    return result.returncode


def main() -> int:
    """Prepare the repo-local environment and launch its development app."""
    root = repository_root()
    environment = server_environment(root)
    uv_executable = shutil.which("uv")
    try:
        python, created = ensure_environment(root, uv_executable, environment)
        if not created and dependencies_need_sync(root):
            if uv_executable is None:
                raise BootstrapError(
                    "Project dependency declarations changed. Install uv and run `uv sync`."
                )
            run_sync(root, uv_executable, environment, existing_python=python)
            write_sync_marker(root)

        if not is_project_interpreter(sys.executable, root):
            print("[VIGI] Switching to the project Python interpreter...", flush=True)
            arguments = reexec_arguments(root, sys.argv[1:])
            try:
                return run_project_interpreter(root, environment, sys.argv[1:])
            except OSError as error:
                raise BootstrapError(
                    f"Could not start {_display_command(arguments)}: {error}"
                ) from error

        print("[VIGI] Checking runtime...", flush=True)
        runtime = ensure_runtime(root, python, uv_executable, environment)
        print_runtime_summary(root, python, runtime)
        return launch_server(root, python, environment)
    except BootstrapError as error:
        print(f"[VIGI] ERROR: {error}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("\n[VIGI] Startup cancelled.", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
