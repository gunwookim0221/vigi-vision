# Reference-Frame Candidate Sets

## Status and purpose

**Status: implemented Phase 4A API, Phase 4B loopback shell, and Phase 4C-1/4C-2
thumbnail display plus transient exactly-one frontend selection. Phase 4C-3
adds explicit applied date/time, timezone clarity, and accessible indeterminate
generation feedback. Phase 5-1/5-2 adds the transient one-rectangle ROI
workspace with pointer and keyboard editing; fixture browser validation is
complete while physical-device and NVR validation remain pending.**

This design adds a bounded synchronous API for several reviewable reference-frame
candidates around one user-entered reference time. It builds on the implemented
single-frame service and local API; it does not alter either existing public
endpoint.

A candidate is always a **frame requested at reference time ± offset**. It is
not evidence that the decoded image depicts the real-world scene exactly that
many seconds before or after an event. Each successful child retains the
existing `measured_clip_relative` evidence and null absolute source-time estimate
unless a future evidence-backed policy changes that contract.

Phase 4A is a server-side candidate-generation boundary only. Phase 4B adds a
basic frontend shell; Phase 4C-1 displays thumbnails, Phase 4C-2 lets a user
select exactly one successful loaded candidate without persistence, and Phase
4C-3 makes application time and busy feedback explicit without changing the
backend contract.

## Goals and exclusions

### Goals

- Default to offsets `[-60, -10, 0, 10, 60]` seconds.
- Support a bounded ordered custom sequence of unique offsets.
- Preserve the user-entered anchor separately from every derived candidate time.
- Default naïve input on this endpoint to `Asia/Seoul`, while supporting explicit
  IANA zones and offset-aware timestamps.
- Reuse compatible existing reference-frame resources exactly as the single-frame
  service already does.
- Return useful ordered partial results when an individual candidate encounters a
  gap, replay failure, or decode failure.

### Exclusions

- No change to `POST /api/v1/reference-frames`, including its explicit timezone
  requirement for naïve timestamps.
- No continuous timeline browsing, jobs, queues, cancellation infrastructure,
  backend selection, ROI, object matching, SDK work, set-level artifacts, or set
  manifest. Frontend selection is transient and local to the page.
- No exact decoded-frame or real-world source-time claim.

## Existing boundaries reused unchanged

| Boundary | Candidate-set use | Not owned by Phase 4A |
| --- | --- | --- |
| `parse_reference_frame_request` | Normalizes derived child input and retains existing whole-second/timezone rules. | New individual-frame timing semantics. |
| `ReferenceFrameService.execute_or_resolve` | Executes or reuses exactly one candidate. | Segment selection, replay, decoding, publication, and cleanup. |
| Artifact/resource stores | Preserve existing deterministic child identity, claims, promotion, and compatible reuse. | Set-level persistence. |
| `RecordingPlanner` | Selects every candidate's segment independently while reusing its existing SDK recording-search process. | Cross-boundary borrowing or a new recording path. |
| Existing error mapping | Produces fixed safe child categories. | Raw exception text or subprocess diagnostics. |

## API boundary

Add one sibling route:

```text
POST /api/v1/reference-frame-candidate-sets
```

It is a new resource shape, not an extension of the single-frame route. The
single-frame route keeps its schema and `201 created` / `200 reused` behavior.
Candidate-set success always uses `200` because its body can contain mixed child
outcomes.

The application remains loopback-only and NVR-only. It uses current application
composition and the same process-wide `CapacityLimiter(1)` as single-frame work.

### Request schema

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `channel_id` | integer | yes | Positive NVR channel ID. |
| `reference_time` | string | yes | Whole-second ISO 8601/RFC 3339 anchor. |
| `source_timezone` | string or `null` | no | Naïve input defaults to `Asia/Seoul`; for aware input it is optional and must agree if supplied. |
| `offsets_seconds` | integer array | no | Ordered, unique values. Omission uses `[-60, -10, 0, 10, 60]`. |

Validation rules:

- Accept from 1 through 5 offsets inclusive.
- Every offset is an integer within the closed range `[-300, 300]` seconds.
- Reject duplicate offsets with `422`; never silently sort, de-duplicate, or
  expand a request.
- Reject fractional seconds, ambiguous/nonexistent local time, unsupported zone,
  non-positive channel, and a future anchor.
- Represent a derived future candidate as that candidate's safe failure, rather
  than discarding otherwise useful older candidates.
- Preserve submitted order in both `offsets_seconds` and `candidates`.

Default request:

```json
{
  "channel_id": 1,
  "reference_time": "2026-07-20T12:34:18"
}
```

Custom request:

```json
{
  "channel_id": 1,
  "reference_time": "2026-07-20T12:34:18+09:00",
  "source_timezone": "Asia/Seoul",
  "offsets_seconds": [-120, -30, 0, 20, 120]
}
```

### Response schema

The set response keeps the normalized original anchor separate from its child
requests.

| Field | Type | Meaning |
| --- | --- | --- |
| `reference_time_utc` | RFC 3339 UTC string | Normalized user-entered anchor, not decoded-frame time. |
| `source_timezone` | string | Accepted source-time interpretation. |
| `offsets_seconds` | integer array | Accepted ordered offsets. |
| `candidates` | array | Exactly one outcome per accepted offset, in submitted order. |
| `summary` | object | Created, reused, and failed child counts. |

Each candidate has the following fields.

| Field | Present when | Meaning |
| --- | --- | --- |
| `offset_seconds` | always | Requested position from `reference_time_utc`, not decoded-frame timing. |
| `candidate_requested_time_utc` | always | `reference_time_utc + offset_seconds`. |
| `status` | always | `succeeded` or `failed`. |
| `outcome` | succeeded | Existing child `created` or `reused`. |
| `reference_frame` | succeeded | Complete existing single-frame response, including resource ID, relative image URL, image data, timing, and warnings. |
| `failure` | failed | Fixed safe `code` and `message`; never exception text. |
| `warnings` | always | Set/derivation warnings. Child warnings remain nested in `reference_frame`. |

Example partial response (`200`):

```json
{
  "reference_time_utc": "2026-07-20T03:34:18Z",
  "source_timezone": "Asia/Seoul",
  "offsets_seconds": [-60, -10, 0, 10, 60],
  "candidates": [
    {
      "offset_seconds": -60,
      "candidate_requested_time_utc": "2026-07-20T03:33:18Z",
      "status": "succeeded",
      "outcome": "reused",
      "reference_frame": {
        "resource_id": "channel-1_20260720T033318Z_segment-20260720T033000Z-20260720T034000Z_nearest-decoded-frame_gpv-1",
        "outcome": "reused",
        "image_url": "/api/v1/reference-frames/channel-1_20260720T033318Z_segment-20260720T033000Z-20260720T034000Z_nearest-decoded-frame_gpv-1/image",
        "timing": {
          "precision_status": "measured_clip_relative",
          "decoded_clip_relative_pts_seconds": 1.8,
          "estimated_source_time_utc": null,
          "offset_from_requested_seconds": null
        },
        "warnings": []
      },
      "warnings": []
    },
    {
      "offset_seconds": -10,
      "candidate_requested_time_utc": "2026-07-20T03:34:08Z",
      "status": "failed",
      "failure": {
        "code": "recording_unavailable",
        "message": "No recording is available for this candidate requested time."
      },
      "warnings": []
    }
  ],
  "summary": {"created": 0, "reused": 1, "failed": 1}
}
```

The PTS shown above is only clip-relative. A production schema makes every
existing child response field required exactly as the single-frame endpoint does;
the example omits unrelated child metadata only for readability.

### HTTP status behavior

| Condition | Status | Body |
| --- | --- | --- |
| All children succeed | `200` | Ordered set with created/reused counts. |
| Some children fail | `200` | Ordered partial result. |
| All children fail after a valid request | `200` | Ordered safe failures. |
| Malformed JSON | `400` | Existing fixed safe error envelope. |
| Invalid body/anchor/offsets | `422` | Existing fixed safe error envelope. |
| App composition fails before child execution | fixed safe `5xx` | Existing fixed safe error envelope. |

Child categories reuse the established safe taxonomy where applicable:
`recording_unavailable`, `replay_timeout`, `decode_timeout`, `nvr_unavailable`,
`replay_failure`, `decode_failure`, `media_tool_unavailable`,
`artifact_failure`, and `internal_error`. A derived future candidate gets a
fixed safe invalid-candidate-time category. No category includes raw text, URL,
hostname, credential, filesystem path, command, or stderr.

## Execution, reuse, ordering, and cleanup

```text
validate one anchor and ordered offsets
  -> derive one requested UTC instant per offset
  -> build one existing ReferenceFrameRequest per candidate
  -> existing execute_or_resolve(request)
  -> map safe child result
  -> return results in submitted order
```

The candidate-set service is synchronous and executes children serially inside
one shared limiter slot. That prevents a batch from multiplying NVR/ffmpeg load
and avoids nested acquisition of the same limiter. It calls the execution
boundary directly, not the single-frame HTTP route.

There is no candidate-set identity, cache, idempotency key, artifact directory,
or manifest. A repeated request derives the same children; each child uses its
existing deterministic identity and compatible lookup. Existing claim/promotion
still prevents duplicate durable publication.

Each child independently selects its segment under the existing half-open rule:

```text
segment.start_utc <= candidate_requested_time_utc < segment.end_utc
```

At a segment end, it never borrows a preceding frame. Existing overlap tie-break
behavior applies. The set service neither coalesces windows nor shares a replay
clip. Existing child cleanup remains responsible for replay clips and incomplete
staging packages; one failure cannot delete a completed sibling. Client
disconnect does not currently cancel synchronous work, and Phase 4A adds no
cancellation claim or background worker.

## Phase 4B frontend shell

| User action | Request |
| --- | --- |
| Normal choices | Omit `offsets_seconds`; submit one `reference_time`. |
| Custom local range | Submit the same anchor with an explicit bounded offset array. |
| Different absolute time | Submit a new `reference_time`; do not page server-side. |
| Retrieve JPEG | Follow the nested existing relative `image_url`; never construct a path. |

The loopback root route serves a native HTML/CSS/JavaScript shell that requires
explicit application of a whole-second local `reference_time` plus a selected
source timezone before submission. It omits `offsets_seconds`, sends the
applied local value and `source_timezone` through the existing candidate API,
shows an application-owned `YYYY-MM-DD HH:mm:ss` summary with the IANA timezone,
and exposes loading and top-level safe error states. Candidate generation uses
an indeterminate spinner/progress indicator only; the backend has no numeric
progress contract. It shows ordered candidate results with JPEG thumbnails,
renders the returned offset, requested UTC time, succeeded/failed status,
created or reused outcome, and fixed safe failure code/message. The known source
timestamp warning is presented as an unverified requested-position limitation
while unknown warnings remain visible. Loaded successful candidates use native
radio controls for exactly one transient selection and a larger uncropped
preview; failed or unavailable candidates remain unselectable. It never turns
an offset into a known event-time distance.

The shell is served from the existing FastAPI application at `/`, with fixed
static assets under `/static`. It introduces no frontend framework, build
system, extra dependency, API route, CORS policy, or candidate-set persistence.
Phase 5-1/5-2 consumes the selected frontend candidate through a separate
transient ROI workspace. One rectangle is drawn, moved, and resized with
unified mouse/touch/pen Pointer Events; keyboard arrows also move and resize
the canonical source-space ROI. Eight handles clamp at image bounds and the
4×4 minimum without crossover flipping. Only one active pointer is accepted,
the interaction surface alone has `touch-action: none`, and source-pixel
coordinates are recalculated for responsive display sizes. Candidate changes,
result replacement, and image failure clear draft and committed ROI state.
Pointer interruption clears the draft and active pointer while preserving a
prior committed ROI. Reset ROI preserves candidate selection and permits
recreation. `getPhase6Snapshot()` is an immutable transient handoff only;
persistence, confirmation, and manifest storage remain deferred. Editing an
applied input marks it dirty and prevents generation until the user applies it
again; editing does not clear already rendered candidates.

## Security and compatibility

- Keep the route loopback-only and NVR-only under current app composition.
- Keep credentials, hostnames, authenticated replay URLs, ffmpeg arguments,
  subprocess output, and temporary paths out of responses, IDs, logs, and
  existing child manifests.
- Keep image URLs relative and resource lookup behind the existing validated
  image endpoint.
- Keep existing resource IDs and `generation_policy_version` semantics unchanged.
- Preserve all existing single-frame route/schema/status/timezone behavior.

## Implemented boundary

The implementation uses typed request/response models, a serial
`ReferenceFrameCandidateSetService`, and the sibling FastAPI route. The service
normalizes the anchor once, derives typed child `ReferenceFrameRequest` values,
and invokes the existing single-frame execution protocol once for each eligible
candidate. It does not call recording, replay, decoder, artifact, or resource
internals directly.

The route acquires the existing shared single-slot limiter once for the complete
serial batch. Existing child claim/reuse and invocation-owned replay/staging
cleanup remain unchanged. Hermetic tests cover model validation, KST and aware
time input, order, reuse outcomes, partial/all-media failures, safe unexpected
failure handling, and regression of the original single-frame routes.

Real-NVR validation remains manual. It must record only safe HTTP outcomes,
resource IDs, JPEG dimensions, and conservative timing fields.

## Test plan

| Area | Required evidence |
| --- | --- |
| Models | Defaults, KST/aware zones, disagreement, seconds, limits, duplicates, UTC derivation, and order. |
| Service | Created/reused, partial/all-failed, unexpected isolation, and one invocation per offset. |
| API | Default/custom, 400/422/200/5xx, OpenAPI, safe redaction, original route unchanged. |
| Concurrency/reuse | Shared limiter serializes work; repeated/concurrent matching children reuse or claim safely. |
| Boundaries/cleanup | Gaps, exact boundaries, overlaps, and unchanged child replay/staging cleanup. |
| Real NVR | Default five and repeat around known KST media, with safe evidence only. |

## Assumptions, risks, and deferred decisions

- Five candidates and a ±300-second envelope are bounded MVP workload controls,
  not a measured NVR throughput guarantee.
- A five-item synchronous request can be slow on a high-latency NVR; serial
  execution caps load rather than adding jobs.
- Visual unsuitability remains possible because source timing is not frame-exact;
  the mitigation is human selection, not stronger timing language.
- A later saved frame/ROI workflow may need a new durable selection resource;
  it must not retroactively turn this transient request into a set manifest.
- Increasing the count/range, adding cancellation, progress, rate limits across
  processes, or any asynchronous architecture requires measured operational
  evidence and a new design decision.

## Proposed branch

`feature/reference-frame-candidates`
