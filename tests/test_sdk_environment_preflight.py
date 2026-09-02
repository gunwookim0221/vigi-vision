from __future__ import annotations

import json
import types
from dataclasses import dataclass
from pathlib import Path

import pytest
from tools import sdk_environment_preflight as preflight


class _FakeDistribution:
    metadata: dict[str, str]
    version: str
    _path: Path

    def __init__(self, metadata_path: Path, version: str = "0.2.0") -> None:
        self.metadata = {"Name": "tp-link-vigi-sdk"}
        self.version = version
        self._path = metadata_path


@dataclass
class _FakeEnvironmentOptions:
    method: bool = True
    module_root: Path | None = None
    direct_url: str | None = "expected"
    stream_service: type | None = None


def _fake_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: _FakeEnvironmentOptions | None = None,
) -> tuple[Path, _FakeDistribution, Path]:
    options = options or _FakeEnvironmentOptions()
    repository_root = tmp_path / "vigi vision"
    sdk_root = tmp_path / "adjacent sdk"
    package_root = sdk_root / "src" / "vigi"
    _ = package_root.mkdir(parents=True)
    _ = repository_root.mkdir()
    _ = (repository_root / "pyproject.toml").write_text(
        '[tool.uv.sources]\ntp-link-vigi-sdk = { path = "../adjacent sdk", editable = true }\n',
        encoding="utf-8",
    )
    module_root = options.module_root or sdk_root
    module_file = module_root / "src" / "vigi" / "__init__.py"
    metadata_path = tmp_path / "site-packages" / "tp_link_vigi_sdk-0.2.0.dist-info"
    metadata_path.mkdir(parents=True)
    direct_url = options.direct_url
    if direct_url == "expected":
        direct_url = sdk_root.as_uri()
    if direct_url is not None:
        _ = (metadata_path / "direct_url.json").write_text(
            json.dumps({"url": direct_url, "dir_info": {"editable": True}}),
            encoding="utf-8",
        )
    distribution = _FakeDistribution(metadata_path)

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
    return repository_root, distribution, sdk_root


def test_correct_project_interpreter_and_adjacent_editable_sdk_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root, _, _ = _fake_environment(tmp_path, monkeypatch)

    report = preflight.collect_preflight(repository_root)

    assert report.passed
    assert report.method_present
    assert report.editable_source != "<unavailable>"


def test_missing_sdk_import_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root, distribution, _ = _fake_environment(tmp_path, monkeypatch)

    def missing_import(name: str) -> object:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(preflight, "import_module", missing_import)

    report = preflight.collect_preflight(repository_root)

    assert not report.passed
    assert "SDK_IMPORT_OR_STREAM_SERVICE_FAILED" in report.failures
    assert report.distribution_version == distribution.version


def test_missing_stream_service_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root, _, _ = _fake_environment(
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

    repository_root, _, _ = _fake_environment(
        tmp_path, monkeypatch, _FakeEnvironmentOptions(stream_service=StreamService)
    )

    report = preflight.collect_preflight(repository_root)

    assert not report.passed
    assert "SDK_IPC_LIVE_URL_BUILDER_MISSING" in report.failures


def test_wrong_editable_source_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wrong_root = tmp_path / "wrong sdk"
    wrong_package = wrong_root / "src" / "vigi"
    wrong_package.mkdir(parents=True)
    repository_root, _, _ = _fake_environment(
        tmp_path,
        monkeypatch,
        _FakeEnvironmentOptions(module_root=wrong_root, direct_url=wrong_root.as_uri()),
    )

    report = preflight.collect_preflight(repository_root)

    assert not report.passed
    assert "SDK_IMPORTED_SOURCE_CONFLICT" in report.failures
    assert "SDK_EDITABLE_SOURCE_CONFLICT" in report.failures


def test_source_path_normalization_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root, _, sdk_root = _fake_environment(tmp_path, monkeypatch)
    metadata_path = tmp_path / "site-packages" / "tp_link_vigi_sdk-0.2.0.dist-info"
    normalized_url = sdk_root.as_uri().replace("adjacent%20sdk", "ADJACENT%20SDK")
    _ = (metadata_path / "direct_url.json").write_text(
        json.dumps({"url": normalized_url, "dir_info": {"editable": True}}),
        encoding="utf-8",
    )

    report = preflight.collect_preflight(repository_root)

    assert report.passed


@pytest.mark.parametrize("metadata", [None, "not-json"])
def test_missing_or_malformed_direct_url_metadata_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata: str | None
) -> None:
    repository_root, _, _ = _fake_environment(
        tmp_path, monkeypatch, _FakeEnvironmentOptions(direct_url=None)
    )
    if metadata is not None:
        metadata_path = tmp_path / "site-packages" / "tp_link_vigi_sdk-0.2.0.dist-info"
        _ = (metadata_path / "direct_url.json").write_text(metadata, encoding="utf-8")

    report = preflight.collect_preflight(repository_root)

    assert not report.passed
    assert "SDK_DISTRIBUTION_METADATA_UNRECONCILED" in report.failures


def test_output_is_non_secret_and_not_an_environment_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root, _, _ = _fake_environment(tmp_path, monkeypatch)
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
    repository_root, _, _ = _fake_environment(tmp_path, monkeypatch)

    assert preflight.main(repository_root) == 0
    assert "verdict: PASS" in capsys.readouterr().out

    def missing_import(_: str) -> object:
        raise ImportError

    monkeypatch.setattr(preflight, "import_module", missing_import)
    assert preflight.main(repository_root) == 1
    assert "verdict: FAIL" in capsys.readouterr().out
