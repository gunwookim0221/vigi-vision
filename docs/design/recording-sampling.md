# Recording Sampling

## Status

`sample-recording` is implemented as the first generic, bounded NVR
frame-sampling command. It does not guarantee that an NVR has coverage for a
requested range or that ffmpeg decodes an exact source frame at every requested
second.

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

## CLI contract

The implemented surface is:

```powershell
vigi-vision sample-recording `
  --channel 3 `
  --start "2026-07-26 18:00:00" `
  --duration 2h `
  --interval 5s
```

| Input | Implemented contract |
| --- | --- |
| `--channel` | Required positive NVR channel ID. |
| `--start` | Required naive wall-clock timestamp in `YYYY-MM-DD HH:MM:SS`; it is not UTC unless `--timezone UTC` is supplied. |
| `--timezone` | Optional source timezone for `--start`; default `Asia/Seoul`. Version 1 supports the project-established `Asia/Seoul` and `UTC` values only because the runtime has no bundled general IANA timezone database. |
| `--duration` | Required positive whole-second duration using an integer plus `s`, `m`, or `h` (`5s`, `10m`, `2h`). |
| `--interval` | Required positive whole-second cadence using the same grammar. Samples are anchored at requested UTC start and never reset at a segment or chunk boundary. |
| `--chunk-duration` | Optional positive whole-second bound using the same grammar; default `10m`. It must be at least the interval. Ten minutes is the conservative initial cap used by the documented invocation and keeps each replay extraction bounded. |
| `--output-dir` | Optional artifact parent; default `artifacts/recording-samples/`, consistent with existing durable artifact roots. |

The command is NVR-only and rejects an IPC source before SDK or ffmpeg work.
It must not accept a profile or any OpenAI option. `analyze-recording` still
accepts only whole seconds ending in `s`; sampling intentionally owns its
extended duration parser.

## Input validation

- Parse the source timestamp and supported source timezone at the CLI boundary,
  then create a canonical whole-second UTC range. The supported v1 zones have
  no daylight-saving ambiguity.
- Require positive channel, duration, interval, and chunk duration values.
- Require `chunk-duration >= interval`; reject a range whose arithmetic would
  exceed the implementation's documented operational limit, if one is added.
- Require a writable output parent and refuse an existing final package path;
  never merge into or delete an existing user directory.
- Validate that frame names and manifest identifiers are derived from safe
  channel IDs and UTC timestamps, never hostnames or replay URLs.

## Recording planning and segmentation

VIGI Vision uses the same public SDK boundary already used by the recording
retrieval layer: NVR-local recording-day discovery, public recording-result
pages, epoch-second segment conversion, and public credential-free replay-URL
construction. Existing behavior establishes that the configured NVR recording
calendar is Asia/Seoul and SDK segment timestamps are Unix epoch seconds;
sampling must retain the requested source timezone separately from canonical
UTC facts.

`SamplingCoverageResolver` enumerates every segment intersecting the requested UTC range,
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

For each planned chunk, VIGI Vision obtains a bounded temporary replay MP4
through the existing public-SDK replay path, then run ffmpeg against that local
file to extract only the scheduled frames for the chunk. The temporary MP4 is
removed after its frames and chunk metadata have been handled. A temporary
frame file is renamed into the package only after successful extraction.

The implementation preserves the scheduled UTC timestamp for each frame. It
does not determine decoded presentation timestamps, so the manifest records the
requested source timestamp rather than claiming exact ffmpeg seek accuracy.

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
```

The implemented final directory name is
`channel-<channel>_<start-utc>_<end-utc>`. Chunks are
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
  extraction), frame relative path when written, its preserved UTC source
  timestamp, and its containing source-coverage interval when covered;
- coverage gaps and totals for requested, written, skipped, and failed frames;
- no replay URLs, hosts, credentials, ffmpeg command lines, or stderr.

The manifest remains the authoritative package index. This is sufficient
for inspection of a partial package; it does not promise resume support. A
later implementation may add resumability only after it defines how it verifies
manifest compatibility and completed frame integrity.

## Timestamp and timezone handling

The command retains three distinct facts: the user-entered wall-clock start and
supported source timezone, the canonical UTC range used for SDK and replay planning,
and each SDK segment's epoch-second endpoints converted to UTC. Frame names use
UTC. The manifest preserves both the source-time representation and UTC values.
No local machine timezone may influence parsing, directory names, or sampling.

## Progress reporting

The command reports safe `chunk X/Y` progress and a final package directory,
status, written-frame count, and skipped-frame count. It does not print replay
URLs, NVR hosts, credentials, ffmpeg arguments, or temporary paths. Gaps are
visible through the final `completed_with_gaps` status and warning.

## Interruption, failure, and cleanup

- **User cancellation:** stop scheduling new chunks, remove the current replay
  MP4 through its existing cleanup boundary, write a
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

Partial packages use a deterministic safe base name plus an invocation-unique
`-partial` suffix to avoid overwriting another failed run.
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

- Automated tests cover duration/timezone parsing, stable source-anchored
  schedules, chunk boundaries, gaps, duplicate-boundary prevention, help,
  package naming, manifest redaction, existing-output refusal, missing coverage,
  frame failure cleanup, and cancellation partial packages.
- Tests use public-SDK-shaped and ffmpeg-shaped fakes. They do not require a
  real camera or NVR.
- A future controlled local-media fixture can measure actual decoded timestamp
  accuracy without requiring a real NVR in the normal test suite.

## Acceptance criteria for this implementation

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

## Implemented scope boundary

This implementation covers range resolution, bounded chunk planning, interval
frame extraction, requested timestamp preservation, structured artifacts,
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
