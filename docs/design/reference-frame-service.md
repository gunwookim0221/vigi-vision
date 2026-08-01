# Reference Frame Service and Minimal HTTP Boundary

## Status

**Status: Phase 2B internal service, Phase 3B synchronous HTTP transport, Phase
3C hardening/real-NVR validation, and Phase 4A candidate-set processing are
complete. Phase 4B provides the loopback shell and Phase 4C-1/4C-2 now displays
ordered candidate thumbnails with transient exactly-one frontend selection;
Phase 4C-3 polishes explicit applied time and indeterminate busy feedback;
Phase 5-1/5-2 adds a transient frontend-only one-rectangle ROI workspace over
the selected image, including bounded move/resize, reset/recreate, keyboard
editing, and an immutable Phase 6 handoff snapshot. ROI persistence,
confirmation, and object comparison remain deferred.**

The implemented internal slice validates source time into UTC, resolves a
deterministic covering segment through `RecordingPlanner`, constrains its replay
window to that segment, extracts a bounded temporary clip, selects the nearest
ffprobe-reported local PTS with an earlier-frame tie-break, extracts that exact
decoded-frame index, validates the JPEG with ffprobe, and publishes a
credential-free JPEG plus manifest. It reports only
`measured_clip_relative` timing for the current decoder; the real-NVR check did
not establish an exact or absolute source-frame timestamp.
When a host lacks an IANA timezone database, the established default
`Asia/Seoul` input uses a fixed KST (+09:00) fallback; other requested zones
still require valid zoneinfo data.

This document specifies the implemented internal boundary used by the approved
[object-disappearance investigation](object-disappearance-investigation.md):
retrieve one reviewable recorded frame for a requested NVR channel and time.
The public HTTP transport is documented separately; this service still does not
implement frontend candidate selection, ROI selection, presence classification,
temporal search, or review-clip generation.

## Purpose

The future UI needs a durable, trustworthy-enough reference image before a user
can select an object region. The service must preserve the difference between
the time a user requested, the replay interval the NVR was asked for, and the
time of the decoded frame. It must never silently label an estimated frame as
an exact source-time frame.

The first product use is a single user-selected object on a single fixed NVR
channel. The service and its terminology remain general: it retrieves a
reference frame, not a shoe frame or a disappearance result.

## Relationship to object-disappearance investigation

This service implements only the first two media steps of the intended flow:

```text
channel and reference time
  -> recorded reference frame
  -> later UI display and ROI selection
  -> later presence search and review clip
```

The returned JPEG will later support ROI coordinate mapping, crop confirmation,
investigation reproducibility, and evidence review. It does not itself decide
whether an object is present, absent, moved, occluded, stolen, or associated
with a person.

## Existing reusable foundation

The implementation reuses the following existing code and contracts rather
than creating parallel recording or ffmpeg workflows.

| Existing component | Actual responsibility | Reuse in this slice |
| --- | --- | --- |
| `recording.RecordingWindow` | Validates positive whole-second UTC intervals for one channel. | Build the bounded replay window after a source segment is selected. |
| `recording.RecordingSegment` | Retains NVR-local recording day, raw epoch endpoints, and UTC endpoints. | Carry a safe selected-segment reference into the result and manifest. |
| `recording.RecordingPlanner` | Authenticates through the public SDK, discovers recording days and results, and builds a credential-free `ReplayRequest` for an overlapping window. | Reuse its public-SDK search and replay-URL construction; expose the selected segment through the narrow refactor described below. |
| `replay.ReplayExtractor` / `ReplayClip` | Creates one bounded temporary MP4 over RTSP/TCP, applies client-side `-t`, maps safe replay failures, and owns removable temporary media. | Extract the planned local MP4 and always call `ReplayClip.remove()` after decoding or failure. |
| `sampling.SamplingRequest` and `parse_sampling_request` | Parse current `Asia/Seoul` or `UTC` wall-clock input into whole-second UTC values. | Reuse their timezone conventions, not their range-only request shape. |
| `sampling_service.SamplingCoverageResolver` | Enumerates all covered ranges and builds bounded replay requests. | Reuse its public-SDK discovery pattern as evidence; do not use its lossy `RecordingCoverage` output when selected-segment metadata is required. |
| `investigation_snapshot.FfmpegAnchorSnapshotExtractor` | Extracts one JPEG from a local MP4 at an integer offset and removes partial JPEGs. | Reuse its local-MP4, bounded, redacted ffmpeg conventions, but not its integer-only API or timing claims. |
| `video.VideoSampler` and `VideoMetadata` | Uses ffprobe for local stream dimensions and ffmpeg for temporary frame extraction. | Reuse the ffprobe/ffmpeg process-boundary style and injected runners; do not use its 30-second analysis sampling policy for a single reference frame. |
| `sampling_artifacts.SamplingArtifactWriter` | Uses invocation-owned staging directories, credential-free manifests, atomic promotion where possible, and overwrite protection. | Reuse these artifact-lifecycle principles; its multi-frame sampling manifest is not the reference-frame manifest. |
| `nvr.SdkNvrGateway.channels` / `Channel` | Retrieves safe NVR channel inventory via the public SDK. | Optionally validate an existing channel; current offline state must not by itself reject historical recordings. |
| `config.CaptureSettings` / `load_capture_settings` | Loads NVR and ffmpeg configuration without an OpenAI key. | Compose the future NVR-only service without OpenAI configuration. |

Current safe errors to propagate or translate include `RecordingWindowError`,
`RecordingDataError`, `RecordingUnavailableError`, `NvrRequestError`,
`ReplayAuthenticationError`, `ReplayUnavailableError`, `ReplayTimeoutError`,
`ReplayExtractionError`, `FfmpegUnavailableError`, and
`AnchorSnapshotError`. Existing errors already redact credentials and raw
subprocess diagnostics.

## Remaining implementation gaps

The implemented service and HTTP transport intentionally do not provide:

- cancellation-aware replay extraction;
- browser/frontend configuration or UI; or
- ROI selection, object comparison, and temporal search.

`RecordingPlanner` exposes the selected covering segment and a
segment-constrained replay plan. The focused decoder measures clip-relative PTS
and retains conservative timing status because real-NVR validation has not
proven an absolute replay-start mapping. The API composes settings safely and
validates/reuses compatible completed resources through the implemented
artifact/resource boundary.

## Design decision: direct reference-frame acquisition after replay stalls

**Status: implemented for new reference-frame resources. The direct acquirer
uses structured FFmpeg timing evidence and increments the generation policy to
`gpv-2`; real-NVR validation remains the release gate.**

### 1. Problem statement and proven evidence

The current reference-frame product is one durable, temporally evidenced JPEG.
Its implementation first requires a complete temporary MP4, even after the
frame needed by the product has been decoded. That coupling failed on one
otherwise ordinary NVR window.

For channel 1, KST `2026-07-20 12:34:18` normalizes to
`2026-07-20T03:34:18Z`. The selected segment for offsets `-10`, `0`, and `+10`
was the same interval, `03:33:15Z` through `03:36:16Z`. The zero-offset replay
window was `03:34:16Z` through `03:34:22Z`; the requested frame position was
local 2 seconds in its 6-second clip. The `-10` and `+10` requests succeeded;
the zero request failed with `replay_timeout`.

The allowlisted timeout summary for that zero request showed `147` frames,
`out_time_us=5840000`, and `total_size=1048620`, followed by about 40 seconds
without media-time or byte progress. It never reached the requested 6 seconds
and did not emit `progress=end`. This is evidence of an input/packet/GOP/
timestamp/decoder stall approximately 0.16 seconds before the requested output
end, not MP4 finalization alone. It is NVR-specific evidence for this window;
synthetic FFmpeg checks are not evidence about the NVR.

### 2. Current execution flow and root-cause boundary

```text
candidate-set API (KST input and ordered offsets)
  -> one-slot, serial ReferenceFrameCandidateSetService
  -> ReferenceFrameService.execute_or_resolve
  -> RecordingPlanner.find_covering_segment / plan_for_segment
  -> ReplayExtractor: temporary complete MP4
  -> FfmpegReferenceFrameDecoder: ffprobe all local frame PTS
  -> nearest frame, earlier on ties, then JPEG extraction and validation
  -> staged manifest and durable package promotion
```

`build_reference_replay_window` supplies the 6-second window: normally
`[requested - 2 s, requested + 4 s)`, clipped to the selected segment. The
two-second pre-roll puts the requested point at clip-relative PTS 2. The
current decoder uses `ffprobe -show_frames` on the completed MP4, selects the
minimum absolute local-PTS distance with the earlier PTS as a tie-break, then
uses `select=eq(n\\,index)` to create and validate `frame.jpg`.

`ReplayExtractor` does not seek: it supplies the replay stream as `-i`, then
places `-t <window-duration>` after that input, with video copy to MP4. The
installed FFmpeg help defines `-t` as stopping transcoding after the specified
duration. Thus this is an output-duration cap, not an input-side `-ss` seek.
Its bounded timeout is `duration + 30 s startup allowance + 10 s finalization
margin`; for this request, 46 seconds. The timeout was correct for a command
whose success contract required a finalized 6-second MP4.

The candidate-set service invokes the existing single-frame service serially
under the one-slot limiter. Its one shared `RecordingPlanner` retains its public
SDK recording-search process ID for the authenticated planner lifetime, while
every candidate independently plans its segment/window and owns its replay and
staging cleanup. Compatible durable-resource reuse is checked after safe
segment/window planning and before replay extraction.

The root-cause boundary is therefore **generic replay completion**, not the
planner, segment selection, artifact writer, or local JPEG decoder. Generic
recording callers (`analyze-recording`, sampling, and investigation collection)
do require a complete reusable MP4 and must retain this behavior. A reference
frame does not.

### 3. Requirement mismatch

The generic replay boundary must reject an incomplete MP4: it cannot safely
return a clip whose container or media ends early. The reference-frame boundary
instead needs one valid JPEG, selected according to the existing policy, with
the requested time, selected segment, replay window, clip-relative timing
evidence, warnings, and credential-free durable manifest. It must not claim an
absolute source timestamp: the current `estimated_source_time_utc` and request
offset remain `null` and the precision remains `measured_clip_relative` until
separate calibration evidence exists.

Consequently, increasing the generic outer timeout is not the primary fix. It
would merely wait longer after the measured stall and would still require the
unusable partial MP4. Accepting that partial MP4 is explicitly unsafe.

### 4. Alternatives considered

| Option | Assessment |
| --- | --- |
| A. Direct single-frame extraction | Best match for the JPEG-only product if it can retain ordered per-frame timing evidence and terminate after the selection is decided. It isolates the fix from generic replay clips. |
| B. Shorter bounded MP4 | Small code change, but still requires a complete container and can fail on a stall after the target. A new duration would be an operational guess, not a semantic solution. |
| C. Longer window | Moves the vulnerable output boundary and increases latency/media work. It has no evidence that it avoids the deterministic zero-offset stall. |
| D. Streaming/fragmented intermediate container | Could make partial media inspectable, but adds container and lifecycle complexity while the product needs one JPEG. It risks weakening the generic clip contract. |
| E. Direct-first fallback | A hidden retry can produce different timing behavior and temporal drift. It is not selected. A future explicit fallback requires its own policy version, failure trigger, and real-NVR evidence. |

### 5. Selected approach and rationale

Select **A: a reference-frame-specific direct stream decoder**. It is the
smallest safe production change that satisfies the actual product requirement:
decode sequentially from the existing bounded replay window, retain only the
two neighboring timestamped candidates around local PTS 2, write/validate the
selected JPEG, then intentionally end the direct decoder process. It does not
create, inspect, or accept a partial MP4.

The existing 6-second request window and two-second pre-roll remain unchanged.
The direct decoder has a six-second *maximum* media horizon for no-frame,
one-sided, and segment-boundary cases, but it has no fixed post-target wait:
the minimum post-target allowance is the first valid timestamped decoded frame
at or after the target. That is exactly the information needed to compare it
with the last valid earlier frame. The normal direct success path therefore
finishes near the target rather than waiting for local 5.84--6 seconds, avoiding
the observed stall location without changing the requested reference time.

This approach is release-gated by the timing-evidence proof below. If the NVR
cannot supply the required ordered timing facts without raw diagnostics, do not
silently ship a less precise direct path; retain the existing clip path and
record the direct path as not validated. Option B is a rollback investigation,
not an automatic fallback.

### 6. Proposed component and API boundaries

Keep `ReferenceFrameRequest`, public HTTP schemas, candidate offsets/order,
`ReferenceFrameService`, `RecordingPlanner`, artifact/resource stores, one-slot
limiter, and serial candidate orchestration intact. Replace only the
reference-frame-specific dependency currently named `ReferenceFrameDecoder` and
the preceding `ReplayExtractionBoundary` call on the *new-resource* path with
one narrow internal boundary, conceptually:

```text
DirectReferenceFrameAcquirer.acquire(
  replay_request, target_local_seconds, selection_policy, staging_jpeg_path
) -> DecodedFrameEvidence
```

It owns credential injection only through the same in-memory replay URL helper,
bounded ffprobe/FFmpeg child lifecycle, machine-readable per-frame evidence,
candidate selection, JPEG validation, and removal of its invocation-owned
temporary candidates. `ReferenceFrameService` continues to own segment/window
selection, completed-resource reuse, staging session, manifest, promotion, and
failure cleanup. `ReplayExtractor` is not changed and remains used by every
non-reference-frame flow.

The service invokes the direct acquirer only after `RecordingPlanner` has
produced the same selected-segment-constrained `ReplayRequest`; it never builds
a replay URL itself. Existing API responses and manifest fields are sufficient:
they already include the request, segment, window, local PTS, timing status,
warnings, and image dimensions. Because acquisition semantics change, the
implementation must intentionally increment the generation-policy version
(for example, `gpv-2`); completed `gpv-1` resources remain readable but are not
compatible reuse candidates for the new policy.

### 7. Conceptual FFmpeg semantics and timing proof

The direct process must use the existing RTSP/TCP input rules and bounded
window, decode from its beginning, and create JPEG candidates in an
invocation-owned staging subdirectory. It must use no input-side `-ss`; input
seeking can land on a keyframe and would not prove the current nearest-decoded
policy. A post-input maximum duration still bounds the no-result path. The
implementation must not log or persist its authenticated URL, command,
stderr, or raw per-frame diagnostic text.

The implementation needs a structured per-frame record with **both an emitted
ordinal and normalized clip-relative PTS**. It must not infer a timestamp from
the requested filename. Local FFmpeg experiments established that `image2`
supports atomic image writing and frame-PTS-based names, and that preserving an
encoder source time base can make those names represent input PTS. Those checks
prove only local FFmpeg capability. Before use against the NVR, a focused
implementation spike must prove that its selected structured channel preserves
an ordered ordinal and PTS without raw stderr parsing, duplicate loss, or
unexplained timestamp rewriting. If a frame's PTS is missing, non-finite,
negative, duplicate without a reliable ordinal, or regresses/discontinues, the
acquirer fails conservatively rather than declaring the JPEG precisely selected.

The active candidate state is bounded to the last valid frame strictly before
the target and the first valid frame at or after it. For monotonically emitted
PTS, no later frame can be nearer; choose the smaller distance and choose the
earlier candidate on equality. This is mathematically equivalent to the current
full-list selector while avoiding storage of every frame. A selected JPEG is
accepted only after it has been finalized atomically, ffprobe-validated as the
expected dimensions/MJPEG, and its evidence has been captured.

### 8. Success, failure, timeout, and cancellation semantics

| Condition | Required result |
| --- | --- |
| Earlier and at/after candidates available | Select nearest; equal distance selects earlier; deliberately stop the child after the selected JPEG is validated. |
| Only earlier candidates at natural end | Preserve the existing one-sided warning and select the nearest earlier candidate. |
| Only at/after candidates at natural end | Preserve the existing one-sided warning and select the nearest later candidate. |
| No frame or no acceptable timestamp evidence | Existing safe `no_acceptable_frame` or `decode_failure`; no artifact. |
| Stall after both neighboring candidates and JPEG validation | Success is allowed. The parent intentionally terminates the no-longer-needed stream, so a later lack of `progress=end` is irrelevant. |
| Stall before the first at/after candidate or before JPEG validation | Safe timeout/decode failure; never promote a partial result. |
| Malformed, missing, duplicate-without-ordinal, or discontinuous timestamps | Conservative decode failure; do not silently downgrade timing precision. |
| JPEG persistence/validation failure | `artifact_failure` or `decode_failure` as today; remove invocation-owned partial files. |
| Explicit timeout or cancellation before success | Terminate, then kill only the owned child if needed; wait for readers; remove candidates and staging. No public cancellation claim is added. |
| Child exits without success and without `progress=end` | Failure unless the controller had already recorded an intentional successful stop. |

The direct acquirer receives one bounded wall-clock budget derived from the
existing window duration plus the established startup allowance; it also has a
small documented shutdown grace before killing its own process. It must drain
all owned stdout/stderr/metadata pipes concurrently so neither pipe blocks the
child. Raw output is discarded after fixed safe classification and never enters
logs, errors, artifacts, API responses, or manifests.

### 9. Artifact, privacy, compatibility, and migration rules

Direct candidate JPEGs live only under the existing invocation staging session.
After selection, retain one validated `frame.jpg`, serialize the existing
credential-free manifest, and promote exactly as `ReferenceFrameArtifactStore`
does today. On any failure, remove only the invocation's candidate images,
partial JPEG, staging directory, claim, and owned child processes. Do not touch
completed resources, artifacts from another request, or generic replay clips.

The manifest remains credential-free: no host, username, password, token,
replay URL, FFmpeg arguments, raw stderr, temporary path, or unbounded
exception text. The current response schema, error envelope, deterministic
resource layout, overwrite protection, candidate ordering, and Phase 4B shell
remain compatible. The only visible material change is a new deterministic
generation-policy identity for newly created resources; old resources are
preserved and are not overwritten.

At an exact segment boundary, retain half-open segment selection and clip the
same requested window to the selected segment. A one-sided direct result must
keep its existing warning. A gap or a window that cannot be represented remains
`recording_unavailable`; the direct path never borrows frames from a neighbor.

### 10. Test strategy and real-NVR acceptance

Hermetic tests for the new acquirer must cover:

- target decoded before a simulated later stall succeeds after intentional stop;
- stall before target, no frames, missing/malformed timestamps, discontinuous
  PTS, and duplicate PTS without a proven ordinal fail safely;
- nearest earlier/later selection, deterministic equal-distance tie handling,
  local PTS evidence, and the current null source-time estimate;
- segment start/end clipping, only-before/only-after warnings, timeout,
  cancellation, pipe draining, partial JPEG cleanup, artifact collision, and
  repeated-request process/file stability;
- unchanged generic `ReplayExtractor` MP4 timeout/partial-media behavior,
  existing API contracts, candidate ordering, one-slot serial lifecycle, and
  Phase 4B UI behavior; and
- safe logs and manifests with no URL, host, credential, command, stderr, or
  temporary-path leakage.

Real-NVR acceptance uses channel 1 at KST `2026-07-20 12:34:18` with offsets
`-10`, `0`, and `+10`. For each candidate, record only the safe request/window,
selected segment bounds, local PTS, timing status/warnings, JPEG dimensions,
resource ID, and process-cleanup result. The zero candidate must either produce
a valid evidenced JPEG without waiting for irrelevant post-selection media or
return a precise conservative failure. Verify no partial MP4 is accepted, no
child remains, no staging files remain, and response order is unchanged.

### 11. Rollback, non-goals, and implementation checklist

Rollback is an application composition switch back to the existing
`ReplayExtractor` plus local-MP4 decoder before release; it does not delete or
rewrite durable artifacts. Do not add a blind runtime retry, extend the generic
timeout, accept partial MP4 output, change the requested time or candidate
ordering, add a public API field, modify the SDK, or redesign recording
retrieval. This is not a thumbnail, ROI, object-comparison, event-search,
background-job, or frontend feature.

Separate implementation task checklist:

1. Prove a Windows-compatible, machine-readable PTS-plus-ordinal channel on a
   local fixture and the target NVR without parsing or retaining raw stderr.
2. Add the reference-only direct acquirer and injected process seams; reuse
   credential injection, NVR planner, artifact staging, safe exception mapping,
   and existing FFmpeg/ffprobe resolution.
3. Implement bounded concurrent pipe draining, atomic candidate output,
   intentional post-selection termination, child wait/kill, and cleanup.
4. Implement the two-candidate selector and conservative timestamp validation;
   retain `measured_clip_relative` with null absolute-source fields.
5. Wire it only into new reference-frame resource creation, increment the
   generation policy, and leave generic replay consumers untouched.
6. Add the hermetic regression tests above, run project validation gates, then
   perform the stated three-offset real-NVR validation before enabling it as the
   default production path.

## User and system workflow

The implemented internal workflow is:

1. A future composition boundary will load `CaptureSettings`, reject
   `VIGI_SOURCE=ipc`, resolve ffmpeg and ffprobe, authenticate the public SDK
   planner, and optionally read safe channel inventory.
2. The internal service validates and canonicalizes a point request to a
   whole-second UTC instant.
3. It finds the `RecordingSegment` that covers that instant using half-open
   interval semantics.
4. It creates a short replay window around the point, clipped to that segment,
   then derives or validates a replay plan against the same selected segment.
5. It reuses the resulting credential-free `ReplayRequest` with the
   reference-frame-only direct acquirer, which decodes from the bounded-window
   start and retains the two adjacent structured timing candidates.
6. The acquirer selects, atomically finalizes, and validates one JPEG in
   invocation-owned staging, then intentionally ends the no-longer-needed
   decoder process and returns safe clip-relative timing evidence.
7. A reference-frame artifact writer writes a credential-free manifest and
   promotes the completed JPEG package to its final directory.
8. The service removes a temporary replay clip only on its retained legacy
   path, and returns only durable artifact-relative data and safe metadata.

No OpenAI request, profile selection, business report, ROI, or event reasoning
occurs in this flow.

## Input and time semantics

### Logical request

The proposed internal request has, conceptually:

- `channel_id`: a positive integer;
- `requested_time_utc`: a timezone-aware, whole-second UTC instant;
- `requested_time_text` and `source_timezone`: retained for traceability;
- `frame_selection_policy`: optional, defaulting to the recommended policy;
- future-only optional extraction tuning owned by the service, not by the first
  HTTP endpoint.

The service receives canonical UTC data, not FastAPI objects or unparsed query
strings. A separate input adapter may accept either:

- an offset-bearing RFC 3339 timestamp; or
- the existing naive `YYYY-MM-DD HH:MM:SS` text with an explicit supported
  source timezone, default `Asia/Seoul`.

The reference-frame parser supports valid IANA zones without consulting the
system-local timezone. `Asia/Seoul` has the established fixed KST fallback when
the host has no timezone database; other missing zones fail safely. A naive
input uses its explicitly supplied zone or the documented `Asia/Seoul` default.
A timezone-aware input supplies its own offset; an optional declared zone must
agree with that offset at the requested instant.

`Asia/Seoul` has no current DST ambiguity. If a future source zone observes
DST, a naive ambiguous or nonexistent local time must be rejected unless an
explicit offset or an approved fold policy is provided. The first implementation
must not silently choose one DST occurrence.

Reject a timestamp later than the service's current UTC time before NVR or
ffmpeg work. A future timestamp is an invalid request, not an unavailable
recording. Reject fractional seconds in the initial implementation because
`RecordingWindow` and replay URL contracts are whole-second; later subsecond
support requires measured evidence and a new recording contract.

## Recording-segment selection

The reference-frame service needs a selected segment, not just a positive
overlap test. The proposed point coverage rule is half-open:

```text
segment.start_utc <= requested_time_utc < segment.end_utc
```

At an exact segment start, select that segment. At an exact segment end, the
old segment does not cover the point; select a subsequent segment only when its
start covers the point. Otherwise report a recording gap/no recording rather
than borrowing a frame from the preceding segment.

If malformed NVR metadata contains multiple segments covering the same point,
selection is deterministic: choose the earliest segment start, then the
earliest end. The constrained replay window must still fit the selected
segment.

The narrowest expected refactor is to add an internal
`RecordingPlanner.find_covering_segment(channel_id, instant_utc)` operation,
or an equivalently small helper within the recording module, that reuses
`_matching_days`, `_segments`, `RecordingSegment.from_sdk`, and the public SDK
search path. `RecordingPlanner.plan(window)` stays unchanged and can reuse that
selection helper where appropriate. Do not copy day/result pagination into a
new reference-frame service and do not use private SDK APIs.

The returned segment is safe to expose as UTC start/end and NVR-local recording
day. Raw replay URLs, hostnames, credentials, and SDK objects are not part of
the segment result or manifest.

### Replay-plan consistency

Selected-segment metadata and the actual replay source are one integrity
contract. The service must not select one segment for metadata and silently
extract from another. The replay extraction plan must be derived from,
constrained to, or explicitly validated against the selected segment before
`ReplayExtractor.extract()` is called.

The current `RecordingPlanner.plan(window)` does not return the segment it
matched, so it does not by itself prove this consistency. The future
implementation must either add a narrow segment-constrained planning seam,
such as `plan_for_segment(selected_segment, window)`, or validate that a plan
produced by `plan(window)` corresponds to the selected segment. A mismatch is a
safe domain failure (or, only where a later product contract supports it, an
explicit indeterminate result), never a successful frame with misleading timing
metadata.

The durable result and manifest retain a credential-safe segment identity:
channel ID, NVR-local recording day, and the segment's raw epoch or canonical
UTC start/end bounds. They never retain a raw or authenticated replay URL.

## Frame-selection policy

### Alternatives

| Policy | Strength | Limitation |
| --- | --- | --- |
| Nearest decoded frame | Minimizes temporal distance and is appropriate for a user who visually confirms the reference object. | Requires decoding candidates on both sides and still cannot prove absolute source timing without validation. |
| First decoded frame at or after | Avoids choosing an earlier visual state. | May move the reference noticeably later, including after a rapid change. |
| Last decoded frame at or before | Avoids future-looking state. | Can be stale and may be unavailable near a segment start. |

### Recommended initial policy

Use **`nearest_decoded_frame`**, breaking an equal-distance tie toward the
earlier decoded frame. The user sees the returned JPEG and its timing warning
before selecting an ROI, so minimizing the displayed frame's estimated distance
from the requested time is more useful than silently biasing the evidence after
or before the request. The result must state the policy and timing status.

This policy is suitable only for manual reference-object confirmation. It does
not prove the object state at the requested instant, and it must not later be
reused as a causal or event-time policy without its own evaluation.

### Proposed bounded extraction window

The first experiment should request a **six-second** replay window, nominally
two seconds before through four seconds after the requested UTC second. Clip
that interval to the selected segment. The asymmetric after-side gives the
decoder room to find an at-or-after candidate while preserving a short bounded
MP4; it is a proposed operational default, not an accuracy guarantee.

If clipping removes one side, decode the available side and select the nearest
candidate only if it meets a future validated maximum-offset rule. Until that
rule is measured against the NVR, return the candidate with a prominent
one-sided warning rather than claim normal precision. If no candidate is
decoded, raise `no_acceptable_frame`; do not substitute a frame from an
adjacent gap or segment.

The current `ReplayExtractor` is reusable because its client-side `-t` bounds
the requested media. The current `FfmpegAnchorSnapshotExtractor` is not enough
for this policy: it seeks one integer offset and returns no PTS or dimensions.
Phase 2 implementation needs a focused `ReferenceFrameDecoder` protocol that
probes a local MP4, enumerates or otherwise verifies candidate frame timing,
selects one policy-compliant frame, and writes exactly one JPEG.

## Timestamp accuracy and uncertainty

A reference-frame result preserves these separate facts:

| Fact | Meaning |
| --- | --- |
| Requested time | Canonical UTC instant the user asked for, plus original text/timezone for traceability. |
| Selected segment | Safe UTC interval and recording-day metadata that covers the request. |
| Extraction window | UTC replay bounds submitted to `RecordingPlanner` and `ReplayExtractor`. |
| Decoded frame time | Clip-relative PTS only when the decoder/ffprobe can measure it reliably. |
| Estimated source time | `extraction_window.start_utc + decoded_relative_pts` only when that mapping is supported by the extracted MP4. |
| Offset | Estimated or measured frame time minus requested UTC time, never inferred from the requested filename. |
| Precision status | Declares whether the timing is measured, estimated, or unknown and carries warnings. |

The implemented `TimingPrecisionStatus` enum is `measured_clip_relative`,
`estimated`, `unavailable`, or `indeterminate`. The current decoder returns
only `measured_clip_relative`: local PTS was measured, while mapping to an NVR
source time still has an explicit warning unless validated. A JPEG must not be
given a source-time estimate merely because it belongs to a requested replay
window.

Do not introduce an `exact` status in the first implementation. It can be
added only after real-NVR validation demonstrates the replay start-to-decoded
PTS relationship and its tolerance for relevant codecs, keyframe spacing,
variable frame rate, retries, and segment boundaries. A frame filename based on
the requested timestamp is an identifier, not timing evidence.

## Internal service boundary

### Responsibility

`ReferenceFrameService` is an application-internal, synchronous orchestration
boundary. It accepts a validated reference-frame request and returns one typed
result for a completed durable JPEG resource. It owns point selection,
bounded replay planning, temporary replay cleanup, decoder invocation, and
artifact handoff. It does not know FastAPI, HTTP, UI state, ROI geometry,
presence classification, OpenAI, or reports.

### Proposed dependency seams

The implementation should inject narrow protocols, matching existing service
style:

- `RecordingSegmentBoundary.find_covering_segment(channel_id, instant_utc)`;
- `SegmentConstrainedReplayPlanningBoundary.plan_for_segment(segment, window)`
  returning an existing `ReplayRequest` plus safe evidence of the selected
  segment, or an adapter that validates a legacy `plan(window)` result against
  that segment;
- `ReplayExtractionBoundary.extract(request)` returning existing `ReplayClip`;
- `ReferenceFrameDecoder.decode(clip, target, policy, output_path)` returning
  selected JPEG metadata;
- `ReferenceFrameArtifactBoundary.begin/write/finalize` for invocation-owned
  staging and durable promotion;
- optional `ChannelInventoryBoundary.channels()` at composition for a safe
  channel-not-found result.

The concrete composition can adapt one `RecordingPlanner` to both segment and
segment-constrained replay-planning protocols. It should use `CaptureSettings`,
`RecordingPlanner.connect`, `ReplayExtractor`, `resolve_ffmpeg`, and a future
decoder implementation. Neither the request nor the result imports FastAPI.

`SdkNvrGateway.channels()` can establish that a channel is absent. Current
offline state is not a rejection for historical replay: an offline camera may
still have valid recordings. If inventory cannot be obtained, map its existing
safe `NvrRequestError`; do not claim that a channel exists merely because a
recording search failed.

### Cancellation and timeouts

The initial service is synchronous because current `ReplayExtractor` and local
ffmpeg boundaries are synchronous and bounded. Its `finally` blocks must remove
the caller-owned `ReplayClip` on success, error, or `KeyboardInterrupt`.
Current subprocess runners do not support cooperative cancellation, so a future
cancellation token must not be advertised until the replay and decoder process
boundaries can terminate a child process safely. Existing bounded replay
timeouts (`duration + 30s startup + 10s finalization`) and local-frame tool
timeouts remain the baseline.

## Proposed models

These are design concepts, not committed class names or JSON schemas.

```text
ReferenceFrameRequest
  channel_id
  requested_time_text
  source_timezone
  requested_time_utc
  frame_selection_policy

SelectedRecordingSegment
  channel_id
  recording_day
  segment_start_epoch_seconds / segment_end_epoch_seconds
  start_utc
  end_utc

SegmentConstrainedReplayPlan
  selected_segment_identity
  extraction_window
  replay_request (internal only)

DecodedFrameEvidence
  jpeg_path (internal only until finalized)
  decoded_width / decoded_height
  decoded_relative_pts (optional)
  estimated_time_utc (optional)
  offset_from_request (optional)
  timing_precision_status
  warnings

ReferenceFrameResult
  resource_id
  manifest_schema_version
  generation_policy_version
  channel_id
  requested time facts
  selected segment facts
  extraction window
  policy
  durable JPEG relative path
  decoded image dimensions
  timing evidence and warnings
  manifest relative path
```

Internal result objects may contain absolute paths only while the service owns
them. Durable manifests and HTTP metadata use artifact-relative filenames or
resource IDs, never absolute temporary paths. Credential-bearing URLs and
credential material never appear in any model shown to an artifact writer,
HTTP mapper, logger, or caller.

`manifest_schema_version` and `generation_policy_version` are separate
concepts. The schema version controls how a manifest is interpreted. The
generation-policy version identifies material semantics used to create the
JPEG, such as frame-selection policy, replay-window behavior, timestamp
mapping, decoder behavior, and artifact semantics. A build or Git commit ID may
be retained as credential-free diagnostic provenance, but it is not a substitute
for an intentional generation-policy version.

## Artifact lifecycle

The JPEG must be durable after success because later ROI confirmation needs the
same image and dimensions. The recommended local artifact layout is:

```text
artifacts/reference-frames/
  channel-1_20260720T030000Z_nearest-decoded-frame_gpv-1/
    frame.jpg
    manifest.json
```

The final directory name contains only a validated channel, requested UTC token,
policy identifier, and generation-policy version. It contains no hostname,
username, password, replay URL, or local temporary path. `manifest.json`
records a `schema_version`, `generation_policy_version`, optional
credential-free build provenance, requested/source UTC facts, source timezone,
credential-safe selected-segment identity, extraction window, policy, relative
JPEG filename, decoded dimensions, timing status, optional safe offset,
warnings, and completed lifecycle status.

Use an invocation-unique staging directory next to the final directory, then
write `frame.jpg` and the manifest before promotion. A completed final resource
may be reused only when request identity and compatible generation semantics
match, including the generation-policy version. Schema compatibility and
generation compatibility are separate checks: a readable older schema does not
by itself make its generated JPEG compatible with the current policy. A changed
frame-selection policy, replay window, timestamp mapping, decoder behavior, or
artifact semantic may require a generation-policy increment.

An incompatible existing resource must receive a new deterministic identity or
an explicit artifact conflict; it must never be silently reused. Existing
completed artifacts must never be overwritten or deleted. An existing path that
is incomplete or unreadable is likewise an artifact conflict.

The implemented store takes an exclusive sibling claim before staging an
identity. This prevents concurrent identical invocations from reaching
promotion together. Promotion rechecks the final path and renames only to an
absent destination; a late existing path is a conflict, including an empty
directory.

Temporary replay MP4s and incomplete JPEGs are removed in every failure or
cancellation path. The initial slice should not preserve failed or partial
reference-frame packages: there is no useful ROI resource until a valid JPEG
and manifest are complete. If later evidence requires failed-package retention,
it needs an explicit lifecycle schema; this design does not finalize broader
investigation storage.

## Error model

Domain failures remain separate from HTTP mapping. Prefer existing safe
exceptions and add only reference-frame-specific errors for conditions no
existing boundary represents.

| Domain outcome | Source / handling | HTTP mapping later |
| --- | --- | --- |
| Invalid request | New narrow input error for channel, time, timezone, future time, or unsupported policy. | `400` with safe code. |
| Unsupported source | NVR-only composition rejects IPC before SDK/media work. | `400` or `409`; recommend `400` `unsupported_source`. |
| Channel not found | Inventory adapter finds no matching ID. Offline is a warning, not absence. | `404` `channel_not_found`. |
| No recording / gap | `RecordingUnavailableError` or no segment covering the half-open point. | `404` with distinct safe code `recording_unavailable` or `recording_gap`. |
| Planner / NVR failure | Existing `NvrRequestError` or `RecordingDataError`. | `502`; auth can be a safe `502` service dependency failure, not client credential disclosure. |
| Replay failure | Existing replay authentication, unavailable, timeout, or extraction errors. | unavailable `404`; timeout `504`; other replay faults `502`. |
| Segment/replay mismatch | New narrow safe consistency error when the selected segment and replay plan cannot be proven to match. | `409` or `502`; recommend `409` `segment_replay_mismatch` until a later contract distinguishes source conflict from planner failure. |
| ffmpeg unavailable | Existing `FfmpegUnavailableError`. | `503` configuration/dependency unavailable. |
| Frame decode / no acceptable frame | New narrow decoder error or existing redacted local-frame failure. | `422` for no policy-compliant frame; `502` for decoder failure. |
| Cancellation | Future explicit cancellation error after cooperative support exists. | Client disconnect has no response; an explicit cancellation endpoint is out of scope. |
| Artifact conflict | New safe artifact conflict error; never replace existing output. | `409`. |
| Unexpected failure | Catch only at composition/API boundary; redact exception text. | `500` with a fixed safe code. |

Warnings are successful results, not errors: current channel offline, one-sided
candidate window, estimated timing, unknown timing, or an offset outside a
future validated normal range. No warning may be used to conceal a missing
JPEG or a policy violation.

## Historical Phase 2 transport exploration (superseded)

The comparative notes below informed Phase 2 service boundaries. The approved
Phase 3A HTTP contract is now in [reference-frame-api.md](reference-frame-api.md).
Phase 3B must follow that document for endpoint paths, request fields, timing
serialization, error mapping, reuse, concurrency, and transport composition.

### Alternative A: direct image endpoint

`GET /api/channels/{channel_id}/frame?time=...` could synchronously return a
JPEG. It is superficially simple but has no natural place for timing metadata,
warnings, durable resource identity, or manifest access. Headers could carry
some metadata, but that is hard for later UI and evidence review to consume and
does not represent an artifact lifecycle well.

### Alternative B: synchronous durable resource with separate image endpoint

`POST /api/reference-frames` accepts a JSON request and synchronously creates
or reuses one durable local resource. It returns JSON metadata including a
resource ID, timing evidence, warnings, and an image URL. A separate
`GET /api/reference-frames/{resource_id}/image` serves `image/jpeg`; an
optional metadata `GET` returns the same safe JSON.

This matches bounded local extraction latency, makes the durable JPEG explicit,
and supports later ROI selection without embedding binary data in JSON. It also
provides a natural place for structured errors and repeat-request behavior.

### Alternative C: asynchronous job resource

A `202 Accepted` job and polling endpoint would handle long or concurrent
extraction, but it introduces job persistence, cancellation state, cleanup,
and retry semantics not justified by the current local single-user MVP.

### Superseded recommendation

Adopt **Alternative B** for the first local MVP, after the internal service has
been accepted and tested:

```text
POST /api/reference-frames
{
  "channel_id": 1,
  "requested_time": "2026-07-20T12:34:18+09:00",
  "source_timezone": "Asia/Seoul",
  "frame_selection_policy": "nearest_decoded_frame"
}

200 or 201 application/json
{
  "resource_id": "channel-1_20260720T033418Z_nearest-decoded-frame_gpv-1",
  "manifest_schema_version": 1,
  "generation_policy_version": 1,
  "requested_time_utc": "2026-07-20T03:34:18Z",
  "frame_selection_policy": "nearest_decoded_frame",
  "timing_precision_status": "measured_clip_relative",
  "warnings": ["..."],
  "image_url": "/api/reference-frames/.../image"
}
```

The actual JSON schema remains implementation work. Request timestamps should
prefer offset-bearing RFC 3339 values. The optional timezone exists only for
the established naive-input path and must agree with an aware input when both
are supplied. The endpoint must not return replay URLs, hosts, credentials,
ffmpeg arguments, stdout/stderr, or temporary paths.

Run synchronous extraction initially with the existing bounded timeout policy.
Repeated identical completed requests return the existing durable resource
(`200`) only when request identity, schema compatibility, and generation
semantics match; a newly created compatible resource returns `201`. An older
incompatible resource receives a new identity or a safe conflict, never silent
reuse or overwrite. A resource/job model becomes necessary only when measured
extraction latency, concurrent local users, or cooperative cancellation exceeds
the usefulness of the synchronous local path.

Bind to localhost for the initial local-PC deployment. The future Vite frontend
may receive an explicit localhost-only CORS allowlist; do not enable permissive
origins or assume public-network deployment. Authentication beyond localhost is
an open concern, not a Phase 2A feature.

## Security and privacy

- Use only public SDK recording and replay APIs; do not modify the neighboring
  SDK or use private endpoints.
- Supply RTSP credentials only in memory to `ReplayExtractor`; never write or
  return authenticated URLs.
- Keep logs, manifests, filenames, errors, and HTTP payloads free of
  credentials, hostnames, replay URLs, ffmpeg commands, raw stderr, and
  temporary authenticated paths.
- Validate positive channel IDs, timestamps, timezone values, policy values,
  and artifact IDs before filesystem or external work.
- Bound replay duration, ffmpeg timeouts, output size, and temporary workspace
  ownership.
- Preserve local-PC operation; a network-exposed API needs a separate threat
  model, authentication, authorization, rate limiting, and audit design.
- The service returns media and timing evidence only. It makes no identity,
  theft, culpability, causality, payment, or continuous-tracking claim.

## Testing strategy

### Unit tests

- Parse aware and supported naive times into whole-second UTC values; reject
  invalid, future, fractional, ambiguous-DST, and unsupported-zone input.
- Verify half-open segment selection at start, end, adjacent, and gapped
  boundaries, including adjacent or overlapping candidate segments.
- Verify that selected-segment identity and the planned replay source match;
  detect a mismatch before extraction and keep credential-bearing URLs out of
  mismatch diagnostics.
- Verify replay-window clipping and policy selection for candidates before,
  after, and equidistant from the request.
- Verify timing-status, offset, warning, dimensions, and safe result
  serialization.
- Assert credential, URL, host, raw stderr, and temporary-path redaction.
- Assert replay/JPEG/staging cleanup, completed-resource reuse, and artifact
  conflict refusal.
- Verify that identical requests with compatible schema and generation versions
  can reuse an artifact when reuse is enabled; a changed generation-policy
  version or incompatible schema cannot reuse an older artifact; and completed
  artifacts are never overwritten.
- Verify schema, generation version, and diagnostic provenance remain
  credential-free.
- Test HTTP error mapping as pure adapters once the API layer is introduced.

### Component tests

- Run `ReferenceFrameService` with fake segment/planning, replay, decoder, and
  artifact boundaries; assert calls and ownership without an NVR.
- Use a deterministic local MP4/JPEG fixture to test policy selection, decoded
  PTS parsing where supported, and decoded dimensions.
- Cover segment boundaries, frames only before/after the request, no candidates,
  selected-segment/replay mismatch, replay timeout, decoder failure,
  `KeyboardInterrupt` cleanup, version incompatibility, and artifact conflicts.

### API tests

After FastAPI exists, test successful JSON metadata plus image retrieval,
invalid timestamp/channel, no recording, extraction failure, safe warnings,
idempotent repeated requests, structured errors, and configured localhost CORS.
No API test may require a real NVR.

## Real NVR validation plan

Run a small manual matrix outside hermetic tests:

| Case | Evidence to collect safely |
| --- | --- |
| Known recorded instant | Compare requested input, selected segment, returned image, and timing status. |
| Near segment start/end | Verify half-open selection, clipping, and no cross-gap borrowing. |
| Recording gap | Verify safe unavailable/gap response and no final artifact. |
| Several visible-time targets | Compare visible CCTV overlays when available with requested and reported timing evidence. |
| Repeated identical requests | Verify compatible-version resource reuse and no overwrite or leaked temporary replay files. |
| Changed generation policy | Verify a new identity or safe conflict rather than reuse of an older JPEG. |
| Credential review | Inspect manifests, terminal/API output, and logs for absent credentials, hosts, URLs, commands, and stderr. |

Measure the difference between requested time and selected decoded frame for
each case. Only after this evidence establishes a replay-start/PTS tolerance
may the implementation strengthen `estimated` timing statements.

## Implementation sequence

1. Add internal request/result models and narrow reference-frame domain errors.
2. Add the minimal recording-module seam that returns the covering
   `RecordingSegment` and derives or validates a segment-constrained replay
   plan without duplicating SDK pagination.
3. Implement a local-MP4 `ReferenceFrameDecoder` with injected process seams
   and truthful PTS/dimension evidence.
4. Implement `ReferenceFrameService` with injected planner, extractor,
   decoder, and artifact boundaries.
5. Add hermetic service, cleanup, redaction, segment-boundary, and artifact
   tests.
6. Implement compatible completed-resource lookup; versioned identity and safe
   conflict handling are already present.
7. Add the minimal FastAPI boundary and API tests.
8. Perform real-NVR validation and adjust only evidence-backed defaults or
   timing-status guarantees.
9. Only then implement frontend display and manual ROI selection.

## Open questions

- Can ffprobe reliably expose PTS values after the target NVR's replay remux,
  and how should missing/discontinuous PTS be represented?
- What pre-roll, post-roll, and maximum acceptable offset are reliable for the
  actual NVR, codecs, and keyframe intervals?
- Does the replay output preserve a stable enough source-time-to-local-PTS
  mapping to ever support a measured absolute timestamp?
- What safe segment identity can the current public SDK path preserve, and can
  a segment-constrained replay plan be produced without an SDK change?
- Should completed identical resources be reused indefinitely, expire locally,
  or be scoped by a later investigation ID, and which changes require a
  generation-policy version increment?
- Which image-dimension measurement is best for ROI mapping without adding an
  unnecessary dependency: ffprobe stream metadata, decoded JPEG probing, or
  another existing tool capability?
- When actual extraction latency is measured, does synchronous localhost HTTP
  remain sufficient?

## Acceptance criteria

This design phase is complete when later implementation can do all of the
following without re-deciding its foundations:

- reuse the current public-SDK recording planner, replay extractor, bounded
  ffmpeg process patterns, capture-only settings, and artifact principles;
- select and describe a source recording segment without claiming point-time
  coverage across gaps, and prove or validate that the replay plan uses that
  same segment;
- return a durable JPEG with policy, dimensions, timing status, and warnings;
- distinguish requested, replay-window, decoded-relative, and estimated source
  times;
- use `nearest_decoded_frame` as the initial manual-confirmation policy without
  claiming exactness;
- clean temporary replay and incomplete output without deleting existing
  completed artifacts;
- retain separate schema and generation-policy versions so incompatible JPEGs
  and manifests are never silently reused or overwritten;
- map safe domain failures to a small local HTTP surface; and
- test hermetically before using the real NVR for validation.
