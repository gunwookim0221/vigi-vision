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
| `search-recordings` | Execute one bounded request-relative Phase 7E disappearance search. | Required `--investigation-id`, `--end`, and `--timezone`; optional `--create-phase8-handoff`. | Schema 5–7 search state under `artifacts/investigation-searches/`, one retained common-session MP4, and a terminal status summary; the option also creates the exact Phase 8 source-clip package. | Yes. NVR only; exactly one replay acquisition is permitted. | No OpenAI model; the approved local mask classifier is required. | Recorded NVR data and local derived evidence. | Implemented; the browser HTTP start reuses the same public service while leaving CLI arguments and behavior unchanged. | [Object-disappearance Recording Search](object-disappearance-recording-search.md) |
| `recording-search-status` | Read the strict Phase 7E and Phase 8 lifecycle projection. | Required `--investigation-id` and `--run-id`. | Read-only terminal status, including Phase 8 `READY`, `DELETING`, or `DELETED` when present. | The current CLI composition requires configured NVR capture settings; status performs no replay or mutation. | No. | Local persisted search records and bound media. | Implemented. | [Object-disappearance Recording Search](object-disappearance-recording-search.md) |
| `create-phase8-handoff` | Create or exactly reuse the closed Phase 8 source-clip request package. | Required `--investigation-id` and `--run-id`. | One atomic package under `artifacts/investigation-searches/.phase8/` containing the indexed source clip, canonical records, and manifest lineage; prints the request identity. | The current CLI composition requires configured NVR capture settings; handoff generation is local from the retained common-session MP4 and performs no second replay. | No. | Strictly reopened local Phase 7E records and retained media. | Implemented. | [Object-disappearance Recording Search](object-disappearance-recording-search.md) |
| `delete-recording-search-media` | Explicitly run or resume the durable two-media deletion lifecycle. | Required `--investigation-id`, `--run-id`, and confirmation flag `--yes`. | Durable `DELETING` then `DELETED`; removes only the identity-bound common-session MP4 and indexed Phase 8 source clip. | The current CLI composition requires configured NVR capture settings; deletion performs no replay. | No. | Two strictly verified local MP4 files and lifecycle records. | Implemented. | [Object-disappearance Recording Search](object-disappearance-recording-search.md) |

`analyze-recording` and `sample-recording` are deliberately different:
the former retrieves one bounded clip for OpenAI temporal analysis, while the
latter will preserve frames for later human or programmatic use and will not
perform analysis.

## Phase 7E command behavior

All four Phase 7E commands require `VIGI_SOURCE=nvr` and the capture settings
needed by the current command composition. Only `search-recordings` acquires
recorded data. Handoff creation derives its bounded source clip from the exact
retained common-session MP4; status and deletion do not contact replay or
reclassify evidence.

Successful `search-recordings` and `recording-search-status` invocations print
the investigation ID, run ID, Phase 7 status, optional reason, and Phase 8
state. `create-phase8-handoff` prints `Phase 8 handoff request created.` and the
stable request ID. `delete-recording-search-media` prints
`Recording-search media: DELETED`; omitting `--yes` fails before mutation.

Exit code `0` means success. Invalid input or missing deletion confirmation
uses `2`; an active run or handoff conflict uses `3`; unavailable execution or
an ineligible/missing run uses `4`; corrupt Phase 8 package state uses `5`; and
missing or corrupt bound media uses `6`. `recording-search-status` uses `1` if
the safe status projection itself cannot be produced. Error output is a stable
safe message and does not expose internal paths or credentials. The normative
identity, request-relative timing, closed-membership, and deletion recovery
rules are defined in the routed
[Phase 7E design](object-disappearance-recording-search.md).

The browser is an additional caller, not a replacement CLI. Its strict POST
contains `investigation_id`, local `search_end`, and UUIDv4 `request_id`; all
other search authority is reconstructed from the confirmed Phase 6 package.

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
