# Investigation Confirmation and Durable Persistence

## Status and scope

**Status: approved Phase 6-1 contract; Phase 6-2A backend foundation implemented.**

This document defines the boundary where a user-reviewed reference frame and
ROI stop being transient browser state and become one immutable, durable input
for the next object-disappearance phase. The HTTP API belongs to Phase 6-2B
and the web confirmation flow belongs to Phase 6-3.

The contract reuses the implemented reference-frame resource and investigation
artifact ownership boundaries. It does not add object comparison, temporal
search, reports, OpenAI, YOLO, background work, or a database.

## Terminology and non-goals

- **Draft investigation conditions:** the current applied anchor, selected
  candidate, ROI, and provenance held only by the browser.
- **Assisted proposal:** a transient model suggestion. It is not authoritative
  until accepted into the canonical rectangle.
- **Final ROI:** the current valid canonical source-pixel rectangle after any
  manual correction. This is what confirmation persists.
- **Confirmed investigation:** an immutable schema 2 package that passed server
  validation and atomic publication and is therefore eligible for Phase 7.
- **Legacy investigation:** an existing unversioned multi-camera package. It is
  not a confirmed object-disappearance input.

This phase does not persist candidate sets or masks, crop a second image, edit
confirmed input, search recordings, compare objects, infer events, introduce a
workflow engine, or change the NVR/SDK boundary.

## Existing contracts that remain authoritative

- A selected candidate is identified by its child
  `reference_frame.resource_id`; the candidate set itself has no durable ID or
  manifest.
- A completed reference-frame resource is immutable and contains the JPEG plus
  truthful timing evidence. Its current absolute source-frame time is unknown:
  `estimated_source_time_utc` and `offset_from_requested_seconds` are null while
  `decoded_local_pts_seconds` is measured.
- The Phase 5 browser owns one canonical integer ROI in source-image pixels.
  The assisted mask is preview evidence only and is never persisted.
- `artifacts/investigations/` already owns durable investigation packages and
  credential-free `manifest.json` files.
- API failures use the existing fixed safe error envelope. Credentials,
  hostnames, URLs, commands, stderr, and authenticated temporary paths never
  enter responses or artifacts.

## Confirmation boundary

Before confirmation, channel, applied time, selected candidate, ROI, and
assistance state are a browser-only draft. Confirmation is the sole transition
that creates a durable object-disappearance investigation package.

The boundary accepts the smallest user-controlled payload needed to prove that
the browser is confirming its current state:

- selected `reference_frame_resource_id`;
- applied reference time and source timezone;
- displayed candidate offset in seconds as a stale-state guard;
- displayed source width and height;
- final integer source-pixel ROI; and
- ROI provenance.

The server does not trust denormalized channel, frame timing, artifact paths, or
generation facts from the browser. It resolves the immutable reference-frame
resource and derives those facts from its validated manifest. It then verifies:

1. the resource is complete and readable through the existing resource store;
2. its channel is positive and matches the selected resource's trusted
   reference-frame manifest. Current inventory may report that a camera is
   offline, but an offline camera must not invalidate a historical recording
   resource that has already been completed;
3. the server derives the candidate offset from the normalized anchor and the
   resource's trusted `requested_time_utc`. If the displayed client offset is
   supplied, it is only an equality guard and must match that derived value;
4. the supplied timezone and applied anchor normalize to the persisted anchor;
5. displayed dimensions exactly match the resource dimensions; and
6. the ROI and provenance satisfy the contracts below.

The candidate's stable identity is the submitted resource ID. The server proves
that identity by resolving the immutable resource and checking its persisted
requested time, channel, generation policy, and complete status; it never admits
a candidate merely because a client submitted an offset. The current
candidate-set implementation has no durable set ID or membership manifest, so
Phase 6 does not claim membership in a prior transient response. The existing
frontend generation/resource guards remain necessary stale-response protection.
Creating candidate-set persistence solely to remove this limitation is out of
scope.

## Artifact and manifest decision

Confirmation extends the existing **investigation package family** rather than
mutating a reference-frame manifest or creating a second persistence root.
Each confirmed object-disappearance investigation is stored at:

```text
artifacts/investigations/
  object-disappearance-ch{channel_id}-{anchor_utc_token}/
    manifest.json
```

The deterministic `investigation_id` is the directory name. The UTC token uses
the existing compact whole-second UTC convention. Including the channel avoids
collisions between cameras at the same anchor. The MVP supports one confirmed
object selection per channel and anchor; a second distinct object at the same
channel and anchor requires a future explicit new-investigation identity flow.

The package references the existing immutable JPEG by resource ID. It does not
copy the JPEG, modify its manifest, or persist the transient mask. This keeps
reference-frame ownership and lifecycle unchanged while giving Phase 7 a
stable, validated lookup key.

Existing unversioned multi-camera investigation manifests are legacy schema 1.
They remain readable by their current consumers, are treated as unconfirmed by
the Phase 7 loader, and are never rewritten automatically. The new confirmed
shape is `schema_version: 2`; implementation must not silently reinterpret an
old package as confirmed.

### Required persisted fields

The schema 2 manifest contains:

- `schema_version`: integer `2`;
- `investigation_id`: deterministic credential-free ID;
- `investigation_kind`: `object_disappearance`;
- `scenario_id`: `object-disappearance`;
- `status`: `confirmed`;
- `anchor_time_utc`: normalized whole-second UTC anchor;
- `source_timezone`: the applied IANA zone, currently normally `Asia/Seoul`;
- `confirmed_at_utc`: server-generated UTC audit time, retained across retries;
- `artifact_directory_relative`: the safe repository-relative package path;
- `confirmation.channel_id`;
- `confirmation.candidate_offset_seconds`;
- `confirmation.reference_frame`: stable resource ID, resource manifest schema
  and generation-policy versions, resource dimensions, requested time text and
  UTC, and frame-selection policy. The JPEG is resolved from this trusted
  resource ID and is neither accepted from the client nor duplicated here;
- `confirmation.timing`: decoded local/clip-relative PTS, nullable estimated source
  time, nullable offset from requested time, precision status, and safe
  warnings copied from the validated reference-frame manifest; and
- `confirmation.roi`: the final ROI plus provenance.

An illustrative shape is:

```json
{
  "schema_version": 2,
  "investigation_id": "object-disappearance-ch1-20260720T033418Z",
  "investigation_kind": "object_disappearance",
  "scenario_id": "object-disappearance",
  "status": "confirmed",
  "anchor_time_utc": "2026-07-20T03:34:18Z",
  "source_timezone": "Asia/Seoul",
  "confirmed_at_utc": "2026-08-02T04:05:06Z",
  "artifact_directory_relative": "artifacts/investigations/object-disappearance-ch1-20260720T033418Z",
  "confirmation": {
    "channel_id": 1,
    "candidate_offset_seconds": -10,
    "reference_frame": {
      "resource_id": "reference-frame-safe-id",
      "schema_version": 1,
      "generation_policy_version": 2,
      "requested_time": "2026-07-20T12:34:13+09:00",
      "requested_time_utc": "2026-07-20T03:34:13Z",
      "source_timezone": "Asia/Seoul",
      "frame_selection_policy": "nearest_decoded_frame",
      "width": 2560,
      "height": 1440
    },
    "timing": {
      "decoded_local_pts_seconds": 2.04,
      "estimated_source_time_utc": null,
      "offset_from_requested_seconds": null,
      "timing_precision_status": "measured_clip_relative",
      "warnings": []
    },
    "roi": {
      "x": 481,
      "y": 927,
      "width": 214,
      "height": 163,
      "coordinate_space": "source_pixels",
      "provenance": "assisted_then_adjusted"
    }
  }
}
```

The resource ID is the only persisted frame reference. No JPEG path is accepted
from or returned to the client, and the confirmation manifest does not duplicate
one. The investigation repository resolves the JPEG and checks its containment
and existence through the trusted reference-frame resource store; it must not
concatenate a client-controlled path.

### Field ownership and validation

| Field group | Authoritative source | Confirmation rule |
| --- | --- | --- |
| Resource ID | Browser selection, then resource store | Treat as an opaque lookup key; reject missing, incomplete, corrupt, or path-unsafe resources. |
| Channel | Validated reference-frame manifest; current inventory is informational for an already completed historical resource | Never accept channel metadata in the body; reject a resource whose trusted channel is invalid or does not match the selected resource. Do not reject solely because the camera is now offline. |
| Applied anchor, timezone, offset | Browser draft checked through the existing input parser; resource time is server-authoritative | Normalize the anchor, derive the offset from trusted resource time, and use any submitted offset only as a stale-state equality guard. |
| Requested frame time and decoder evidence | Validated reference-frame manifest | Copy server-side; never accept replacements from the browser. |
| Source dimensions | Validated reference-frame manifest | Body dimensions are equality guards; the manifest values are authoritative. |
| ROI and provenance | Current user-reviewed browser state | Validate strictly against authoritative dimensions; persist final integer coordinates and one allowed provenance value. |
| Investigation ID, kind, status, artifact path | Confirmation service/repository | Derive deterministically; never accept a client path or ID. |
| Confirmation time | Server UTC clock | Generate only for the winning publication and retain on reuse. |

### Candidate-offset evidence

The implemented candidate policy is request-dependent, not a universal
confirmation allowlist. `src/vigi_vision/reference_frame_candidate_models.py`
defines `DEFAULT_CANDIDATE_OFFSETS` as `(-60, -10, 0, 10, 60)`. The same
module's `ReferenceFrameCandidateSetRequest` accepts one through five unique
integer offsets in the inclusive range `[-300, 300]` and preserves submitted
order. `tests/test_reference_frame_candidate_models.py` covers the default,
explicit custom offsets, cardinality, duplicates, and bounds; API ordering is
also asserted by `tests/test_reference_frame_candidate_api.py`.

Each child request records its requested UTC time in the immutable
reference-frame manifest. The transient
`ReferenceFrameCandidateRequest.offset_seconds` is descriptive evidence; it is
not a persisted membership proof. Phase 6 therefore derives the canonical
offset as:

```text
resource.requested_time_utc - confirmed_anchor_time_utc
```

The `-10` example in this document is valid under the current default policy.
Phase 6 adds no new global offset allowlist.

### Timing truthfulness

The selected candidate's **requested** frame time is durable and exact at the
request boundary. The decoded frame's actual absolute NVR source time is not
currently known. Confirmation therefore persists the nullable timing fields and
precision status without manufacturing an absolute timestamp.

The web review must label these separately. When
`estimated_source_time_utc` is null it shows a concise Korean explanation such
as `정확한 선택 프레임 시각을 확인할 수 없음` and may display the measured
clip-relative PTS as technical evidence. It must never present requested time or
candidate offset as the actual captured instant.

## Canonical ROI contract

The persisted rectangle uses source-image pixels only:

- origin is the top-left source pixel;
- `x` increases rightward and `y` downward;
- `x`, `y`, `width`, and `height` are JSON integers, not booleans;
- the rectangle is half-open: `[x, x + width) × [y, y + height)`;
- `x >= 0`, `y >= 0`, `width >= 4`, and `height >= 4`;
- `x + width <= source_width` and `y + height <= source_height`; and
- the dimensions used for validation must exactly equal the selected resource.

The API rejects floats, strings, booleans, empty/inverted rectangles,
out-of-bounds values, stale dimensions, and integer overflow. It does not clip,
round, normalize, or repair input. The existing frontend `Math.round()` and
source-bound clamping are the browser-to-source-pixel conversion boundary.

Normalized coordinates are not persisted because they are derivable and add a
second representation that can disagree after rounding. A later consumer may
derive them from the persisted rectangle and exact source dimensions.

### ROI provenance

`provenance` is one of:

| Value | Meaning |
| --- | --- |
| `manual` | The final rectangle was created manually and no accepted assisted suggestion precedes it. |
| `assisted` | An accepted assisted suggestion produced the final rectangle and no geometry edit followed. |
| `assisted_then_adjusted` | An assisted suggestion was accepted, then any pointer or keyboard move/resize changed the canonical rectangle. |

Reset clears both rectangle and provenance. A new manual draw after reset is
`manual`. Mask availability does not affect provenance; the mask remains
transient and the final rectangle is always authoritative.

## HTTP API contract

Phase 6-2 adds one create-or-resolve endpoint:

```http
POST /api/v1/investigation-confirmations
```

Request fields are the user-controlled confirmation-boundary fields listed
above. The route parses transport data, invokes one confirmation service, and
uses the existing safe error envelope. It does not write files directly.

```json
{
  "reference_frame_resource_id": "reference-frame-safe-id",
  "reference_time": "2026-07-20T12:34:18+09:00",
  "source_timezone": "Asia/Seoul",
  "candidate_offset_seconds": -10,
  "source_width": 2560,
  "source_height": 1440,
  "roi": {
    "x": 481,
    "y": 927,
    "width": 214,
    "height": 163,
    "provenance": "assisted_then_adjusted"
  }
}
```

There is no path parameter on create because the current web flow has no
persisted draft investigation ID. The service creates its deterministic
investigation identity from the trusted resource channel and normalized anchor.
Thus a client cannot attach a candidate or channel to an arbitrary existing
investigation. The current candidate/resource relation is proven from resource
facts rather than a nonexistent draft manifest.

Successful responses contain only safe canonical facts:

- `investigation_id`;
- `outcome`: `created` or `reused`;
- `status`: `confirmed`;
- `schema_version`;
- `confirmed_at_utc`;
- `artifact_directory_relative`; and
- a canonical confirmation summary suitable for replacing browser draft state.

The first successful publication returns `201 Created`. An identical completed
confirmation returns `200 OK` with `outcome: reused` and the original audit
time. A minimal read endpoint is also required so refresh/reopen can show the
immutable confirmed state without resubmitting it:

```json
{
  "investigation_id": "object-disappearance-ch1-20260720T033418Z",
  "outcome": "created",
  "status": "confirmed",
  "schema_version": 2,
  "confirmed_at_utc": "2026-08-02T04:05:06Z",
  "artifact_directory_relative": "artifacts/investigations/object-disappearance-ch1-20260720T033418Z",
  "confirmation": {
    "channel_id": 1,
    "candidate_offset_seconds": -10,
    "reference_frame_resource_id": "reference-frame-safe-id",
    "requested_time_utc": "2026-07-20T03:34:13Z",
    "timing": {
      "estimated_source_time_utc": null,
      "timing_precision_status": "measured_clip_relative"
    },
    "source_width": 2560,
    "source_height": 1440,
    "roi": {
      "x": 481,
      "y": 927,
      "width": 214,
      "height": 163,
      "provenance": "assisted_then_adjusted"
    }
  }
}
```

```http
GET /api/v1/investigation-confirmations/{investigation_id}
```

It returns the same canonical safe representation or a fixed safe error. No
update/delete endpoint, draft resource, arbitrary artifact-path parameter, or
client-selected investigation ID is part of the MVP.

### Safe failures

Use fixed messages and the existing envelope. The transport mapping is:

| Condition | Status | Safe code/category |
| --- | ---: | --- |
| Malformed JSON or structurally invalid request | 400 | `invalid_request` |
| Reference frame or investigation not found | 404 | existing `resource_not_found` or `investigation_not_found` |
| Current request disagrees with the resource, dimensions, anchor, or existing confirmation | 409 | `stale_selection` or `confirmation_conflict` |
| Resolved resource's trusted channel or requested-time relation disagrees with the confirmation | 409 | `stale_selection` |
| An identical publication currently owns the claim | 409 | `confirmation_in_progress` |
| ROI, timezone, or provenance violates the contract | 422 | `invalid_confirmation` |
| Referenced resource fails integrity validation | 500 | existing `resource_corrupt` |
| Existing confirmation fails strict parsing | 500 | `confirmation_corrupt` |
| Staging, writing, or promotion fails | 500 | `artifact_failure` |
| Unclassified safe fallback | 500 | `internal_error` |

Neither logs nor responses include raw exception text unless the existing error
type explicitly guarantees a fixed secret-safe string.

## Idempotency, concurrency, and immutability

Canonical equivalence includes every persisted confirmation fact except the
server-generated `confirmed_at_utc`. The service derives and serializes the
canonical document before publication.

- Two sequential identical submissions resolve to the same investigation and
  secondarily return `reused`; they never rewrite the manifest.
- If the client loses the first response, an identical retry safely recovers
  the confirmed result.
- Concurrent requests use an exclusive sibling claim. One invocation writes;
  another receives `confirmation_in_progress` and may retry after the first
  completes.
- A materially different submission for the same deterministic ID returns
  `confirmation_conflict`. It does not overwrite, merge, or create a numbered
  sibling implicitly.
- A confirmed package is immutable. Reopen is read-only; editing and explicit
  creation of a replacement investigation are future product decisions.

The browser also disables the confirmation action while a request is active,
but server-side idempotency remains authoritative for double clicks, retries,
and multiple tabs.

### Claim record and abandoned-claim recovery

The existing reference-frame claim is an empty exclusive file and has no
orphan-recovery metadata. It is therefore not a suitable recovery policy for
confirmation. Phase 6 defines the smallest local policy needed for this new
claim type:

- A confirmation claim is a direct child named for the deterministic
  investigation ID. Its JSON record contains a random `operation_id`,
  `created_at_utc`, and refreshed `heartbeat_at_utc`. A process ID may be
  recorded for diagnostics but is not used as proof because PIDs can be reused.
- A 30-minute stale threshold applies to the heartbeat. This is deliberately
  longer than the current bounded candidate workload (at most five serial
  requests, each using the existing six-second frame window and bounded
  startup/finalization policy), so a normally slow local operation is not
  stolen. Age is only a recovery signal because the repository has no stronger
  liveness service.
- Recovery first acquires a sibling recovery lock with exclusive create. While
  holding it, the process rechecks that no final directory exists, rereads and
  validates the claim record, and confirms that its heartbeat is at least 30
  minutes old. Missing, malformed, future-dated, or otherwise unverifiable
  metadata is never recovered and returns safe `confirmation_in_progress`.
- A validated stale claim may be removed and replaced only while holding that
  recovery lock. The replacement claim is created atomically with exclusive
  create and receives a new operation ID and timestamps. No active or recently
  created claim is deleted.
- Publication cleanup removes a claim only when the current operation can prove
  that its operation ID still owns the claim. Staging cleanup follows the same
  invocation-ownership rule.

If two processes attempt stale recovery, only the process holding the recovery
lock can revalidate and replace the claim; the other rereads the claim and
returns `confirmation_in_progress` or the newly published result. This is a
local filesystem policy, not distributed locking.

## Durable publication protocol

Phase 6-2 should mirror the proven reference-frame artifact lifecycle. The
mandatory retry order is:

1. Resolve and validate the investigation and requested confirmation identity.
2. Check whether the final immutable confirmation directory already exists.
3. If it exists, return the existing result for an identical request; return
   conflict for materially different input, or a safe corruption error if it
   cannot be parsed.
4. Only if no final result exists, inspect or acquire the exclusive claim.
5. Recover a claim only when it is demonstrably stale under the claim policy;
   otherwise return `confirmation_in_progress` without deleting it.
6. Stage, revalidate, and atomically publish the confirmation.
7. Clean up only a claim and staging directory owned by the current operation.

The resulting cases are explicit:

- **Claim and final directory both exist:** the final directory wins. Return
  identical reuse or conflict/corruption as appropriate; never return
  `confirmation_in_progress` merely because claim cleanup was interrupted.
- **Live claim with no final directory:** return `confirmation_in_progress` and
  leave the claim untouched.
- **Stale, valid claim with no final directory:** acquire the recovery lock,
  revalidate, replace the claim atomically, and continue publication.
- **Ownership or staleness cannot be proven:** return safe
  `confirmation_in_progress`; do not delete or overwrite the claim.
- **Restart after final publication before claim cleanup:** a later request
  returns the final result first; safe claim cleanup is independent and cannot
  invalidate that result.
- **Client timeout after server publication:** an identical retry returns
  `reused` from the final directory.
- **Two stale-recovery attempts:** the exclusive recovery lock serializes them;
  the loser rereads the claim and returns the published result or
  `confirmation_in_progress`.

The publication details are:

1. Parse and validate the request without creating the final directory.
2. Resolve and strictly validate the completed reference-frame resource and its
   server-owned JPEG path through the existing resource store.
3. Derive the investigation ID and canonical manifest.
4. Create a unique invocation-owned staging directory under the same
   `artifacts/investigations/` filesystem.
5. Write deterministic UTF-8 JSON with sorted keys, stable indentation, and one
   trailing newline. Flush and `fsync` the file where supported.
6. Re-resolve the reference-frame resource immediately before promotion so a
   manually removed or corrupted dependency cannot be confirmed knowingly.
7. Atomically rename the staging directory to the final directory without
   overwrite. Sync the parent directory where the platform supports it.
8. Remove only the invocation-owned staging directory and claim on failure.

Atomic rename prevents readers from observing a partial final package. Filesystem
durability after sudden power loss varies by platform; file and parent syncing
are required where supported and documented as best-effort on platforms that do
not expose equivalent guarantees. No implementation may delete a pre-existing
final package or reference-frame resource during cleanup.

The reference-frame resource remains a separate immutable dependency. Manual
external deletion after publication cannot be made atomic with the confirmation
package; the Phase 7 loader must revalidate it and fail safely if unavailable.

## State model

```text
browser draft (not durable)
  -> confirming (transient request state)
  -> confirmed (immutable durable package)
```

A confirmation failure returns the browser to its reviewable draft with the
current valid candidate and ROI intact. There is no durable `confirming` or
`failed` manifest. Once confirmed, candidate and ROI editing controls become
read-only for that investigation. Identical retry and read are allowed; mutation
is not.

## Inline Korean web flow

Phase 6-3 adds a final review section within the existing page, not a new app or
route. It presents:

- selected reference-frame image with the final rectangle overlay;
- channel;
- applied anchor in KST and the source timezone;
- selected candidate requested time and offset;
- actual/estimated frame time only when evidence supports it, otherwise the
  explicit unavailable message;
- source dimensions and integer ROI;
- Korean provenance label; and
- the safe destination shown after success.

The primary action label is `조사 조건 확정`. It is enabled only when the
applied time is current, a successful selected image is loaded, the Phase 6
snapshot and provenance are valid, and no candidate, assisted-selection, or
confirmation request is active.

While pending, the action is disabled, the section uses `aria-busy="true"`, and
a concise polite live message reports progress without inventing a percentage.
Success moves focus to a confirmed summary, announces completion, displays the
investigation ID and safe artifact location, and makes the review state
read-only. Failure moves focus or announcement to the existing error/status
surface, preserves the valid draft, and permits retry. The layout remains the
existing single-column mobile flow with a full-width action and source-aspect
image; no modal or hover-only interaction is introduced.

## Phase 7 handoff

Phase 7 consumes a confirmed domain object, not browser state, API models, or
unvalidated JSON. A narrow repository/loader boundary should expose one method:

```text
load_confirmed(investigation_id) -> ConfirmedInvestigationInput
```

The loader:

- confines lookup to `artifacts/investigations/`;
- rejects symlinks, traversal, unknown schema versions, duplicate/unknown keys,
  non-`confirmed` status, and legacy manifests;
- parses time, dimensions, ROI, provenance, and timing evidence strictly;
- resolves the reference-frame resource and its JPEG through the existing store,
  applying path-containment and artifact-existence checks rather than reading a
  confirmation-supplied path;
- verifies channel, requested time, generation facts, and dimensions still agree
  with the confirmation snapshot; and
- returns credential-free typed values and resolved local paths only inside the
  service boundary.

Phase 7 search/classification receives this loader output and cannot accept an
unconfirmed frontend snapshot. A missing/corrupt referenced frame, corrupt
confirmation, or unsupported schema fails before media or model work.

## Backward compatibility

- Existing reference-frame resource schemas, image endpoints, candidate-set
  endpoints, ROI suggestion endpoints, and frontend selection behavior do not
  change.
- Existing reference-frame packages remain immutable and are never rewritten
  or adopted as confirmation packages.
- Existing unversioned multi-camera investigation manifests default to legacy
  schema 1 for readers that understand them. They have no implied confirmation
  status and Phase 7 returns a safe `not_confirmed` result before processing.
- No destructive migration is required. A future explicit migration could
  create a new schema 2 package only from independently validated user intent;
  it must not infer an ROI from legacy data.
- Unknown future schema versions fail closed rather than being treated as the
  current contract.

## Security, privacy, and semantic limits

- Persist and return only credential-free IDs, safe relative paths, local audit
  facts, and fixed safe errors.
- Never persist or expose NVR credentials, hostnames, RTSP/replay URLs, ffmpeg
  arguments, subprocess commands, stderr, or authenticated temporary paths.
- Keep artifacts local and Git-ignored under the existing artifact policy.
- Confirmation records what the user selected. It does not establish object
  identity, ownership, disappearance, theft, intent, payment, a responsible
  person, cross-camera linkage, or continuous tracking.
- Do not infer an exact source instant from requested time, candidate offset, or
  clip-relative PTS.

## Failure recovery

- Validation/resource failure before staging creates no package.
- Write/promotion failure removes only the current staging directory and claim;
  the browser retains its draft for retry.
- A stale or changed selection receives a conflict and must be reviewed again.
- An existing identical package is returned, not rebuilt.
- An existing conflicting or corrupt package is preserved for operator review;
  the service never deletes it automatically.
- A legacy unconfirmed package remains readable by its legacy consumer but is
  rejected by the Phase 7 confirmation loader.

## Delivery slices

### Phase 6-2: backend persistence

- Add typed confirmation request/result/domain models and validation.
- Add the confirmation service and repository with claim, staging, strict read,
  canonical equivalence, and atomic promotion.
- Add POST/GET transport models and fixed safe error translation.
- Preserve all existing reference-frame, candidate, ROI-suggestion, recording,
  and multi-camera investigation contracts.
- Test geometry, provenance, stale-state checks, schema parsing, idempotent retry,
  conflicts, concurrent claims, partial-write cleanup, resource revalidation,
  legacy rejection, and credential redaction.

### Phase 6-3: web confirmation

- Track provenance alongside the existing canonical rectangle.
- Extend the Phase 6 snapshot with the confirmation request fields without
  changing ROI interaction behavior.
- Add the inline Korean review/pending/success/failure/read-only states.
- Add POST/GET integration, duplicate-action suppression, refresh/reopen, stale
  response guards, focus management, and mobile/keyboard accessibility tests.

### Phase 6-4: real-NVR validation

- Confirm a manual ROI, an assisted ROI, and an assisted-then-adjusted ROI on
  real local reference-frame resources.
- Verify created/reused/conflict behavior, durable JSON, restart/reopen loading,
  truthful unavailable absolute timing, and Phase 7 loader readiness.
- Exercise controlled write/promotion failure and dependency-removal recovery
  without deleting pre-existing artifacts.
- Record only credential-free evidence and leave NVR, SDK, and external services
  unchanged.

## Focused testing strategy

Backend tests should cover strict request parsing, current-channel validation,
resource/time/dimension ownership, every ROI boundary, all provenance
transitions, deterministic identity, canonical JSON, identical reuse, lost
response retry, concurrent claim conflict, different-payload conflict, staging
cleanup, dependency disappearance before promotion, restart/read, malformed and
legacy manifests, Phase 7 loader rejection, path containment, symlink rejection,
and secret redaction.

Frontend tests should exercise enablement from the actual selected image and ROI,
manual/assisted/adjusted provenance, stale candidate and dimension rejection,
duplicate-click suppression, pending/live/focus behavior, safe retry with the
draft preserved, identical/already-confirmed rendering, read-only state after
success or reopen, unavailable actual-time wording, keyboard access, and narrow
mobile layout. Phase 6-4 then verifies create, reuse, restart, reopen, and loader
behavior against local real-NVR artifacts without adding automated NVR tests.

## Deferred decisions and known limitations

- Editing a confirmed investigation or confirming two objects for the same
  channel and anchor requires a new identity/user flow.
- Retention, deletion, export, database indexing, and multi-host coordination are
  not defined.
- Candidate sets are not durable, so membership is validated by deterministic
  resource/time facts rather than a set manifest.
- The current web draft has no forward search-end field. Phase 7 must define its
  bounded search horizon as a separate validated input or policy; confirmation
  must not invent or silently persist one.
- Reference-frame dependency and confirmation publication are not one filesystem
  transaction; the loader detects later dependency loss.
- Exact absolute selected-frame time remains unavailable until an evidence-backed
  calibration policy changes the reference-frame contract.
- Crop previews may be derived in the browser from the selected image and ROI;
  Phase 6 does not persist another image solely for preview.

## Acceptance criteria for implementation

Phase 6 is ready to hand to Phase 7 only when an operator can review one current
candidate/ROI, confirm it once, safely retry or reopen it, and load the same
immutable typed input after process restart; no partial package is observable,
no conflicting submission overwrites it, and all timing/security limitations
remain explicit.
