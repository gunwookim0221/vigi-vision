# Synchronous Reference-Frame API

## Status and purpose

**Status: implemented Phase 3B local API.**

This document defines the local, synchronous HTTP boundary for the implemented
[reference-frame service](reference-frame-service.md). It exposes a durable
recorded JPEG for a requested NVR channel and source time. It does not add ROI
selection, object comparison, forward search, authentication, deployment, or
background work.

The API is a transport and composition layer only. Recording segment selection,
replay extraction, frame decoding, candidate selection, artifact publication,
and timing evidence remain in the existing domain modules. No endpoint reads
NVR credentials, builds replay URLs, or invokes ffmpeg directly.

The implemented Phase 4A candidate-set transport is specified separately in
[reference-frame-candidates.md](reference-frame-candidates.md). It is a sibling
endpoint that delegates each child to this existing single-frame boundary. It
does not change the route, schema, status behavior, or explicit naïve-timezone
requirement documented here.

## Current-contract constraints

The Phase 2 service accepts a validated `ReferenceFrameRequest` and returns a
`ReferenceFrameResult`. It currently:

- accepts only NVR input and whole-second timestamps;
- normalizes the request to UTC;
- finds one half-open covering recording segment;
- makes a six-second, selected-segment-contained replay window;
- reports `measured_clip_relative` decoded PTS and leaves estimated source time
  and request offset unavailable (`null`);
- creates a deterministic resource directory containing `frame.jpg` and
  `manifest.json`; and
- refuses an existing deterministic resource with
  `ReferenceFrameArtifactConflictError`.

Phase 3B adds a **narrow completed-resource adapter** so the API can truthfully
return an existing compatible resource as reused. It validates a
completed manifest and JPEG under the configured artifact root, compare schema
and generation-policy versions, and return a typed credential-free package.
It must not scan arbitrary paths, infer compatibility from a readable manifest,
or change replay/decoder behavior.

## API boundary

The initial API namespace is `/api/v1`. Versioning the transport path keeps the
HTTP contract separate from the manifest schema version.

| Endpoint | Responsibility | Success |
| --- | --- | --- |
| `POST /api/v1/reference-frames` | Synchronously create or safely reuse one durable reference-frame resource. | `201` created or `200` reused JSON metadata. |
| `GET /api/v1/reference-frames/{resource_id}/image` | Return the immutable JPEG belonging to a completed resource. | `200 image/jpeg`. |

`POST /api/v1/reference-frames` is preferred over a channel-scoped image route:
the durable resource has metadata, warnings, timing evidence, and a lifecycle
that cannot be expressed safely by a direct binary response. A separate image
endpoint keeps JSON metadata out of headers and lets later ROI work retrieve the
same durable frame.

FastAPI is confined to schemas, HTTP error translation, route handlers,
application composition, and file responses. The reference-frame domain does
not import FastAPI or HTTP types.

## Creation request

The public request is intentionally smaller than the internal request. The
initial selection policy and generation-policy version are server-owned; clients
cannot choose them.

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `channel_id` | integer | yes | Positive integer. |
| `requested_time` | string | yes | ISO 8601/RFC 3339 whole-second timestamp. Offset-bearing input is preferred. |
| `source_timezone` | string or `null` | no for aware time; yes for naive time | IANA timezone used to localize a naive timestamp, or checked against an aware timestamp's offset at that instant. |

The API calls `parse_reference_frame_request` exactly once after schema parsing:

- Aware input such as `2026-07-20T12:34:18+09:00` is preferred. It is
  normalized to UTC. `source_timezone`, if supplied, must have the same offset
  at that instant; conflicting input is rejected.
- Naive input is accepted only with an explicit IANA `source_timezone`. The API
  does not rely on the internal parser's default `Asia/Seoul` value, because an
  HTTP request must state its time interpretation.
- `Z` is accepted as UTC. Fractional seconds, ambiguous or nonexistent local
  times, unsupported zones, non-positive channels, and future instants are
  rejected.
- The internal `source_kind` is not an HTTP field. API composition is NVR-only
  and rejects a configured IPC source before media work.

Example request:

```json
{
  "channel_id": 1,
  "requested_time": "2026-07-20T12:34:18+09:00",
  "source_timezone": "Asia/Seoul"
}
```

## Creation response

The response represents one completed durable package. `outcome` distinguishes
new publication from compatible reuse; the HTTP status repeats that distinction
for ordinary HTTP clients.

| Field | Type | Source | Meaning |
| --- | --- | --- | --- |
| `resource_id` | string | artifact store | Opaque API identifier. |
| `outcome` | `created` or `reused` | API adapter | Whether this request published a new package or resolved a compatible completed one. |
| `manifest_schema_version` | integer | result/manifest | Manifest interpretation version. |
| `generation_policy_version` | integer | result/manifest | Generation semantics version. |
| `channel_id` | integer | request/result | Selected NVR channel. |
| `requested_time_utc` | RFC 3339 UTC string | result | Normalized requested instant. |
| `source_timezone` | string | result | Input interpretation retained for review. |
| `selected_segment` | object | result | Safe segment ID and UTC start/end facts. |
| `extraction_window` | object | result | UTC start/end of the bounded replay request. |
| `frame_selection_policy` | enum | result | Initially only `nearest_decoded_frame`. |
| `image_url` | relative URL | API | Relative JPEG retrieval URL. |
| `image` | object | result | JPEG media type, width, and height. |
| `timing` | object | result | Conservative decoded timing evidence. |
| `warnings` | string array | result | Safe successful-operation caveats. |

`timing` contains these exact concepts:

| Field | Type | Current value/meaning |
| --- | --- | --- |
| `precision_status` | enum | Currently `measured_clip_relative`. |
| `decoded_clip_relative_pts_seconds` | number or `null` | ffprobe-selected PTS relative to the local replay clip. |
| `estimated_source_time_utc` | RFC 3339 UTC string or `null` | Currently `null`; reserved for future evidence-backed estimation. |
| `offset_from_requested_seconds` | number or `null` | Currently `null`; unavailable without a proven source-time mapping. |

OpenAPI declares the closed `precision_status` enum as
`measured_clip_relative`, `estimated`, `unavailable`, or `indeterminate`.
Phase 3B returns only the current service value, `measured_clip_relative`,
until an evidence-backed domain change adds another result state. Every response
field in the creation-response table is required except the two explicitly
nullable source-time estimate fields; `warnings` is always an array.

There is no `exact` status and no field claiming an exact absolute source-frame
time. Filesystem paths, replay/RTSP URLs, credentials, temporary clips,
subprocess output, and staging details are never serialized.

Example newly created response (`201`):

```json
{
  "resource_id": "channel-1_20260720T033418Z_segment-20260720T033400Z-20260720T040000Z_nearest-decoded-frame_gpv-1",
  "outcome": "created",
  "manifest_schema_version": 1,
  "generation_policy_version": 1,
  "channel_id": 1,
  "requested_time_utc": "2026-07-20T03:34:18Z",
  "source_timezone": "Asia/Seoul",
  "selected_segment": {
    "id": "segment-20260720T033400Z-20260720T040000Z",
    "start_utc": "2026-07-20T03:34:00Z",
    "end_utc": "2026-07-20T04:00:00Z"
  },
  "extraction_window": {
    "start_utc": "2026-07-20T03:34:16Z",
    "end_utc": "2026-07-20T03:34:22Z"
  },
  "frame_selection_policy": "nearest_decoded_frame",
  "image_url": "/api/v1/reference-frames/channel-1_20260720T033418Z_segment-20260720T033400Z-20260720T040000Z_nearest-decoded-frame_gpv-1/image",
  "image": {
    "media_type": "image/jpeg",
    "width": 2560,
    "height": 1440
  },
  "timing": {
    "precision_status": "measured_clip_relative",
    "decoded_clip_relative_pts_seconds": 1.966667,
    "estimated_source_time_utc": null,
    "offset_from_requested_seconds": null
  },
  "warnings": [
    "Source timestamp mapping is unavailable pending real-NVR replay validation."
  ]
}
```

The example's segment and PTS are illustrative schema values, not an absolute
timing claim.

## Durable identity, reuse, and concurrency

The internal deterministic resource ID remains opaque at HTTP boundary. Phase
3B validates it as a single ASCII URL segment matching
`^[A-Za-z0-9][A-Za-z0-9_-]{0,191}$`, preserving the established Phase 2 UTC
`T` and `Z` separators; invalid and unknown IDs both return the same
safe `404 resource_not_found` response. Clients may receive and use an ID but
must not construct it as a supported request input. Raw artifact paths never
become identifiers.

The existing identity includes channel, normalized requested UTC second,
selected-segment identity, server-owned policy, and generation-policy version.
Thus semantically equivalent aware timestamps normalize to the same identity.

| Situation | Required behavior |
| --- | --- |
| Compatible completed resource already exists | Return validated metadata with `200` and `outcome: reused`; no replay, decode, overwrite, or modification. |
| First compatible request | Publish through the existing staging and non-overwrite flow; return `201` and `outcome: created`. |
| Two identical concurrent requests | One invocation may publish. The other resolves that completed compatible resource after the existing claim/promotion conflict, returning `200`; it must not overwrite. |
| Existing incomplete, corrupt, or incompatible package | Return `409 artifact_conflict`; do not reuse, delete, or overwrite it. |
| Equivalent textual time | Normalize before identity comparison; outcome follows the normalized request and selected segment. |

This is deterministic resource resolution, not a general HTTP idempotency
guarantee. An `Idempotency-Key` adds no unmet requirement for the current
single-resource request and is not included.

## JPEG retrieval

`GET /api/v1/reference-frames/{resource_id}/image` returns only a JPEG from a
validated completed artifact package.

- Success is `200`, `Content-Type: image/jpeg`, and
  `Content-Disposition: inline; filename="reference-frame.jpg"`.
- The artifact adapter validates the opaque ID, confines resolution to its
  configured output root, requires a completed compatible manifest, requires
  the fixed `frame.jpg` filename, and rejects symlinks and non-regular files.
- Unknown or malformed IDs return `404 resource_not_found` without exposing
  path syntax. A manifest with a missing or corrupt JPEG returns
  `500 resource_corrupt`; it is not silently treated as a new resource.
- Completed resources are immutable. Send
  `Cache-Control: private, max-age=3600, immutable`. Phase 3B does not promise
  conditional `304` behavior; any framework validators are incidental until
  explicitly tested.
- The route passes only the internally resolved path to `FileResponse`; it
  accepts no local-path parameter. If the package disappears before the file
  response begins, return a safe `500 resource_unavailable`. If it disappears
  after streaming begins, HTTP cannot replace an already-started body with an
  error; application ownership must prevent external artifact deletion.

## Error contract

All non-image errors use one credential-safe envelope:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "The reference-frame request is invalid.",
    "details": [
      {"field": "requested_time", "code": "invalid"}
    ]
  }
}
```

`details` is optional and contains only stable field names and fixed categories,
never rejected values or exception text. Request IDs are not needed for this
local MVP; server logs can correlate a safe resource ID after one exists.

| HTTP | API code | Cases |
| --- | --- | --- |
| `400` | `malformed_json`, `unsupported_source` | Invalid JSON syntax; defensive non-NVR composition mapping. A normal IPC configuration fails startup and serves no API. |
| `404` | `channel_not_found`, `recording_unavailable`, `resource_not_found` | Missing channel, no covering segment/replay, or hidden invalid/unknown resource ID. |
| `409` | `artifact_conflict`, `incompatible_resource` | Existing incomplete/corrupt/incompatible output or a non-reusable publication conflict. |
| `422` | `invalid_request`, `no_acceptable_frame` | Schema/semantic timestamp or channel validation; future time; no policy-compliant decoded frame. |
| `500` | `artifact_failure`, `resource_corrupt`, `internal_error` | Durable publication/read failure, corrupt completed package, or redacted unexpected failure. |
| `503` | `nvr_unavailable`, `media_tool_unavailable`, `replay_failure`, `decode_failure` | Safe SDK/dependency/replay/decode operational failure. |
| `504` | `replay_timeout`, `decode_timeout` | Existing bounded replay or local decoder timeout. |

Domain-to-HTTP translation is a single Phase 3B adapter:

| Existing domain outcome | API result |
| --- | --- |
| `ReferenceFrameInputError` | `422 invalid_request` with a fixed field category where safely known. |
| `UnsupportedReferenceFrameSourceError` | `400 unsupported_source`. |
| `ReferenceFrameChannelNotFoundError` | `404 channel_not_found`. |
| `RecordingUnavailableError` or `ReplayUnavailableError` | `404 recording_unavailable`. |
| `NvrRequestError`, `RecordingDataError`, `ReplayAuthenticationError`, `ReplayExtractionError` | `503 nvr_unavailable` or `503 replay_failure`; never propagate the exception text. |
| `ReplayTimeoutError` | `504 replay_timeout`. |
| `ReferenceFrameSegmentMismatchError` | `503 replay_failure` with a fixed safe message, because this is an internal consistency/dependency failure rather than a client resource conflict. |
| `FfmpegUnavailableError` | `503 media_tool_unavailable`. |
| `ReferenceFrameDecodeTimeoutError` | `504 decode_timeout`. |
| `ReferenceFrameNoCandidateError` | `422 no_acceptable_frame`. |
| `ReferenceFrameDecodeError` | `503 decode_failure`. |
| `ReferenceFrameArtifactConflictError` | Attempt compatible resolution once; otherwise `409 artifact_conflict`. |
| `ReferenceFrameArtifactError` or `ReferenceFrameCleanupError` | `500 artifact_failure`. |
| Missing/corrupt completed package | `500 resource_corrupt`. |
| Unknown exception at the transport boundary | `500 internal_error`, fixed message only. |

Example validation response (`422`):

```json
{
  "error": {
    "code": "invalid_request",
    "message": "The reference-frame request is invalid.",
    "details": [{"field": "requested_time", "code": "invalid"}]
  }
}
```

Example no-recording response (`404`):

```json
{
  "error": {
    "code": "recording_unavailable",
    "message": "No recording is available for the requested time."
  }
}
```

Example internal-processing response (`503`):

```json
{
  "error": {
    "code": "replay_failure",
    "message": "The recording replay could not be processed safely."
  }
}
```

## Synchronous execution and composition

The resource operation is synchronous: the creation response is not sent until
the service returns a compatible completed resource or fails. It is not a job
API and does not use `202`, polling, queues, WebSockets, Celery, or background
tasks.

Phase 3B uses an `async def` route solely to call the existing synchronous
`ReferenceFrameService.execute_or_resolve` through `anyio.to_thread.run_sync`
with an application-owned `CapacityLimiter(1)`. This avoids event-loop blocking
and bounds the local MVP to one NVR/ffmpeg extraction at a time. It is more
explicit than relying on FastAPI's shared thread pool for a long-running media
operation. The operation remains synchronous at the HTTP contract.

The limiter belongs to the application factory, not a module global. Start with
one Uvicorn worker and one application limiter slot. Increasing worker count or
the limit requires measured NVR and ffmpeg capacity evidence. Existing replay
timeouts (`duration + 30 seconds startup + 10 seconds finalization`) and the
decoder's bounded local tool timeout remain the operation time budgets; the API
does not add a competing shorter timeout.

Client disconnect or server cancellation does not prove that a thread,
subprocess, replay, or artifact publication stopped. Phase 3B must not claim
cooperative cancellation until the existing process boundaries support it.

## Configuration and dependency ownership

Application startup owns composition; handlers receive typed dependencies via
application state/dependencies and never read environment variables directly.

| Concern | Owner | Phase 3B composition |
| --- | --- | --- |
| NVR settings and secrets | `CaptureSettings` / `load_capture_settings` | Load once at application-factory startup. OpenAI is not required. |
| NVR connection and inventory | `NvrConnection`, `SdkNvrGateway` | Reject IPC configuration; inject the gateway as optional channel inventory. |
| Recording planning | `RecordingPlanner.connect` | Authenticate through the public SDK once during startup and inject the planner. |
| Replay extraction | `ReplayExtractor` | Construct with resolved ffmpeg, configured credentials, and invocation-owned temporary paths. |
| Frame decoding | `FfmpegReferenceFrameDecoder` | Resolve ffmpeg/ffprobe at startup and inject bounded runners. |
| Durable packages | `ReferenceFrameArtifactStore` | Use the configured reference-frame artifact root; provide the Phase 3B completed-resource adapter. |
| Orchestration | `ReferenceFrameService` | Build once from those existing collaborators. |
| HTTP transport | FastAPI app factory | Own schemas, limiter, handlers, error mapping, and FileResponse only. |

Missing capture settings, IPC configuration, inaccessible artifact root, or
unavailable ffmpeg/ffprobe are startup failures. The app fails to start
with a fixed safe configuration error, not serve a partially composed API.
Credentials remain only inside established settings and media/SDK dependencies;
they never appear in OpenAPI examples, logs, requests, responses, or manifests.

## Security and observability

This API is a local or trusted-network MVP. It has no authentication or
authorization and must bind to loopback by default. Exposure to an untrusted
network is unsupported until a separate authentication, authorization, rate
limit, audit, and deployment threat-model phase.

Request body size should be capped by the ASGI deployment configuration to a
small JSON-only limit (for example 16 KiB); the API has no upload endpoint.
Path parameters use the opaque-ID validator before filesystem resolution.

| Surface | Allowed | Forbidden |
| --- | --- | --- |
| HTTP responses/OpenAPI | IDs, channel, normalized timestamps, dimensions, timing status, fixed codes/messages, relative image URL | Credentials, hostnames, RTSP/replay URLs, local paths, commands, stderr, raw exception text. |
| Manifests | Existing credential-free result facts and warnings | Same sensitive values; no HTTP request headers. |
| Logs | Operation, safe resource ID after creation, channel, normalized UTC time, duration, outcome/error code | Authenticated URLs, passwords/tokens, raw environment values, subprocess commands/stderr, raw exception text, user-visible machine paths. |

No request ID is required for the single-user MVP. If future concurrent or
remote operation needs correlation, add a separately reviewed generated ID that
is not derived from secrets or user input.

## OpenAPI contract

Phase 3B defines Pydantic transport schemas separate from domain dataclasses:

- `ReferenceFrameCreateBody`: `channel_id`, `requested_time`, and optional
  `source_timezone`, with descriptions that say timestamps are whole-second and
  aware input is preferred.
- `ReferenceFrameResponse`: the fields in the creation-response table,
  including explicit nullable source-time estimate and offset fields.
- `ReferenceFrameErrorResponse`: the stable error envelope.
- `ReferenceFrameImageResponse`: `image/jpeg` success documentation plus the
  same error envelope for pre-response failures.

Endpoint summaries state that creation is synchronous, the result is durable,
and timing is clip-relative rather than exact source time. Examples use only
relative URLs and credential-free values. FastAPI's default validation response
must be replaced so every documented error follows the stable envelope.

## Dependency decision

The project uses Pydantic 2 and declares the narrow Phase 3B dependency set:

- `fastapi` for schemas, routing, OpenAPI, and `FileResponse` transport;
- `uvicorn[standard]` only as the local ASGI execution entry point; and
- no new HTTP client or job framework.

FastAPI's AnyIO dependency provides the required thread offload support. The
in-process tests use the FastAPI/Starlette-compatible `httpx2` test dependency.

## Local startup

Start the trusted-loopback API with the application factory:

```text
uv run uvicorn vigi_vision.reference_frame_api:create_reference_frame_app_from_environment --factory --host 127.0.0.1 --port 8000
```

The API has no authentication and must not be exposed publicly. It loads only
the existing NVR capture settings, not an OpenAI key. The first `POST` produces
`201 created`; later verified compatible requests produce `200 reused`. A
client disconnect does not guarantee cancellation of the underlying replay,
decoder, or artifact work.

## Phase 3B implementation

The implementation uses these files:

| File or module | Phase 3B responsibility |
| --- | --- |
| `src/vigi_vision/reference_frame_artifacts.py` | Existing staged non-overwrite publication for durable packages. |
| `src/vigi_vision/reference_frame_resources.py` | Typed completed-package lookup and fixed JPEG resolution confined to the artifact root. |
| `src/vigi_vision/reference_frame_service.py` | Add compatible completed-resource resolution at the existing deterministic identity seam; preserve existing extraction behavior for new resources. |
| `src/vigi_vision/reference_frame_api_models.py` | Pydantic HTTP request, response, timing, and error schemas. |
| `src/vigi_vision/reference_frame_api_errors.py` | Pure safe domain-error-to-HTTP mapping. |
| `src/vigi_vision/reference_frame_api.py` | Application factory, dependency composition, one-slot limiter, routes, and safe FileResponse boundary. |
| `uvicorn` factory command | Deliberate documented local ASGI launch surface. |
| `tests/test_reference_frame_resources.py` | Completed-resource lookup, compatibility, traversal, and corruption tests. |
| `tests/test_reference_frame_api_errors.py` | Hermetic error-mapping tests. |
| `tests/test_reference_frame_api.py` | In-process creation, image retrieval, reuse, concurrency, validation, OpenAPI, and redaction tests. |
| `PROJECT.md`, `docs/README.md`, and this document | Update implementation status and public API contract once runtime behavior exists. |

Phase 3B is complete when:

- domain modules remain free of FastAPI imports;
- a synchronous creation request avoids event-loop blocking through the bounded
  application-owned offload;
- completed-resource reuse and conflicting/corrupt resources are deterministic
  and never overwrite output;
- JPEG resolution is artifact-root confined and path-safe;
- all errors and OpenAPI examples are credential-safe;
- timing remains `measured_clip_relative` unless new calibration evidence
  supports a different documented status;
- repeated and concurrent requests follow the stated outcomes;
- API tests are hermetic and cover both JSON and image surfaces; and
- pytest, Ruff, format, basedpyright, and documentation checks pass.

## Limitations and deferred work

- No absolute source-frame timestamp is available; decoded PTS is only relative
  to the replay clip.
- The service has no cooperative cancellation and no API-level cancellation.
- Local synchronous operation may need re-evaluation after measured multi-user
  load or replay latency evidence.
- Authentication, public-network deployment, CORS for a future frontend, ROI,
  comparison, search, reports, and frontend work are outside Phase 3B.
