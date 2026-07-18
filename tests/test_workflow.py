from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe

from vigi_vision.analysis import SceneAnalysis
from vigi_vision.channel_selection import Channel
from vigi_vision.workflow import InspectionWorkflow, LiveStream

_TEST_OPENAI_KEY = token_urlsafe()
_TEST_PASSWORD = token_urlsafe()


@dataclass(frozen=True, slots=True)
class FakeGateway:
    stream_value: LiveStream

    def stream(self) -> LiveStream:
        return self.stream_value


class FakeExtractor:
    def extract(self, live_url: str, *, username: str, password: str, output_path: Path) -> Path:
        assert live_url in {
            "rtsp://ipc.example.invalid/stream1",
            "rtsp://nvr.example.invalid/live/1/1/avm",
        }
        assert username == "operator"
        assert password == _TEST_PASSWORD
        _ = output_path.parent.mkdir(parents=True, exist_ok=True)
        _ = output_path.write_bytes(b"jpeg")
        return output_path


class FakeAnalyzer:
    def analyze(self, image_path: Path) -> SceneAnalysis:
        assert image_path.read_bytes() == b"jpeg"
        return SceneAnalysis(
            summary="Quiet entrance.",
            person_visible=False,
            notable_observations=("A parked bicycle.",),
            limitations="Single still frame.",
        )


def test_workflow_reuses_one_vertical_slice_for_ipc(tmp_path: Path) -> None:
    # Given
    stream = LiveStream(
        label="Standalone IPC",
        channel=None,
        live_url="rtsp://ipc.example.invalid/stream1",
        username="operator",
        password=_TEST_PASSWORD,
        artifact_stem="ipc",
    )
    workflow = InspectionWorkflow(FakeGateway(stream), FakeExtractor(), FakeAnalyzer(), tmp_path)

    # When
    result = workflow.run()

    # Then
    assert result.channel is None
    assert result.label == "Standalone IPC"
    assert result.snapshot_path.parent == tmp_path / "snapshots"
    assert result.analysis.person_visible is False


def test_workflow_preserves_nvr_channel_result(tmp_path: Path) -> None:
    # Given
    channel = Channel(channel_id=1, name="Front", alias="Front", online=True)
    stream = LiveStream(
        label="NVR channel 1",
        channel=channel,
        live_url="rtsp://nvr.example.invalid/live/1/1/avm",
        username="operator",
        password=_TEST_PASSWORD,
        artifact_stem="channel-1",
    )
    workflow = InspectionWorkflow(FakeGateway(stream), FakeExtractor(), FakeAnalyzer(), tmp_path)

    # When
    result = workflow.run()

    # Then
    assert result.channel == channel
    assert result.label == "NVR channel 1"
