"""Validate the SDK editable development environment without loading secrets."""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
from dataclasses import dataclass, field
from importlib import import_module
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

if TYPE_CHECKING:
    from collections.abc import Callable

SDK_DISTRIBUTION = "tp-link-vigi-sdk"
SDK_MODULE = "vigi"
SDK_SOURCE_SECTION = "tool.uv.sources"


@dataclass
class PreflightReport:
    """Non-secret facts and failures collected by the preflight."""

    executable: str
    python_version: str
    distribution_name: str = "<unavailable>"
    distribution_version: str = "<unavailable>"
    metadata_location: str = "<unavailable>"
    imported_module_path: str = "<unavailable>"
    editable_source: str = "<unavailable>"
    method_present: bool = False
    method_signature: str = "<unavailable>"
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return whether all required development-environment checks passed."""
        return not self.failures


def _canonical_path(path: Path) -> Path:
    """Resolve a path for safe, case-insensitive comparison on Windows."""
    return Path(os.path.realpath(path)).resolve(strict=False)


def _same_path(left: Path, right: Path) -> bool:
    """Compare canonical paths using the platform's case rules."""
    return os.path.normcase(str(_canonical_path(left))) == os.path.normcase(
        str(_canonical_path(right))
    )


def _canonical_distribution_name(name: str) -> str:
    """Normalize a distribution name according to packaging's separators."""
    return re.sub(r"[-_.]+", "-", name).casefold()


def _expected_sdk_source(repository_root: Path) -> Path:
    """Read the repository's local editable SDK source from pyproject.toml."""
    pyproject = repository_root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    section_match = re.search(
        rf"(?ms)^\[{re.escape(SDK_SOURCE_SECTION)}\]\s*(.*?)(?=^\[|\Z)",
        text,
    )
    if section_match is None:
        raise ValueError from None
    dependency_match = re.search(
        r"(?m)^\s*tp-link-vigi-sdk\s*=\s*\{([^}]*)\}",
        section_match.group(1),
    )
    if dependency_match is None:
        raise ValueError from None
    path_match = re.search(r"\bpath\s*=\s*([\"'])(.*?)\1", dependency_match.group(1))
    if path_match is None or not path_match.group(2).strip():
        raise ValueError from None
    return _canonical_path(repository_root / path_match.group(2).strip())


def _metadata_location(distribution: Distribution) -> Path:
    """Return the installed distribution's metadata directory."""
    raw_path = getattr(distribution, "_path", None)
    if not isinstance(raw_path, (str, os.PathLike)):
        raise TypeError from None
    location = _canonical_path(Path(cast("str | os.PathLike[str]", raw_path)))
    if not location.is_dir():
        raise ValueError from None
    return location


def _direct_url_source(metadata_location: Path) -> Path:
    """Read and validate the editable PEP 610 direct URL source."""
    direct_url_path = metadata_location / "direct_url.json"
    if not direct_url_path.is_file():
        raise ValueError from None
    try:
        raw_payload: object = cast(
            "object", json.loads(direct_url_path.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError from error
    if not isinstance(raw_payload, dict):
        raise TypeError from None
    payload = cast("dict[str, object]", raw_payload)
    url = payload.get("url")
    directory_info = payload.get("dir_info")
    if not isinstance(url, str) or not isinstance(directory_info, dict):
        raise TypeError from None
    directory_info = cast("dict[str, object]", directory_info)
    if directory_info.get("editable") is not True:
        raise ValueError from None
    parsed = urlparse(url)
    if parsed.scheme.casefold() != "file" or parsed.netloc not in ("", "localhost"):
        raise ValueError from None
    path_text = url2pathname(unquote(parsed.path))
    if not path_text:
        raise ValueError from None
    return _canonical_path(Path(path_text))


def _inspect_distribution(report: PreflightReport) -> None:
    """Collect distribution metadata and its editable source."""
    try:
        sdk_distribution = distribution(SDK_DISTRIBUTION)
        metadata_name = str(sdk_distribution.metadata["Name"]).strip()
        if not metadata_name or _canonical_distribution_name(
            metadata_name
        ) != _canonical_distribution_name(SDK_DISTRIBUTION):
            report.failures.append("SDK_DISTRIBUTION_NAME_UNRECONCILED")
        else:
            report.distribution_name = metadata_name
        version = str(sdk_distribution.version).strip()
        if not version:
            report.failures.append("SDK_DISTRIBUTION_VERSION_UNRECONCILED")
        else:
            report.distribution_version = version
        metadata_location = _metadata_location(sdk_distribution)
        report.metadata_location = str(metadata_location)
        report.editable_source = str(_direct_url_source(metadata_location))
    except (KeyError, PackageNotFoundError, OSError, TypeError, ValueError):
        report.failures.append("SDK_DISTRIBUTION_METADATA_UNRECONCILED")


def _inspect_sdk_module(report: PreflightReport, expected_source: Path | None) -> None:
    """Collect SDK module and public capability facts."""
    try:
        sdk_module = import_module(SDK_MODULE)
        module_file: object = getattr(sdk_module, "__file__", None)
        if not isinstance(module_file, str) or not module_file:
            report.failures.append("SDK_IMPORT_OR_STREAM_SERVICE_FAILED")
            return
        module_path = _canonical_path(Path(module_file))
        report.imported_module_path = str(module_path)
        stream_module = import_module("vigi.stream")
        service_name = "StreamService"
        stream_service: object = getattr(stream_module, service_name, None)
        if not isinstance(stream_service, type):
            report.failures.append("SDK_IMPORT_OR_STREAM_SERVICE_FAILED")
            return
        method_name = "build_ipc_live_url"
        method = getattr(stream_service, method_name, None)
        report.method_present = callable(method)
        if not report.method_present:
            report.failures.append("SDK_IPC_LIVE_URL_BUILDER_MISSING")
        else:
            try:
                callable_method = cast("Callable[..., object]", method)
                report.method_signature = str(inspect.signature(callable_method))
            except (TypeError, ValueError):
                report.method_signature = "<unavailable>"
        if expected_source is not None:
            expected_package = expected_source / "src" / SDK_MODULE
            if not _same_path(module_path.parent, expected_package):
                report.failures.append("SDK_IMPORTED_SOURCE_CONFLICT")
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        report.failures.append("SDK_IMPORT_OR_STREAM_SERVICE_FAILED")


def collect_preflight(repository_root: Path | None = None) -> PreflightReport:
    """Collect non-secret SDK environment facts and required checks."""
    root = _canonical_path(repository_root or Path(__file__).resolve().parents[1])
    report = PreflightReport(
        executable=str(_canonical_path(Path(sys.executable))),
        python_version=sys.version.split()[0],
    )

    try:
        expected_source = _expected_sdk_source(root)
    except (OSError, UnicodeError, ValueError):
        report.failures.append("EXPECTED_LOCAL_SDK_SOURCE_UNRESOLVED")
        expected_source = None

    _inspect_distribution(report)
    _inspect_sdk_module(report, expected_source)

    if (
        expected_source is not None
        and report.editable_source != "<unavailable>"
        and not _same_path(Path(report.editable_source), expected_source)
    ):
        report.failures.append("SDK_EDITABLE_SOURCE_CONFLICT")

    return report


def render_report(report: PreflightReport) -> str:
    """Render only the fixed, non-secret preflight fields."""
    lines = [
        "VIGI SDK environment preflight",
        f"sys.executable: {report.executable}",
        f"python.version: {report.python_version}",
        f"sdk.distribution: {report.distribution_name}",
        f"sdk.version: {report.distribution_version}",
        f"sdk.module_path: {report.imported_module_path}",
        f"sdk.metadata_location: {report.metadata_location}",
        f"sdk.editable_source: {report.editable_source}",
        f"sdk.build_ipc_live_url: {'present' if report.method_present else 'absent'}",
        f"sdk.build_ipc_live_url_signature: {report.method_signature}",
    ]
    lines.extend(f"failure: {failure}" for failure in report.failures)
    lines.append(f"verdict: {'PASS' if report.passed else 'FAIL'}")
    return "\n".join(lines)


def main(repository_root: Path | None = None) -> int:
    """Run the preflight and return a process exit status."""
    report = collect_preflight(repository_root)
    print(render_report(report))  # noqa: T201
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
