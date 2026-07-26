# Recording Sampling

## Status

This is a planned design contract for `sample-recording`. It describes a
future VIGI Vision command; it does not describe an implemented command or
guarantee that the current SDK can supply every desired range.

## Problem statement

Long NVR intervals are cumbersome to inspect as one downloaded MP4. The
application needs generic, reusable frame-search material at known times while
keeping each network and ffmpeg operation bounded. The first version must only
sample frames. It must not turn sparse frames into a search result, event, or
business conclusion.

## Goals

- Resolve the requested NVR range through public SDK recording and replay
  capabilities.
- Preserve a regular user-requested sample schedule without downloading the
  whole range as one MP4.
- Split replay extraction into bounded chunks, write timestamped JPEG frames,
  and retain credential-free structured metadata.
- Make coverage gaps, skipped requested samples, and partial failures visible.
- Keep ffmpeg work, artifact creation, chunk orchestration, and progress
  reporting in VIGI Vision.

## Non-goals

The first implementation explicitly excludes ROI selection UI, shoe detection,
object tracking, scene-change or disappearance detection, VLM analysis, person
or group matching, counter or POS correlation, resolution reduction, alternate
recording quality or substream selection, and any general search result or event
verdict.

The command is not named `search-recording`: it samples frames and performs no
semantic or visual search.

## User workflow

1. The user selects an NVR channel, a local source time with its timezone, a
   finite range, a sampling interval, and a bounded chunk duration.
2. VIGI Vision converts the requested range to UTC, discovers public-SDK
   recording segments, and plans only the covered sample timestamps.
3. It retrieves one bounded replay chunk at a time, extracts that chunk's
   scheduled JPEG frames, and records their source and extraction metadata.
4. It writes a manifest and reports completed chunks, frames, gaps, and the
   final artifact directory. No OpenAI call, profile, report, or conclusion is
   produced.

## Planned CLI contract

The planned surface is:

```powershell
vigi-vision sample-recording `
  --channel 3 `
  --start "2026-07-26 18:00:00" `
  --timezone Asia/Seoul `
  --duration 2h `
  --interval 5s `
  --chunk-duration 10m `
  --output-dir artifacts/recording-samples
```

| Input | Planned contract |
| --- | --- |
| `--channel` | Required positive NVR channel ID. |
| `--start` | Required naive wall-clock timestamp in `YYYY-MM-DD HH:MM:SS`; it is not UTC unless `--timezone UTC` is supplied. |
| `--timezone` | Required IANA source timezone for `--start`, for example `Asia/Seoul`. It avoids the ambiguity between the existing UTC recording command and the Asia/Seoul investigation command. |
| `--duration` | Required positive whole-second duration. The planned grammar is an integer plus `s`, `m`, or `h` (`5s`, `10m`, `2h`). |
| `--interval` | Required positive whole-second cadence using the same grammar. Samples are anchored at requested UTC start, never reset at a segment or chunk boundary. |
| `--chunk-duration` | Required positive whole-second bound using the same grammar. It limits one replay extraction; it must be at least the interval. No default is finalized because the repository contains no evidence for a safe long-replay default. |
| `--output-dir` | Required or defaulted by implementation only after an explicit product decision. The intended parent is `artifacts/recording-samples/`, consistent with existing durable artifact roots. |

The command is NVR-only and must reject an IPC source before SDK or ffmpeg work.
It must not accept a profile or any OpenAI option. The duration grammar above is
planned; the current `analyze-recording` command accepts only whole seconds
ending in `s`, so its parser cannot be reused without an intentional change.

## Input validation

- Parse the source timestamp and IANA timezone at the CLI boundary, then create
  a canonical whole-second UTC range. Reject nonexistent or ambiguous local
  times rather than silently selecting an offset.
- Require positive channel, duration, interval, and chunk duration values.
- Require `chunk-duration >= interval`; reject a range whose arithmetic would
  exceed the implementation's documented operational limit, if one is added.
- Require a writable output parent and refuse an existing final package path;
  never merge into or delete an existing user directory.
- Validate that frame names and manifest identifiers are derived from safe
  channel IDs and UTC timestamps, never hostnames or replay URLs.

## Recording planning and segmentation

VIGI Vision will use the same public SDK boundary already used by the recording
retrieval layer: NVR-local recording-day discovery, public recording-result
pages, epoch-second segment conversion, and public credential-free replay-URL
construction. Existing behavior establishes that the configured NVR recording
calendar is Asia/Seoul and SDK segment timestamps are Unix epoch seconds;
sampling must retain the requested source timezone separately from canonical
UTC facts.

The planner must enumerate every segment intersecting the requested UTC range,
normalize them to UTC, and create a coverage map. It must not rely on the
current `RecordingPlanner.plan()` result alone, because that boundary returns
the first overlapping segment for one requested window.

Planning rules:

1. Generate the requested sample timestamps at `start_utc + n * interval` while
   they are before `end_utc`.
2. Intersect the range with returned recording segments; do not fabricate
   coverage between segment endpoints.
3. Divide covered work into contiguous replay chunks no longer than
   `chunk-duration`, respecting segment boundaries.
4. Assign each scheduled timestamp to exactly one covered chunk. A timestamp in
   a gap is recorded as skipped, not moved to the nearest available frame.
5. Build each replay URL through the SDK for its bounded UTC chunk, then use
   ffmpeg only for that chunk.

Segment boundaries are source facts, not requested chunk boundaries. A segment
may require several chunks; adjacent segments must remain separate unless the
implementation can prove they form continuous coverage.

## Chunked frame extraction flow

For each planned chunk, VIGI Vision will obtain a bounded temporary replay MP4
through the existing public-SDK replay path, then run ffmpeg against that local
file to extract only the scheduled frames for the chunk. The temporary MP4 is
removed after its frames and chunk metadata have been handled. A temporary
frame file is renamed into the package only after successful extraction.

The extraction plan must preserve the scheduled UTC timestamp for each frame;
ffmpeg seek precision is an observed extraction fact and belongs in metadata if
it differs from the requested schedule. The initial implementation need not
deduplicate visual frames or reduce their resolution.

## Output directory and artifacts

A successful invocation creates one new package below the supplied output
parent, with a safe identifier derived from channel and requested UTC bounds:

```text
artifacts/recording-samples/
  channel-3_20260726T090000Z_20260726T110000Z/
    manifest.json
    frames/
      20260726T090000Z.jpg
      20260726T090005Z.jpg
    chunks/
      0001.json
```

The exact directory spelling is proposed rather than frozen, but final names
must be deterministic, filesystem-safe, and credential-free. Chunks are
processed in an invocation-owned staging directory beside the final package.
On complete success it is atomically promoted where the filesystem permits.
Existing final paths are an error and are never overwritten.

## Manifest and metadata requirements

`manifest.json` is required. It must include:

- schema version and lifecycle state (`completed`, `completed_with_gaps`,
  `cancelled`, or `failed`);
- channel; original start text and source timezone; canonical start/end UTC;
  requested duration, interval, and chunk duration;
- every planned chunk with UTC bounds, source segment bounds, status, frame
  count, and a safe error category when applicable;
- every requested timestamp with status (`written`, `skipped_gap`, or failed
  extraction), frame relative path when written, and its preserved UTC source
  timestamp;
- coverage gaps and totals for requested, written, skipped, and failed frames;
- tool/version fields that enable later inspection without exposing command
  arguments, URLs, hosts, credentials, or ffmpeg stderr.

Per-chunk JSON may mirror the chunk entry for interruption-tolerant progress,
but the manifest remains the authoritative package index. This is sufficient
for inspection of a partial package; it does not promise resume support. A
later implementation may add resumability only after it defines how it verifies
manifest compatibility and completed frame integrity.

## Timestamp and timezone handling

The command retains three distinct facts: the user-entered wall-clock start and
IANA source timezone, the canonical UTC range used for SDK and replay planning,
and each SDK segment's epoch-second endpoints converted to UTC. Frame names use
UTC. The manifest preserves both the source-time representation and UTC values.
No local machine timezone may influence parsing, directory names, or sampling.

## Progress reporting

The command should report safe aggregate progress: validated request, recording
coverage discovered, `chunk X/Y`, frames written versus scheduled, and final
package state. It must not print replay URLs, NVR hosts, credentials, ffmpeg
arguments, or temporary paths. Gaps and partial failures must be summarized by
UTC range and safe category.

## Interruption, failure, and cleanup

- **User cancellation:** stop scheduling new chunks, terminate the active
  ffmpeg process, remove its incomplete temporary MP4 and frame, write a
  credential-free `cancelled` manifest when possible, and preserve already
  completed frames in a clearly marked partial package.
- **ffmpeg failure:** remove the incomplete temporary files for that chunk,
  mark its safe failure category, stop further chunks, write a `failed`
  manifest when possible, and preserve earlier completed frames as a partial
  package for inspection.
- **No recording coverage:** report an unavailable-recording error and remove
  the empty staging directory; do not create an empty final package.
- **Gaps between segments:** never synthesize or shift frames. Complete other
  covered chunks, record skipped timestamps and gap bounds, and finish as
  `completed_with_gaps` with a visible warning.
- **Failure after earlier chunks:** preserve earlier finalized frames and their
  metadata in the partial package, but do not claim it is complete or
  resumable.
- **Existing output path:** fail without writing into, deleting, or renaming
  the existing path.

Partial packages must use a deterministic safe base name plus an
invocation-unique `-partial` suffix to avoid overwriting another failed run.
Only files created by the active invocation may be removed during cleanup.

## Credential and URL security

Replay URLs remain credential-free application values. RTSP credentials are
provided to ffmpeg in memory only, following the current replay-extraction
boundary. Exceptions, manifests, chunk files, terminal progress, and logs must
redact or omit credentials, hosts, replay URLs, ffmpeg command lines, and raw
stderr. Frames and manifests are surveillance artifacts and remain under the
ignored artifact root; they must not be committed.

## SDK boundary

`tp-link-vigi-sdk` remains a dependency. This phase does not modify the SDK or
require a new SDK API. VIGI Vision owns public-API orchestration across segments,
bounded ffmpeg sampling, artifact creation, and progress reporting. Any
observed inability to enumerate, distinguish, or replay required public
segments is a documented SDK constraint, not a reason to call private SDK APIs
or move AI behavior into the SDK. This is a session boundary, not a permanent
prohibition on a separately justified SDK enhancement.

## Test strategy

- Unit-test duration/timezone parsing, whole-second schedule generation, UTC
  conversion, segment-to-coverage planning, chunk boundaries, gap handling,
  naming, manifest serialization, and redaction.
- Use public-SDK-shaped fakes to test multi-segment planning, no coverage,
  overlapping segments, and SDK data errors without NVR access.
- Test ffmpeg orchestration with a narrow runner seam: extraction failure,
  cancellation, cleanup ownership, and no sensitive values in surfaced errors.
- Add CLI tests for required options, NVR-only rejection, progress summaries,
  existing-output refusal, and final versus partial package observable state.
- Add a controlled end-to-end fixture with local media or a recorded public
  contract fixture to prove bounded chunk extraction and timestamps without
  requiring a real NVR in the normal test suite.

## Acceptance criteria for the first implementation

1. A valid NVR range is resolved through public SDK recording information.
2. Work is split into bounded chunks that do not cross unsupported coverage.
3. Frames are extracted at the requested cadence for covered timestamps and
   preserve useful UTC source timestamps in filenames and metadata.
4. A structured credential-free manifest describes frames, chunks, coverage,
   gaps, and final state.
5. Safe progress shows the work performed without leaking URLs or credentials.
6. Cancellation and failures clean only invocation-owned temporary output and
   leave an inspectable partial package when earlier work succeeded.
7. No OpenAI request, profile analysis, report, event conclusion, or SDK
   modification occurs.

## Scope boundary for the first implementation

The first coding session implements range resolution, bounded chunk planning,
interval frame extraction, timestamp preservation, structured artifacts,
progress, and safe partial cleanup only. It stops before every capability listed
under Non-goals.

## Open questions for implementation

- What maximum replay chunk duration is reliable on the target NVR and should
  it become a tested default after measurement?
- Can the existing replay extractor efficiently use a caller-owned staging
  directory and expose cancellation without changing the SDK?
- What seek/timestamp accuracy can ffmpeg guarantee for copied replay MP4s,
  and should actual decoded timestamps be recorded alongside requested times?
- Which public SDK segment fields, if any, distinguish adjacent continuous
  segments from a true gap?
- Should a future resume command reuse partial manifests, and what integrity
  checks would make that safe?
