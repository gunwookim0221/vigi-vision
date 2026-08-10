# Phase 7 Object-Disappearance Recording Search MVP

## Status and normative authority

**Status: normative design for the current single-site Phase 7 MVP. Phase 7A-1
implements only the validated local run lifecycle, baseline gate, isolated
repository, duplicate/interruption handling, and safe start/status API. Recording
acquisition, classifier, search orchestration, and Phase 8 review-media generation
remain unimplemented; the required Phase 6C schema 3 compatibility increment is
complete.**

This document is the current implementation and review contract for Phase 7.
It is intentionally limited to one restaurant, one local application host, one
NVR, and one active search run per investigation. Normally one person uses the
application; a second person may open the same local workflow.

Earlier lease, fencing, takeover, resume, crash-recovery, full-manifest, and
multi-process analysis is preserved only in
[Recording Search Resilience: Future Reference](../future/recording-search-resilience.md).
That document is non-normative and is not part of MVP implementation, review,
or completion criteria.

## Scope and phase boundaries

Phase 7 searches recorded video for a candidate interval in which one
user-confirmed object stopped being visible in its original ROI.

```text
Phase 6 confirmed investigation
  -> Phase 7 coarse recording samples
  -> PRESENT / ABSENT / INDETERMINATE observations
  -> binary narrowing of one candidate interval
  -> Phase 8 handoff request
  -> Phase 9 user judgment
```

Responsibilities are fixed:

- **Phase 6** schema 3 persists the reviewed reference-frame resource,
  authoritative JPEG digest/size, source-pixel ROI, channel, reference time,
  dimensions, timing evidence, and provenance.
- **Phase 7** validates that input, samples the same channel's recordings,
  classifies the confirmed region, and persists a candidate interval.
- **Phase 8** creates review images, an evidence timeline, and video around the
  candidate boundary.
- **Phase 9** lets the user make the final decision.

Phase 7 does not create review video, identify or track people, infer object
ownership, declare theft, determine cause or intent, or claim an exact event
instant. `ABSENT` means only that the confirmed object is not sufficiently
supported as visible in the confirmed region for that observation.

## Existing boundaries to reuse

The implementation composes existing services instead of copying them:

| Boundary | Phase 7 use | Excluded responsibility |
| --- | --- | --- |
| `InvestigationConfirmationService.load_confirmed()` | Load the strict immutable Phase 6 input and trusted JPEG path. | Browser state or confirmation mutation. |
| Existing recording coverage resolver | Find covered UTC intervals and gaps for one channel and range. | Object state or search decisions. |
| `RecordingPlanner` and `ReplayExtractor` | Plan and extract bounded temporary replay media. | Classification or durable search state. |
| Existing reference-frame decoder boundary | Continue serving existing single-target callers unchanged. | Multi-target identity or transition reasoning. |
| New Phase 7 batch decoder extension | Resolve one bounded target set in one decode session and return frame identity, order, timing, and digest facts. | Classification or search decisions. |
| New `ObjectObservationClassifier` production adapter | Combine EfficientSAM-Ti masks with a deterministic aligned-ROI appearance comparison and return one typed observation. | Search order, filesystem, or media acquisition. |
| New `RecordingSearchService` | Own run lifecycle and compose acquisition, classification, search, persistence, and handoff. | HTTP/CLI parsing or Phase 8 media generation. |

The generic `sample-recording` command is neither subprocess nor search engine.

## Inputs and preconditions

### Request

The transport shape is deferred. The service accepts one validated internal
request:

```text
RecordingSearchRequest
  investigation_id: string
  search_end_time_text: string
  source_timezone: string
```

`investigation_id` is a lookup key only. The caller cannot resubmit channel,
ROI, dimensions, reference resource, artifact paths, or decoder facts.
`source_timezone` is a stale-input guard and must exactly equal the normalized
Phase 6 value.

The search end accepts an offset-aware whole-second value, or an unambiguous
naive whole-second value when Phase 6 preserved an IANA timezone. It must be
strictly later than the confirmed reference requested time, not in the future,
and no more than 24 hours later. Ambiguous, nonexistent, fractional, or
incompatible local times fail before run creation.

### Current and required Phase 6 handoff

The schema 2 read-only display representation currently provides these persisted
facts:

```text
investigation_id
channel_id
anchor_time_utc
source_timezone
candidate_offset_seconds
reference_frame_resource_id
requested_time_text
requested_time_utc
generation_policy_version
frame_selection_policy
estimated_source_time_utc | null
decoded_local_pts_seconds | null
timing_precision_status
warnings
source_width
source_height
roi
jpeg_path  # trusted internal path; never persisted or exposed
```

Schema 2 does not bind the JPEG bytes to the confirmation. It is therefore not
eligible for automatic Phase 7 search. Phase 6C publishes schema 3 confirmations
and extends `ConfirmedInvestigationInput` with these server-owned fields:

```text
jpeg_sha256       # lowercase SHA-256 computed at confirmation
jpeg_size_bytes   # positive byte size computed from the same bytes
```

Phase 6 computes both values only after resolving the resource through its
trusted store, fully decoding the JPEG, checking decoded dimensions, and
validating the source-pixel ROI. They are persisted beside the existing
resource ID, dimensions, ROI, and `coordinate_space: source_pixels`; neither
field is accepted from the browser. Schema 3 uses a versioned deterministic
investigation-ID namespace, so reconfirmation publishes a new immutable package
without replacing a schema 2 package.

Existing schema 2 confirmations remain readable by Phase 6 but fail the Phase 7
eligibility check with `reconfirmation_required` and direct the user to Phase
6C's **Reconfirm for recording search** action. That action reopens the existing
JPEG and source-pixel ROI read-only, displays both for explicit review, and on
activation publishes a new schema 3 package under a new versioned identity. It
preserves the schema 2 package, resolves and revalidates the trusted resource and
ROI, and computes digest and size at the new confirmation moment. Phase 7 never
computes a first digest from a legacy path and treats that value as historical
truth.

It does not provide a camera serial number, NVR identity, or stable
`source_identity`. Phase 7 must not describe one as Phase 6-confirmed evidence.
If existing inventory metadata explicitly proves that the configured channel no
longer matches, the run stops and requests reconfirmation. When the SDK can
prove only that the same positive channel ID is available, the MVP documents
that physical-camera continuity cannot be guaranteed automatically.

### Preconditions

Before creating an acquisition frame or later recording observation, Phase 7
must:

1. call the Phase 6 `load_confirmed()` boundary and require schema 3;
2. use only its already confined `jpeg_path`, authoritative SHA-256, byte size,
   resource ID, dimensions, and ROI;
3. read the file once into invocation-owned immutable bytes, recompute SHA-256
   and byte size, fully decode those exact bytes, and require decoded dimensions
   to equal the confirmed dimensions;
4. reject a missing, ambiguous, outside-root, indirect, corrupt, size-mismatched,
   digest-mismatched, or out-of-bounds baseline;
5. verify the channel is currently usable for recording search, without
   treating a merely offline camera as proof that historical media is invalid;
6. normalize and validate the search end; and
7. verify the selected classifier, checkpoint, comparison, and acquisition
   policy versions are available.

A failed baseline gate ends with `FAILED` and `baseline_validation_failed`. It
creates no baseline `PRESENT`, probe `ABSENT`, or terminal `FOUND`. It does not
require the historical recording segment used to create the reference JPEG.

## Single-host run lifecycle

### Run identity and storage

Every attempt receives a unique safe `search_run_id`. Results are isolated:

```text
artifacts/investigation-searches/
  .locks/
    {investigation_id}.lock
  {investigation_id}/
    {search_run_id}/
      manifest.json
      observations/            # future classifier version, not schema 2
      evidence/
      phase8-request.json  # only after FOUND; created separately
```

All IDs are validated before path construction. Paths remain below the ignored
artifact root. Symlinks, junctions, reparse points, traversal, foreign files,
and unsafe IDs fail closed. Successful evidence files are immutable and use
no-overwrite creation. `manifest.json` may be updated through same-directory
atomic replacement using the project's normal local artifact convention.

One run never reads observations from another run. A failed or interrupted run
remains historical evidence until an explicit retention action outside the
search algorithm removes it.

### Starting and observing a run

For each investigation, one stable OS-backed exclusive lock protects active
execution:

1. A start request attempts to acquire the lock within a short bounded timeout.
2. If another process holds it, the service returns `ALREADY_RUNNING` and the
   currently visible run ID when available. It creates no directory or run.
3. After acquiring the lock, the service checks strict existing run manifests,
   creates a new `search_run_id` and directory exclusively, writes `PENDING`,
   then changes it to `RUNNING` before long work.
4. The process holds the lock until it writes a terminal state or exits.
5. Refresh or reopen calls a status lookup; it never starts a run.

Frontend double-click suppression is helpful but not authoritative. The lock
and service result make duplicate starts deterministic across tabs or two local
users.

### Phase 7A-1 HTTP boundary

The existing local FastAPI application exposes only the lifecycle boundary in this
slice:

- `POST /api/v1/recording-searches` accepts `investigation_id`,
  `search_end_time_text`, and `source_timezone`; all baseline facts are loaded
  server-side.
- `GET /api/v1/recording-searches/{investigation_id}/{search_run_id}` returns
  the strict persisted lifecycle manifest and performs the documented interruption
  inspection.

Both routes use the existing credential-safe error envelope. They expose no
filesystem paths, authenticated URLs, subprocess details, or recording-search
results; acquisition and classification remain later slices. The Phase 7A-1
repository accepts only `PENDING`, `RUNNING`, `FAILED`, and `INTERRUPTED`
manifests with fixed safe reason codes. Observation IDs, aliases, candidate
intervals, Phase 8 handoff fields, and later terminal states are rejected until
their implementing slices exist.

### Interruption and explicit restart

If the process exits or the PC reboots, the operating system releases the lock.
On the next status inspection or start request:

1. attempt to acquire the per-investigation lock; if acquisition fails because
   another process still holds it, return the current `RUNNING` status and make
   no state change;
2. after acquiring the lock, strictly inspect the latest nonterminal run;
3. if its manifest is still `PENDING` or `RUNNING`, atomically change it to
   `INTERRUPTED` with a fixed reason and completion time;
4. do not resume it, take it over, or adopt its evidence; and
5. release the lock, or create a new run only when the user explicitly requested
   one.

The new attempt receives a new ID and directory. An interrupted run can never
become `FOUND` without executing a complete new search.

### Persisted states

| State | Terminal | Meaning | UI guidance |
| --- | --- | --- | --- |
| `PENDING` | No | Run directory exists; execution has not started long work. | Starting search. |
| `RUNNING` | No | This local process holds the investigation lock and is searching. | Show status; disable another start. |
| `FOUND` | Yes | A supported candidate interval was persisted. | Show interval/handoff and allow an explicit new run. |
| `NOT_FOUND` | Yes | Required sampling completed and no supported transition was found. | Explain the window and allow an explicit new run. |
| `INDETERMINATE` | Yes | Recording or visual evidence could not support a safe conclusion. | Show the fixed safe limitation and allow a new run. |
| `FAILED` | Yes | Input, storage, or unexpected infrastructure failure ended the run safely. | Show a fixed safe failure and allow a new run. |
| `INTERRUPTED` | Yes | A prior nonterminal run lost its process lock. | Explain interruption; offer explicit new run. |

Allowed transitions are:

```text
PENDING -> RUNNING
PENDING -> FAILED
PENDING -> INTERRUPTED
RUNNING -> FOUND
RUNNING -> NOT_FOUND
RUNNING -> INDETERMINATE
RUNNING -> FAILED
RUNNING -> INTERRUPTED
```

Terminal states never reactivate. Every terminal state permits an explicit new
run, which always receives a new ID and never adopts prior evidence.

## Baseline, decoded-frame identity, and observation records

### Acquisition records (Phase 7A-2)

Phase 7A-2 persists acquisition evidence only. It does not classify a frame,
decide `PRESENT`, `ABSENT`, or `INDETERMINATE`, or create an observation. The
strict acquisition variants are:

```text
AcquisitionRecord =
  ProbeFrameRequestRecord
  | CanonicalProbeFrameRecord
```

`CanonicalProbeFrameRecord` is the durable identity and provenance for one
validated JPEG decoded by one bounded batch operation:

```text
CanonicalProbeFrameRecord
  record_type: canonical_probe_frame
  canonical_frame_id
  investigation_id
  search_run_id
  operation_id
  channel_id
  acquisition_id
  source_segment_id
  segment_start_utc
  segment_end_utc
  extraction_start_utc
  extraction_end_utc
  decode_session_id
  physical_replay_origin_utc
  source_pts
  source_time_base: numerator, denominator
  decoded_frame_utc
  decoded_pts              # exact decoder PTS ticks
  replay_time_base: numerator, denominator
  decoded_ordinal
  source_width, source_height
  jpeg_relative_path        # run-relative, never absolute, never contains '..'
  jpeg_sha256
  jpeg_size_bytes
  acquired_at_utc
```

The following closed field contract is normative. It reuses the implemented A1
identifier grammar for `investigation_id` and `search_run_id`; the remaining A2
identifiers use the same lowercase ASCII, hyphen-separated convention with the
stated maximum length. Every field is required, server-owned, and forbidden from
being repeated under another name. No unknown or duplicate JSON key is accepted.

| Field | Serialized type and grammar | Range/normalization and relational checks | Role |
| --- | --- | --- | --- |
| `record_type` | exact string `canonical_probe_frame` | exact, immutable | required provenance but not an identity input |
| `canonical_frame_id` | string `frame-[0-9a-f]{64}` | max 70 characters; equals the derived hash below | operational metadata |
| `investigation_id` | existing A1 grammar `object-disappearance-(v3-)?ch[1-9][0-9]*-[0-9]{8}T[0-9]{6}Z` | max 128 characters; exact owning investigation | canonical identity input |
| `search_run_id` | existing A1 grammar `search-run-[0-9a-f]{8,64}` | max 75 characters; exact owning run | canonical identity input |
| `operation_id` | string `acquisition-op-[a-z0-9-]{1,96}` | max 111 characters; operation that first published this frame | operational metadata |
| `channel_id` | strict integer | `1..2^31-1`; equals the immutable confirmed baseline and owning segment | canonical identity input |
| `acquisition_id` | string `acquisition-[0-9a-f]{64}` | max 76 characters; trusted segment/window/policy identity | operational metadata |
| `source_segment_id` | exact `segment-YYYYMMDDTHHMMSSZ-YYYYMMDDTHHMMSSZ` from the existing `segment_identity()` boundary | exact trusted channel segment; no invented recording-session ID | canonical identity input |
| `segment_start_utc`, `segment_end_utc` | canonical UTC string `YYYY-MM-DDTHH:MM:SSZ` | `start < end`; whole-second precision; trusted segment coverage | required provenance but not an identity input |
| `extraction_start_utc`, `extraction_end_utc` | canonical UTC string `YYYY-MM-DDTHH:MM:SSZ` | `start < end`; exact bounded replay window used for this decode | required provenance but not an identity input |
| `decode_session_id` | string `decode-session-[a-z0-9-]{1,96}` | max 111 characters; identifies one decoder attempt | operational metadata |
| `physical_replay_origin_utc` | canonical UTC string with exactly 6 fractional digits, `YYYY-MM-DDTHH:MM:SS.ffffffZ` | decoder/NVR source-timing origin for `source_pts = 0`; never copied from the requested target or URL start; explicitly supplied by the A2 timing capability | required provenance but not an identity input |
| `source_pts` | strict non-negative integer JSON value | exact source/container PTS ticks before any timestamp-reset filter; no float, exponent, string, or coercion | required provenance but not an identity input |
| `source_time_base.numerator`, `source_time_base.denominator` | strict integers | both positive and at most `2^31-1`; reduced by greatest common divisor | required provenance but not an identity input |
| `decoded_frame_utc` | canonical UTC string with exactly 6 fractional digits, `YYYY-MM-DDTHH:MM:SS.ffffffZ` | derived as `physical_replay_origin_utc + source_pts * source_time_base`, rounded once to nearest microsecond with ties-to-even; representable at this precision and inside segment coverage using its half-open end | canonical identity input |
| `decoded_pts` | strict non-negative integer JSON value | exact replay-local decoder PTS ticks after `setpts=PTS-STARTPTS`; no float, exponent, string, or coercion | required provenance but not an identity input |
| `replay_time_base.numerator`, `replay_time_base.denominator` | strict integers | both positive and at most `2^31-1`; reduced by greatest common divisor | required provenance but not an identity input |
| `decoded_ordinal` | strict integer | non-negative, zero-based, at most `2^63-1`; unique only within this decode attempt | required provenance but not an identity input |
| `source_width`, `source_height` | strict integers | positive and within the existing image limit of `1..16384`; equal the validated JPEG dimensions and confirmed baseline | required provenance but not an identity input |
| `jpeg_relative_path` | POSIX relative string `evidence/frames/frame-[0-9a-f]{64}.jpg` | normalized, no `.`/`..`, backslash, drive, URI, symlink, reparse point, or root escape; remains under this run | operational metadata |
| `jpeg_sha256` | lowercase string of exactly 64 hexadecimal characters | equals the validated JPEG bytes | operational metadata |
| `jpeg_size_bytes` | strict integer | positive, at most 256 MiB; equals the JPEG byte length | operational metadata |
| `acquired_at_utc` | canonical UTC string with exactly 6 fractional digits, `YYYY-MM-DDTHH:MM:SS.ffffffZ` | not earlier than the operation start or the decoded frame; exact UTC comparison | operational metadata |

The ownership checks are mandatory: investigation, run, operation, and channel
must equal their owning manifest/handle; the segment must belong to the channel
and operation; the request and frame must agree on every repeated identity; and
the decoded UTC, source PTS, source time base, replay-local PTS, replay time
base, ordinal, extraction window, and segment must describe the same
decoder-selected frame. Mandatory provenance that is absent or unverifiable
fails before a frame is published. `setpts=PTS-STARTPTS` makes the decoder PTS
replay-local, so `segment_start_utc + PTS` and `extraction_start_utc + PTS` are
not valid source-time mappings. The only authoritative derivation is the exact
physical replay origin supplied by the A2 source-time capability plus raw
source/container PTS scaled by its positive source time base:
`decoded_frame_utc = physical_replay_origin_utc + source_pts *
source_time_base`, rounded once to six fractional digits with ties-to-even. The
requested `requested_time_utc`, recording request start, extraction-window start,
replay-local PTS, and ordinal are never substituted for this decoded result.
The A2 decoder must preserve source timing independently of the `setpts` reset;
the current A1 direct decoder does not provide this capability. If the physical
origin is missing, ambiguous, discontinuous, the source PTS is negative, either
time-base component is non-positive, the normalized UTC is outside segment
coverage, or an overlapping acquisition cannot reproduce the same normalized
UTC for the same physical frame, the request fails with `missing_provenance`.
Strict reopen repeats the exact rational calculation and rejects any mismatch.
If two distinct frames normalize to one UTC at persisted precision, neither is
published under a colliding ID.
The record has no `state`, `reason_code`, mask, confidence, similarity, or
classifier field. The JPEG is reopened from the confined relative path and its
format, bytes, byte length, digest, and dimensions are revalidated before
publication and on every read. Records are immutable and cannot be overwritten.

`canonical_frame_id` is the lowercase hexadecimal SHA-256 of the UTF-8 bytes of
one canonical JSON object with exactly these ordered keys:
`investigation_id`, `search_run_id`, `channel_id`, `source_segment_id`, and
`decoded_frame_utc`. JSON uses compact separators, UTF-8, no escaping of ASCII,
and strict integer/decimal serialization. The value is encoded as `frame-` plus
the 64-character digest. Requested time, `acquisition_id`, source PTS,
source/replay time-base, decoded PTS, decoded ordinal, JPEG digest, dimensions, operation ID, and
invocation/decode-attempt tokens do not define identity. The segment identity is
the existing trusted `segment_identity()` value, not a fabricated session ID.
Two acquisitions of the same segment and normalized decoded-frame UTC therefore
derive the same ID. A decoder must reject duplicate normalized positions within
one segment, so different authoritative frames cannot collide merely because
their replay-local PTS, ordinal, requested time, JPEG digest, or dimensions
match. Strict reopen recomputes this one tuple and rejects any mismatch.
The `jpeg_relative_path` is an artifact reference, not a public filesystem path.

`ProbeFrameRequestRecord` represents one requested target and its acquisition
outcome:

```text
ProbeFrameRequestRecord
  record_type: probe_frame_request
  probe_request_id
  investigation_id
  search_run_id
  operation_id
  channel_id
  requested_time_utc
  status: PENDING | SUCCEEDED | FAILED
  canonical_frame_id | null
  alias_of_probe_request_id | null
  failure_reason | null
  created_at_utc
  completed_at_utc | null
```

The request fields are closed as follows:

| Field | Serialized type and grammar | Required/relational rule |
| --- | --- | --- |
| `record_type` | exact string `probe_frame_request` | required and immutable |
| `probe_request_id` | string `probe-request-[a-z0-9-]{1,96}` | required, max 110 characters, unique in this run |
| `investigation_id`, `search_run_id`, `operation_id` | the exact canonical grammars above | required; investigation/run equal the owner, and `operation_id` is this request's publishing operation listed in `acquisition_operation_ids` |
| `channel_id` | strict integer `1..2^31-1` | required and equal to the immutable baseline |
| `requested_time_utc` | canonical UTC `YYYY-MM-DDTHH:MM:SSZ` | required, whole-second precision, within the policy window |
| `status` | exact enum `PENDING`, `SUCCEEDED`, or `FAILED` | required; legal transitions are defined below |
| `canonical_frame_id` | `frame-[0-9a-f]{64}` or JSON `null` | required key; non-null exactly for `SUCCEEDED` |
| `alias_of_probe_request_id` | `probe-request-[a-z0-9-]{1,96}` or JSON `null` | required key; non-null only for an alias success and points to an earlier same-run success |
| `failure_reason` | one closed reason string or JSON `null` | required key; non-null exactly for `FAILED` |
| `created_at_utc`, `completed_at_utc` | canonical UTC `YYYY-MM-DDTHH:MM:SSZ` or JSON `null` | created is required; completed is null only for `PENDING`, and terminal completion is not earlier than creation |

The record parser rejects duplicate, missing, unknown, wrongly typed, and
non-canonical fields. Ownership is checked against the active run, baseline,
operation, and every referenced child; requested time is never accepted from a
client-controlled persisted field.

Frame publication and request publication have distinct ownership meanings.
`CanonicalProbeFrameRecord.operation_id` is the acquisition operation that first
committed that immutable frame. `ProbeFrameRequestRecord.operation_id` is the
operation that created and committed that request relationship. A request may
reference a frame published by another operation only when both operation IDs
are valid entries in the same run's durable `acquisition_operation_ids` index
and resolve to immutable operation records, the frame
operation is a valid frame-acquisition operation, and the request operation is
the current operation publishing the request. Investigation, run, channel,
segment, normalized decoded UTC, and canonical frame ID must agree; the reused
frame's publication operation, provenance, timestamp, artifact path, digest,
and dimensions are immutable and are never rewritten. Operation-ID equality is
not required.

Only the server creates IDs and provenance. `probe_request_id` uses
`probe-request-[a-z0-9-]{1,96}` (maximum 110 characters), and every request's
investigation, run, operation, channel, and requested time match its owner.
The legal state machine is `PENDING -> SUCCEEDED` or `PENDING -> FAILED`; no
terminal record may transition again. `PENDING` may exist only in invocation
staging and has `completed_at_utc`, `canonical_frame_id`, `alias_of_probe_request_id`,
and `failure_reason` all null. A terminal record has a non-null completion time
with `created_at_utc <= completed_at_utc`.

`SUCCEEDED` requires exactly one valid frame reference. A primary success has a
null alias; an alias success has exactly one `alias_of_probe_request_id` pointing
to an earlier successful request in this run and the same frame ID. `FAILED`
requires null frame and alias IDs and exactly one closed reason. The fixed
precedence is: invalid/absent coverage -> `recording_unavailable`; source or
segment resolution -> `acquisition_failed`; decoder failure before
frame selection -> `decode_failed`; missing/invalid PTS, time base, ordinal, or
decoded timestamp -> `missing_provenance`; invalid JPEG encoding, digest, size,
dimensions, or path -> `invalid_artifact`; manifest or publication conflict ->
`publication_conflict`; lock loss or an interrupted owner -> `interrupted`; and
only an otherwise-unclassified safe internal failure -> `unexpected_error`.
Missing PTS, time base, or ordinal always uses `missing_provenance`, never one of
the other categories. No failure maps to `ABSENT`, `INDETERMINATE`, or any
classifier state, and raw SDK, decoder, filesystem, URL, credential, or exception
text is never persisted or exposed.

The duplicate request identity is exactly the ordered tuple
`(search_run_id, channel_id, requested_time_utc)`. The first two values are the
owning run and immutable baseline channel; `requested_time_utc` is normalized to
the canonical whole-second `YYYY-MM-DDTHH:MM:SSZ` string. A probe role is not a
request field in A2, so it is not silently added to the tuple; any future role
requires a new manifest version. The tuple is serialized as one compact UTF-8
JSON array in that order for comparison; the server-generated
`probe_request_id` is not derived from it. Target identity is validated through
the owning run rather than redundantly hashed.

The committed `probe_request_ids` index contains each request exactly once in
creation order, with `probe_request_id` as a tie-breaker for equal timestamps.
Every indexed ID resolves to exactly one child record in the same run; every
child is indexed; no ID is dangling, duplicated, foreign, or omitted. A retry of
a terminal request uses a new request ID and follows the same rules. An equal
duplicate in `SUCCEEDED` reuses its immutable frame relationship. An equal
duplicate in `PENDING` remains serialized behind the active operation and is
not fabricated as complete; an equal duplicate in `FAILED` follows the existing
explicit retry rule and creates a new request only after the failure is committed.
Two different request tuples may legitimately resolve to the same canonical
frame and therefore create two request relationships with one frame index entry.
Three aliases to one frame yield one frame and three request records, not three
evidence frames.

If the process exits while a request is still `PENDING`, that staging record is
discarded or ignored, the run is handled by the A1 `INTERRUPTED` rule, and a
later explicit attempt creates a new request ID. A `PENDING` request is never
recovered as success and never becomes a classifier observation.

### Durable acquisition-operation membership

Every A2 publication operation has one server-created immutable
`AcquisitionOperationRecord`:

```text
AcquisitionOperationRecord
  record_type: acquisition_operation
  operation_id
  investigation_id
  search_run_id
  operation_kind: recording_probe_acquisition_v1
  state: ADMITTED
  admitted_at_utc
```

The record has exactly those fields, uses the identifier and canonical UTC
grammars above, rejects unknown/duplicate/missing fields, and contains no
invocation token, URL, path, command, or client-supplied ownership value. It is
stored at `operations/{operation_id}.json`. The schema-2 manifest field
`acquisition_operation_ids` is a strict array of unique operation IDs in
server-creation order; IDs are never sorted or client-selected. Every indexed ID
resolves to exactly one immutable operation record at that path, and every
operation record is indexed once by its owning manifest. The record's
investigation and run must match the manifest, and `operation_kind` and `state`
are fixed to the values above.

Admission is a server-only transition beneath the already-held A1 OS lock and
the shared per-run A2 mutex. The caller revalidates the active handle, held lock,
acquisition-admitting state, current operation, and latest committed manifest,
then stages the closed operation record, publishes it without overwrite, and
atomically replaces the manifest with the operation ID appended once. No frame
or request may reference the operation before that manifest replacement commits.
The operation record is immutable after admission; merely inserting a string in
`acquisition_operation_ids` is never proof of membership. A staged but uncommitted
operation record is invocation-owned and removed with its staging directory; an
unindexed final record is ignored on reopen and removed when ownership is proven.

Frame and request records must each back-reference an admitted operation record.
Strict reopening rejects a missing, malformed, duplicate, foreign-run,
unindexed, orphaned, or path-mismatched operation record, and rejects any frame
or request whose operation ID does not resolve through the complete index.
`CanonicalProbeFrameRecord.operation_id` is the admitted operation that first
committed that immutable frame. `ProbeFrameRequestRecord.operation_id` is the
admitted operation that committed that request relationship. They may differ,
but both must belong to the same investigation and run. Reuse never rewrites
the frame's original operation, provenance, artifact path, digest, timestamp,
dimensions, or identity.

### Baseline and later observation records

The complete later-phase record union is deliberately separate from acquisition:

```text
ObservationRecord =
  ConfirmedReferenceBaselineRecord
  | RecordingProbeObservationRecord
  | TargetAliasRecord
```

```text
ConfirmedReferenceBaselineRecord
  record_type: confirmed_reference_baseline
  observation_id
  investigation_id
  channel_id
  reference_frame_resource_id
  requested_time_utc
  source_width, source_height
  roi: x, y, width, height, coordinate_space, provenance
  jpeg_sha256, jpeg_size_bytes
  timing_precision_status, warnings
  state: PRESENT
  reason_code: user_confirmed_reference
```

It never contains segment, acquisition, session, PTS, or ordinal fields. After
schema 3 integrity validation, persist it at `S` and initialize `last_present =
S`; never compare the JPEG with itself or require its historical segment.

`RecordingProbeObservationRecord` is created by Phase 7B from one acquired
canonical frame. It contains the frame reference and classifier evidence, not a
second copy of acquisition provenance:

```text
record_type: recording_probe
observation_id
canonical_frame_id
probe_request_id
primary_requested_time_utc
state: PRESENT | ABSENT | INDETERMINATE
reason_code: null | insufficient_visual_evidence
classifier_evidence:
  classifier_policy_version
  mask_iou | null
  roi_luma_ncc | null
```

The referenced canonical frame must be in the same investigation, run, channel,
and target as the successful request. Its frame-publication operation and the
request operation may differ: both must resolve to immutable acquisition
operation records listed by the owning run, and the request must reference the immutable frame published
by the historical operation. The request's frame ID must equal the observation's
frame ID; and `primary_requested_time_utc` must exactly equal the request's
authoritative requested time. Repeated acquisition fields are never independently
trusted. Strict validation permits only these state/evidence
combinations:

| State | Metrics | `reason_code` |
| --- | --- | --- |
| `PRESENT` | Both metrics are finite and satisfy the persisted PRESENT thresholds. | `null` |
| `ABSENT` | Both metrics are finite and satisfy the persisted ABSENT thresholds. | `null` |
| `INDETERMINATE` | Either or both metrics may be finite or null diagnostic facts, but they cannot satisfy a terminal mapping. | Exactly `insufficient_visual_evidence` |

Acquisition gaps, replay failures, decoder failures, and missing provenance remain
`FAILED` `ProbeFrameRequestRecord` outcomes and never produce a
`RecordingProbeObservationRecord`. `insufficient_visual_evidence` covers only
classifier failure, invalid or empty geometry/mask, zero variance, non-finite
comparison, insufficient distinct frames, and invalid decoded order. No other
frame-backed probe reason code is valid in the MVP. If Phase 7B later needs
non-observation accounting for a failed target, that is a separate future record,
not this frame-backed variant.

The later `TargetAliasRecord` has this exact shape:

```text
TargetAliasRecord
  record_type: target_alias
  alias_id
  requested_time_utc
  canonical_observation_id
  reason_code: same_decoded_frame
```

`TargetAliasRecord` is a later classifier/search reference only. It points to an
observation, never to an acquisition frame, and never supplies independent
support. A2 aliases are represented only by additional acquired
`ProbeFrameRequestRecord` values that reference an existing frame. Three such
requests resolving to one frame therefore yield one `canonical_frame_id`, not
three independent frames.

In the remainder of this document, **request alias** means an acquired
`ProbeFrameRequestRecord` with `alias_of_probe_request_id`; **observation alias**
means the later `TargetAliasRecord`. An unqualified alias in classifier/search
rules is an observation alias and can never stand in for an acquisition frame.

### Phase 7A-2 batch decoder extension

Phase 7A-2 adds a local bounded operation without changing existing single-target
callers:

```text
decode_targets(acquisition, ordered_requested_targets)
  -> ordered DecodedTargetResult values
```

One operation opens one replay/decode session and selects all requested targets
chronologically. Each result exposes requested UTC, the trusted segment and
bounded extraction-window endpoints, the physical replay origin, exact raw
source/container PTS and positive source time base, normalized decoded UTC,
credential-free acquisition and segment IDs, decode-session ID, exact
replay-local PTS and positive replay time base, ordinal within that decode
attempt, dimensions, JPEG digest, and canonical frame ID. The source-time
mapping is a mandatory new A2 decoder capability: the replay/extraction boundary
must provide the physical origin and source/container timing, and the decoder
must retain those raw values in parallel with any `setpts=PTS-STARTPTS`
selection stream. A missing, negative, ambiguous, discontinuous, or
irreproducible source-time value, PTS, time base, ordinal, dimensions, or digest
is an acquisition failure; it is never represented as a usable frame. The
operation retains no URL, credentials, command, stderr, or temporary path.

`source_segment_id` is the exact existing `segment_identity()` value built from
the trusted channel segment's UTC start and end; the current SDK/application
boundary exposes no separate recording-session identifier, so none is invented
or persisted. `acquisition_id` hashes that segment ID, the bounded decode-window
UTC endpoints, and acquisition-policy version. `decode_session_id` identifies
the decoder attempt. Because the direct decoder applies `setpts=PTS-STARTPTS`,
replay-local PTS and ordinal are replay-/attempt-local only. The A2 capability
computes `decoded_frame_utc` from the physical replay origin and raw source PTS
with rational positive source-time scaling, rounds once to six fractional digits
with ties-to-even, and rejects duplicate normalized positions within a segment.
It also rejects any overlapping-window result whose normalized UTC cannot be
reproduced from the same source-time mapping. A fresh invocation or
decoder-attempt token is operational metadata only and is never included in
`canonical_frame_id`. The canonical
identity tuple and serialization are the closed rules above, so the same
segment/frame position reuses the same frame ID across operations. Every rule
that needs frame distinctness, including `[t, t+1s, t+2s]`, uses one batch
operation and requires three different frame IDs with strictly increasing
normalized decoded UTC values. Aliased requested targets cannot satisfy that
rule. If authoritative provenance is not stable enough to decide equality,
acquisition fails safely instead of guessing.
The existing single-target reference-frame result remains
`measured_clip_relative` with no source-time claim; this A2 contract does not
change that caller or reinterpret its warning.

One canonical frame can later produce at most one observation for a given
classifier policy/version. Additional target requests selecting it do not create
another frame or observation, do not update bounds, count as support, or
increment the coarse-target uncertainty counter. Phase 7B accepts a strictly
validated schema 2 acquisition state and validates the successful request/frame
pair before classification. A2 rejects any observation, classifier,
candidate-interval, `FOUND`, or `NOT_FOUND` field; after classification, a later
schema/version contract owns observation persistence. Phase 7B does not reject a
valid A2 result merely because classification is a later operation.

### Production classifier policy

Phase 7B owns production policy `efficient-sam-ti-roi-ncc-v1`. EfficientSAM-
Ti supplies category-agnostic masks only; it does not establish object identity
or correspondence. The production adapter therefore reuses the verified lazy
predictor and adds one local NumPy/Pillow aligned-ROI comparator:

1. Input is the verified baseline JPEG, confirmed source-pixel ROI, one decoded
   probe JPEG, and equal source dimensions.
2. Use the deterministic ROI-center point `x + floor((width - 1) / 2), y +
   floor((height - 1) / 2)` on both images. Run the existing EfficientSAM-Ti
   point-prompt mask path and clip each validated mask to the confirmed ROI.
3. For each image, compute mask coverage as the number of segmented mask pixels
   inside that clipped ROI divided by the total pixels of the clipped ROI. A
   missing, empty, or clipped-to-zero ROI, or coverage greater than or equal to
   95%, is `INDETERMINATE` with `insufficient_visual_evidence` before comparison.
4. Compute `mask_iou` for the two clipped masks. Compute `roi_luma_ncc` as
   mean-centered normalized cross-correlation over **all pixels** of the aligned
   source-pixel ROI in the two grayscale images; masks do not select NCC pixels.
   An ROI or either clipped mask with fewer than 64 pixels, zero luma variance,
   non-finite values, invalid masks, or unequal geometry is `INDETERMINATE`.
5. Return `PRESENT` only when `mask_iou >= 0.50` and `roi_luma_ncc >= 0.60`.
6. Return `ABSENT` only when both masks are valid, `mask_iou <= 0.10`, and
   `roi_luma_ncc <= 0.20`.
7. Every other finite result is `INDETERMINATE`.

Source bytes, ROI coordinates, point rule, preprocessing, model/checkpoint,
coverage rule, comparison arithmetic, and thresholds are all captured by the
versioned policy. Identical inputs and policy values therefore produce identical
measurements and state mapping.

The policy snapshot persists the classifier version, EfficientSAM source commit,
checkpoint SHA-256, center-point rule, 95% mask-coverage rejection, minimum
pixel count, and all four thresholds. Timeout, unavailable runtime/checkpoint,
segmentation failure, corrupt image, obstruction, or comparison failure never
becomes `ABSENT`; unavailable production composition before probes is `FAILED`,
while per-probe unusable evidence is `INDETERMINATE`.

Deterministic doubles implement the same narrow `ObjectObservationClassifier`
protocol for policy tests but never replace production composition. Phase 7B is
complete only after unit tests cover every threshold boundary and failure, and
an integration test runs the real EfficientSAM predictor plus comparator on
local fixtures. Phase 7E then validates and, if evidence requires, versions new
thresholds against representative NVR frames before deployment.

## Coarse-to-binary search policy

Policy `recording-search-mvp-v1` uses a configurable five-minute default coarse
interval and persists the resolved value with every run:

| Setting | Value |
| --- | ---: |
| Maximum requested span | 24 hours |
| Coarse interval | 300 seconds |
| Binary stopping resolution | 1 second |
| Absence confirmation | 3 distinct frames |
| Confirmation cadence | 1 second |
| Maximum consecutive indeterminate coarse targets | 3 |
| Classifier | `efficient-sam-ti-roi-ncc-v1` |
| Present thresholds | mask IoU 0.50; luma NCC 0.60 |
| Absent thresholds | mask IoU 0.10; luma NCC 0.20 |
| Minimum ROI and clipped-mask pixels | 64 |
| Maximum ROI-relative mask coverage | 0.95 |

Changing a value requires a new policy version. These are initial operating
limits to validate against representative NVR footage, not accuracy claims.

### Coarse sampling

Let `S` be Phase 6 `requested_time_utc` and `E` the validated search end.

1. Create targets `S + 300s`, `S + 600s`, and so on; append `E` if needed.
2. Process targets chronologically with the baseline as initial lower bound.
3. A distinct canonical `PRESENT` observation updates `last_present`; an alias
   never does.
4. An `ABSENT` coarse target `t` is tentative until one continuous decode selects the
   exact targets `[t, t + 1s, t + 2s]`. The fixed two-second support horizon may
   extend past `E`, but cannot create a candidate target later than `E`.
5. Those values are requested targets, not promised decoded timestamps. The
   operation must resolve them to three distinct canonical frame IDs with
   strictly increasing normalized decoded UTC (using PTS/ordinal only as
   same-attempt tie-break provenance), all classified `ABSENT`; only then is
   `t` the first-absence upper bound and `[last_present, t]` a bracket.
6. Aliases count once and cannot establish absence.
7. `consecutive_unusable_coarse_targets` counts coarse targets, not decoded
   support frames. Each coarse target increments it at most once; aliases never
   increment it. A valid canonical PRESENT coarse result resets it to zero.
   Reaching three ends the run as `INDETERMINATE`.

Tentative-absence handling uses this precedence and is exhaustive:

```text
if primary coarse result at t is PRESENT:
  persist it and update last_present
  consecutive_unusable_coarse_targets = 0
  continue with the next configured coarse target

if primary acquisition/decode failed:
  persist one INDETERMINATE target with recording_unavailable
  increment consecutive_unusable_coarse_targets once
  finish INDETERMINATE if the limit is reached; otherwise continue

if primary classifier result is INDETERMINATE:
  persist it with insufficient_visual_evidence
  increment consecutive_unusable_coarse_targets once
  finish INDETERMINATE if the limit is reached; otherwise continue

# primary result is tentative ABSENT
support = decode_and_classify_one_session([t, t + 1s, t + 2s])

if recording/replay/acquisition/decode operation failed:
  persist t as INDETERMINATE with recording_unavailable
  increment consecutive_unusable_coarse_targets once
  finish INDETERMINATE if the limit is reached; otherwise resume after t + 2s

canonical, aliases = canonicalize(support)
persist aliases without counting them

if classifier failed or any canonical result is INDETERMINATE:
  persist t as INDETERMINATE with insufficient_visual_evidence
  increment consecutive_unusable_coarse_targets once
  finish INDETERMINATE if the limit is reached; otherwise resume after t + 2s

if fewer than three distinct canonical frames exist
   or normalized decoded UTC order is not strictly increasing:
  persist t as INDETERMINATE with insufficient_visual_evidence
  increment consecutive_unusable_coarse_targets once
  finish INDETERMINATE if the limit is reached; otherwise resume after t + 2s

if any canonical observation is PRESENT:
  update last_present from the latest later canonical PRESENT primary target
  consecutive_unusable_coarse_targets = 0
  resume at the first configured coarse target after t + 2s

if canonical contains exactly three distinct ABSENT frames:
  if last_present is an indexed canonical PRESENT recording probe:
    consecutive_unusable_coarse_targets = 0
    emit internal bracket [last_present, t] with all three support IDs
  otherwise:
    persist t as INDETERMINATE with insufficient_visual_evidence
    increment consecutive_unusable_coarse_targets once
    finish INDETERMINATE if the limit is reached; otherwise resume after t + 2s
```

One canonical frame is classified once, so aliases cannot disagree about its
state. Multiple unusable support frames for one tentative coarse target still
increment the counter only once. Identical canonical evidence therefore yields
one action independent of how many requested targets alias it.

`NOT_FOUND` requires every coarse target through `E` to be classifiable and no
bracket. A completed scan with even one unresolved target, unavailable required
coverage, or no usable `E` ends `INDETERMINATE` instead.

### Binary narrowing

For one valid bracket `[L, U]`:

```text
while U - L > 1 second:
  M = L + floor((U - L) / 2)
  observation = acquire_and_classify(M)

  if observation is PRESENT:
    L = M
    continue

  if observation is ABSENT:
    support = confirm_absence_at([M, M + 1s, M + 2s])
    if support succeeds:
      U = M
      continue

  finish INDETERMINATE

finish FOUND with candidate interval [L, U]
```

Each midpoint is a canonical whole-second UTC request. Midpoint ties use the
earlier second through floor division. Binary search never widens the bracket,
crosses a known recording gap, or treats an isolated `ABSENT` as an upper bound.
It uses the same batch identity and support table. During narrowing, any support
result other than three valid ordered ABSENT frames contradicts or cannot prove
the current upper bound and ends `INDETERMINATE`; it does not resume a coarse
scan or manufacture another persisted state.

The MVP does not attempt automatic non-monotonic recovery. If later evidence
inside the active bracket contradicts its `PRESENT -> ABSENT` ordering, the run
ends `INDETERMINATE` and Phase 9 remains authoritative.

### Deterministic outcomes

| Condition | Run state | Safe reason |
| --- | --- | --- |
| Supported bracket narrowed to the stopping resolution | `FOUND` | `candidate_interval_found` |
| Complete usable coarse scan with no bracket | `NOT_FOUND` | `no_transition_in_window` |
| Required gap or replay/acquisition/decode failure for a coarse target | Count one unusable coarse target; terminal `INDETERMINATE` at the configured limit or when the scan ends with unresolved evidence | Search-level `recording_unavailable` (not a frame-backed observation reason) |
| Classifier failure, invalid evidence, missing canonical PRESENT lower probe, insufficient distinct frames, or invalid order for a coarse target | Count one unusable coarse target; terminal `INDETERMINATE` at the configured limit or when the scan ends with unresolved evidence | `insufficient_visual_evidence` |
| Confirmed JPEG or Phase 6 input fails integrity validation | `FAILED` | `baseline_validation_failed` |
| Process/PC exits during a nonterminal run | `INTERRUPTED` | `run_interrupted` |
| Unexpected storage or persistence failure outside a coarse-target acquisition | `FAILED` | fixed stage-safe reason |

## Minimal persistence contract

### Manifest

`manifest.json` is one strict UTF-8 JSON object with duplicate and unknown keys
rejected. Timestamps are canonical whole-second UTC. Phase 7A-1 keeps its
`schema_version: 1` lifecycle manifest unchanged and readable. Phase 7A-2
publishes the exact acquisition-only `schema_version: 2` form below. A reader
dispatches strictly by supported version: v1 is parsed as the exact A1 shape,
v2 as the exact A2 shape, and every unsupported future version is rejected. A
v1 load does not infer empty A2 collections or reinterpret the record as v2.

```text
RecordingSearchManifestV2
  schema_version: 2                 # A2 acquisition only
  investigation_id
  search_run_id
  state: PENDING | RUNNING | FAILED | INTERRUPTED
  created_at_utc
  started_at_utc | null
  completed_at_utc | null
  confirmation:
    channel_id
    reference_frame_resource_id
    reference_requested_time_utc
    source_timezone
    source_width, source_height
    roi, coordinate_space, and provenance
    jpeg_sha256, jpeg_size_bytes
    timing_precision_status, warnings
  policy:
    search_start_utc, search_end_utc
    maximum_requested_span_seconds
    coarse_interval_seconds
    binary_stop_resolution_seconds
    absence_confirmation_frames
    absence_cadence_seconds
    maximum_consecutive_indeterminate_targets
    acquisition_policy_version
    classifier_policy_version
    efficient_sam_source_commit, checkpoint_sha256
    prompt_rule
    maximum_roi_mask_coverage_ratio
    minimum_roi_pixels, minimum_clipped_mask_pixels
    present_mask_iou_minimum, present_luma_ncc_minimum
    absent_mask_iou_maximum, absent_luma_ncc_maximum
    policy_version
  acquisition_operation_ids: array[string], ordered unique operation IDs for this run
  probe_request_ids: ordered request-record references
  canonical_frame_ids: ordered unique acquisition-record references
  failure_reason | null
```

The v2 shape contains no observation, classifier, candidate, result, or Phase 8
field. Its `failure_reason` is a closed acquisition/storage reason only. Later
classifier/search persistence is reserved for a future recording-search manifest
version; no future version number or terminal shape is accepted by A2 today.

The following is the complete shape of a valid A2 `RUNNING` manifest. The frame
and request records are stored separately and are reachable only through these
indexes:

```json
{
  "schema_version": 2,
  "investigation_id": "object-disappearance-v3-ch1-20260720T033418Z",
  "search_run_id": "search-run-9d764020",
  "state": "RUNNING",
  "created_at_utc": "2026-08-09T04:00:00Z",
  "started_at_utc": "2026-08-09T04:00:01Z",
  "completed_at_utc": null,
  "confirmation": {
    "channel_id": 1,
    "reference_frame_resource_id": "reference-frame-safe-id",
    "reference_requested_time_utc": "2026-07-20T03:34:13Z",
    "source_timezone": "Asia/Seoul",
    "source_width": 2560,
    "source_height": 1440,
    "roi": {"x": 481, "y": 927, "width": 214, "height": 163, "coordinate_space": "source_pixels", "provenance": "manual"},
    "jpeg_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
    "jpeg_size_bytes": 481920,
    "timing_precision_status": "measured_clip_relative",
    "warnings": []
  },
  "policy": {
    "search_start_utc": "2026-07-20T03:34:13Z",
    "search_end_utc": "2026-07-20T12:00:00Z",
    "maximum_requested_span_seconds": 86400,
    "coarse_interval_seconds": 300,
    "binary_stop_resolution_seconds": 1,
    "absence_confirmation_frames": 3,
    "absence_cadence_seconds": 1,
    "maximum_consecutive_indeterminate_targets": 3,
    "acquisition_policy_version": "phase7-batch-decoder-v1",
    "classifier_policy_version": "efficient-sam-ti-roi-ncc-v1",
    "efficient_sam_source_commit": "d525f622e6f640acf5a0fc37c7ca1f243da5bde0",
    "checkpoint_sha256": "dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a",
    "prompt_rule": "confirmed_roi_center_v1",
    "maximum_roi_mask_coverage_ratio": 0.95,
    "minimum_roi_pixels": 64,
    "minimum_clipped_mask_pixels": 64,
    "present_mask_iou_minimum": 0.5,
    "present_luma_ncc_minimum": 0.6,
    "absent_mask_iou_maximum": 0.1,
    "absent_luma_ncc_maximum": 0.2,
    "policy_version": "recording-search-mvp-v1"
  },
  "acquisition_operation_ids": [
    "acquisition-op-1"
  ],
  "probe_request_ids": [
    "probe-request-last-present"
  ],
  "canonical_frame_ids": [
    "frame-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  ],
  "failure_reason": null
}
```

For an A2 `RUNNING` manifest, `acquisition_operation_ids`, `probe_request_ids`,
and `canonical_frame_ids` are the acquisition indexes. Every operation ID is a
unique server-admitted ID in creation order and resolves to exactly one strict
`AcquisitionOperationRecord` under `operations/{operation_id}.json` for this
run. Every request ID resolves to
one request record in this run; every acquired request resolves to one indexed
frame, and every frame ID resolves to one `CanonicalProbeFrameRecord` plus its
validated JPEG. A request
with `alias_of_probe_request_id` must point to an earlier successful request in
the same run with the same frame ID. An alias may repeat a frame ID, but a frame
ID appears only once in `canonical_frame_ids`. The v2 manifest does not contain
the later observation or alias indexes. Its run state remains one of
`PENDING`, `RUNNING`, `FAILED`, or `INTERRUPTED`; it rejects observation states,
classifier evidence, `FOUND`, `NOT_FOUND`, candidate intervals, and Phase 8
fields while acquisition is the active phase. It also rejects a frame or request
from another run, a missing JPEG, a digest/size/dimension mismatch, missing
PTS/ordinal/time base, or a path that is absolute, traversing, linked, or outside
the run root.

Observation records are immutable strict JSON below `observations/` and are only
created by Phase 7B under a future recording-search manifest version. The
observation index and later alias index are distinct from the A2 frame and request
indexes. Every record rejects unknown, duplicate, missing, or cross-variant
fields. A later observation must reference an indexed canonical frame and its
successful request; it cannot be reconstructed from requested time alone.

The following relationships are reserved for that future classifier/search
manifest and are not accepted by the A2 loader:

| Manifest state | Required candidate/evidence relationship |
| --- | --- |
| `FOUND` | `candidate_interval` is present and `failure_reason` is null. Its lower-bound ID resolves to an indexed canonical `RecordingProbeObservationRecord` in `PRESENT`; its upper-bound ID resolves to a later indexed canonical probe in `ABSENT`; stored requested times exactly match those records. `absence_support_observation_ids` resolves to exactly three distinct indexed canonical `ABSENT` probes from one decode session in strictly increasing normalized decoded UTC, with PTS/ordinal only as same-session tie-break provenance, includes the upper-bound probe, and satisfies the persisted cadence/policy. No alias or baseline record can fill any evidence position. |
| Every state other than `FOUND` | `candidate_interval` is null and no Phase 8 handoff is `READY`. State-specific fixed failure fields follow the state table above. |

When that future contract exists, all resolved probes must belong to the same run manifest and match its
investigation, channel, acquisition/classifier policy, dimensions, and indexed
record identity. A missing, mismatched, aliased, out-of-order, threshold-invalid,
or semantically inconsistent record makes the manifest invalid. It must never be
returned, rendered, or handed to Phase 8 as `FOUND`.

No manifest, observation, alias, evidence file, or file name contains
credentials, hostnames, usernames,
authenticated URLs, ffmpeg arguments, subprocess commands, raw stderr,
tracebacks, or absolute paths. `jpeg_path` is runtime-only.

### Schema 1 to schema 2 promotion

The existing Phase 7A-1 repository remains the source of truth for schema 1.
`start()` has already acquired the run-local OS lock and the active run handle
continuously owns it. The A2 caller obtains the one shared per-run mutation
mutex beneath that held lock, strictly reloads the published schema 1 manifest,
and revalidates the active handle, held OS lock, operation, and acquisition-
admitting `PENDING` or `RUNNING` state. It then constructs a complete schema 2
successor in memory, preserving every immutable A1 field byte-for-byte in value
and adding only the A2 acquisition policy plus empty
`acquisition_operation_ids`, `probe_request_ids`, and `canonical_frame_ids`
indexes. It never acquires, releases, or reacquires the OS lock and never edits
the published schema 1 JSON in place.

The successor is validated as a complete v2 manifest and published through the
existing atomic replacement mechanism while the A2 mutex remains held. Readers
therefore see either a valid schema 1 or a valid schema 2; they never see an
intermediate promotion. A crash before replacement leaves schema 1 authoritative;
a crash after replacement leaves schema 2 authoritative with no fabricated A2
operation history. Only after that v2 commit does the normal operation-admission
step create the first `AcquisitionOperationRecord` and atomically append its ID;
only after that admission may a frame or request be staged. A schema 1 reader
never infers A2 collections, and a v2 reader rejects all v1-only or future
classifier/result fields. The status reader dispatches only to the strict v1 or
v2 loader and reports common lifecycle facts plus A2 acquisition counts; it
rejects unsupported versions and never accepts future observation/search fields.

### A2 artifact layout and publication

The A2 run repository uses only these durable paths:

```text
{search_run_id}/
  manifest.json
  operations/{operation_id}.json
  frames/{canonical_frame_id}.json
  requests/{probe_request_id}.json
  evidence/frames/{canonical_frame_id}.jpg
```

The acquisition service accepts only an active `RecordingSearchRunHandle` and a
server-created request containing a validated requested UTC (and, if used by the
planner, a closed probe role). It owns channel, IDs, provenance, paths, policy,
and publication. A public endpoint is not added in A2; any future API must keep
these server-owned fields out of the request and return only safe IDs/status.

The existing Phase 7A-1 `RecordingSearchRunHandle` continuously owns the
per-investigation OS-backed lock from `start` until terminal transition or
explicit handle release. Each active handle also owns one in-process A2
mutation mutex created with that handle and retained in the service's active-run
state. Every A2 frame, request, index, and manifest mutation for that run uses
that same mutex; a second mutex is never created for a later operation. The
caller must hold the valid active A1 handle before acquiring the A2 mutex.

The A2 mutex is held across latest-manifest reload, duplicate detection,
operation/state/OS-lock revalidation, recording resolution, bounded decode,
provenance and JPEG validation, staging/publication, frame/request/index
construction, atomic manifest replacement, and the cleanup decision. Releasing
the A2 mutex never releases the OS lock. The owner revalidates the active handle,
current acquisition-admitting state, current operation ownership, and
`os_lock_held` before making any decision; no two in-process writers replace
descendants of the same manifest. This is a two-level local protocol, not a
lease, fencing, takeover, or general transaction system.

Frame JSON, request JSON, and JPEG bytes are first written under an
invocation-owned directory inside the run, `.phase7a2-{operation_id}`. The
complete bundle is validated, then final child files are published without
overwrite and `manifest.json` is atomically replaced as the sole commit point.
Readers accept only IDs listed by the committed manifest and revalidate every
referenced frame and JPEG. A final path that already exists with the same
canonical identity and validated bytes is reused; a path with conflicting
identity or bytes yields `publication_conflict` and is never overwritten.

If staging succeeds but manifest replacement fails, or the process stops after a
final child file is published but before the manifest commit, the invocation
removes only its own staging and unindexed child files when it can prove
ownership. Any unreferenced final file that remains is ignored on reopening and
is never canonical evidence. A committed child is never deleted by a losing or
stale invocation. A missing or corrupt manifest-referenced artifact makes the
run invalid on reopen; the loader does not repair or infer evidence from files.

Concurrent same-frame requests have one outcome. The active A1 handle
continuously holds the OS lock. Owner A obtains the shared per-run A2 mutex,
reloads the committed manifest, resolves/validates/stages the frame and request,
and atomically publishes the frame, request relationship, indexes, and manifest.
A releases only the A2 mutex, not the OS lock. Owner B then obtains that same
mutex, reloads A's committed manifest, and deterministically reuses A's
`canonical_frame_id` when its authoritative segment/frame provenance matches.
B may publish only its distinct request relationship, with B's request operation
ID and A's frame-publication operation ID both retained. It cannot lose A's index
entry or silently merge incompatible records; a mismatch is
`publication_conflict`.

The run remains `RUNNING` while A2 targets are being acquired. A per-request
`FAILED` outcome records only a closed acquisition reason and does not become
`ABSENT`; a storage or manifest publication failure outside target acquisition
transitions the run to `FAILED` with a fixed safe reason. Status inspection first
attempts the OS lock. If another process still holds the continuously-owned
lock, it returns `RUNNING` without mutation; releasing only the A2 mutex can
never cause `INTERRUPTED`. Once the handle reaches terminal state or is
explicitly released, the service removes its active mutex; a late caller cannot
acquire that mutex or revive/mutate the terminal run and fails with the existing
safe ownership/state error. Only after the OS lock is acquired and no active
handle owns it may a stale nonterminal run be marked `INTERRUPTED`. Staged A2
data is ignored or cleaned only when owned by that invocation. There is no lease,
fencing, resume, takeover, or multi-host protocol.

### Deterministic acquisition traces

The following traces close the A2 outcomes without defining later classifier or
search persistence:

| Trace | Safe outcome |
| --- | --- |
| 1. One request resolves to one new frame | One admitted operation record, one `SUCCEEDED` request, one new immutable frame/JPEG, and one atomically committed v2 index successor. |
| 2. Two requested times resolve to the same frame | Two distinct successful request records; the later has `alias_of_probe_request_id`; one canonical frame ID derived from the reproducible source-time mapping. |
| 3. Two concurrent requests resolve to the same new frame | The active A1 handle keeps one OS lock continuously. A and B use the shared per-run A2 mutex and separately admitted operation records; B reloads A's committed manifest, reuses the stable source-time frame ID, and publishes only B's distinct request relationship. No index entry is lost and status never reports false `INTERRUPTED`; frame and request operation IDs may differ. |
| 4. Recording coverage is missing | One `FAILED` request with `recording_unavailable`; no frame, JPEG, or classifier state. |
| 5. Decoding succeeds but source PTS, source/replay time base, or ordinal is unavailable | One `FAILED` request with `missing_provenance`; the incomplete frame is never staged for publication. |
| 6. JPEG staging succeeds but manifest promotion fails | The manifest remains authoritative at its prior valid version; only invocation-owned staging/unindexed files are cleaned, and any orphan is ignored on reopen. |
| 7. Final artifact publication succeeds but the process stops before request completion | The lost lock causes `INTERRUPTED` on the next inspection; the unindexed child is ignored and cannot become a successful request. |
| 8. A request references a frame from another run | Strict ownership validation rejects the request/manifest as corrupt; no cross-run evidence is returned. |
| 9. Stored JPEG digest, size, or dimensions do not match | Strict artifact validation yields `invalid_artifact`; the request cannot be `SUCCEEDED`. |
| 10. A schema 1 interrupted A1 run is reopened | The v1 loader reads the exact A1 shape, the lock-safe inspection marks it `INTERRUPTED`, and no A2 collections are inferred. |
| 11. A valid schema 2 acquisition run is reopened | The v2 loader validates the closed operation-record index, recomputes the canonical identity from persisted physical replay origin plus source PTS/time base, validates positive source/replay time bases and attempt-local PTS/ordinal scope, ownership, and every JPEG; it accepts a frame published by one admitted operation and referenced by a request from another admitted operation in the same run. |
| 12. A schema 2 manifest contains observation/classifier/result data | The strict v2 parser rejects unknown fields or invalid state combinations; it never presents a result or silently upgrades the schema. |
| 13. A later classifier receives a valid acquired frame/request pair | Phase 7B accepts one strict successful request and its indexed canonical frame when investigation, run, target, channel, segment/frame provenance, canonical ID, and two durable same-run operation records match; request and frame operation IDs may differ. Foreign, invented, or unindexed operations are rejected. A2 remains acquisition-only and does not persist an observation. |
| 14. Three aliases reference one frame | Three request records resolve to one canonical frame; they cannot satisfy three-distinct-frame absence support or create three observations. |

## Phase 8 handoff

After the search manifest is durably `FOUND`, Phase 7 creates a separate compact
`phase8-request.json` containing:

```text
search_run_id
investigation_id
channel_id
last_present_observation_id and requested time
first_absent_observation_id and requested time
supporting observation/evidence references
nominal_review_start_utc: boundary minus 10 seconds
nominal_review_end_utc: boundary plus 30 seconds
timing_precision_statuses and warnings
```

Phase 8 revalidates the Phase 6/7 facts, resolves recording coverage, and creates
review images and video. Phase 7 does not promise contiguous review coverage
and does not extract or persist Phase 8 media.

A handoff write failure updates only `phase8_handoff_status` and its fixed safe
reason. It does not change `FOUND`, the candidate interval, or existing
evidence. An explicit handoff retry may create the same deterministic request;
it does not rerun or mutate the search.

## Failure, safety, and operational constraints

- Temporary replay clips and decoded working frames are invocation-owned and
  removed by their existing consumers. Successful retained evidence is not
  removed automatically.
- Cleanup never follows path indirection and never deletes Phase 6 resources,
  another run, pre-existing evidence, or ambiguous files.
- Fixed reason codes cross persistence and user-visible boundaries; raw
  exceptions and subprocess diagnostics do not.
- CCTV evidence remains local and Git-ignored. Retention and deletion are
  operational policy outside the search algorithm.
- Remote access relies on the application's existing authentication or trusted
  network/Tailscale boundary; Phase 7 introduces no access-control framework.
- A visibly changed or explicitly mismatched camera/channel requires
  reconfirmation. When stable physical identity is unavailable from the SDK,
  that limitation is shown rather than hidden.
- Manual review remains authoritative. A user decision is a later record and
  never rewrites machine observations.

## Luna-oriented implementation slices

Each slice is designed for one focused GPT-5.6 Luna implementation and review.
They are implementation increments, not separately deployable persisted workflow
stages; the public state model remains unchanged.

### Phase 6C: schema 3 compatibility

- **Inputs:** one read-only schema 2 confirmation, its trusted reference resource,
  and explicit user activation after the existing JPEG/ROI review.
- **Outputs:** one new immutable schema 3 package and strict
  `ConfirmedInvestigationInput`; schema 2 remains unchanged.
- **Tests:** read-only reopen, explicit action, digest/size and ROI validation,
  changed or unsafe resource failure, new identity, and old-package immutability.
- **Complete:** the user can explicitly reconfirm schema 2 and load schema 3 after
  restart without in-place migration.

### Phase 7A-1: baseline and run lifecycle

- **Inputs:** validated search request, schema 3 loader/resource boundary, clock,
  ID generator, artifact root, and per-investigation OS lock.
- **Outputs:** verified invocation-owned baseline bytes, isolated run repository,
  lifecycle, duplicate-start result, status lookup, and interruption handling.
- **Tests:** every baseline gate, duplicate start, lock-held status, interrupted
  nonterminal run, new-run isolation, paths, cleanup, and redaction.
- **Complete:** one valid schema 3 baseline starts one isolated `RUNNING` run;
  invalid input or abandoned state fails without creating search evidence, and
  the safe start/status boundary exposes only lifecycle facts.

### Phase 7A-2: bounded multi-target acquisition

- **Inputs:** one active run, coverage/planner/replay boundaries, ordered target
  set, existing single-target decoder behavior, and acquisition policy.
- **Outputs:** one immutable `ProbeFrameRequestRecord` per requested target,
  zero or more immutable `CanonicalProbeFrameRecord` values, ordered
  `probe_request_ids`, unique `canonical_frame_ids`, and validated run-owned
  JPEG/frame artifacts. A2 emits no observation, classifier state, candidate,
  alias-observation, or Phase 8 result.
- **Tests:** one request-to-one-frame mapping, ordered selection, distinct versus
  aliased targets, stable segment/window timestamp provenance, cross-run rejection,
  missing PTS/ordinal or dimensions, gaps, timeouts, JPEG digest/size recheck,
  manifest-commit failure, cleanup ownership, and credential redaction.
- **Complete:** one real/fixture batch proves distinct versus aliased frames,
  reopening accepts only manifest-indexed complete bundles, and existing
  single-target callers remain unchanged. The output is ready for Phase 7B to
  create observations, but is not itself classified.

### Phase 7B: production ROI observation classifier

- **Inputs:** verified baseline JPEG and ROI, decoded probe image and dimensions,
  EfficientSAM-Ti production predictor, aligned-ROI luma-NCC policy, ROI-relative
  mask-coverage rule, and thresholds.
- **Outputs:** the closed baseline/probe/alias record union; every canonical
  probe has exactly one state, fixed reason, and bounded classifier evidence.
- **Files:** production classifier protocol/adapter and composition, aligned-ROI
  comparator, observation models, focused tests, and deterministic doubles.
- **Exclude:** run orchestration, targets, binary search, storage ownership,
  Phase 8.
- **Tests:** real predictor/comparator integration fixture; every IoU/NCC
  threshold boundary; baseline geometry; three states; timeout, unavailable,
  invalid-mask and zero-variance mapping; deterministic-double parity.
- **Complete/document:** production composition exists and records its policy;
  no infrastructure or uncertain comparison becomes `ABSENT`; probe records
  retain mandatory acquisition/frame provenance.

### Phase 7C: coarse sampling

- **Inputs:** `S`, `E`, policy, confirmed baseline, acquisition/classification.
- **Outputs:** a typed internal bracket, `NOT_FOUND`, or `INDETERMINATE` plus
  ordered canonical observations and aliases. A bracket is not a persisted
  terminal result.
- **Files:** pure coarse-policy or bounded orchestration component and focused
  tests; integrated service wiring completes in 7D.
- **Exclude:** binary narrowing, Phase 8, review media, recovery/resume.
- **Tests:** anchored targets plus `E`, every tentative-absence table row,
  ordering, alias collapse, one-event-per-coarse-target uncertainty accounting,
  reset on canonical PRESENT, gaps, and
  complete no-result behavior.
- **Complete/document:** the component returns one deterministic bounded internal
  result. In an integrated run, a bracket passes directly to 7D while the
  manifest stays `RUNNING`; no standalone 7C execution is production-complete.
  Interruption before terminal persistence follows the existing `INTERRUPTED`
  rule, and partial 7C output is never `FOUND`.

### Phase 7D: binary narrowing and persistence

- **Inputs:** 7C internal bracket, resolution, observation boundary, manifest,
  and evidence store while the same run remains `RUNNING`.
- **Outputs:** `FOUND` interval or `INDETERMINATE`, durable candidate, separate
  handoff request/status.
- **Files:** pure binary policy, service/repository extension, handoff models,
  focused tests.
- **Exclude:** Phase 8 media, user judgment, automatic restart/resume.
- **Tests:** midpoint rules, monotonic shrink, support frames, gaps, persistence
  ordering, handoff failure/retry, immutable terminal state.
- **Complete/document:** a valid bracket reaches one second and handoff failure
  cannot change evidence; update the schema only for a proven need.

### Phase 7E: real NVR validation

- **Inputs:** representative local clearly-present, clearly-absent, occluded,
  lighting-change, viewpoint/image-quality-degraded, gap, and decode-failure
  frames/recordings processed by the production classifier and complete search.
- **Outputs:** credential-free measurements, limitations, safe outcomes, and a
  policy-readiness decision.
- **Files:** existing validation documentation or ignored local evidence only.
- **Exclude:** weakened safety, footage upload, Phase 8/9, SDK changes.
- **Tests:** automated gates, then bounded NVR runs covering all input classes,
  cleanup, timing, geometry, latency, memory, and safe failures.
- **Complete/document:** representative evidence supports or rejects the policy
  and a normal run succeeds after controlled failure. Threshold tuning creates
  a new documented policy version and never weakens the rule that uncertainty or
  infrastructure failure cannot become `ABSENT`.

## Deferred resilience reference

Lease/fencing ownership, takeover, resume, multi-host coordination, full
manifests, race proofs, and source binding remain excluded until demonstrated
need, future analysis, and a new ADR promote them.
