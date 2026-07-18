# VIGI Vision

An AI-assisted application for investigating TP-Link VIGI camera data.

Start with the [project charter](PROJECT.md). Coding agents should also read
[AGENTS.md](AGENTS.md); deeper documentation is routed from
[docs/README.md](docs/README.md).

## First Working Slice

`vigi-vision inspect` performs one current-frame investigation:

1. select the configured NVR or standalone IPC source;
2. for NVR, authenticate and list channels through the public `VigiClient` API
   and select the configured or sole online channel;
3. build a credential-free RTSP URL through `client.stream.build_live_url` for
   NVR or public `StreamService.build_ipc_live_url` for a standalone IPC;
4. invoke installed `ffmpeg` with separate RTSP Digest credentials to save one
   JPEG below git-ignored `artifacts/snapshots/`;
5. send that image to the OpenAI Responses API and print a structured result.

`vigi-vision analyze-image` analyzes a previously captured camera frame with a
business-specific profile. It never invokes ffmpeg and does not connect to a
live camera.

## Setup

Install the project with `uv sync`, copy `.env.example` to `.env`, and set:

- `OPENAI_API_KEY`
- `VIGI_SOURCE=nvr` with `VIGI_HOST`, `VIGI_USERNAME`, and `VIGI_PASSWORD`; or
- `VIGI_SOURCE=ipc` with `VIGI_IPC_HOST`, `VIGI_IPC_USERNAME`, and
  `VIGI_IPC_PASSWORD`
- optionally `VIGI_PORT`, `VIGI_VERIFY_SSL`, `VIGI_CHANNEL_ID`, `VIGI_STREAM`,
  and `FFMPEG_PATH`

VIGI Vision explicitly loads `.env` from the current working directory. OS
environment variables override matching `.env` values when needed.

`ffmpeg` must be available on `PATH` unless `FFMPEG_PATH` names its executable.
The selected NVR or IPC credentials are supplied separately to ffmpeg for its
RTSP Digest challenge; they are not embedded in the SDK-built URL. Standard IPC
RTSP uses the SDK-generated default-port URL; Vision does not support
`VIGI_IPC_PORT`. `VIGI_IPC_VERIFY_TLS` is an SDK/OpenAPI control-plane setting
and is not read by this RTSP-only Vision path. Do not log or persist the URL,
credentials, or extracted frame outside `artifacts/`.

## Usage

For NVR, list safe channel metadata first to choose `VIGI_CHANNEL_ID` when
needed. This command is deliberately NVR-only:

```text
uv run vigi-vision channels
```

Then run the current-frame inspection:

```text
uv run vigi-vision inspect
```

For a standalone IPC, set `VIGI_SOURCE=ipc`; `inspect` uses the public IPC RTSP
builder and does not perform IPC OpenAPI authentication. The first live run is
only complete when its source validation, one-frame extraction, and OpenAI
structured image analysis all succeed.

For hackathon demonstrations, analyze a previously captured real camera frame
without taking a new live capture. Select exactly one of `counter`, `dining`,
or `entrance`; each profile uses its own prompt and strict structured schema.
The output presents a concise business report with summary, qualitative
confidence, observable evidence, profile findings, optional recommendations,
and single-frame limitations. The structured analysis remains the source of
truth.

```text
uv run vigi-vision analyze-image artifacts/snapshots/ipc-20260718T094854Z.jpg --profile counter
uv run vigi-vision analyze-image artifacts/snapshots/ipc-20260718T094854Z.jpg --profile dining
uv run vigi-vision analyze-image artifacts/snapshots/ipc-20260718T094854Z.jpg --profile entrance
```

Documented Korean aliases are accepted at the CLI boundary and resolve to the
same canonical profile IDs: `카운터` for `counter`, `홀` or `식사공간` for
`dining`, and `입구` or `신발장` for `entrance`. Structured output always
uses the canonical English profile ID.

The `counter` profile reports only a possible payment interaction, never a
definite payment conclusion from a single frame. Dining and entrance counts are
estimates from a still image.

## Image data boundaries

`sample_data/` contains public demonstration images used only for profile-based
analysis examples. `private_data/` is reserved for local real-camera captures
and is ignored by Git; production, customer, employee, and surveillance data
must never be committed.
