# Recording and Media Pipelines

This document describes the recording retrieval, replay extraction, video
analysis, investigation, and sampling pipelines: how they are organized, how
data flows through them, and how they compose shared boundaries.

## Recording retrieval

Recording retrieval resolves a UTC time window into a temporary local MP4
extracted from an NVR replay stream. It is used by `analyze-recording`,
the investigation pipeline, the sampling pipeline, and the reference-frame
pipeline.

### Components

| Component | Module | Responsibility |
|-----------|--------|----------------|
| `RecordingWindow` | `recording_models.py` | Validated whole-second UTC interval for one channel |
| `RecordingSegment` | `recording_models.py` | One NVR segment with epoch seconds and UTC endpoints |
| `ReplayRequest` | `recording_models.py` | Credential-free replay plan (window + RTSP URL) |
| `RecordingPlanner` | `recording.py` | SDK segment search ??overlap verification ??replay planning |
| `ReplayExtractor` | `replay.py` | FFmpeg RTSP extraction ??temporary MP4 |
| `ReplayClip` | `replay.py` | Caller-owned temporary MP4 with `remove()` cleanup |

### Data flow

```
RecordingWindow (channel, start_utc, end_utc)
       ??       ??RecordingPlanner.plan(window)
  ?쒋?? SDK recording search (NVR-local calendar day)
  ?쒋?? segment epoch ??UTC conversion
  ?쒋?? overlap verification
  ?붴?? replay URL construction (UTC lowercase format)
       ??       ??ReplayRequest (window + credential-free RTSP URL)
       ??       ??ReplayExtractor.extract(request)
  ?쒋?? RTSP credentials added in memory
  ?쒋?? ffmpeg subprocess: -rtsp_transport tcp, -t <duration>, video-only MP4
  ?쒋?? timeout: duration + 30s startup + 10s finalization
  ?붴?? partial file removed on failure
       ??       ??ReplayClip (temporary_mp4_path, remove())
```

### Recording planner internals

`RecordingPlanner` uses the public SDK to search NVR recordings:

1. Convert the requested UTC instant to the NVR-local calendar day (KST,
   UTC+09:00).
2. Reserve an SDK recording-search process (lazily, once per planner lifetime).
3. Page through SDK recording results for that day.
4. Convert each segment's epoch-second endpoints to UTC.
5. Select the segment that contains the requested instant.
6. Build a credential-free RTSP replay URL using the NVR host, channel,
   and whole-second UTC time format (`YYYYMMDDtHHMMSSz`).

The planner retains one SDK search process ID for its authenticated lifetime;
all segment searches reuse that process.

### Replay extraction internals

`ReplayExtractor` runs ffmpeg as a subprocess to extract a bounded temporary
MP4:

- Uses RTSP-over-TCP (`-rtsp_transport tcp`).
- Applies `-t <window duration>` as an output-duration cap (not an input seek).
- Copies video only (`-c:v copy -an`).
- Timeout is `duration + 30s startup allowance + 10s finalization margin`.
- Distinguishes RTSP 401 (authentication), RTSP 454 (recording unavailable),
  timeout, and generic extraction failures.
- Partial MP4 files are removed for all failure modes.

### Replay progress diagnostics

When `VIGI_REPLAY_PROGRESS_DIAGNOSTICS=true` is set, `ReplayExtractor`
delegates to `run_ffmpeg_with_progress` in
[replay_progress.py](../../src/vigi_vision/replay_progress.py). This runs
ffmpeg with its machine-readable progress stream, collects bounded aggregates
in `ReplayProgressDiagnostics` (frame count, output media time, output size,
progress/stall ages), and logs only an allowlisted summary on timeout.

When `VIGI_REPLAY_TIMEOUT_DIAGNOSTIC_DIRECTORY` is set, timeout partials
are copied (not moved) to that directory with a diagnostic filename for
local-only inspection.

Neither diagnostic changes timeout or retry behavior, and neither exposes
progress data through the API, browser, manifests, or artifacts.

---

## Local video analysis

The `analyze-video` and `analyze-recording` commands use a shared local video
analysis pipeline that probes a bounded MP4, extracts sparse representative
frames, sends them to OpenAI, and renders a temporal business report.

### Components

| Component | Module | Responsibility |
|-----------|--------|----------------|
| `VideoMetadata` | `video.py` | Validated local video facts from ffprobe |
| `VideoService` (internal) | `video.py` | Probe ??sample ??extract ??return frames |
| `TemporalAnalysis` | `analysis.py` | OpenAI sparse-frame temporal analysis |
| `TemporalProfileDefinition` | `temporal_profiles.py` | Profile-specific prompts and output schemas |

### Data flow

```
Local MP4 path + analysis profile
       ??       ??ffprobe: validate duration (??0s), resolution, codec
       ??       ??Compute 2??0 sample points at ~3s intervals
       ??       ??ffmpeg: extract each sample as a scaled JPEG (??024px edge)
       ??       ??OpenAI: analyze ordered frames with profile-specific prompt
       ??       ??Temporal business report (terminal output)
       ??       ??Cleanup: remove temporary extracted frames
```

### Integration with recording retrieval

`analyze-recording` composes recording retrieval with video analysis:

1. `RecordingPlanner.plan()` ??`ReplayRequest`
2. `ReplayExtractor.extract()` ??`ReplayClip`
3. Video analysis service processes `ReplayClip.temporary_mp4_path`
4. `ReplayClip.remove()` in a `finally` block

The retrieval layer is unaware of analysis profiles or OpenAI.

---

## Investigation pipeline

The investigation pipeline collects replay clips from multiple NVR channels
for a specific scenario and time, builds durable artifact packages with anchor
snapshots and manifests, and reports the result.

### Components

| Component | Module | Responsibility |
|-----------|--------|----------------|
| `InvestigationPlan` | `investigation.py` | Pure planning: anchor time ??UTC windows per channel/role |
| `InvestigationCollector` | `investigation_collection.py` | Serial per-item replay collection |
| `InvestigationArtifactBuilder` | `investigation_artifacts.py` | Clip preservation, anchor snapshots, manifest |
| `InvestigationService` | `investigation_service.py` | Plan ??Collect ??Artifacts orchestration |
| `InvestigationManifest` | `investigation_manifest.py` | Credential-free JSON manifest schema |

### Data flow

```
InvestigationRequest (anchor_time, scenario, camera_assignments)
       ??       ??InvestigationPlan.plan()
  ?쒋?? Parse Asia/Seoul KST input ??canonical UTC anchor
  ?쒋?? Expand scenario role rules over assigned channels
  ?붴?? Produce ordered RecordingWindow values (pure, no I/O)
       ??       ??InvestigationCollector.collect(plan)
  ?쒋?? For each planned item (serial):
  ??  ?쒋?? RecordingPlanner.plan(window) ??ReplayRequest
  ??  ?쒋?? ReplayExtractor.extract(request) ??ReplayClip
  ??  ?붴?? Classify result: success or typed failure
  ?붴?? Return ordered CollectionResult with caller-owned clips
       ??       ??InvestigationArtifactBuilder.build(collection)
  ?쒋?? Create investigation directory under artifacts/investigations/
  ?쒋?? For each successful collection item:
  ??  ?쒋?? Copy replay clip to package directory
  ??  ?쒋?? Extract anchor snapshot (ffmpeg, one frame at anchor offset)
  ??  ?붴?? Remove original temporary clip
  ?쒋?? Write credential-free manifest.json
  ?붴?? Return InvestigationResult
       ??       ??InvestigationResult (plan, collection, manifest, artifact_directory)
```

### Artifact structure

```
artifacts/investigations/<investigation-id>/
?쒋?? manifest.json           # Credential-free investigation manifest
?쒋?? <role>-<channel>-<timestamp>.mp4   # Preserved replay clips
?붴?? <role>-<channel>-<timestamp>.jpg   # Anchor snapshot JPEGs
```

### Failure handling

- Each collection item fails independently; partial results are preserved.
- Known failure categories: `recording_unavailable`, `authentication_failed`,
  `extraction_failed`, `timeout`, `unexpected_error`.
- Failed items are recorded in the manifest without media artifacts.

---

## Recording sampling pipeline

The sampling pipeline extracts generic timestamped frames from long NVR
recording ranges at a configurable cadence, producing a durable JPEG package
with a credential-free manifest.

### Components

| Component | Module | Responsibility |
|-----------|--------|----------------|
| `SamplingRequest` | `sampling.py` | Validated sampling parameters (channel, range, interval) |
| `build_sampling_plan` | `sampling.py` | Compute chunks and sample points from coverage |
| `SamplingService` | `sampling_service.py` | Coverage ??chunks ??replay ??extract ??write |
| `SamplingArtifactWriter` | `sampling_artifacts.py` | Timestamped JPEG package and manifest |

### Data flow

```
SamplingRequest (channel, start, duration, interval, chunk_duration)
       ??       ??SamplingService.execute()
  ?쒋?? Resolve SDK recording coverage for the requested range
  ?쒋?? Compute sampling plan: divide range into replay chunks, assign sample points
  ?쒋?? For each chunk (serial):
  ??  ?쒋?? RecordingPlanner ??ReplayRequest
  ??  ?쒋?? ReplayExtractor ??ReplayClip
  ??  ?쒋?? For each sample point in chunk:
  ??  ??  ?붴?? ffmpeg: extract one JPEG at sample offset
  ??  ?붴?? ReplayClip.remove()
  ?쒋?? Write credential-free manifest
  ?붴?? Return SamplingResult with gap reporting
       ??       ??Artifact package: artifacts/recording-samples/<package>/
?쒋?? manifest.json
?붴?? <timestamp>.jpg (one per sample point)
```

### Shared boundaries

The sampling pipeline reuses the same `RecordingPlanner` and
`ReplayExtractor` boundaries used by the investigation pipeline and
`analyze-recording`. It adds its own coverage resolution, chunk computation,
and artifact writing without depending on investigation or analysis modules.
