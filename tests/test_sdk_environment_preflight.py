from __future__ import annotations

import types
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tools import sdk_environment_preflight as preflight

if TYPE_CHECKING:
    import pytest


class _FakeDistribution:
    metadata: dict[str, str]
    version: str
    _path: Path

    def __init__(self, metadata_path: Path, version: str = "0.3.0") -> None:
        self.metadata = {"Name": "tp-link-vigi-sdk"}
        self.version = version
        self._path = metadata_path


@dataclass
class _FakeEnvironmentOptions:
    method: bool = True
    module_root: Path | None = None
    direct_url: bool = False
    version: str = "0.3.0"
    requirement: str = "tp-link-vigi-sdk==0.3.0"
    stream_service: type | None = None


def _fake_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: _FakeEnvironmentOptions | None = None,
) -> tuple[Path, _FakeDistribution]:
    options = options or _FakeEnvironmentOptions()
    repository_root = tmp_path / "vigi vision"
    site_packages = tmp_path / "site-packages"
    (site_packages / "vigi").mkdir(parents=True)
    repository_root.mkdir()
    _ = (repository_root / "pyproject.toml").write_text(
        f'[project]\ndependencies = [\n    "{options.requirement}",\n]\n', encoding="utf-8"
    )
    module_root = options.module_root or site_packages
    module_file = module_root / "vigi" / "__init__.py"
    metadata_path = site_packages / f"tp_link_vigi_sdk-{options.version}.dist-info"
    metadata_path.mkdir(parents=True)
    if options.direct_url:
        _ = (metadata_path / "direct_url.json").write_text("{}", encoding="utf-8")
    distribution = _FakeDistribution(metadata_path, options.version)

    class _StreamService:
        def build_ipc_live_url(self, host: str, stream: object = "1") -> str:
            return f"rtsp://{host}/{stream}"

    selected_service = options.stream_service or _StreamService
    sdk_module = types.SimpleNamespace(__file__=str(module_file))
    stream_module = types.SimpleNamespace()
    if options.method:
        stream_module.StreamService = selected_service

    def fake_import(name: str) -> object:
        if name == "vigi":
            return sdk_module
        if name == "vigi.stream":
            return stream_module
        raise ModuleNotFoundError(name)

    def fake_distribution(_: str) -> _FakeDistribution:
        return distribution

    monkeypatch.setattr(preflight, "distribution", fake_distribution)
    monkeypatch.setattr(preflight, "import_module", fake_import)
    return repository_root, distribution


def test_exact_released_sdk_from_registry_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root, _ = _fake_environment(tmp_path, monkeypatch)

    report = preflight.collect_preflight(repository_root)

    assert report.passed
    assert report.method_present
    assert report.required_version == "0.3.0"
    assert report.installation_source == "registry"


def test_missing_sdk_import_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root, distribution = _fake_environment(tmp_path, monkeypatch)

    def missing_import(_: str) -> object:
        raise ModuleNotFoundError

    monkeypatch.setattr(preflight, "import_module", missing_import)

    report = preflight.collect_preflight(repository_root)

    assert not report.passed
    assert "SDK_IMPORT_OR_STREAM_SERVICE_FAILED" in report.failures
    assert report.distribution_version == distribution.version


def test_missing_stream_service_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root, _ = _fake_environment(
        tmp_path, monkeypatch, _FakeEnvironmentOptions(method=False)
    )

    report = preflight.collect_preflight(repository_root)

    assert not report.passed
    assert "SDK_IMPORT_OR_STREAM_SERVICE_FAILED" in report.failures


def test_missing_ipc_live_url_builder_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StreamService:
        pass

    repository_root, _ = _fake_environment(
        tmp_path, monkeypatch, _FakeEnvironmentOptions(stream_service=StreamService)
    )

    report = preflight.collect_preflight(repository_root)

    assert not report.passed
    assert "SDK_IPC_LIVE_URL_BUILDER_MISSING" in report.failures


def test_wrong_import_source_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wrong_root = tmp_path / "adjacent sdk" / "src"
    (wrong_root / "vigi").mkdir(parents=True)
    repository_root, _ = _fake_environment(
        tmp_path, monkeypatch, _FakeEnvironmentOptions(module_root=wrong_root)
    )

    report = preflight.collect_preflight(repository_root)

    assert not report.passed
    assert "SDK_IMPORTED_SOURCE_CONFLICT" in report.failures


def test_direct_url_install_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root, _ = _fake_environment(
        tmp_path, monkeypatch, _FakeEnvironmentOptions(direct_url=True)
    )

    report = preflight.collect_preflight(repository_root)

    assert not report.passed
    assert "SDK_NON_REGISTRY_INSTALL" in report.failures


def test_version_mismatch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root, _ = _fake_environment(
        tmp_path, monkeypatch, _FakeEnvironmentOptions(version="0.2.0")
    )

    report = preflight.collect_preflight(repository_root)

    assert not report.passed
    assert "SDK_DISTRIBUTION_VERSION_MISMATCH" in report.failures


def test_non_exact_requirement_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root, _ = _fake_environment(
        tmp_path, monkeypatch, _FakeEnvironmentOptions(requirement="tp-link-vigi-sdk>=0.3.0")
    )

    report = preflight.collect_preflight(repository_root)

    assert not report.passed
    assert "SDK_REQUIRED_VERSION_UNRESOLVED" in report.failures


def test_output_is_non_secret_and_not_an_environment_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root, _ = _fake_environment(tmp_path, monkeypatch)
    marker = "do-not-print-this-secret"
    monkeypatch.setenv("OPENAI_API_KEY", marker)
    monkeypatch.setenv("VIGI_PASSWORD", marker)

    output = preflight.render_report(preflight.collect_preflight(repository_root))

    assert marker not in output
    assert "OPENAI_API_KEY" not in output
    assert "VIGI_PASSWORD" not in output
    assert "os.environ" not in output
    assert "verdict: PASS" in output


def test_main_returns_zero_for_pass_and_one_for_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repository_root, _ = _fake_environment(tmp_path, monkeypatch)

    assert preflight.main(repository_root) == 0
    assert "verdict: PASS" in capsys.readouterr().out

    def missing_import(_: str) -> object:
        raise ImportError

    monkeypatch.setattr(preflight, "import_module", missing_import)
    assert preflight.main(repository_root) == 1
    assert "verdict: FAIL" in capsys.readouterr().out
