# System Overview

This document describes how VIGI Vision is organized, how its subsystems relate,
and how runtime execution is entered.

## Repository boundary

VIGI Vision owns AI-assisted investigation workflows for TP-Link VIGI camera
data. The neighboring `tp-link-vigi-sdk` repository owns authentication,
documented OpenAPI communication, camera and recording metadata access, and
stream URL construction. This boundary is enforced at the import level:
`vigi_vision` imports from `vigi` (the SDK package) but never writes to it.

## Package structure

All application code lives in `src/vigi_vision/`. The package is installed
with `setuptools` and declares one CLI entry point and one ASGI factory.

```
src/vigi_vision/
?쒋?? __init__.py
?쒋?? __main__.py
?쒋?? cli.py                    # Typer CLI (entry point: vigi-vision)
?쒋?? config.py                 # .env configuration (Settings, CaptureSettings)
?쒋?? gateway.py                # Source gateway selection (NVR or IPC)
???쒋?? nvr.py                    # SDK NVR adapter (channels, live stream)
?쒋?? ipc.py                    # SDK IPC adapter (standalone RTSP)
???쒋?? recording.py              # RecordingPlanner (SDK search ??replay plan)
?쒋?? recording_models.py       # Value types: RecordingWindow, RecordingSegment, ReplayRequest
?쒋?? replay.py                 # ReplayExtractor (ffmpeg ??temporary MP4)
?쒋?? replay_progress.py        # Bounded FFmpeg progress diagnostics
???쒋?? ffmpeg.py                 # One-frame extraction boundary
?쒋?? video.py                  # Local MP4 probing and frame sampling
???쒋?? workflow.py               # Live inspection orchestration
?쒋?? analysis.py               # OpenAI image/temporal analysis boundary
?쒋?? profiles.py               # Business analysis profile registry
?쒋?? temporal_profiles.py      # Temporal analysis profile definitions
???쒋?? investigation.py          # Pure investigation planning domain
?쒋?? investigation_collection.py   # Per-item replay collection
?쒋?? investigation_artifacts.py    # Durable investigation package builder
?쒋?? investigation_service.py      # Plan ??Collect ??Artifacts orchestration
?쒋?? investigation_manifest.py     # Investigation manifest schema
?쒋?? investigation_snapshot.py     # Anchor snapshot boundary
?쒋?? investigation_progress.py     # Investigation stage reporting
?쒋?? investigate_cli.py            # investigate CLI command
???쒋?? sampling.py               # Recording-sampling plan computation
?쒋?? sampling_service.py       # Sampling orchestration (coverage ??chunks ??frames)
?쒋?? sampling_artifacts.py     # Sampling artifact writer and manifest
?쒋?? sampling_cli.py           # sample-recording CLI command
???쒋?? reference_frame_models.py         # Reference-frame domain types and policies
?쒋?? reference_frame_service.py        # Single-frame orchestration
?쒋?? reference_frame_decoder.py        # Replay-based decoder (ffprobe + ffmpeg)
?쒋?? reference_frame_direct.py         # Direct FFmpeg decoder (tee/framemd5)
?쒋?? reference_frame_direct_support.py # Direct decoder helpers and protocols
?쒋?? reference_frame_artifacts.py      # Staged artifact store (begin ??finalize/discard)
?쒋?? reference_frame_resources.py      # Completed resource lookup and reuse
?쒋?? reference_frame_candidate_service.py  # Serial candidate-set orchestration
?쒋?? reference_frame_candidate_models.py   # Candidate-set domain types
?쒋?? reference_frame_api.py            # FastAPI application and routes
?쒋?? reference_frame_api_errors.py     # HTTP error redaction
?쒋?? reference_frame_api_models.py     # API request/response schemas
?쒋?? reference_frame_candidate_api_models.py  # Candidate-set API schemas
?쒋?? reference_frame_web_ui.py         # Static browser shell mount
?쒋?? reference_frame_web/              # HTML, CSS, JS assets
???쒋?? recording_cli.py          # analyze-recording CLI command
?쒋?? snapshot_cli.py           # snapshot CLI command
?쒋?? video_cli.py              # analyze-video CLI command
?쒋?? channel_selection.py      # NVR channel selection logic
?쒋?? cli_output.py             # Shared CLI output formatting
?쒋?? openai_errors.py          # OpenAI error classification
```

## Entry points

### CLI (`vigi-vision`)

The CLI is the primary user-facing entry point. It is defined in
[cli.py](../../src/vigi_vision/cli.py) as a Typer application registered as
`vigi-vision` in `pyproject.toml`. Each CLI command composes its own service
graph from configuration and delegates to the relevant service boundary.

Current commands: `inspect`, `analyze-image`, `analyze-video`, `channels`,
`snapshot`, `analyze-recording`, `investigate`, `sample-recording`.

### ASGI factory (`reference_frame_api`)

The reference-frame API is a local FastAPI application started through
`uvicorn`. Its ASGI factory
`create_reference_frame_app_from_environment` in
[reference_frame_api.py](../../src/vigi_vision/reference_frame_api.py)
reads `.env`, composes the full service graph (planner, extractor, decoder,
artifact store, resource store, direct acquirer, channel inventory), and
returns a ready application. It mounts a static browser shell at `/` and API
routes under `/api/v1/`.

## Configuration

[config.py](../../src/vigi_vision/config.py) defines two pydantic-settings
models:

- **`Settings`** ??full configuration including OpenAI API key, used by CLI
  commands that invoke AI analysis.
- **`CaptureSettings`** ??NVR/IPC connection settings without OpenAI, used by
  the reference-frame API.

Both load from a `.env` file with `dotenv_values`. The configuration model
validates one `VIGI_SOURCE` discriminator (`nvr` or `ipc`) and conditionally
requires the matching connection settings. `NvrConnection` and `IpcConnection`
are frozen dataclasses that carry validated connection parameters.

## Module layer structure

Modules follow a layered dependency pattern:

```
CLI commands / ASGI factory
       ??       ??Service orchestration
(InvestigationService, ReferenceFrameService, SamplingService, InspectionWorkflow)
       ??       ??Domain boundaries (Protocol interfaces)
(RecordingPlanner, ReplayExtractor, ReferenceFrameDecoder, OpenAiAnalyzer, ...)
       ??       ??External adapters
(vigi SDK, ffmpeg/ffprobe subprocess, OpenAI client, filesystem)
```

Services accept their collaborators as Protocol-typed constructor parameters.
CLI commands and the ASGI factory are the composition roots that instantiate
concrete implementations and wire them together.

## Credential safety

Credentials are treated as a cross-cutting concern:

- `.env` values are loaded through `SecretStr` fields that suppress repr output.
- `ReplayRequest`, `ReplayClip`, and all persisted manifests carry
  credential-free URLs only.
- RTSP credentials are supplied to ffmpeg as in-memory arguments and are
  not stored in return values or exception messages.
- API error responses use a redaction boundary that maps domain exceptions to
  fixed safe HTTP error shapes, never exposing exception text.
- NVR SDK errors are classified into `NvrErrorKind` categories with fixed
  diagnostic messages.

## Storage locations

All persistent artifacts are written under `artifacts/` relative to the
working directory:

| Path | Owner | Contents |
|------|-------|----------|
| `artifacts/snapshots/` | Live inspection workflow | Timestamped snapshot JPEGs |
| `artifacts/channel-snapshots/` | Snapshot command | Per-channel current JPEGs |
| `artifacts/investigations/` | Investigation service | Per-investigation packages (clips, snapshots, manifests) |
| `artifacts/recording-samples/` | Sampling service | Timestamped JPEG packages with manifests |
| `artifacts/reference-frames/` | Reference-frame service | Per-resource directories (frame.jpg + manifest.json) |

Temporary files (replay clips, extracted frames) are created in system temp
directories and removed after consumption or on failure.

## Subsystem cross-references

- [Recording and Media Pipelines](recording-and-media-pipelines.md) ??how
  recording retrieval, replay extraction, video analysis, investigation, and
  sampling pipelines are organized and compose shared boundaries.
- [Reference Frame Pipeline](reference-frame-pipeline.md) ??how the
  reference-frame subsystem acquires, decodes, persists, and reuses durable
  frames through its service, API, and browser shell.
