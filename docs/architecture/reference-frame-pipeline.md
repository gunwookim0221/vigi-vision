# Reference Frame Pipeline

This document describes how the reference-frame subsystem acquires, decodes,
persists, reuses, and serves durable recorded frames through its service, API,
candidate-set orchestration, and browser shell.

## Purpose

The reference-frame subsystem produces one durable, credential-free JPEG for a
requested NVR channel and time. It preserves the difference between the
requested time, the replay interval the NVR was asked for, and the time of the
decoded frame. Timing is reported as `measured_clip_relative`; the system does
not claim absolute source-time accuracy.

## Components

| Component | Module | Responsibility |
|-----------|--------|----------------|
| `ReferenceFrameService` | `reference_frame_service.py` | Single-frame orchestration: validate ??segment ??window ??decode ??stage ??promote |
| `FfmpegReferenceFrameDecoder` | `reference_frame_decoder.py` | Replay-based decoder: ffprobe PTS selection ??ffmpeg frame extraction |
| `FfmpegDirectReferenceFrameAcquirer` | `reference_frame_direct.py` | Direct decoder: tee/framemd5 pipeline with streaming frame selection |
| `ReferenceFrameArtifactStore` | `reference_frame_artifacts.py` | Staged artifact lifecycle: begin ??finalize/discard |
| `ReferenceFrameResourceStore` | `reference_frame_resources.py` | Completed resource verification and reuse |
| `ReferenceFrameCandidateSetService` | `reference_frame_candidate_service.py` | Serial candidate-set orchestration |
| `create_reference_frame_app` | `reference_frame_api.py` | FastAPI application composition and routes |
| `install_reference_frame_web_ui` | `reference_frame_web_ui.py` | Static browser shell mount |

## Single-frame service pipeline

`ReferenceFrameService.execute_or_resolve()` is the core orchestration. It
uses Protocol-typed boundaries so the composition root selects concrete
implementations.

```
ReferenceFrameRequest
  (channel_id, requested_time_text, source_timezone, frame_selection_policy)
       ??       ??1. Channel inventory check (optional)
   ?붴?? Verify channel exists; warn if offline
       ??       ??2. RecordingPlanner.find_covering_segment(channel_id, requested_time_utc)
   ?붴?? SDK segment search ??selected RecordingSegment
       ??       ??3. build_reference_replay_window(request, segment)
   ?붴?? Compute 6-second extraction window: [requested - 2s, requested + 4s]
       clipped to the selected segment boundaries
       ??       ??4. RecordingPlanner.plan_for_segment(segment, window)
   ?붴?? Credential-free ReplayRequest
       ??       ??5. Completed resource check (optional)
   ?붴?? If a compatible resource exists ??return REUSED resolution
       ??       ??6. ReferenceFrameArtifactStore.begin(request, segment)
   ?붴?? Create staging directory, claim in-flight resource ID
       ??       ??7. Decode frame (one of two paths):
   ?쒋?? Direct acquirer (if configured):
   ??  ?붴?? FfmpegDirectReferenceFrameAcquirer.acquire()
   ??      ??tee/framemd5 pipeline ??streaming selection ??validated JPEG
   ?붴?? Replay-based decoder (fallback):
       ?쒋?? ReplayExtractor.extract() ??temporary MP4
       ?붴?? FfmpegReferenceFrameDecoder.decode()
           ??ffprobe PTS ??frame selection ??ffmpeg extraction ??validated JPEG
       ??       ??8. Build manifest with evidence
   ?붴?? ReferenceFrameManifest (request, segment, window, evidence)
       ??       ??9. Session.finalize(manifest)
   ?붴?? Write manifest.json, promote staging to durable resource
       ??       ??ReferenceFrameResolution (result, outcome=CREATED)
```

### Failure handling

- If decoding fails, `session.discard()` removes the staging directory.
- If a replay clip was extracted and decoding succeeded, the clip is removed
  before finalization.
- If a replay clip was extracted and decoding failed, the clip is removed in
  the `finally` block.

---

## Replay-based decoder

`FfmpegReferenceFrameDecoder` in
[reference_frame_decoder.py](../../src/vigi_vision/reference_frame_decoder.py)
works on a completed local MP4:

1. **ffprobe**: run `-show_frames` on the local MP4 to list all decoded video
   frame PTS values.
2. **Select**: find the frame whose PTS is nearest to the target offset, with
   an earlier-frame tie-break. Verify PTS values are monotonically
   non-decreasing.
3. **ffmpeg**: extract exactly that frame index using
   `select=eq(n\\,<index>)` and write a JPEG.
4. **Validate**: ffprobe the output JPEG to verify it is a valid `mjpeg`
   image with positive dimensions.

Reports timing precision as `measured_clip_relative`.

---

## Direct decoder

`FfmpegDirectReferenceFrameAcquirer` in
[reference_frame_direct.py](../../src/vigi_vision/reference_frame_direct.py)
decodes frames directly from the NVR replay stream without producing a
complete temporary MP4. It stops after selecting and validating one frame.

### FFmpeg tee/framemd5 pipeline

The direct decoder runs a single ffmpeg process with:

- Input: authenticated RTSP replay URL with `-rtsp_transport tcp`
- Duration cap: `-t <window_duration>` on the output side
- PTS normalization: `-vf setpts=PTS-STARTPTS` with `-vsync 0 -enc_time_base -1`
- Output via `-f tee`:
  - `[f=image2:atomic_writing=1:start_number=0]candidate-%08d.jpg` ??each
    decoded frame as a sequentially numbered JPEG
  - `[f=framemd5]pipe:\1` ??frame timing metadata on stdout

### Streaming timing reader

Frame timing is parsed on a background thread from the ffmpeg stdout pipe:

1. Parse the `#tb 0: <numerator>/<denominator>` line to establish the time
   base as a `Decimal` ratio.
2. For each subsequent non-comment line, parse the framemd5 record to extract
   PTS and compute `local_pts_seconds = pts * numerator / denominator`.
3. Enqueue `FrameTiming(ordinal, local_pts_seconds)` for the main-thread
   selector.

A separate thread drains stderr without retention.

### Adjacent-frame selection

The main thread consumes `FrameTiming` records from the queue and applies the
`select_adjacent` algorithm:

- Wait for each frame timing and verify its corresponding JPEG file exists on
  disk.
- If the current frame's PTS is still before the target offset, record it as
  `previous` and continue.
- Once the current frame's PTS is at or past the target, compare it with the
  previous frame. Select whichever is nearest; on ties, select the earlier PTS.
- If the process exits naturally with only before-target frames, select the
  last frame.
- PTS monotonicity is enforced; a decrease raises `ReferenceFrameDecodeError`.

After selection, the selected candidate JPEG is atomically moved to the output
path, the ffmpeg process is terminated, both pipes are drained and closed, and
the temporary candidate directory is removed.

### Validation

After selection and publication, the output JPEG is validated using ffprobe
(same `validate_jpeg` function as the replay-based decoder): verify it is a
valid `mjpeg` codec with positive dimensions.

### Timeout

The direct decoder enforces a timeout of
`window_duration + 30s startup allowance`. If exceeded, the ffmpeg process is
terminated and `ReferenceFrameDecodeTimeoutError` is raised.

### Process lifecycle

- `spawn_process`: start ffmpeg with both stdout and stderr as text pipes.
- `stop_process`: terminate; if the process does not exit within the grace
  period, kill.
- `close_pipes`: close both pipes after reader threads have joined.
- `remove_partial`: delete the output JPEG if decoding did not complete.

---

## Artifact lifecycle

Reference-frame artifacts live under `artifacts/reference-frames/`.

### Storage structure

```
artifacts/reference-frames/
?붴?? <resource-id>/
    ?쒋?? frame.jpg           # Durable validated JPEG
    ?붴?? manifest.json       # Credential-free metadata
```

### Resource identity

The resource ID is deterministically derived from the request parameters and
the selected segment. Two requests that produce the same resource ID are
considered compatible and can reuse the same artifact.

### Staging protocol

`ReferenceFrameArtifactStore` implements a staged write protocol:

1. **`begin(request, segment)`** ??create a staging directory and claim the
   resource ID. Raises `ReferenceFrameArtifactConflictError` if another
   invocation is already writing the same resource.
2. **Decode** ??the decoder writes `frame.jpg` into the staging directory.
3. **`finalize(manifest)`** ??write `manifest.json` and atomically promote
   the staging directory to the durable resource path.
4. **`discard()`** ??remove the staging directory on failure (called from
   the service's `finally` block).

### Resource reuse

`ReferenceFrameResourceStore` verifies completed resources:

1. Derive the resource ID from the current request and segment.
2. Check if the resource directory exists with a valid `frame.jpg` and
   `manifest.json`.
3. Parse and validate the manifest against strict schema rules (matching
   schema version, `"completed"` status, matching resource ID, valid JPEG).
4. Verify compatibility: same channel, segment identity, extraction window,
   frame selection policy, and minimum generation policy version.
5. If compatible, return the existing result as a `REUSED` resolution.
6. If the manifest is corrupt or incompatible, raise the appropriate error.

### Manifest schema

The manifest is a credential-free JSON document containing:

- Schema version, generation policy version, resource ID, status
- Channel, requested time (text and UTC), source timezone
- Selected segment identity and UTC boundaries
- Extraction window UTC boundaries
- Frame selection policy
- JPEG filename, width, height
- Decoded local PTS (clip-relative seconds)
- Timing precision status (`measured_clip_relative`)
- Warnings array

---

## Candidate-set orchestration

`ReferenceFrameCandidateSetService` in
[reference_frame_candidate_service.py](../../src/vigi_vision/reference_frame_candidate_service.py)
wraps the single-frame service to process multiple candidates for a reference
time with offsets.

### Data flow

```
ReferenceFrameCandidateSetRequest
  (channel_id, reference_time, source_timezone, offsets_seconds[])
       ??       ??For each candidate (serial, preserving order):
  ?쒋?? Validate candidate time is not in the future
  ?쒋?? Construct ReferenceFrameRequest with offset-adjusted time
  ?쒋?? ReferenceFrameService.execute_or_resolve(request)
  ?붴?? Capture result (success with resolution) or failure (safe error code + message)
       ??       ??ReferenceFrameCandidateSetResult
  (request, ordered items[], summary{created, reused, failed})
```

### Failure isolation

Each candidate is executed independently. Known media failures
(`ReferenceFrameError`, `NvrRequestError`, `RecordingUnavailableError`,
`ReplayTimeoutError`, etc.) are caught per-candidate and recorded as safe
error codes and messages using the API error redaction boundary. A failure
in one candidate does not prevent the others from executing.

---

## API layer

The reference-frame API is a local FastAPI application served by uvicorn.

### Routes

| Method | Path | Handler |
|--------|------|---------|
| GET | `/` | Static browser shell (`index.html`) |
| GET | `/static/*` | Static CSS/JS assets |
| POST | `/api/v1/reference-frames` | Create or resolve a single reference frame |
| GET | `/api/v1/reference-frames/{resource_id}/image` | Retrieve a durable JPEG |
| POST | `/api/v1/reference-frame-candidate-sets` | Create or reuse bounded candidates |

### Concurrency

All service operations run off the event loop using `anyio.to_thread.run_sync`
with a shared `CapacityLimiter(1)`. This serializes all NVR access and ffmpeg
invocations on one worker thread.

### Error redaction

`domain_error()` in
[reference_frame_api_errors.py](../../src/vigi_vision/reference_frame_api_errors.py)
maps every domain exception to a fixed safe `(code, message, http_status)`
triple. Exception text, tracebacks, and internal details are never exposed in
API responses. A broad exception handler at both route level and as a global
exception handler ensures no unredacted errors reach the client.

### Composition

`create_reference_frame_app_from_environment()` is the ASGI factory:

1. Load `CaptureSettings` from `.env`
2. Resolve ffmpeg and ffprobe paths
3. Create artifact directory
4. Connect `RecordingPlanner` to the SDK
5. Create `ReferenceFrameService` with all boundaries:
   - `RecordingPlanner` (segment planning)
   - `ReplayExtractor` (MP4 extraction, with optional progress diagnostics)
   - `FfmpegReferenceFrameDecoder` (replay-based decoder)
   - `FfmpegDirectReferenceFrameAcquirer` (direct decoder)
   - `ReferenceFrameArtifactStore` (staging)
   - `ReferenceFrameResourceStore` (reuse)
   - `SdkNvrGateway` (channel inventory)
6. Mount web UI and API routes
7. Return the configured `FastAPI` application

---

## Browser shell

The browser shell is a static HTML/CSS/JS application mounted at the
FastAPI application root. It is implemented in
[reference_frame_web/](../../src/vigi_vision/reference_frame_web/) and
installed by
[reference_frame_web_ui.py](../../src/vigi_vision/reference_frame_web_ui.py).

- `GET /` serves `index.html`.
- `GET /static/*` serves CSS and JS assets from the `reference_frame_web`
  package directory.
- The shell submits the candidate-set API request after the operator applies a
  whole-second local time and timezone, and renders ordered safe result facts
  with successful thumbnails.
- A loaded successful candidate can be selected exactly once for a larger
  transient preview, then edited with the source-pixel ROI workspace (manual
  pointer/keyboard editing and optional assisted suggestion). Phase 6
  confirmation/reconfirmation persists the reviewed candidate, ROI, and
  investigation facts; pre-confirmation selection and draft state remain
  browser-memory only.
- The shell has no caller for the Phase 7E execution boundary. Search remains
  CLI-owned, the Phase 7E HTTP POST is intentionally disabled, and no browser
  status/result or Phase 8 review-media surface is present.
