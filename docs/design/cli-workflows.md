# CLI Workflow Catalog

## Purpose

This catalog is the single inventory of VIGI Vision's public CLI workflows. It
records the behavior that is implemented today and separates it from planned
work so that a command name or example is not mistaken for a shipped feature.
User-facing invocations remain in the repository [README](../../README.md).

## Current commands

| Command | Primary purpose | Main inputs | Main outputs or artifacts | Camera or NVR connection | OpenAI model | Data handled | Status | Detailed documentation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `inspect` | Capture and analyze one current frame from the configured source. | Configuration in `.env`; no command options. | A current snapshot under `artifacts/` and a terminal inspection report. | Yes. Uses the configured NVR or standalone IPC source. | Yes. | Live camera data. | Implemented. | [README](../../README.md#detailed-cli-notes) |
| `analyze-image` | Analyze one supplied image with a business profile. | Positional image path; required `--profile`. | Terminal profile report; the supplied image remains the input. | No. | Yes. | Local image file. | Implemented. | [README](../../README.md#detailed-cli-notes) |
| `analyze-video` | Analyze a bounded local MP4 from sparse ordered frames. | Positional MP4 path; required `--profile`. | Terminal temporal report; temporary extracted JPEGs are cleaned up. | No. | Yes. | Local MP4 file, limited to 30 seconds. | Implemented. | [README](../../README.md#detailed-cli-notes) |
| `channels` | List safe NVR channel metadata for channel selection. | NVR configuration in `.env`; no command options. | Terminal channel inventory. | Yes. NVR only. | No. | Live NVR metadata. | Implemented. | [README](../../README.md#detailed-cli-notes) |
| `snapshot` | Capture one current JPEG from an online NVR channel. | Required positive `--channel`. | Persistent JPEG under `artifacts/channel-snapshots/` and a terminal report. | Yes. NVR only. | No. | Live NVR data. | Implemented. | [README](../../README.md#detailed-cli-notes) |
| `analyze-recording` | Retrieve and analyze one bounded NVR replay clip. | Required `--channel`, UTC `--start`, positive whole-second `--duration`, and `--profile`. | Terminal temporal report; temporary replay MP4 and local-video temporary frames are cleaned up. | Yes. NVR only. | Yes. | Recorded NVR data. | Implemented. | [Recording Retrieval](../integrations/recording-retrieval.md) |
| `investigate` | Collect the current restaurant-checkout deployment into a durable package. | Required `--scenario restaurant-checkout` and Asia/Seoul `--time`. | Credential-free package under `artifacts/investigations/`, replay clips, anchor snapshots, manifest, and terminal summary. | Yes. NVR only. | No. | Recorded NVR data. | Implemented. | [Investigation Plan](investigation-plan.md) |
| `sample-recording` | Collect generic, timestamped frames from a long NVR recording range at a chosen cadence. | Required `--channel`, `--start`, `--duration`, and `--interval`; optional `--timezone` (default `Asia/Seoul`), `--chunk-duration` (default `10m`), and `--output-dir` (default `artifacts/recording-samples`). | Durable timestamped JPEG package and credential-free manifest; no semantic result. | Yes. NVR only. | No. | Recorded NVR data. | Implemented. | [Recording Sampling](recording-sampling.md) |

`analyze-recording` and `sample-recording` are deliberately different:
the former retrieves one bounded clip for OpenAI temporal analysis, while the
latter will preserve frames for later human or programmatic use and will not
perform analysis.

## Documentation maintenance rule

- Every public CLI command and user-visible option must be reflected in this
  catalog when it is added, removed, or changed.
- User-facing examples belong in [README.md](../../README.md).
- Detailed internal behavior and feature contracts belong in a dedicated design
  or integration document.
- Important completed decisions and session outcomes belong in
  [PROJECT.md](../../PROJECT.md).
- Documentation that duplicates CLI defaults should be protected by tests where
  practical, so changing a default cannot silently stale the documentation.
