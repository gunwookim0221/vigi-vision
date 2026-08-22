# Phase 7 Object-Disappearance Recording Search MVP

## Status and normative authority

**Status: normative design for the current single-site Phase 7 MVP. Phase 7A-1
implements the validated local run lifecycle, baseline gate, isolated repository,
duplicate/interruption handling, and safe start/status API. Phase 7A-2 implements
acquisition-only request/frame persistence, strict provenance, and reopen
validation. Phase 7B implements bounded production single-probe classification,
timeout/abandonment authority revocation, strict revalidation, and atomic
schema-3 observation publication. Phase 7C-1 implements the deterministic
chronological coarse target plan and sequential A2/B4 execution foundation;
7C-2 implements the pure, non-persistent interpretation of that ordered
evidence. Phase 7D-1 implements deterministic non-terminal binary narrowing
through the existing A2/B4 boundaries and returns only an in-memory narrowed
bracket. D2-1 now implements the typed in-memory history, canonical identity
producers, and strict reconstruction handoff for that bracket. D2-2 now
implements only the pure terminal-result models/interpreter, strict evidence
reconstruction, visual snapshot digest binding, and in-memory result identity;
D2-3 now implements schema-4 publication and the canonical lock-order migration;
D2-4 now implements strict process-restart terminal reopen validation and the
non-sensitive schema-4 status projection. The Phase 8 handoff remains unimplemented. Phase 7E
real-NVR validation and Phase 8 review-media generation
also remain unimplemented. The required Phase 6C
schema 3 compatibility increment is complete.**

This document is the current implementation and review contract for Phase 7.
It is intentionally limited to one restaurant, one local application host, one
NVR, and one active search run per investigation. Normally one person uses the
application; a second person may open the same local workflow.

Earlier lease, fencing, takeover, resume, crash-recovery, general event-sourced
full-manifest, and multi-process analysis is preserved only in
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
| New `ObjectObservationClassifier` production adapter | Combine EfficientSAM-Ti masks with a deterministic aligned-ROI appearance comparison under the [Phase 7B classification contract](object-presence-classification.md). | Search order, recording acquisition, or interval reasoning. |
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
jpeg_path  # trusted start-time input only; read into handle.baseline_bytes and never persisted or exposed
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

1. call the Phase 6 `load_confirmed()` boundary during run start and require
   schema 3;
2. pass its confined resource metadata, authoritative SHA-256, byte size,
   dimensions, and ROI through the existing Phase 7A-1 baseline gate, which
   captures the validated bytes in `RecordingSearchRunHandle.baseline_bytes`;
3. use that active handle as the only baseline byte source. Phase 7B adds a
   narrow in-memory baseline validator that receives those exact bytes plus the
   immutable schema-3 metadata, bounds size, hashes and validates JPEG structure
   and dimensions, decodes the bytes, and returns the decoded RGB value; it does
   not call the general path helper and reopen the baseline path. For a probe,
   use a narrow Phase 7A read-side validation extension that reads the
   run-relative JPEG once, verifies digest/size and dimensions, decodes those
   exact bytes, and returns the bytes with the admitted request/frame and
   immutable provenance. Phase 7B must not reopen either path for classification;
4. reject a missing, ambiguous, outside-root, indirect, corrupt, size-mismatched,
   digest-mismatched, or out-of-bounds baseline;
5. verify the channel is currently usable for recording search, without
   treating a merely offline camera as proof that historical media is invalid;
6. normalize and validate the search end; and
7. verify the selected classifier, checkpoint, comparison, and acquisition
   policy versions are available.

A failed baseline gate before schema-3 promotion publishes no schema-3 failure
record, baseline observation, probe `ABSENT`, or terminal `FOUND`; schema 2/A2
remains authoritative and the safe result is `baseline_corrupt` or an equivalent
closed operational category. It does not require the historical recording
segment used to create the reference JPEG. If strict reopening later detects
that a committed schema-3 baseline no longer matches its digest/size or
resource identity, reopening fails closed as corruption and does not mutate the
stored lifecycle state.

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
      observations/            # Phase 7B schema 3, not schema 2
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
| `INDETERMINATE` | Yes | A schema-4 `INCONCLUSIVE` result preserves strictly reopened visual inadequacy or contradiction. | Show the fixed visual limitation and allow a new run. |
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

The [Phase 7B recording-probe classification design](object-presence-classification.md)
owns the exact field, identity, publication, and strict-reopen contract. This
section is the search-level summary. The later-phase record union remains
deliberately separate from acquisition:

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
canonical frame. Conceptually it contains the frame reference and classifier
evidence, not a second copy of acquisition provenance; the exact record also
binds its run, baseline, classification operation, policy, and publication
facts as defined by the Phase 7B design:

```text
record_type: recording_probe
observation_id
canonical_frame_id
probe_request_id
primary_requested_time_utc
state: PRESENT | ABSENT | INDETERMINATE
reason_code: null | closed visual-uncertainty reason
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
trusted. Strict validation permits only these state/evidence combinations at
the search level:

| State | Metrics | `reason_code` |
| --- | --- | --- |
| `PRESENT` | Both metrics are finite and satisfy the persisted PRESENT thresholds. | `null` |
| `ABSENT` | Both metrics are finite and satisfy the persisted ABSENT thresholds. | `null` |
| `INDETERMINATE` | Bounded evidence is structurally valid but visual input, mask, comparison area, variance, or finite measurements cannot support a terminal mapping. | Exactly one closed Phase 7B visual-uncertainty reason. |

Acquisition gaps, replay failures, decoder failures, missing provenance, corrupt
input, classifier/runtime failure, invalid classifier output, ownership loss,
and persistence failure never produce a `RecordingProbeObservationRecord`.
Only a successfully completed visual comparison may publish one of the three
states. Phase 7C may account for an operationally failed target at search level,
but it must not fabricate a frame-backed `INDETERMINATE` observation.

The Phase 7B `RawComparison` matrix is closed: every key is present, including
`effective_comparison_area`; fields not valid for the selected reason are null,
and the outer `reason_code` is null for `PRESENT`/`ABSENT`, exactly
`insufficient_visual_evidence` for a comparable policy gap, or exactly equal to
the inner unusable reason. Evaluation is ordered: exact bytes/media, decode and
RGB normalization, ROI geometry, mask/domain validation, aligned comparison
domain, mask IoU and overlap-gate validation, effective comparison area, luma normalization,
variance/NCC preconditions, finite NCC, then policy. The versioned policy snapshot
includes the finite `[0, 1]` `minimum_mask_overlap_for_comparison` gate: a finite
IoU that fails it yields `insufficient_mask_overlap` with later
fields null; a passing IoU with an invalid effective area yields
`insufficient_comparison_area` with NCC null. Unknown reasons, partial
pre-failure measurements, non-finite/out-of-domain values, and inner/outer
mismatches fail strict reopen and publish no observation. Unsupported media,
media-type mismatch, failed decode, invalid decoded structure, unsupported
channel layout, RGB normalization failure, or preprocessing failure is
operational `invalid_media_input`: it creates no RawComparison, observation,
alias, operation, or schema-3 promotion.

The later `TargetAliasRecord` has this conceptual search shape; Phase 7B adds
the exact run/channel/publication ownership fields:

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
`ProbeFrameRequestRecord` values that reference an existing frame. Multiple such
requests resolving to one frame therefore yield one `canonical_frame_id`, not
multiple independent frames.

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
that needs frame distinctness uses one batch operation and requires the
configured number of different frame IDs with strictly increasing normalized
decoded UTC values. Aliased requested targets cannot satisfy that
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
candidate-interval, `FOUND`, or `NOT_FOUND` field; Phase 7B schema 3 owns
observation persistence. Phase 7B does not reject a valid A2 result merely
because classification is a later operation.

### Production classifier policy

The exact classifier, evidence, numeric, error, identity, and publication rules
are in the
[Phase 7B recording-probe classification design](object-presence-classification.md).
Phase 7B owns production policy `efficient-sam-ti-roi-ncc-v1`. EfficientSAM-Ti
supplies category-agnostic masks only; it does not establish object identity or
correspondence. The production adapter therefore reuses the verified lazy
predictor and adds one local NumPy/Pillow aligned-ROI comparator:

1. Input is the verified `handle.baseline_bytes`, confirmed source-pixel ROI,
   one decoded probe JPEG returned by the Phase 7A single-read extension, and
   equal source dimensions. Hashing, decode, preprocessing, and comparison use
   those exact byte sequences; a path is never reopened for classification.
2. Use the deterministic ROI-center point `x + floor((width - 1) / 2), y +
   floor((height - 1) / 2)` on both images. Run the existing EfficientSAM-Ti
   point-prompt mask path and clip each validated mask to the confirmed ROI.
3. For each image, compute mask coverage as the number of segmented mask pixels
   inside that clipped ROI divided by the total pixels of the clipped ROI. A
   missing, empty, or clipped-to-zero ROI, or coverage greater than or equal to
   95%, is `INDETERMINATE` with the matching closed visual reason before
   comparison.
4. Compute `mask_iou` for the two clipped masks and apply the required
   versioned minimum-overlap gate. Compute the finite integral
   `effective_comparison_area`; if either gate fails, persist only the fields
   allowed by the closed RawComparison matrix. Compute `roi_luma_ncc` as
   mean-centered normalized cross-correlation over **all pixels** of the aligned
   source-pixel ROI in the two grayscale images; masks do not select NCC pixels.
   A valid ROI or clipped mask below the minimum, zero luma variance, or a
   structurally valid but unusable mask is `INDETERMINATE`. Non-finite or
   contract-invalid classifier output, corrupt bytes, and unequal stored
   geometry publish no observation.
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
pixel counts, the versioned `minimum_mask_overlap_for_comparison` gate, metric
quantization, and all four outcome thresholds. A visually unusable
but authentic frame may become `INDETERMINATE`. Timeout, unavailable runtime or
checkpoint, invalid classifier output, corrupt evidence, ownership loss, and
persistence failure publish no observation and never become `ABSENT`.

The classification service validates ownership and snapshots these inputs under
the active `RecordingSearchRunHandle` mutation mutex, but writes no operation,
observation, alias, or schema-3 child before classifier success. It releases
that mutex while the bounded predictor/comparator worker runs, and reacquires it
for complete prepublication revalidation. Timeout, cancellation, abandonment,
invalid output, and other operational failure revoke the in-memory attempt and
publish nothing; a schema-2/A2 tree remains byte-for-byte unchanged when the
call began there. Only a still-valid active handle with a timely, revalidated
result may atomically publish the operation, observation/alias, and any schema-3
successor. The exact ownership, timeout, response matrix, and strict-reopen
rules are normative in the [Phase 7B classification design](object-presence-classification.md).

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
| Absence confirmation | `absence_confirmation_frames` distinct frames (default 3) |
| Confirmation cadence | 1 second |
| Maximum consecutive indeterminate coarse targets | 3 |
| Classifier | `efficient-sam-ti-roi-ncc-v1` |
| Present thresholds | mask IoU 0.50; luma NCC 0.60 |
| Absent thresholds | mask IoU 0.10; luma NCC 0.20 |
| Minimum ROI and clipped-mask pixels | 64 |
| Maximum ROI-relative mask coverage | 0.95 |

Changing a value requires a new policy version. These are initial operating
limits to validate against representative NVR footage, not accuracy claims. The
positive support count is bounded by the finite whole-second slots in the
validated search window; a support sequence that does not fit through `E` is
not acquired.

### Coarse sampling

Let `S` be Phase 6 `requested_time_utc` and `E` the validated search end.

1. Create targets `S + 300s`, `S + 600s`, and so on; append `E` if needed.
2. Process targets chronologically with the baseline as initial lower bound.
3. A distinct canonical `PRESENT` observation updates `last_present`; an alias
   never does.
4. An `ABSENT` coarse target `t` is tentative until one bounded confirmation
   batch selects the exact in-window targets `[t + i * cadence for i in
   range(absence_confirmation_frames)]`. If the full sequence does not fit
   through `E`, no support acquisition is attempted and the target remains
   unresolved.
5. Those values are requested targets, not promised decoded timestamps. The
   batch must resolve them to `absence_confirmation_frames` distinct canonical
   frame IDs with strictly increasing decoded UTC, PTS, and ordinal provenance, all classified `ABSENT`;
   only then is `t` the first-absence upper bound and `[last_present, t]` a bracket.
6. Aliases count once and cannot establish absence.
7. `consecutive_unusable_coarse_targets` counts coarse targets, not decoded
   support frames. Each coarse target increments it at most once; aliases never
   increment it. A valid canonical PRESENT coarse result resets it to zero.
   Reaching the configured maximum ends C2 interpretation. D2 publishes
   `INCONCLUSIVE` only when every proving uncertainty is valid durable visual
   evidence; an operational cause remains nonterminal or administrative.

Tentative-absence handling uses this precedence and is exhaustive:

```text
if primary coarse result at t is PRESENT:
  persist it and update last_present
  consecutive_unusable_coarse_targets = 0
  continue with the next configured coarse target

if primary acquisition/decode failed:
  retain the failed request with recording_unavailable
  increment consecutive_unusable_coarse_targets once
  return a typed operational result if the limit is reached; otherwise continue

if primary classifier result is INDETERMINATE:
  retain its committed visual observation
  increment consecutive_unusable_coarse_targets once
  return a typed visual-inconclusive candidate if the limit is reached; otherwise continue

# primary result is tentative ABSENT. The executor may have acquired this
# bounded confirmation batch before classification so all support frames share
# one A2 decoder session; it classifies the primary first and uses the batch
# only when the primary is ABSENT.
support = classify_confirmation_batch(
    [t + i * cadence for i in range(absence_confirmation_frames)]
)

if recording/replay/acquisition/decode operation failed:
  retain the failed request with recording_unavailable
  increment consecutive_unusable_coarse_targets once
  return a typed operational result if the limit is reached; otherwise resume after the support window

canonical, aliases = canonicalize(support)
persist aliases without counting them

if classifier execution failed:
  record one search-level target uncertainty with the fixed operational reason
  publish no RecordingProbeObservationRecord for the failed classification
  increment consecutive_unusable_coarse_targets once
  return a typed operational result if the limit is reached; otherwise resume after the support window

if any canonical observation is INDETERMINATE:
  retain the committed observation and its closed visual reason
  increment consecutive_unusable_coarse_targets once
  return a typed visual-inconclusive candidate if the limit is reached; otherwise resume after the support window

if fewer than absence_confirmation_frames distinct canonical frames exist:
  increment consecutive_unusable_coarse_targets once
  return a typed visual-inconclusive candidate if durable aliasing is the cause;
  otherwise return a typed operational result when the limit is reached

if normalized decoded UTC/PTS/ordinal order is invalid:
  return CORRUPT without a visual terminal candidate

if any canonical observation is PRESENT:
  update last_present from the latest later canonical PRESENT primary target
  consecutive_unusable_coarse_targets = 0
  resume at the first configured coarse target after the support window

if canonical contains exactly absence_confirmation_frames distinct ABSENT frames:
  if last_present is an indexed canonical PRESENT recording probe:
    consecutive_unusable_coarse_targets = 0
    emit internal bracket [last_present, t] with all support IDs
  otherwise:
    increment consecutive_unusable_coarse_targets once
    return a typed operational incomplete-evidence stop if the limit is reached;
    otherwise resume after the support window
```

The missing-lower-bound branch is operational even when the preceding support
frames are valid `ABSENT` observations: absence of a `PRESENT` lower bound is a
missing search precondition, not visual evidence of inadequacy. It therefore
cannot produce a visual-inconclusive candidate or an evidence digest.

One canonical frame is classified once, so aliases cannot disagree about its
state. Multiple unusable support frames for one tentative coarse target still
increment the counter only once. Identical canonical evidence therefore yields
one action independent of how many requested targets alias it.

`NOT_FOUND` requires every coarse target through `E` to resolve to a distinct
canonical `PRESENT` observation and no bracket. A completed scan with an alias,
isolated `ABSENT`, visual uncertainty, unavailable coverage, operational
failure, or no usable `E` cannot become `NOT_FOUND`.

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
    support = confirm_absence_at(
      [M + i * cadence for i in range(absence_confirmation_frames)]
    )
    if support succeeds:
      U = M
      continue

  return a typed nonterminal safe result

finish FOUND with candidate interval [L, U]
```

Each midpoint is a canonical whole-second UTC request. Midpoint ties use the
earlier second through floor division. Binary search never widens the bracket,
crosses a known recording gap, or treats an isolated `ABSENT` as an upper bound.
It uses the same batch identity and support table. During narrowing, any support
result other than the configured number of valid ordered `ABSENT` frames
contradicts or cannot prove the current upper bound and returns a typed safe
result without moving the bound. A later D2 finalizer may publish
`INCONCLUSIVE` only when strictly reopened durable visual evidence proves a
closed visual limitation; a gap, timeout, operational failure, stale input, or
corrupt evidence remains nonterminal or administrative and never becomes a
visual result.

The MVP does not attempt automatic non-monotonic recovery. If strictly reopened
visual evidence inside the active bracket contradicts its `PRESENT -> ABSENT`
ordering, D2 may publish result kind `INCONCLUSIVE` with lifecycle state
`INDETERMINATE`; Phase 9 remains authoritative.

### Deterministic outcomes

| Condition | Run state | Safe reason |
| --- | --- | --- |
| Supported bracket narrowed to the stopping resolution | `FOUND` | `candidate_interval_found` |
| Complete distinct coarse grid with every target canonically `PRESENT` | `NOT_FOUND` | `no_transition_in_window` |
| Required gap or replay/acquisition/decode failure for a coarse target | No terminal result; remain `RUNNING` for bounded retry or use existing administrative failure/interruption | Search-level `recording_unavailable`, never visual evidence |
| Classifier operational failure | Publish no probe observation and no terminal result | Fixed operational reason; bounded retry or administration |
| Valid visual `INDETERMINATE`, contradictory ordering, or policy support made non-distinct by aliases | schema-4 state `INDETERMINATE`, result kind `INCONCLUSIVE`, only after strict D2 proof | One closed visual limitation, never `ABSENT` |
| Invalid decoded order, missing index, or corrupt evidence | No terminal result or lifecycle mutation | `authoritative_evidence_invalid` |
| Confirmed JPEG or Phase 6 input fails integrity validation before schema-3 promotion | No schema-3 promotion, observation, or `FOUND`; authoritative schema 2/A2 remains unchanged | `baseline_corrupt` (repair or human intervention required) |
| Strict reopen finds a committed schema-3 baseline digest/resource mismatch | No lifecycle mutation or visual outcome; existing committed evidence remains immutable but the run is not operational | `manifest_corrupt`/`baseline_corrupt` |
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
classifier/search persistence is rejected by the v2 loader. Phase 7B promotes a
strict active v2 manifest to `schema_version: 3`, preserves every A2 field and
index, adds one baseline observation plus ordered classification-operation,
canonical-observation, and alias indexes, and still excludes terminal search and
Phase 8 fields. Its exact shape and atomic promotion rules are defined in the
[Phase 7B classification design](object-presence-classification.md). A2 itself
continues to accept no future version or terminal shape.

The following is the complete shape of a valid A2 `RUNNING` manifest. The frame
and request records are stored separately and are reachable only through these
indexes. The Phase 7B-only preprocessing and comparison fields, including
`minimum_mask_overlap_for_comparison`, are absent from schema 2 and are added as
finite validated values only by schema-3 promotion.

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
    "anchor_time_utc": "2026-07-20T03:34:18Z",
    "reference_requested_time_utc": "2026-07-20T03:34:13Z",
    "source_timezone": "Asia/Seoul",
    "source_width": 2560,
    "source_height": 1440,
    "roi": {"x": 481, "y": 927, "width": 214, "height": 163, "coordinate_space": "source_pixels", "provenance": "manual"},
    "jpeg_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
    "jpeg_size_bytes": 481920,
    "candidate_offset_seconds": -5,
    "generation_policy_version": 2,
    "frame_selection_policy": "gpv-2",
    "estimated_source_time_utc": null,
    "decoded_local_pts_seconds": null,
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
created by Phase 7B under schema 3. The
observation index and later alias index are distinct from the A2 frame and request
indexes. Every record rejects unknown, duplicate, missing, or cross-variant
fields. A later observation must reference an indexed canonical frame and its
successful request; it cannot be reconstructed from requested time alone.

Schemas 2 and 3 continue to reject terminal fields. The corrected schema-4
relationship, result union, strict reference rules, and parser dispatch are
defined in the D2 contract below. Every resolved probe must belong to the same
run and match its investigation, channel, policy, dimensions, and indexed
identity. A missing, mismatched, out-of-order, threshold-invalid, aliased
support, or semantically inconsistent record makes terminal reopen invalid and
must not be returned, rendered, or handed to Phase 8.

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
| 14. Multiple aliases reference one frame | Multiple request records resolve to one canonical frame; they cannot satisfy configured distinct-frame absence support or create multiple observations. |

## Phase 7D-2 terminal result contract

**Implementation status:** the pure D2-2 terminal interpreter and typed
in-memory identities are implemented; D2-3 provides schema-4 publication and
D2-4 provides strict process-restart reopen validation, safe status projection,
and duplicate conflict handling. D2 is the only boundary that may turn strictly reopened Phase 7
evidence into an authoritative terminal search result. It does not acquire or
classify media, reinterpret arbitrary paths, or create Phase 8 media.

### D1 precondition and required handoff correction

The implemented D1 service derives absence support from
`policy.absence_confirmation_frames`; the current default is three, but three is
not a D1 invariant. It also requires that many distinct observation and
canonical-frame IDs from one decode session with strictly increasing decoded
UTC, PTS, and ordinal values. The phrase “three distinct frames” in older D1
test names describes the default fixture only.

The current `NarrowedBracket` is not yet sufficient for durable terminalization.
Its `source_bracket_id` is a hash of the transient C2 bracket, but the original
C2 bracket fields are discarded as narrowing advances. D2 cannot reconstruct
that hash unambiguously from the final bounds. Likewise, a non-ready D1 result
does not retain the specific persisted visual observation or support sequence
that caused a visual stop. Before D2 runtime is implemented, D1 models must be
extended narrowly so that:

1. a ready bracket carries the complete immutable `CoarseCandidateBracket`, its
   `plan_id`, a SHA-256 identity of the complete persisted policy snapshot, and
   a deterministic `narrowed_bracket_id` computed from that source plus every
   ordered narrowing target and evidence reference;
2. the source-bracket hash remains a recomputed check, not the only source fact;
3. a visually stopped result carries the exact admitted blocking observation or
   ordered support references and a refreshed authoritative manifest digest;
4. operational, interrupted, stale, corrupt, and capacity results carry no
   visual terminal candidate; and
5. all additions remain in-memory only. D1 still publishes no result record or
   manifest state.

This is an implementation prerequisite, not a redesign of midpoint selection or
the A2/B4 evidence boundaries. Until it is satisfied, D2 must reject the input
without mutation as `terminal_input_incomplete`.

### Closed D1/C2 visual and operational handoff

D1 and C2 use closed internal unions. A status plus a free-form `safe_reason`
is not an authoritative result and is never interpreted by D2. The serialized
names below are the only reason codes; diagnostic text may be attached to an
in-memory failure for debugging, but it is not persisted, hashed, or used for a
branch decision.

```text
OperationalStopReason =
  interrupted
  | cancelled
  | timeout
  | capacity_exhausted
  | recording_coverage_gap
  | acquisition_failed
  | decode_failed
  | classification_failed
  | incomplete_evidence
  | stale_authority
  | inactive_authority
  | ownership_mismatch
  | corrupt_persisted_evidence
  | adapter_unknown_result
  | publication_in_progress
  | publication_readback_failed
  | publication_invariant_failure
  | unexpected_error

VisualStopReason =
  insufficient_visual_evidence
  | nonmonotonic_visual_evidence
  | insufficient_distinct_visual_support

C2Result =
  C2BracketReady
    bracket: CoarseCandidateBracket
    evidence_snapshot_digest: lowercase SHA-256
  | C2NoCandidate
    complete_present_grid: ordered typed evidence references
    evidence_snapshot_digest: lowercase SHA-256
  | C2VisualInconclusive
    reason: VisualStopReason
    evidence: ordered typed visual evidence references
    evidence_snapshot_digest: lowercase SHA-256
  | C2OperationalStop
    reason: OperationalStopReason
    attempted_target_ids: ordered unique target IDs
  # OperationalStop has no visual evidence, bracket, or digest.

D1Result =
  D1BracketReady
    narrowed_bracket: corrected NarrowedBracket
    evidence_snapshot_digest: lowercase SHA-256
  | D1VisualTerminal
    reason: VisualStopReason
    narrowing_history: complete typed history
    blocking_evidence: ordered typed visual evidence references
    evidence_snapshot_digest: lowercase SHA-256
  | D1OperationalStop
    reason: OperationalStopReason
    attempted_target_ids: ordered unique target IDs
  | D1NonTerminalStop
    reason: no_distinct_midpoint | maximum_iterations | incomplete_evidence
    narrowing_history: complete typed history through the stop
  # Neither stop variant carries a visual terminal candidate or digest.
```

`C2BracketReady`, `C2NoCandidate`, and `C2VisualInconclusive` (and the
corresponding `D1BracketReady`/`D1VisualTerminal` forms) may carry only evidence
that was successfully acquired, decoded, classified, durably published, and
strictly reopened from the owning run indexes. `C2BracketReady` and
`D1BracketReady` must carry their complete source bracket and all ordered
narrowing evidence. `C2NoCandidate` must carry the complete distinct canonical
`PRESENT` grid. `C2VisualInconclusive` and `D1VisualTerminal` must carry the
exact blocking visual observation(s) or support group that prove the closed
visual reason. `C2OperationalStop`, `D1OperationalStop`, and
`D1NonTerminalStop` may carry only target IDs and, for D1, its typed history;
they carry no observation, frame, support-group, or visual-terminal field. D2 accepts
only typed bracket-ready, no-candidate, and visual-terminal forms and routes
both stop forms to a nonterminal safe outcome. In particular, a
recording gap, decoder/classifier failure, timeout, interruption, capacity
exhaustion, stale authority, ownership loss, or corrupt evidence can never be
converted into `NOT_FOUND`, `FOUND`, or visual `INCONCLUSIVE`.

### Normative C2/D1 result adapter (D2-0)

The current C2 and D1 implementations still return status enums plus free-form
reasons. D2-0 adds one total, pure adapter over those values and the strict
authoritative evidence snapshot. It is the only place where those legacy
outputs are converted into the closed unions above. The adapter never infers a
visual state from a string alone: every visual target requires the exact
reopened observation/support references named by the target variant.

The adapter first validates the source result shape and then applies the tables
below. When more than one operational cause is present, it chooses the first
cause in persisted target/iteration order; a tie uses this fixed precedence:
`interrupted`, `recording_coverage_gap`, `timeout`, `classification_failed`,
`decode_failed`, `acquisition_failed`, `unexpected_error`,
`incomplete_evidence`. It never chooses a visual result merely because the
source status is `INCONCLUSIVE` or `INDETERMINATE`.

#### C2 adapter

| Current C2 output | Required evidence/condition | D2-0 result | Visual evidence/digest | Publication/retry |
| --- | --- | --- | --- | --- |
| `BRACKET_READY` with `bracket` | Complete bracket, source revision, and every support reference strictly reopened and validated | `C2BracketReady` | Permitted; digest required | Candidate may proceed to D2 `FOUND`; retry only after an explicit stale-snapshot recapture |
| `NO_CANDIDATE` / `no_supported_transition` | Complete plan through `E`, every coarse target is a distinct canonical `PRESENT`, no alias/gap/uncertainty | `C2NoCandidate` | PRESENT grid only; digest required | D2 may publish `NOT_FOUND`; no automatic retry |
| `INCONCLUSIVE` / `nonmonotonic_visual_evidence` | Strictly reopened contradictory visual observations, with no operational cause | `C2VisualInconclusive(nonmonotonic_visual_evidence)` | Required; digest required | D2 may publish `INCONCLUSIVE`; explicit new run |
| `INCONCLUSIVE` / `insufficient_visual_evidence` | Strictly reopened visual `INDETERMINATE` evidence, with no operational cause | `C2VisualInconclusive(insufficient_visual_evidence)` | Required; digest required | D2 may publish `INCONCLUSIVE`; explicit new run |
| `INCONCLUSIVE` / `maximum_consecutive_unusable_targets` | Every proving uncertainty is valid visual evidence caused by non-distinct aliases/duplicate visual support; no failed request, gap, timeout, interruption, or classifier failure | `C2VisualInconclusive(insufficient_distinct_visual_support)` | Required; digest required | D2 may publish `INCONCLUSIVE`; explicit new run |
| `INCONCLUSIVE` / `maximum_consecutive_unusable_targets` | Any operational failure, missing lower bound, or absent proof of the preceding row | `C2OperationalStop` with the fixed cause from the precedence rule | Forbidden; no digest | No terminal publication; bounded retry or administration |
| `INCONCLUSIVE` / `missing_present_lower_bound` | No indexed PRESENT baseline/probe lower bound | `C2OperationalStop(incomplete_evidence)` | Forbidden; no digest | No terminal publication; retry after a valid baseline/run |
| `INCONCLUSIVE` / `insufficient_visual_evidence` without strict visual references | Source evidence is incomplete, failed, foreign, or only operational | `C2OperationalStop(incomplete_evidence)` or the exact operational cause | Forbidden; no digest | No terminal publication; bounded retry or administration |
| `INCOMPLETE` / `coarse_execution_incomplete` | Execution is incomplete and no interruption cause is present | `C2OperationalStop(incomplete_evidence)` | Forbidden; no digest | No terminal publication; bounded retry |
| `INCOMPLETE` with an interrupted sample | A persisted sample has `CoarseSampleStatus.INTERRUPTED` | `C2OperationalStop(interrupted)` | Forbidden; no digest | No terminal publication; explicit new run |
| `INTERRUPTED` / `coarse_execution_interrupted` | Interrupted execution | `C2OperationalStop(interrupted)` | Forbidden; no digest | No terminal publication; explicit new run |
| Any source sample `SUCCESS` | The request, observation, classification, frame, session, and decoded fields are all strictly present and owned; this is evidence input, not a terminal result | Continue the enclosing C2 row; malformed success is `C2OperationalStop(corrupt_persisted_evidence)` | Only the enclosing visual/bracket row may carry a digest | No independent publication/retry decision |
| `CORRUPT` / `coarse_plan_mismatch` or `authoritative_evidence_invalid` | Any strict manifest, index, ownership, provenance, or digest failure | `C2OperationalStop(corrupt_persisted_evidence)` | Forbidden; no digest | No terminal publication; fail closed |
| Any source sample `RECORDING_UNAVAILABLE` or reason `recording_unavailable` | Recording coverage is unavailable | `C2OperationalStop(recording_coverage_gap)` | Forbidden; no digest | No terminal publication; bounded retry |
| Any source sample `TIMEOUT` | Acquisition/classifier timeout | `C2OperationalStop(timeout)` | Forbidden; no digest | No terminal publication; bounded retry |
| Any source sample `CLASSIFICATION_FAILED` | Classifier failed without a committed visual observation | `C2OperationalStop(classification_failed)` | Forbidden; no digest | No terminal publication; bounded retry |
| Any source sample `ACQUISITION_FAILED` with retained `decode_failed` reason | Decode/provenance failure | `C2OperationalStop(decode_failed)` | Forbidden; no digest | No terminal publication; bounded retry |
| Any source sample `ACQUISITION_FAILED` otherwise | Acquisition failure | `C2OperationalStop(acquisition_failed)` | Forbidden; no digest | No terminal publication; bounded retry |
| Any source sample `UNEXPECTED_ERROR` | Unclassified boundary failure | `C2OperationalStop(unexpected_error)` | Forbidden; no digest | No terminal publication; bounded retry or administration |
| Any source failure with an unrecognized free-form `safe_reason` (including `inactive_handle`, `stale_run_owner`, `invalid_acquisition_result`, `request_time_mismatch`, `request_not_ready`, `classification_identity_mismatch`, or `coarse_support_target_failed`) | Status/evidence is insufficient to prove a closed known cause | `C2OperationalStop(adapter_unknown_result)` | Forbidden; no digest | No terminal publication; fail closed and require a versioned adapter update |
| Unknown status, unknown reason, contradictory status/fields, or a newly introduced free-form reason | Adapter cannot prove a known closed case | `C2OperationalStop(adapter_unknown_result)` | Forbidden; no digest | No terminal publication; fail closed and require a versioned adapter update |

`NO_CANDIDATE` is not accepted from a partial or unresolved snapshot merely
because the enum is present. Conversely, an isolated `ABSENT`, a gap, a
timeout, or a classifier failure is never repaired into a visual row.

#### D1 adapter

| Current D1 output | Required evidence/condition | D2-0 result | Visual evidence/digest | Publication/retry |
| --- | --- | --- | --- | --- |
| `READY` (serialized status `NARROWED_BRACKET_READY`) / `TARGET_PRECISION_REACHED` (serialized `target_precision_reached`) | Corrected bracket and complete history strictly reopened; interval is within policy resolution | `D1BracketReady` | Required; digest required | D2 may publish `FOUND`; stale input requires recapture |
| `READY` / `MAXIMUM_ITERATIONS` (serialized `maximum_iterations`) | Bracket remains wider than the policy resolution | `D1NonTerminalStop(maximum_iterations)` | Forbidden as a terminal candidate; no digest | No terminal publication; bounded retry or explicit new run |
| `INDETERMINATE` / `no_distinct_midpoint` (serialized `no_distinct_midpoint`) | No valid interior whole-second midpoint | `D1NonTerminalStop(no_distinct_midpoint)` | Forbidden; no digest | No terminal publication; explicit new run or corrected policy |
| `INDETERMINATE` / `visual_indeterminate` | Strictly reopened visual `INDETERMINATE` midpoint evidence | `D1VisualTerminal(insufficient_visual_evidence)` | Required; digest required | D2 may publish `INCONCLUSIVE`; explicit new run |
| `INDETERMINATE` / `absence_support_unusable` | Valid visual support is present but aliases/duplicates cannot supply the policy-count distinct frames | `D1VisualTerminal(insufficient_distinct_visual_support)` | Required; digest required | D2 may publish `INCONCLUSIVE`; explicit new run |
| `INDETERMINATE` / `absence_support_unusable` without valid visual support | Support is missing because of a gap, failed request, timeout, or incomplete evidence | `D1OperationalStop` with the exact operational cause, or `D1NonTerminalStop(incomplete_evidence)` when no narrower cause is available | Forbidden; no digest | No terminal publication; bounded retry |
| `INDETERMINATE` / `acquisition_failed` | Acquisition failed | `D1OperationalStop(acquisition_failed)` | Forbidden; no digest | No terminal publication; bounded retry |
| `INDETERMINATE` / `acquisition_timeout` or source `TIMEOUT` | Timeout | `D1OperationalStop(timeout)` | Forbidden; no digest | No terminal publication; bounded retry |
| `INDETERMINATE` / `classification_failed` | Classifier failure without a visual observation | `D1OperationalStop(classification_failed)` | Forbidden; no digest | No terminal publication; bounded retry |
| `INDETERMINATE` / `unexpected_error` or `narrowing_evidence_unusable` | Unclassified execution failure | `D1OperationalStop(unexpected_error)` | Forbidden; no digest | No terminal publication; bounded retry or administration |
| Probe evidence `SUCCESS` | Complete request, classification, observation, frame, decode, and ownership references are strictly reopened; continue the named D1 transition | Only the resulting bracket/visual row may carry a digest | No independent result |
| `INTERRUPTED` / `inactive_run_handle` | Caller no longer owns the active authority | `D1OperationalStop(inactive_authority)` | Forbidden; no digest | No terminal publication; explicit new run |
| `INTERRUPTED` / `interrupted` | Process or active execution interruption | `D1OperationalStop(interrupted)` | Forbidden; no digest | No terminal publication; explicit new run |
| `CORRUPT` / `authoritative_evidence_invalid` | Strict reopen, ownership, provenance, or index failure | `D1OperationalStop(corrupt_persisted_evidence)` | Forbidden; no digest | No terminal publication; fail closed |
| `CORRUPT` / `stale_authoritative_evidence` | Manifest/index digest changed or source authority is stale | `D1OperationalStop(stale_authority)` | Forbidden; no digest | No terminal publication; explicit recapture |
| `INCOMPLETE` or `RESOURCE_EXHAUSTED` | Current enum is emitted by a future/extended D1 path | `D1NonTerminalStop(incomplete_evidence)` or `D1OperationalStop(capacity_exhausted)` respectively | Forbidden; no digest | No terminal publication; bounded retry/administration |
| Failure probe with an unrecognized free-form reason | Source status/evidence cannot prove a known closed cause | `D1OperationalStop(adapter_unknown_result)` | Forbidden; no digest | No terminal publication; fail closed and require a versioned adapter update |
| Unknown status, stop reason, source status, or contradictory fields | Adapter cannot prove a known closed case | `D1OperationalStop(adapter_unknown_result)` | Forbidden; no digest | No terminal publication; fail closed and require a versioned adapter update |

The adapter's unknown branch is part of the contract, not a fallback to
`INCONCLUSIVE`. D2-0 adds one discrimination test for every table row and one
unknown-status/reason test for each source union. These tests must exercise the
adapter with real C2/D1 result objects and prove that no operational row carries
visual evidence or a digest.

The digest is computed only after this adapter accepts a visual or bracket-ready
variant. Visual variants hash only their allowlisted, strictly reopened visual
references and immutable context. Operational and nonterminal variants have no
visual digest and cannot reuse one from an earlier candidate. The dependency
graph is acyclic: source C2/policy -> D1 input identity -> support-group IDs ->
ordered history/narrowed-bracket ID -> evidence snapshot digest -> terminal
result ID. No adapter reason, diagnostic, digest, bracket ID, or result ID is an
input to an earlier node in that graph.

The `evidence_snapshot_digest` is defined exactly as follows. Its algorithm tag
is `recording-search-evidence-snapshot-v1`; the digest is the lowercase
hexadecimal SHA-256 of the UTF-8 bytes of that payload. The payload is compact
canonical JSON with lexicographically sorted object keys at every object level,
the separators `,` and `:`, `ensure_ascii=false`, no insignificant whitespace,
no floating-point or non-finite values, strict JSON integers, and canonical UTC
strings. Arrays retain the semantic order below and are never sorted to repair
untrusted input:

```json
{
  "identity_schema": "recording-search-evidence-snapshot-v1",
  "investigation_id": "...",
  "search_run_id": "...",
  "phase6_confirmation_id": "...",
  "baseline_observation_id": "...",
  "plan_id": "...",
  "policy_identity": "...",
  "source_revision": {
    "manifest_digest": "...",
    "c2_bracket_id": "...",
    "d1_source_bracket_id": "..."
  },
  "references": [
    {
      "role": "BASELINE",
      "target_id": null,
      "requested_time_utc": "...",
      "acquisition_operation_id": null,
      "probe_request_id": null,
      "classification_operation_id": null,
      "observation_id": "...",
      "canonical_frame_id": null,
      "alias_id": null,
      "decode_session_id": null,
      "decoded_frame_utc": null,
      "decoded_pts": null,
      "decoded_ordinal": null,
      "support_group_id": null,
      "support_index": null,
      "is_phase6_baseline": true
    }
  ],
  "support_groups": [
    {
      "support_group_id": "...",
      "origin_target_id": "...",
      "support_count": 3,
      "cadence_seconds": 1,
      "decode_session_id": "...",
      "member_target_ids": ["..."],
      "member_observation_ids": ["..."],
      "member_canonical_frame_ids": ["..."]
    }
  ]
}
```

The `role` value is exactly one of `BASELINE`, `COARSE_TARGET`, `D1_MIDPOINT`,
or `ABSENCE_SUPPORT`. All keys shown are present; `null` is used only where the
role has no such identity (for example, the Phase 6 baseline). The numeric `3`
and `1` in this illustrative payload are not product defaults; the persisted
policy supplies the actual support count and cadence. Reference arrays are in
coarse
plan order, then narrowing iteration order, then support index order. A support
reference must have one `support_group_id`, a zero-based `support_index`, and
the group must contain exactly `support_count` distinct target, observation,
and canonical-frame IDs at the persisted cadence. Every non-baseline visual
reference must have matching request, observation, canonical-frame, operation,
decode-session, decoded UTC, PTS, and ordinal identities from the same run;
aliases are recorded but cannot occupy a distinct support position. The digest
excludes wall-clock publication/completion times, filesystem paths, URLs,
credentials, invocation tokens, free-form reasons, and other unstable values.
The validator rejects missing, extra, duplicated, reordered, foreign,
noncanonical, or contradictory fields before the digest is accepted.

The corrected D1 handoff retains a lossless ordered history rather than only the
current bounds:

```text
NarrowingHistoryEntry
  iteration: strict integer, contiguous from zero
  requested_midpoint_time_utc: canonical whole-second UTC
  role: MIDPOINT | ABSENCE_SUPPORT
  bracket_before:
    lower_requested_time_utc, upper_requested_time_utc
    lower_bound_reference, upper_support_references
  midpoint:
    target_id, probe_request_id, observation_id, canonical_frame_id
    acquisition_operation_id, classification_operation_id
    decode_session_id, decoded_frame_utc, decoded_pts, decoded_ordinal
  visual_classification: PRESENT | ABSENT | INDETERMINATE | null
  support_group: null | {
    support_group_id, origin_target_id, support_index, support_count,
    cadence_seconds, decode_session_id, ordered member references
  }
  bracket_after:
    lower_requested_time_utc, upper_requested_time_utc
    lower_bound_reference, upper_support_references
  stop_reason: null | closed visual, operational, or nonterminal reason
```

Every entry contains the complete bracket before and after the transition. A
midpoint entry has the exact requested target and all available acquisition,
decode, and observation provenance; a support entry has the same fields plus its
support-group membership and zero-based index. `visual_classification` is null
only when the target was not admitted as a visual observation. The final entry
alone may carry `stop_reason`; no entry may follow a stopped entry. The source
C2 bracket is retained in full, including its investigation/run/plan/policy and
baseline bindings, requested bounds, lower reference, ordered support target,
request, observation, canonical-frame, decode-session, decoded UTC/PTS/ordinal,
and manifest-digest fields. `source_bracket_id` remains a recomputed check, not
the only retained source fact.

History validation is deterministic: iteration numbers are contiguous and
ordered; entry `bracket_before` equals the prior `bracket_after` (the first
equals the complete C2 bracket); each midpoint is the unique floor midpoint
strictly inside its preceding interval; a PRESENT transition moves only the
lower bound to that midpoint; an ABSENT transition moves only the upper bound
after a complete policy-count support group; and a visual, operational, or
nonterminal stop leaves bounds unchanged. Every target, request, observation,
canonical-frame, operation, and support-group ID is unique within its role and
belongs to the same run, plan, policy, and source revision. Support members have
one decode session and strictly increasing requested UTC, decoded UTC, PTS, and
ordinal values; aliases, gaps, missing provenance, and duplicate canonical
frames cannot occupy support positions. The configured
`absence_confirmation_frames` and cadence are read from the persisted policy,
not assumed to be three or one second. Malformed, incomplete, reordered,
duplicated, foreign, or contradictory history fails closed as
`terminal_input_incomplete` without mutation.

#### D1 history construction and support identity (D2-1)

`BinaryNarrowingService.narrow` is the sole owner of D1 history construction;
`RepositoryNarrowingEvidenceStore` only persists, reopens, and strictly
revalidates the records produced by that service. D2-1 implements this
transaction-local algorithm, while D2-0 supplies the closed result adapter:

1. Before probing a midpoint, the service snapshots the complete current C2/D1
   bracket and creates one provisional `MIDPOINT` entry containing the
   iteration, floor midpoint, source revision, and bracket-before value.
2. Acquisition/classification evidence is attached only after the existing
   A2/B4 records are strictly reopened. A failed, timed-out, interrupted,
   foreign, or otherwise non-admitted probe finalizes the entry as an
   operational stop with unchanged bounds and no visual evidence.
3. A strictly admitted `PRESENT` midpoint finalizes the entry by moving only
   the lower bound to that midpoint and recording bracket-after.
4. A first strictly admitted `ABSENT` midpoint creates (or references) one
   D1 support group before any support member is appended. The midpoint is
   member index zero; later support probes append by zero-based index in
   requested-time order.
5. Only a complete policy-count support group with distinct canonical frames,
   observations, one decode session, and increasing decoded UTC/PTS/ordinal
   values finalizes the entry by moving only the upper bound. A gap, alias,
   duplicate, count/cadence mismatch, foreign member, or failed support probe
   leaves bounds unchanged and records an operational or nonterminal stop.
6. A visual `INDETERMINATE` or contradiction stop is recorded only when D2-0
   has strictly reopened the exact visual references proving that reason.
   Missing or operational evidence uses the operational stop path.
7. The service appends the finalized entry exactly once, computes the new
   manifest digest, and returns immutable ordered history with the complete
   source C2 bracket. No operation follows a stopped entry.

The D1 handoff contains the source bracket, policy identity, every ordered
entry, and all child references. D2-1 reconstruction reopens those records,
recomputes every bracket transition and support group, compares the history
and history digest, and rejects any inferred or missing entry; D2 never
reconstructs history from a generic evidence list.

`d1_input_bracket_id` is the first D1-specific identity. D2-1 computes
`d1-input-bracket-v1-<sha256>` from this exact object before any D1 support
group exists:

```json
{
  "identity_schema": "phase7d-d1-input-bracket-v1",
  "investigation_id": "...",
  "search_run_id": "...",
  "phase6_confirmation_id": "...",
  "baseline_identity": "...",
  "plan_id": "...",
  "policy_identity": "...",
  "source_revision": {
    "c2_bracket_id": "...",
    "c2_manifest_digest": "..."
  },
  "lower_bound": {
    "kind": "PHASE6_BASELINE | PRESENT_PROBE",
    "target_id": "... | null",
    "requested_time_utc": "...",
    "observation_id": "...",
    "probe_request_id": "... | null",
    "canonical_frame_id": "... | null"
  },
  "upper_absence_support": {
    "kind": "C2_ABSENCE_SUPPORT",
    "c2_support_group_id": "...",
    "origin_requested_time_utc": "...",
    "upper_bound_requested_time_utc": "...",
    "support_count": 3,
    "cadence_seconds": 1,
    "requested_time_utc": ["...", "...", "..."],
    "probe_request_ids": ["...", "...", "..."],
    "observation_ids": ["...", "...", "..."],
    "canonical_frame_ids": ["...", "...", "..."],
    "decode_session_id": "...",
    "decoded_frame_utc": ["...", "...", "..."],
    "decoded_pts": [0, 0, 0],
    "decoded_ordinals": [0, 0, 0]
  }
}
```

The displayed `|` alternatives are documentation notation only. Persisted JSON
has one value: `lower_bound.kind` is exactly `PHASE6_BASELINE` or
`PRESENT_PROBE`; baseline lower bounds require `target_id`, `probe_request_id`,
and `canonical_frame_id` to be JSON `null`, while probe lower bounds require
all three strings and reject `null`. All other keys are required and never
null. `plan_id` is the complete canonical C2 coarse-plan identity, and
`policy_identity` is the SHA-256 identity of the complete persisted policy.
The source revision binds the C2 bracket and its complete manifest digest.
The upper arrays are aligned by zero-based support index and preserve requested
time order; they are never sorted to repair input. Requested and decoded UTC
values use canonical `YYYY-MM-DDTHH:MM:SSZ` strings in this identity, PTS and
ordinal values are strict JSON integers, and every support value is within
`search_end_utc`. The C2 support identity is the existing C1-derived
`confirmation_run_id`; it is explicitly a C2 identity and is not a D1 support
identity.

The canonical serialization for this object and every identity below is UTF-8
JSON with lexicographically sorted object keys at every level,
`ensure_ascii=false`, separators `,` and `:`, no insignificant whitespace,
strict integers only, and no non-finite values. The SHA-256 is over those exact
UTF-8 bytes and is emitted as lowercase hexadecimal. The versioned tag provides
domain separation from C1 plans/support IDs, D1 support groups, history,
narrowed brackets, evidence snapshots, and terminal results. D2-1 strictly
recomputes this payload from the reopened C2 bracket and rejects any mismatch;
it never trusts a stored derived ID.

For an independent input-identity check, the following complete fixture has
digest `4a1c216e94837b8077d6e3190a4554b31293dc58050abba7a0c11656f9f4e08d`:

```json
{
  "baseline_identity": "baseline-demo",
  "identity_schema": "phase7d-d1-input-bracket-v1",
  "investigation_id": "investigation-demo",
  "lower_bound": {
    "canonical_frame_id": "frame-present",
    "kind": "PRESENT_PROBE",
    "observation_id": "obs-present",
    "probe_request_id": "probe-present",
    "requested_time_utc": "2026-07-20T03:00:00Z",
    "target_id": "target-present"
  },
  "phase6_confirmation_id": "confirmation-demo",
  "plan_id": "coarse-plan-demo",
  "policy_identity": "policy-demo",
  "search_run_id": "search-run-demo",
  "source_revision": {
    "c2_bracket_id": "coarse-bracket-demo",
    "c2_manifest_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "upper_absence_support": {
    "c2_support_group_id": "coarse-confirmation-demo",
    "cadence_seconds": 1,
    "canonical_frame_ids": ["frame-absent-0", "frame-absent-1", "frame-absent-2"],
    "decode_session_id": "decode-demo",
    "decoded_frame_utc": [
      "2026-07-20T03:00:04Z",
      "2026-07-20T03:00:05Z",
      "2026-07-20T03:00:06Z"
    ],
    "decoded_ordinals": [100, 101, 102],
    "decoded_pts": [4000, 5000, 6000],
    "kind": "C2_ABSENCE_SUPPORT",
    "observation_ids": ["obs-absent-0", "obs-absent-1", "obs-absent-2"],
    "origin_requested_time_utc": "2026-07-20T03:00:04Z",
    "probe_request_ids": ["probe-absent-0", "probe-absent-1", "probe-absent-2"],
    "requested_time_utc": [
      "2026-07-20T03:00:04Z",
      "2026-07-20T03:00:05Z",
      "2026-07-20T03:00:06Z"
    ],
    "support_count": 3,
    "upper_bound_requested_time_utc": "2026-07-20T03:00:04Z"
  }
}
```

D2-1 owns the identity producer and its independent reconstruction tests.

D1 uses a separate support-group identity (choice B). The current C1
`confirmation_run_id_for` payload has no D1 input-bracket, source-revision,
iteration, or ordered support-time dimensions. D2-1 defines
`support_group_id` as
`d1-support-group-v1-<sha256>` over the following exact object, serialized as
compact canonical JSON (UTF-8, `ensure_ascii=false`, lexicographically sorted
keys at every object level, separators `,` and `:`, no floats/non-finite
values, strict integers, canonical UTC strings):

```json
{
  "identity_schema": "phase7d-d1-support-group-v1",
  "investigation_id": "...",
  "search_run_id": "...",
  "phase6_confirmation_id": "...",
  "baseline_identity": "...",
  "plan_id": "...",
  "policy_identity": "...",
  "source_revision": {
    "c2_bracket_id": "...",
    "c2_manifest_digest": "..."
  },
  "d1_input_bracket_id": "...",
  "iteration": 0,
  "origin_midpoint_requested_time_utc": "...",
  "support_count": 3,
  "cadence_seconds": 1,
  "requested_support_times": ["...", "...", "..."]
}
```

`requested_support_times[i]` is exactly
`origin_midpoint_requested_time_utc + i * cadence_seconds`, with `i` zero
based, and every value is at or before `search_end_utc`. Count and cadence
come from persisted policy, not defaults. Decoded UTC, PTS, ordinal,
observation IDs, and canonical-frame IDs are evidence fields only: they are
strictly validated but are not support-ID inputs. Reused classified frames
retain their original ownership and canonical IDs and must occupy the same
requested index. Aliases, duplicate observations or canonical frames, foreign
ownership, reordered members, count/cadence mismatch, wrong source revision,
and support times beyond the search end are rejected. No identity uses
fabricated provenance absent from the Phase 6 baseline.

For independent reconstruction, this fixed test payload produces the fixed
digest shown below (the values are test data, not product defaults):

```json
{
  "baseline_identity": "baseline-demo",
  "cadence_seconds": 1,
  "d1_input_bracket_id": "d1-input-bracket-demo",
  "identity_schema": "phase7d-d1-support-group-v1",
  "investigation_id": "investigation-demo",
  "iteration": 0,
  "origin_midpoint_requested_time_utc": "2026-07-20T03:00:04+00:00",
  "phase6_confirmation_id": "investigation-demo",
  "plan_id": "coarse-plan-demo",
  "policy_identity": "policy-demo",
  "requested_support_times": [
    "2026-07-20T03:00:04+00:00",
    "2026-07-20T03:00:05+00:00",
    "2026-07-20T03:00:06+00:00"
  ],
  "search_run_id": "search-run-demo",
  "source_revision": {
    "c2_bracket_id": "coarse-bracket-demo",
    "c2_manifest_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "support_count": 3
}
```

The lowercase SHA-256 of its canonical UTF-8 bytes is
`a8fc88a9f5c5c0aa49fd106795d572233ef65ff0109a98e0268c51823e03b5c6`.
This domain-separated payload cannot collide with C1 support IDs. The
dependency remains acyclic: source C2/policy -> D1 input bracket -> support
group -> history/narrowed bracket -> evidence digest -> terminal result ID.

`history_digest` is the second D1-specific identity. D2-1 represents an
operational stop as a typed non-transition history entry; it is not silently
discarded and it never carries visual evidence. The complete immutable history
is hashed as `d1-history-v1-<sha256>` from an exact canonical object with these
required top-level keys: `identity_schema`, `d1_input_bracket_id`,
`investigation_id`, `search_run_id`, `phase6_confirmation_id`,
`baseline_identity`, `plan_id`, `policy_identity`, `source_revision`, and
ordered `entries`. `source_revision` contains exactly `c2_bracket_id` and
`c2_manifest_digest`.

Every history entry always has exactly these keys: `iteration` (strict
zero-based integer), `entry_kind` (`PRESENT_TRANSITION`, `ABSENT_TRANSITION`,
`VISUAL_STOP`, or `OPERATIONAL_STOP`), `target_id`,
`midpoint_requested_time_utc`, `bracket_before`, `evidence`, `classification`,
`support_group_id`, `support_indexes`, `bracket_after`,
`visual_stop_reason`, and `operational_stop_reason`. All UTC values are
canonical `YYYY-MM-DDTHH:MM:SSZ` strings. `bracket_before` and `bracket_after`
contain lower/upper requested times, the complete lower-reference object, and
the upper support-group ID. A PRESENT transition changes only the lower bound;
a complete ABSENT transition changes only the upper bound. Both stop kinds
leave bounds unchanged. Operational entries have empty `evidence`, null
`classification`, and a non-null `operational_stop_reason`; visual stops have
strictly reopened blocking evidence and a non-null `visual_stop_reason`. The
two stop-reason fields are mutually exclusive and both are null for a valid
transition.

Each evidence object has exactly these keys: `role` (`MIDPOINT` or
`ABSENCE_SUPPORT`), `target_id`, `probe_request_id`, `observation_id`,
`canonical_frame_id`, `acquisition_operation_id`, `classification_operation_id`,
`decode_session_id`, `decoded_frame_utc`, `decoded_pts`, `decoded_ordinal`, and
`classification`. Evidence is ordered midpoint-first and then by zero-based
support index. `support_group_id` and `support_indexes` are non-null only for
`ABSENT_TRANSITION`; support indexes must equal `range(support_count)`. Aliases,
duplicate observations/canonical frames, foreign ownership, missing provenance,
reordered entries, contradictory bounds, and an entry after a stop fail closed
as `terminal_input_incomplete`. The immutable finalization point is the
append-and-digest operation under the D1 mutation boundary; no entry can be
added or changed afterward. D2-1 strictly recomputes the complete payload and
rejects any missing, reordered, duplicated, foreign, or contradictory entry.
The same UTF-8 canonical JSON and lowercase SHA-256 rules used for
`d1_input_bracket_id` apply, and neither identity trusts a stored derived ID.

For an independent history-identity check, the following complete fixture has
digest `07494a60a99955734727fce60996e40abf7ad10b546b23f4c65b24ee0781bdb5`:

```json
{
  "baseline_identity": "baseline-demo",
  "d1_input_bracket_id": "d1-input-bracket-v1-4a1c216e94837b8077d6e3190a4554b31293dc58050abba7a0c11656f9f4e08d",
  "entries": [
    {
      "bracket_after": {
        "lower_reference": {
          "canonical_frame_id": "frame-mid-0",
          "kind": "PRESENT_PROBE",
          "observation_id": "obs-mid-0",
          "probe_request_id": "probe-mid-0",
          "requested_time_utc": "2026-07-20T03:00:02Z",
          "target_id": "midpoint-target-0"
        },
        "lower_requested_time_utc": "2026-07-20T03:00:02Z",
        "upper_requested_time_utc": "2026-07-20T03:00:04Z",
        "upper_support_group_id": "coarse-confirmation-demo"
      },
      "bracket_before": {
        "lower_reference": {
          "canonical_frame_id": "frame-present",
          "kind": "PRESENT_PROBE",
          "observation_id": "obs-present",
          "probe_request_id": "probe-present",
          "requested_time_utc": "2026-07-20T03:00:00Z",
          "target_id": "target-present"
        },
        "lower_requested_time_utc": "2026-07-20T03:00:00Z",
        "upper_requested_time_utc": "2026-07-20T03:00:04Z",
        "upper_support_group_id": "coarse-confirmation-demo"
      },
      "classification": "PRESENT",
      "entry_kind": "PRESENT_TRANSITION",
      "evidence": [
        {
          "acquisition_operation_id": "acq-mid-0",
          "canonical_frame_id": "frame-mid-0",
          "classification": "PRESENT",
          "classification_operation_id": "class-mid-0",
          "decode_session_id": "decode-mid-0",
          "decoded_frame_utc": "2026-07-20T03:00:02Z",
          "decoded_ordinal": 50,
          "decoded_pts": 2000,
          "observation_id": "obs-mid-0",
          "probe_request_id": "probe-mid-0",
          "role": "MIDPOINT",
          "target_id": "midpoint-target-0"
        }
      ],
      "iteration": 0,
      "midpoint_requested_time_utc": "2026-07-20T03:00:02Z",
      "operational_stop_reason": null,
      "support_group_id": null,
      "support_indexes": [],
      "target_id": "midpoint-target-0",
      "visual_stop_reason": null
    }
  ],
  "identity_schema": "phase7d-d1-history-v1",
  "investigation_id": "investigation-demo",
  "phase6_confirmation_id": "confirmation-demo",
  "plan_id": "coarse-plan-demo",
  "policy_identity": "policy-demo",
  "search_run_id": "search-run-demo",
  "source_revision": {
    "c2_bracket_id": "coarse-bracket-demo",
    "c2_manifest_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}
```

The identity dependency order is: source/baseline/plan/policy identities -> C2
bracket and C2 support/evidence identities -> `d1_input_bracket_id` -> D1
support-group IDs -> `history_digest` -> `narrowed_bracket_id` ->
`evidence_snapshot_digest` -> terminal `result_id`. The D1 input excludes D1
support IDs, history, and later digests; history consumes support IDs but not
its own digest; later identities consume only finalized history. No circular
dependency exists.

`narrowed_bracket_id` is
`narrowed-bracket-v1-<sha256>`. Its identity payload is compact canonical JSON
with the same serialization rules as `evidence_snapshot_digest` and contains,
in this exact semantic order: the identity tag; the complete source C2 bracket;
`d1_input_bracket_id`; `plan_id`; the complete policy identity; final requested
lower/upper bounds; lower-bound and ordered upper-support references; the
ordered history entries; `history_digest`; iteration count; achieved precision;
finite stop reason; and the D1 manifest digest. It contains no clock, path, URL,
diagnostic, or invocation value. D2 recomputes and compares the complete
history, both D1 identities, source-bracket hash, final bounds, stop semantics,
narrowed-bracket ID, evidence snapshot digest, and result ID;
it never infers missing history from a generic evidence list or trusts a supplied
ID.

### Authoritative input

The production service exposes one terminalization operation. Its internal
input is a closed union:

```text
TerminalizationCandidate =
  FoundCandidate
    narrowed_bracket: corrected NarrowedBracket
  | CoarseTerminalCandidate
    result: C2NoCandidate | C2VisualInconclusive
  | NarrowingVisualTerminalCandidate
    narrowing_result: corrected visually stopped NarrowingResult
```

`C2BracketReady`, `C2OperationalStop`, and `D1OperationalStop` are not
terminalization candidates. They remain nonterminal typed outcomes and are
returned without a terminal result. No transport accepts this union.
`RecordingSearchService` creates it from the existing C1/C2/D1 production path
and passes the same explicit
`RecordingSearchRunHandle` to D2. Publication requires the handle to be current,
open, bound to the same investigation/run/Phase 6 confirmation/baseline, and to
own the continuously held OS lock. A read-only exact-duplicate lookup after
terminalization may use the immutable handle identities or result ID, but it is
not a publication path and never recreates a handle.

Under the handle mutation mutex, D2 strictly reloads a schema-3 `RUNNING`
manifest and reconstructs every claim from its indexed children. It requires:

- exact investigation, run, Phase 6 confirmation, baseline observation,
  channel, source dimensions, ROI, C1 plan, policy snapshot, C2 bracket, and D1
  identity bindings;
- a source manifest digest matching the complete manifest and all indexed A2/B4
  records used by the candidate;
- strict Phase 6 schema-3 JPEG revalidation through the existing confirmation
  loader;
- strict confined reopen of every referenced acquisition operation, request,
  canonical frame/JPEG, classification operation, observation, and alias;
- membership in the committed ordered indexes and matching back-references;
- no arbitrary path, directory scan used to discover evidence, unpublished
  staging file, unindexed child, operational record, or foreign-run/baseline
  record; and
- stale-input rejection if any manifest, index, child, policy, plan, bracket,
  or evidence identity changed.

The complete coarse execution is accepted only as a claim to recheck. D2
rebuilds the plan and evidence snapshot from the repository and reruns the
existing C2 interpretation. A transient status can never override durable
facts.

### Closed terminal result vocabulary

Result kind and lifecycle state are separate concepts. The terminal-result union
is exactly `FOUND | NOT_FOUND | INCONCLUSIVE`. Administrative `FAILED` and
`INTERRUPTED` states have no terminal result. The existing lifecycle spelling
`INDETERMINATE` is retained as the state projected for an `INCONCLUSIVE`
result, avoiding a public state rename.

| Result kind | Lifecycle state | Evidence precondition | D1 bracket | Closes run | Phase 8 | Another attempt |
| --- | --- | --- | --- | --- | --- | --- |
| `FOUND` | `FOUND` | Supported PRESENT lower bound, policy-count distinct ordered ABSENT upper support, and precision met | Required | Yes | Eligible | New run |
| `NOT_FOUND` | `NOT_FOUND` | Complete policy grid through `E`, every target a distinct valid canonical `PRESENT`, no gap/alias/uncertainty, and no supported transition | Forbidden | Yes | Ineligible | New run |
| `INCONCLUSIVE` | `INDETERMINATE` | Strict durable visual inadequacy or contradictory visual evidence from a complete typed terminal candidate | Optional | Yes | Ineligible | New run |
| operational/corrupt/interrupted condition | unchanged `RUNNING`, or existing `FAILED`/`INTERRUPTED` administration | No visual terminal claim | Not applicable | Not by D2 | Ineligible | Retry active step or explicit new run after administration |

`NOT_FOUND` is therefore never produced from a `NarrowedBracket`. It is the D2
terminalization of the existing C2 `NO_CANDIDATE` path after D2 independently
proves a complete distinct canonical `PRESENT` grid. An isolated `ABSENT`, an
alias, an `INDETERMINATE` observation, or a missing target disqualifies
`NOT_FOUND`.

`INCONCLUSIVE` is allowed only for visual facts that were successfully decoded,
classified, persisted, and strictly reopened. Closed reasons are:

```text
insufficient_visual_evidence
nonmonotonic_visual_evidence
insufficient_distinct_visual_support
```

The first includes a persisted visual `INDETERMINATE`; the second requires
durable contradictory PRESENT/ABSENT ordering; the third requires otherwise
valid visual support that aliases or cannot supply the policy-count distinct
frames. Recording unavailability, replay/decode failure, classifier timeout or
runtime failure, stale input, manifest/artifact corruption, interruption, and
storage/capacity exhaustion are operational. They never become
`INCONCLUSIVE`. They leave the active run `RUNNING` for an explicit bounded
retry, or use the existing `FAILED`/`INTERRUPTED` administrative transition when
the caller ends the attempt.

A D1 finite-iteration stop is publishable as `FOUND` only when the interval is
no wider than `policy.binary_stop_resolution_seconds`. A wider
`MAXIMUM_ITERATIONS`, resource/capacity stop, or absent distinct midpoint is not
`INCONCLUSIVE`; it remains nonterminal and requires a corrected/repeated D1
step or a new run after explicit administration.

### FOUND interval semantics

The authoritative result is the closed requested-time interval
`[last_present_requested_time_utc, first_absent_requested_time_utc]`. It is not
an exact physical disappearance instant. The lower bound is either the Phase 6
baseline `PRESENT` observation or one indexed canonical recording-probe
`PRESENT` observation. The upper bound is the first requested target of exactly
`policy.absence_confirmation_frames` indexed canonical `ABSENT` observations.

Upper support must have distinct target, request, observation, and canonical
frame IDs; no alias may occupy a support position. All support frames share one
decode session and have strictly increasing requested UTC, decoded UTC, PTS,
and ordinal values. Requested targets follow the persisted cadence and may
differ from decoded timestamps; decoded values never replace requested interval
bounds. A recording-probe lower bound retains its own operation, request,
frame, observation, session, decoded UTC, PTS, and ordinal references. The
Phase 6 baseline has none of that invented recording provenance.

Every ordered D1 midpoint and support reference must reproduce deterministic
floor-midpoint planning from the preceding interval. The interval stays within
the source C2 bracket and policy window, never widens, and has positive whole-
second width no greater than the persisted stopping resolution. Any later
PRESENT at or after the upper bound, ABSENT at or before the lower bound,
nonmonotonic narrowing evidence, gap, or `INDETERMINATE` evidence rejects
`FOUND`.

For Phase 8 only, `review_anchor_utc` is derived as the upper requested bound.
It is a clip/display anchor, not an event estimate. It is excluded from result
identity and recomputed on strict reopen. UI text must expose the interval and
uncertainty, never relabel the anchor as a disappearance time.

### Schema 4 manifest and result records

D2 introduces `RecordingSearchManifestV4`. Schema 3 cannot represent terminal
results because its closed state union is `RUNNING | FAILED | INTERRUPTED` and
it deliberately rejects result and handoff fields. V4 is created only by one
atomic schema-3 `RUNNING` to schema-4 terminal replacement. It preserves every
schema-3 value and ordered index, changes lifecycle fields, and adds exactly one
strict terminal result:

```text
RecordingSearchManifestV4
  schema_version: 4
  investigation_id, search_run_id
  state: FOUND | NOT_FOUND | INDETERMINATE
  created_at_utc, started_at_utc, completed_at_utc
  confirmation                         # exact schema-3 value
  policy                               # exact schema-3 value
  acquisition_operation_ids            # exact ordered schema-3 index
  probe_request_ids                     # exact ordered schema-3 index
  canonical_frame_ids                   # exact ordered schema-3 index
  baseline_observation_id               # exact schema-3 value
  classification_operation_ids          # exact ordered schema-3 index
  canonical_observation_ids             # exact ordered schema-3 index
  target_alias_ids                      # exact ordered schema-3 index
  failure_reason: null
  terminal_result: FoundResult | NotFoundResult | InconclusiveResult
```

All result variants use strict fields, canonical whole-second UTC for requested
and publication times, exactly six UTC fractional digits for copied decoded
times, lowercase 64-character SHA-256 values, and canonically ordered tuples:

```text
TerminalResultCommon
  result_schema_version: 1
  result_id
  result_kind
  investigation_id, search_run_id
  phase6_confirmation_id
  baseline_observation_id
  plan_id
  policy_identity
  source_manifest_digest
  evidence_snapshot_digest
  terminal_reason
  limitations                         # ordered unique closed codes
  published_at_utc                    # injected authoritative clock

EvidenceReference
  target_id
  requested_time_utc
  acquisition_operation_id | null    # null only for Phase 6 baseline
  probe_request_id | null
  canonical_frame_id | null
  classification_operation_id | null
  observation_id
  alias_id | null
  decode_session_id | null
  decoded_frame_utc | null
  decoded_pts | null
  decoded_ordinal | null
  is_phase6_baseline
```

`FoundResult` adds `source_bracket_id`, `narrowed_bracket_id`, lower and upper
requested bounds, `achieved_precision_seconds`, one lower-bound reference,
policy-count ordered upper-support references, and every ordered narrowing
target/evidence reference. `NotFoundResult` adds the search window and exactly
one ordered reference for every coarse plan target. `InconclusiveResult` adds
`source_stage: COARSE | NARROWING` and only the minimal ordered references that
prove its closed visual limitation. An alias reference may appear only in an
`INCONCLUSIVE` limitation; it is forbidden in `FOUND` support and `NOT_FOUND`
coverage.

`evidence_snapshot_digest` is required on every persisted terminal result and
must equal the recomputed digest for that result's exact ordered references.
The D2 validator compares it before result-ID construction; an operational or
nonterminal stop has no terminal result and therefore has no digest.

`terminal_reason` is exactly `candidate_interval_found` for `FOUND`,
`no_transition_in_window` for `NOT_FOUND`, or the applicable one of the three
closed `INCONCLUSIVE` reasons above. `limitations` is an ordered subset of this
closed vocabulary, in the order shown:

```text
requested_time_interval_not_exact_event
configured_samples_only
decoded_time_differs_from_requested
camera_continuity_unverified
policy_pending_phase7e_validation
insufficient_visual_evidence
nonmonotonic_visual_evidence
insufficient_distinct_visual_support
```

`FOUND` always includes `requested_time_interval_not_exact_event`;
`NOT_FOUND` always includes `configured_samples_only`; all current policy-v1
results include `policy_pending_phase7e_validation`. The decoded-time and camera
limitations are included only when the reopened facts require them. An
`INCONCLUSIVE` result includes exactly its matching visual limitation plus any
applicable general limitations. Phase 6 free-form warning text is not copied
into this closed result list; it remains in the preserved confirmation/status
field. Duplicate, unknown, missing-required, or noncanonical limitations reject
the result.

The record stores no path. All references resolve through repository-confined
indexes. `completed_at_utc` equals `terminal_result.published_at_utc`, state
maps exactly to result kind, and `failure_reason` is always null. V4 accepts no
additional child evidence after publication.

### Deterministic result identity

`result_id` is
`recording-search-result-v1-<sha256>`. The digest input is compact UTF-8 JSON
with lexicographically sorted object keys and no insignificant whitespace.
Timestamps use the canonical persisted string form. The identity payload is:

```text
identity_schema: recording-search-terminal-result-v1
result_kind
investigation_id, search_run_id, phase6_confirmation_id
baseline_observation_id
plan_id, policy_identity
source_manifest_digest
evidence_snapshot_digest
terminal_reason, limitations
variant payload:
  FOUND: source/narrowed bracket IDs, interval, precision, lower reference,
         ordered upper support, ordered narrowing references
  NOT_FOUND: search window and ordered complete coarse references
  INCONCLUSIVE: source stage, closed visual reason, ordered proving references
```

`policy_identity` is SHA-256 over the complete canonical schema-3 policy JSON,
not merely `policy_version`. Evidence collections use semantic order:
coarse-plan order, then D1 iteration order, and support requested-time order;
each validator rejects duplicates or a noncanonical order rather than sorting
untrusted input. Publication wall-clock time, derived `review_anchor_utc`, and
Phase 8 request state are excluded. Limitations are material result semantics,
so their canonical closed order is included. Identical ownership and evidence
produce the same ID; changing a run, baseline, plan, policy, bracket, interval,
reference, visual reason, or source manifest revision changes it.

`source_manifest_digest` is SHA-256 over compact canonical JSON containing the
exact schema-3 `RUNNING` predecessor plus every indexed acquisition operation,
request, canonical frame metadata, baseline observation, classification
operation, recording observation, and alias in lexicographic ID order. JPEG
bytes are bound by the indexed digest/size fields and are rehashed separately.
Strict V4 reopen reconstructs that predecessor from the preserved V4 fields
(with schema version 3, state `RUNNING`, null completion/failure, and no terminal
result) and recomputes the same digest. The existing D1 digest is also
recomputed according to its D1 algorithm for input compatibility; it does not
replace this complete D2 source digest.

### Lifecycle and atomic publication

The closed transition table is:

| Source | Request | Result |
| --- | --- | --- |
| schema-3 `RUNNING` | valid found candidate | schema-4 `FOUND` |
| schema-3 `RUNNING` | valid complete no-candidate coarse evidence | schema-4 `NOT_FOUND` |
| schema-3 `RUNNING` | valid visual terminal candidate | schema-4 `INDETERMINATE` plus `INCONCLUSIVE` result |
| schema-3 `RUNNING` | interruption wins mutex first | schema-3 `INTERRUPTED`; D2 rejects |
| schema-3 `RUNNING` | existing administrative failure path | schema-3 `FAILED`; no result |
| terminal schema 4 | exact duplicate | return existing result, no write |
| terminal schema 4 | different proposal | immutable conflict, no write |
| terminal schema 4 | acquisition/classification | reject terminal state |
| stale/closed foreign handle | any publication | reject authority, no write |
| corrupt/unsupported manifest | any transition | fail closed, no write |

Publication uses the existing
`LocalInvestigationLock(repository.lock_path(investigation_id))` as the
serialization boundary. `lock_path` is the existing stable, per-investigation
OS-backed lock; `run_path(investigation_id, search_run_id)` separately resolves
the exact repository run after strict caller-owned ID validation. No new
per-run lock, registry, lease, takeover, or repository is introduced. Two
separate lock instances were probed on the supported Windows path: the first
acquires and the second is denied until release, so the existing OS lock
serializes both in-process and cross-process terminal entry and is not bypassed
by a closed-handle duplicate branch.

#### Global synchronization inventory and D2-3 migration

The current D1 service contains two guard-first paths: `start` enters
`RecordingSearchService._guard` before trying `LocalInvestigationLock`, and
`status` does the same when no active handle is present. D2-3 must migrate both
paths before D2 publication is implemented; the design does not claim that the
current D1 runtime already has the final order. No path may wait for an OS lock
while holding `_guard` after that migration. A caller that looked up an active
handle before waiting must revalidate the map, run ID, repository identity, and
lock ownership after it acquires the lock.

The normative table is:

| Path | Validated locator | OS lock | `_guard` | Active mutex | Normative order, revalidation, and mutation | Release/cleanup |
| --- | --- | --- | --- | --- | --- | --- |
| `start` (migrates current guard-first code) | Validate investigation root/lock path before waiting; validate the new exact `run_path` after generating its run ID | Required before checking latest nonterminal state | Acquire after OS lock; recheck whether another active run won | No active mutex for a new run | `investigation_id/root -> OS -> guard`; if an active run appeared, return it without mutation; otherwise create the new run while OS lock is held | Release guard, then owned OS lock on every exit; remove only invocation-owned staging |
| `status`/interruption (migrates current guard-first code) | Validate exact investigation/run path before waiting | Required | Acquire after OS lock, recheck active map, then release before blocking manifest I/O; reacquire before any transition | Only if a live schema-3 transition is explicitly owned | Live owner means return `RUNNING` and do not interrupt; only no owner may transition a still-running manifest to `INTERRUPTED` | Release guard before OS lock; no state change on lock/read failure |
| C1/C2/D1 active mutation | Handle identities and repository/run path already validated by the active handle | Already held by handle; never reacquired under guard | Acquire after the already-held OS lock | Required for mutation | `OS-held -> guard -> active mutex`; revalidate handle closed/run ID/lock ownership before each mutation | Release mutex, then guard; handle retains OS lock until explicit close |
| D2 schema-3 publication | Validate exact run path and candidate identities before waiting | Required | Acquire after OS lock and snapshot active map | Required for live `RUNNING` mutation | `run path -> OS -> guard -> mutation`; revalidate exact active handle and manifest under the same lock before replacement | Strict readback, release mutation, guard, then OS; preserve V4 after commit |
| D2 schema-4 duplicate/conflict reopen | Validate exact run path and proposed identity before waiting | Required | Briefly acquire to snapshot active state; no active mutex | Forbidden/not needed | `run path -> OS -> guard`; release guard before strict readback while OS remains held; exact ID reuses, different ID conflicts | Read-only; release guard then OS, never write or reacquire evidence |
| Post-replacement strict readback | Same exact run path selected by the committed invocation | Still held by publisher | Not required after active snapshot | Held only until the readback boundary for schema 3 | Read V4 under OS; no lifecycle mutation or second replacement | Release mutation, guard, and OS in ownership-safe reverse order |
| Handle closure/removal | Active handle identity already validated | Already held | Acquire guard, then active mutex if a mutation may be in flight | Required before removing an active entry | Revalidate map entry; remove it only after mutation quiesces; no waiter depends on the removed mutex | Release mutation/guard, then that handle's OS lock; never delete a foreign resource |
| Restart retry / closed-handle duplicate | Validate exact persisted run path and IDs | Required | Acquire after OS lock only to inspect active map | None for schema 4; required for a live schema-3 owner | Schema 4 is read-only reuse/conflict; schema 3 without exact active authority is rejected | Release guard then OS on every parser/read failure |
| Acquisition of any primitive fails | Validate locator before first acquisition | Bounded acquisition; no guard held while waiting | Only after OS success | Only after guard and active authority success | No mutation or publication when any acquisition fails | Release every primitive actually owned, in reverse order |
| Manifest read/replacement/readback exception | Exact run path and ownership already established | Keep OS lock through the decision boundary | No guard held during blocking I/O; reacquire only to update in-memory ownership | Keep only while required for schema-3 mutation | Fail closed; replacement remains the sole commit point | Remove only owned staging; never roll back or rewrite committed V4; release owned locks/mutexes |

`LocalInvestigationLock.release()` does not wait for another owner, so cleanup
may release it after `_guard`; all potentially blocking acquisition waits occur
before `_guard` is acquired. The active mutation mutex is never destroyed while
held: handle removal first quiesces the mutex under the guard, then removes the
map entry and releases the OS lock. A stale caller rechecks authority after
every wait and cannot use a removed mutex to mutate.

The canonical order for every publication, duplicate lookup, status, and
interruption path is **validate IDs and exact `run_path` -> acquire the existing
OS lock -> enter the service `_guard` -> enter the active handle's mutation
mutex only for a schema-3 `RUNNING` mutation -> release `_guard` -> release the
OS lock**. No path waits for the OS lock while holding `_guard`, and no cleanup
waits on a mutation mutex after its active owner has been removed.

Status and interruption use the same order without a takeover protocol. A
non-blocking `_guard` snapshot may identify a currently active owner, but that
snapshot is released before any OS-lock acquisition and is not a nested
guard-to-OS acquisition edge. The authoritative sequence is then acquire the
OS lock, enter `_guard`, and recheck the active map. If a live owner is
present, return its `RUNNING` projection and make no interruption change. Only
while the OS lock is held and no active owner exists may the operation strictly
read the manifest and mark a still-running record `INTERRUPTED`; release
`_guard` before releasing the OS lock. Publication, status, interruption, and
cleanup therefore cannot hold the service guard while waiting on one another or
observe a gap in which a winner has removed authority but has not yet committed
terminal state.

The exact entry sequence is:

1. Validate request syntax and the caller-owned investigation/run locator; never
   scan paths or trust terminal contents to choose a run. Acquire the bounded
   existing OS lock. A lock timeout returns `publication_in_progress` and
   releases any partial resources without a result.
2. Enter `_guard` only after the OS lock is held, snapshot the active-handle
   map, and release `_guard` before blocking manifest I/O. Read the authoritative
   manifest for the exact `run_path` while the OS lock remains held. For schema 4,
   strict readback and deterministic ID comparison are read-only: an exact ID
   returns the existing result with no write; a different ID returns
   `terminal_result_conflict`; malformed, foreign, or unreadable state fails
   closed as `corrupt_persisted_evidence` or `publication_readback_failed`.
   This branch never requires an active handle.
3. Reacquire `_guard` after the read and recheck the active map and manifest
   version. For schema-3 `RUNNING`, require the exact active handle, matching
   investigation/run/repository identity, and its held OS lock. Acquire that
   handle's existing mutation mutex under `_guard`, then snapshot complete
   indexes and digest and bind the candidate to that snapshot. Schema 1/2,
   schema-3 `FAILED`/`INTERRUPTED`, missing, foreign, or unsupported state is
   rejected without mutation. Release `_guard` only after the active branch
   has acquired its mutation mutex; the schema-4 branch releases it before
   returning its read-only result.
4. Release the mutation mutex only for pure validation, deterministic interpretation,
   JPEG decoding, and hashing of immutable referenced bytes. No write or final
   decision occurs outside the mutex.
5. Reacquire the same mutation mutex. Revalidate handle/lock authority, lifecycle,
   investigation/run/baseline, complete plan and policy identities, current
   manifest/index digest, D1/C2 identities, every referenced child, and digest/
   size of the Phase 6 and referenced probe JPEG bytes. Any change makes the
   candidate stale; D2 does not retry automatically.
6. Build the complete V4 successor from the current V3 value, validate the full
   model and tree, write an invocation-owned same-directory temporary manifest,
   flush/fsync it, and atomically replace `manifest.json`. That replacement is
   the sole commit point for lifecycle and result together.
7. Strictly read back V4 while the mutation mutex is held. Only then close/remove
   the active handle, release `_guard`, and release the OS lock. A failure before replacement admits
   nothing; a failure after replacement treats strict V4 as authoritative.

No concurrent child admission is lost: if it commits while pure validation is
outside the mutex, the manifest digest changes and D2 rejects stale input. A
worker whose timeout/cancellation authority was revoked cannot call this
publication boundary. There is no unbounded retry. A caller may recompute once
from a newly captured authoritative snapshot through an explicit new call.

The interruption race is decided by the same mutex. If interruption or authority
loss commits first, D2 cannot publish. If V4 replacement commits first,
interruption observes a terminal run and cannot rewrite it. No separate state
file can diverge from the terminal result.

### Post-replacement readback and cleanup

The successful atomic replacement of `manifest.json` is the irreversible commit
point. From that instant schema 4 is authoritative even if the caller cannot
read it back. D2 must never roll back, downgrade, rewrite schema 3, replace the
file a second time, or retry publication automatically after replacement. The
caller-visible post-replacement outcomes are closed safe categories:

| Readback condition | Caller result | Durable state | Automatic action |
| --- | --- | --- | --- |
| Strict V4 readback succeeds | Return the committed terminal result | V4 remains authoritative | Close handle and release OS lock |
| On-disk I/O or read failure | `publication_readback_failed` | V4 remains authoritative but status/reopen is unavailable until a later read succeeds | No rewrite; close/release in `finally` |
| Parsed V4 is malformed, digest-invalid, or has a foreign/unsafe child | `corrupt_persisted_evidence` | V4 remains authoritative and is treated as corrupt | No repair, downgrade, or rewrite; close/release in `finally` |
| Deterministic validator or invariant failure after replacement | `publication_invariant_failure` | V4 remains authoritative | No rewrite; close/release in `finally` |
| Unexpected cleanup/close failure | `unexpected_error` for the caller | V4 remains authoritative | Attempt every remaining cleanup action; never mutate V4 |

Any handle, mutation mutex, and OS lock acquired by the invocation are released
on every row, including parser, I/O, validation, and unexpected-exception paths.
Cleanup is idempotent:
the service records whether the invocation still owns each resource, closes only
owned handles, and never follows or deletes a foreign manifest or staging path.
If a failure occurs before replacement, the prior valid manifest remains
authoritative and only invocation-owned temporary staging is removed. If it
occurs after replacement, only invocation-owned temporary state is removed and
the committed V4 file is preserved.

Status and reopen subsequently use the strict V4 loader. A read failure returns
the fixed safe readback category; malformed or digest-invalid V4 returns the
fixed safe corruption category; neither operation changes lifecycle state or
attempts recovery. A retry after either category is read-only strict terminal
reopen. It may return the committed result once the same V4 becomes readable and
valid, or continue to fail closed; it never republishes evidence or creates a
second V4 successor. Raw parser errors, paths, handles, claims, and native I/O
details are diagnostics only and never cross the public boundary or enter an
identity.

### Idempotency, conflict, and strict reopen

An exact existing V4 result ID returns the strictly reopened result without
rewriting `manifest.json`, timestamps, or children. This includes a duplicate
call racing publication and a read-only retry after restart. A different result
ID for the same run, a different source revision, stale D1 bracket, or a second
result kind is `terminal_result_conflict`. Terminal evidence is never replaced
or adopted by another run.

The duplicate branch is evaluated after the exact repository/run locator and
existing OS lock are acquired, but before any active-handle mutation branch; it
is scoped to that exact identity. Consequently:

1. Two in-process callers serialize at the existing mutation boundary. The
   winner replaces schema 3 with V4. The loser reloads V4, strictly reopens it,
   and returns `reused` when its deterministic proposed ID matches, or
   `terminal_result_conflict` when it differs.
2. If the winner closes the loser's handle, the loser still takes the read-only
   V4 branch; it is not reported as an incorrect inactive/in-progress result.
   The loser does not reacquire evidence, require an active schema-3 handle, or
   republish.
3. A retry after process restart follows the same exact repository/run lookup:
   valid identical V4 is reused, a different proposal conflicts, and a
   malformed or unreadable V4 fails closed as terminal corruption/readback.
4. A schema-3 `RUNNING` state is the only branch that requires the active handle
   and continuously held OS lock. A schema-3 `FAILED`/`INTERRUPTED`, schema 1/2,
   missing state, foreign run, or unsupported version is rejected without a
   terminal write.
5. The duplicate lookup never scans globally, searches by path, trusts an
   unbound result ID, or creates a second lock. It reads only the authoritative
   terminal file selected by the exact run identity and uses the strict V4
   validator before returning any result.

The remaining races have the same deterministic outcomes: a loser waiting
before the winner commits acquires the OS lock afterward and reopens V4 as
`reused` or `terminal_result_conflict`; two cross-process callers serialize by
that same lock; a corrupt V4 fails closed without repair; a stale or absent
handle against schema-3 `RUNNING` is `inactive_authority`; and a winner that
commits V4 before closing its handle cannot be interrupted or rewritten by a
later status/cleanup call. A winner that loses before replacement leaves schema
3 authoritative and removes only its owned staging, so a subsequent valid
owner can continue under the same lock.

The required deterministic race traces are:

| Trace | Locked observation and authority check | Result and cleanup |
| --- | --- | --- |
| `start` racing `status` | Both validate the locator, wait for OS outside `_guard`, then recheck the active map after acquiring OS | The OS winner proceeds; the loser returns the winner's active state or safely reads the persisted state; release guard then OS, with no AB-BA cycle |
| `start` racing D2 publication | `start` cannot hold guard while waiting; D2 holds OS through terminal inspection/commit | D2 commits V4 or leaves prior state; `start` observes the authoritative locked state and may begin only a separate valid run; no terminal rewrite |
| `status` racing D2 publication | Both serialize on OS; status rechecks active ownership after acquiring OS | V4 is reported after publication, or status interrupts only an unowned still-running manifest; all locks release |
| Interruption racing publication | Both use OS first; schema-3 mutation additionally uses guard/mutation mutex | First commit wins: V4 prevents interruption, or interruption makes publication stale; no second writer or leaked lock |
| Two in-process publications | Separate callers serialize on the same OS lock; active schema-3 winner revalidates handle under guard/mutex | Identical proposal returns `reused`; different proposal returns `terminal_result_conflict`; loser performs no write |
| Two cross-process publications | Separate `LocalInvestigationLock` instances serialize the same lock file | Same reuse/conflict outcomes; each process releases only its owned OS handle |
| Identical loser waiting before winner commits | Loser acquires OS only after winner's replacement and strict readback | Strict V4 reopen returns the existing result with no mutation |
| Identical retry after active-handle removal | Exact run path and OS lock are reacquired; no active handle is required for schema 4 | Strict V4 reuse; no evidence reacquisition or new mutex dependency |
| Retry after process restart | Exact persisted run path is validated and locked before any read | Valid V4 reuses, conflict differs, corrupt/read failure fails closed; no migration or rewrite |
| Failure acquiring any primitive | The failing acquisition occurs before the next primitive is requested | No mutation; release only already-owned resources in reverse order |
| Manifest read, replacement, or strict-readback exception | OS lock remains held through the decision boundary; guard/mutex are reacquired only where ownership bookkeeping requires | Before replacement, prior state remains and owned staging is removed; after replacement, V4 remains authoritative and is never rolled back |
| Handle removed after another caller observed it | The waiting caller rechecks active map, run ID, closed flag, repository identity, and lock ownership after OS/guard acquisition | Stale caller returns `inactive_authority` and cannot mutate or use the removed mutex; owner cleanup completes normally |

D2-3 implements these traces with deterministic barriers/events and bounded
thread/process joins, never sleeps. Each test asserts that no guard is held
while waiting for OS lock, authority is revalidated after every wait, only one
writer can replace the manifest, duplicate terminal reopen is read-only, and
all owned mutexes/OS handles are released on success and injected exception
paths.

Strict parser dispatch is exact: schema 1 uses the A1 parser, schema 2 the A2
parser, schema 3 the B2 parser, and schema 4 only the V4 parser. Unknown/newer
schemas, fallthrough, coercion, and downgrade are rejected. V4 reopen:

1. revalidates the full inherited schema-3 tree, current Phase 6 confirmation
   and JPEG integrity, and rejects any extra post-terminal child;
2. recomputes policy, plan, source C2 bracket, corrected D1 bracket, terminal
   result, and result IDs from the indexed records;
3. validates interval/search-window ordering, precision, result/state mapping,
   publication time, closed reason/limitations, and canonical collection order;
4. validates every operation, request, frame/JPEG, classification,
   observation, alias, and index relationship, including session and decoded
   UTC/PTS/ordinal rules; and
5. fails closed for a missing, corrupt, foreign, contradictory, unindexed, or
   path-unsafe reference. It never repairs, downgrades, or mutates on read.

Schema-1 and schema-2 manifests remain readable by their existing operations
and cannot be finalized by D2. A valid schema-3 `RUNNING` manifest may be
atomically finalized. Schema-3 `FAILED` or `INTERRUPTED` remains readable and
cannot be upgraded. Merely reading any version performs no migration.

| Schema | Strict read/status | Child writes | D2 finalization |
| --- | --- | --- | --- |
| 1 | Existing exact A1 behavior | Existing promotion path only while active | Rejected; no inferred A2/B evidence |
| 2 | Existing exact A2 behavior | Existing A2/B3 promotion paths only while active | Rejected; schema 3 evidence required |
| 3 `RUNNING` | Existing strict B2 tree | Existing A2/B4 append paths while active | Allowed as one atomic V4 successor |
| 3 `FAILED`/`INTERRUPTED` | Read-only status | Forbidden | Rejected; explicit new run |
| 4 terminal | Strict V4 read-only | Forbidden, except separate no-overwrite handoff request | Exact duplicate read only; conflict otherwise |
| unsupported newer | Rejected | Rejected | Rejected |

### Public status projection

No new HTTP route is required. The existing status route keeps all currently
returned common lifecycle, confirmation, policy, and A2 index fields for
schemas 1-3. Schema 4 projects those same compatible fields plus only:

```text
result:
  result_id
  kind: FOUND | NOT_FOUND | INCONCLUSIVE
  terminal_reason
  interval: {lower_requested_time_utc, upper_requested_time_utc} | null
  achieved_precision_seconds | null
  limitations
  review_anchor_utc | null
phase8_handoff_status: PENDING | READY | NOT_APPLICABLE
```

`PENDING` means a valid `FOUND` result exists but no valid handoff request is
present; `READY` means the separate request strictly reopens; other result kinds
are `NOT_APPLICABLE`. Existing raw observations, comparisons, operation records,
alias details, paths, decoded timestamps, PTS, ordinals, and private evidence
remain hidden. The current policy/confirmation fields are preserved only for
response compatibility; D2 adds no broader policy or evidence exposure. A
corrupt manifest/request returns the existing fixed safe error and no partial
result. Schemas 1-3 have no `result` and project
`phase8_handoff_status: NOT_APPLICABLE`; schema-3 interruption remains an
administrative state.

### Phase 8 handoff request

Only a strictly reopened schema-4 `FOUND` result is eligible. After the V4 commit,
Phase 7 may create one separate immutable `phase8-request.json` containing a
deterministic `handoff_request_id`, result ID, investigation/run, channel,
source timezone, authoritative interval, non-authoritative review anchor,
nominal clip window, and the minimal result/observation IDs Phase 8 must reopen.
The request contains no path or copied private evidence. Its ID uses canonical
JSON and SHA-256 over those immutable fields.

```text
Phase8HandoffRequestV1
  schema_version: 1
  handoff_request_id: phase8-handoff-v1-<sha256>
  terminal_result_id
  investigation_id
  search_run_id
  channel_id
  source_timezone
  lower_bound_requested_time_utc
  upper_bound_requested_time_utc
  review_anchor_utc
  nominal_review_start_utc
  nominal_review_end_utc
  lower_bound_observation_id
  upper_support_observation_ids       # canonical policy order
  phase6_confirmation_id
  baseline_observation_id
  created_at_utc
```

The handoff digest excludes `created_at_utc` and binds every other field in
compact canonical JSON. `created_at_utc` comes from the injected clock and an
exact duplicate preserves the original value. The nominal window is clipped to
the terminal result's validated search window; invalid or empty clipping rejects
the request without affecting the result.

The MVP clip-window policy is owned by the Phase 8 request contract: start ten
seconds before and end thirty seconds after `review_anchor_utc`, clipped only by
the validated recording-search window. Phase 8 must independently resolve
recording coverage and strictly reopen the terminal result, Phase 6 JPEG, and
referenced evidence. It may read but never mutate them. Missing historical
coverage yields a Phase 8 failure, not a changed Phase 7 result. A changed or
corrupt Phase 6 JPEG/result rejects handoff.

Request creation is a post-commit, no-overwrite step. Exact retry reuses the
same valid file; conflict fails closed. Write failure leaves V4 `FOUND`
authoritative and status `PENDING`; an explicit handoff retry may run without
rerunning search. The request is a child of `result_id`, but Phase 8 review media
and their future identities/confinement remain Phase 8 work. `NOT_FOUND` and
`INCONCLUSIVE` cannot create a request.

### Deterministic failure matrix

| Case | Decision and mutation | Lifecycle/status | Retry or audit category |
| --- | --- | --- | --- |
| Valid narrowed FOUND evidence | Accept; one V4 replacement | `FOUND` | `candidate_interval_found` |
| Same result already published | Return existing; no write | unchanged terminal | `reused` |
| Same result with winner-closed loser handle | Read-only strict V4 reopen; no write | unchanged terminal | `reused` |
| Conflicting result published | Reject; no write | unchanged terminal | `terminal_result_conflict`; new run for another search |
| Stale bracket/source digest | Reject; no write | `RUNNING` | `stale_terminal_input`; recompute explicitly |
| Manifest changes after snapshot | Reject; no write | current state | `stale_terminal_input` |
| Active handle loses authority | Reject; no write | current/administrative | `inactive_run_handle` |
| User interruption before commit | Reject; no result | `INTERRUPTED` if interruption commits | new run |
| Interruption races publication | Mutex winner decides | either V4 terminal or schema-3 `INTERRUPTED` | deterministic reopen |
| Phase 6 JPEG missing/corrupt | Reject; no write | `RUNNING` or existing admin state | `baseline_corrupt` |
| Probe artifact missing/corrupt | Reject; no write | `RUNNING` | `authoritative_evidence_invalid` |
| Alias presented as distinct support | Reject FOUND/NOT_FOUND | eligible visual case may be `INCONCLUSIVE`; otherwise `RUNNING` | `insufficient_distinct_visual_support` |
| Missing PRESENT lower bound | No visual transition; do not emit visual `INCONCLUSIVE` | `RUNNING` or operational stop | `incomplete_evidence`; bounded retry |
| Invalid decode session/order | Reject; no write | `RUNNING` | `authoritative_evidence_invalid` |
| Interval wider than resolution | Reject; no write | `RUNNING` | `terminal_precision_not_met` |
| Unsupported result kind | Reject; no write | unchanged | `invalid_terminal_result` |
| Incomplete coarse/narrowing evidence | Reject; no write | `RUNNING` | `terminal_input_incomplete` |
| C2/D1 typed operational stop | No terminal result; no visual candidate | `RUNNING` or explicit existing `FAILED` | closed operational reason; bounded retry/new run |
| Unknown C2/D1 status/reason or contradictory fields | Adapter fails closed; no digest or terminal result | `RUNNING` | `adapter_unknown_result`; versioned adapter update |
| Operational failure/timeout/capacity | No terminal result | `RUNNING` or explicit existing `FAILED` | bounded retry or new run |
| Persisted visual INDETERMINATE | Accept only with strict typed evidence | state `INDETERMINATE`, kind `INCONCLUSIVE` | new run |
| Recording gap | No terminal result | `RUNNING` or explicit existing `FAILED` | retry/new run; never NOT_FOUND |
| Complete distinct coarse grid, all PRESENT | Accept NOT_FOUND | `NOT_FOUND` | `no_transition_in_window`; new run for another attempt |
| No qualifying transition but alias/isolated ABSENT | Reject NOT_FOUND; accept only proven visual limitation | `INDETERMINATE`/`INCONCLUSIVE` or `RUNNING` | closed limitation or retry |
| Nonmonotonic durable visual evidence | Accept INCONCLUSIVE only | `INDETERMINATE` | `nonmonotonic_visual_evidence`; new run |
| Schema 1/2 reopen | Read existing shape; no D2 write | existing state | schema 3 required |
| Schema-3 RUNNING reopen | Read; eligible for D2 | `RUNNING` | active handle required |
| Schema-3 FAILED/INTERRUPTED reopen | Read; not finalizable | administrative terminal | new run |
| Unsupported newer schema | Reject; no mutation | safe corrupt response | `unsupported_manifest_schema` |
| V4 has post-terminal child | Reject reopen; no repair | no public result | `search_manifest_corrupt` |
| V4 replacement then readback I/O failure | Preserve V4; release resources; no rewrite | terminal but unreadable | `publication_readback_failed` |
| V4 replacement then malformed/digest-invalid readback | Preserve V4; release resources; no repair | terminal corrupt | `corrupt_persisted_evidence` |
| V4 replacement then validator/invariant failure | Preserve V4; release resources; no rewrite | terminal authoritative | `publication_invariant_failure` |
| Phase 8 request for ineligible result | Reject; no file | terminal result unchanged | `phase8_not_applicable` |
| `start`/`status` waits for OS lock | Guard is released before bounded lock acquisition; active map is rechecked after acquisition | Existing active owner is returned; stale unowned RUNNING may be interrupted only while OS lock is held | `already_running`, `process_lock_released`, or safe readback failure |
| Any synchronization acquisition failure | No state or evidence mutation | Existing lifecycle remains authoritative | bounded `publication_in_progress`/safe operational category; release owned primitives |
| Guard-first legacy path discovered during D2-3 migration | Refactor before enabling D2 publication | No mixed lock order is permitted | deterministic deadlock-regression failure |

### Implementation blueprint and acceptance

The later D2 implementation should use `recording_search_d2_models.py` for the
closed result/V4/status records, `recording_search_d2_identity.py` for canonical
identities, `recording_search_d2_validator.py` for pure interpretation and
strict evidence validation, `recording_search_d2_repository.py` for V4 parser,
reopen, atomic replacement, and handoff request, and
`recording_search_d2_service.py` for handle/mutex orchestration. Extend the
existing repository parser, `RecordingSearchService`, and status projection;
do not create a parallel repository, executor, lifecycle, or public route.
Apply the narrow D1 handoff correction above first.

The implementation order is deliberately Luna-sized and each step leaves the
existing runtime usable:

| Step | Deliverable | Must not include |
| --- | --- | --- |
| D2-0 | Total C2/D1 adapter, closed reason conversion, unknown-reason fail-closed behavior, versioned `evidence_snapshot_digest`, and one discrimination test per status/reason/evidence branch | Persistence, lifecycle, or new media work |
| D2-1 | Lossless typed D1 history recorder at midpoint/PRESENT/ABSENT/support/stop transitions, complete source-C2 retention, exact `d1_input_bracket_id` and `history_digest` payloads/examples, separate D1 support-group identity, exact reconstruction and alias/order/foreign tests | Filesystem writes or service composition |
| D2-2 | Strict result/V4 models, parser/reopen, atomic successor publication, post-replacement readback categories, cleanup table, and compatibility tests | Status/UI or Phase 8 media |
| D2-3 | Migration of every guard-first path including `start`, unowned `status`, interruption, and cleanup; shared internal lock-order helpers only where needed; authority revalidation after waiting; existing-OS-lock publication entry and closed-handle duplicate branch; deterministic deadlock, duplicate/conflict/restart, exception-boundary, and lock-ownership tests | New lock/repository/executor, lease, takeover, or parallel lifecycle |
| D2-4 | Compatible status projection and safe readback/corruption categories for process-restart reopen | New HTTP route, Phase 8 handoff, or review extraction |
| D2-5 | Cross-boundary acceptance/failure matrix, deterministic-event coverage, and durable documentation synchronization | Real-NVR tuning or Phase 7E/8/9 work |

Acceptance tests must cover:

1. valid `FOUND`, exact interval boundaries, baseline and recording-probe lower
   bounds, policy-driven support count, precision, distinctness, session and
   UTC/PTS/ordinal order, nonmonotonic evidence, closed visual-versus-operational
   discrimination, the exact evidence-snapshot digest, and deterministic result
   identity;
2. valid `NOT_FOUND` only for the complete distinct all-PRESENT coarse grid,
   and valid `INCONCLUSIVE` only for the three closed visual limitations;
3. lossless D1 history reconstruction, source-C2/plan/policy identity, exact
   `d1_input_bracket_id` and `history_digest` fixtures, midpoint transition
   rules, support-group count/cadence, alias rejection,
   malformed/reordered/foreign history failure, and exact narrowed-bracket ID;
4. atomic schema-3 to schema-4 publication, failure-before-replacement no-op,
   strict post-replacement readback categories, cleanup on every exit, exact
   duplicate no-rewrite, conflict/corruption, Phase 6/probe artifact corruption,
   schemas 1-3 compatibility, newer-schema rejection, and no post-terminal
   evidence;
5. mutex snapshot, pure outside-mutex validation, in-mutex revalidation and
   replacement, no guard-held OS wait, canonical `start`/`status`/interruption/
   publication order, stale input, concurrent child admission, authority loss,
   interruption race, duplicate finalization with a closed loser handle,
   terminal mutation rejection, acquisition/read/replace/readback exceptions,
   and no handle/lock leak, all with deterministic events/barriers and bounded
   joins rather than sleeps;
6. compatible status, hidden raw evidence, truthful interval/uncertainty,
   eligible/ineligible Phase 8 request, handoff failure preserving `FOUND`, and
   idempotent request retry; and
7. no Phase 8 media, real-NVR work, human adjudication, identity/person tracking,
   ownership/theft inference, production-accuracy claim, or new transport.

## Phase 8 handoff

After the search manifest is durably `FOUND`, Phase 7 may create the separate
compact `phase8-request.json` defined by the D2 contract above. It contains:

```text
search_run_id
investigation_id
terminal result ID and deterministic handoff request ID
channel_id
last_present_observation_id and requested time
first_absent_observation_id and requested time
supporting observation/evidence references
review_anchor_utc: first-absent requested bound, explicitly non-authoritative
nominal_review_start_utc: anchor minus 10 seconds, clipped to the search window
nominal_review_end_utc: anchor plus 30 seconds, clipped to the search window
timing_precision_statuses and warnings
```

Phase 8 revalidates the Phase 6/7 facts, resolves recording coverage, and creates
review images and video. Phase 7 does not promise contiguous review coverage
and does not extract or persist Phase 8 media.

A handoff write failure creates no request and leaves the immutable schema-4
`FOUND` manifest unchanged. Status derives `PENDING` from that result until a
strict request exists. An explicit handoff retry creates or reuses the same
deterministic request; it does not rerun or mutate the search.

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
  JPEG/frame artifacts. Its narrow Phase 7B read-side extension can return one
  admitted request/frame plus the exact validated probe JPEG bytes and immutable
  frame metadata without changing existing callers. A2 emits no observation,
  classifier state, candidate, alias-observation, or Phase 8 result.
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
  mask-coverage rule, thresholds, and one strict successful Phase 7A-2
  request/frame pair.
- **Outputs:** schema-3 baseline/classification-operation/observation/alias
  persistence; every completed canonical probe has exactly one state, closed
  reason, and bounded classifier evidence.
- **Files:** production classifier protocol/adapter and composition, aligned-ROI
  comparator, observation models, schema-3 repository/service extension,
  focused tests, and deterministic doubles.
- **Exclude:** target selection, coarse orchestration, binary search, terminal
  search results, public transport, UI/CLI, and Phase 8.
- **Tests:** real predictor/comparator integration fixture; every IoU/NCC
  threshold boundary; baseline geometry; three states; timeout, unavailable,
  invalid-mask and zero-variance mapping; deterministic-double parity.
- **Complete/document:** the pure classification foundation, atomic schema-3
  persistence, bounded single-read byte admission, production classifier
  composition, handle-owned invocation authority, bounded execution,
  timeout/abandonment revocation, revalidation, and publication orchestration
  are implemented.
  No infrastructure, corrupt input, or uncertain comparison becomes `ABSENT`;
  probe records retain mandatory acquisition/frame provenance by immutable
  reference. See the
  [exact Phase 7B contract](object-presence-classification.md).

### Phase 7C: coarse sampling

Phase 7C-1 is the execution foundation. Phase 7C-2 consumes its ordered result
through a strict handle-bound snapshot of the existing A2/B4 records and returns
only an internal typed bracket or safe non-candidate outcome. Neither slice
publishes a terminal search state.

- **Inputs:** the active run handle, its persisted policy snapshot, confirmed
  baseline, and the existing A2/B4 service boundaries.
- **Plan:** starting at `S = policy.search_start_utc`, request whole-second UTC
  targets `S + interval`, `S + 2 * interval`, and so on while strictly before
  `E = policy.search_end_utc`; append `E` exactly once. A window shorter than
  one interval therefore has only `E`. The plan is strictly increasing,
  duplicate-free, inclusive of `E`, and bounded by
  `ceil((E - S) / interval)` targets and the policy maximum span.
- **Execution:** process targets in chronological order. The executor uses the
  existing Phase 7A-2 batch boundary to acquire a primary target and, when its
  in-window confirmation sequence fits, the derived
  `[t + i * cadence for i in range(absence_confirmation_frames)]`
  support frames in one bounded decoder session. It classifies the primary first
  through Phase 7B-4 and classifies that support batch only when the primary is
  `ABSENT`; a support batch is never inferred from requested timestamps. The
  executor never holds a second writer lock or performs its own media work;
  authority, bounded timeout, late-result, byte-integrity, and publication rules
  remain in A2/B4.
- **Durable progress:** no new manifest schema is introduced. A2 request records
  durably capture each attempted target, including safe failed-request reasons;
  successful B4 observations and aliases extend the existing immutable schema-3
  indexes. The Phase 6 schema-3 package has no separate confirmation identifier,
  so its canonical `investigation_id` is the `phase6_confirmation_id` binding.
  Each derived confirmation batch carries a canonical identity binding
  the investigation and run, Phase 6 confirmation and baseline, immutable plan,
  origin target, support count, and cadence. Repeating an active plan therefore reuses A2 requests and B4
  canonical duplicates. A process interruption leaves the run governed by the
  existing `INTERRUPTED` rule; a later run is an explicit new run, never an
  implicit resume or takeover.
- **Result:** a typed ordered sample result reports each target as successful or
  a fixed safe operational category (`RECORDING_UNAVAILABLE`, acquisition
  failure, timeout, classification failure, interruption, or unexpected
  failure). A successful result retains the B4 visual state only as evidence;
  it is not a disappearance outcome.
- **Files:** `recording_search_c1_planner.py`,
  `recording_search_c1_models.py`, `recording_search_c1_service.py`, and the
  active-run delegation in `recording_search_service.py`.
- **Tests:** boundary and exact-end targets, deterministic identity, ordering,
  A2/B4 delegation, per-target failure isolation, timeout handling, active
  handle interruption, and absence of 7C-2/7D terminal fields.
- **Exclude:** binary narrowing, terminal persistence, Phase 8, review media,
  and recovery/resume.

Phase 7C-2 requires one preceding canonical `PRESENT` target and exactly the
configured `absence_confirmation_frames` distinct canonical `ABSENT`
observations for requested targets `[t + i * cadence for i in
range(absence_confirmation_frames)]`. The requested times are not decoded timestamps: the support frames
must share one decode session and have strictly increasing decoded UTC, PTS,
and ordinal values. Aliases, decode gaps, operational failures, and
`INDETERMINATE` outcomes cannot support absence. The interpreter emits the first
supported `[last_present, first_absent_support_target]` bracket, a typed
`C2NoCandidate`, a strictly visual `C2VisualInconclusive`, or a typed
`C2OperationalStop` (including `incomplete_evidence`). `C2OperationalStop`
carries no visual evidence or digest; a recording gap, missing
support target, decode failure, timeout, interruption, or corrupt record is
therefore never visual `INCONCLUSIVE`. The interpreter performs no acquisition,
classification, filesystem write, manifest mutation, or schema change. Phase
7D-1 owns non-terminal binary narrowing; Phase 7D-2 owns terminal persistence.

### Phase 7D: binary narrowing and persistence

- **Inputs:** one validated 7C internal bracket, the persisted policy snapshot,
  and the same active `RUNNING` handle.
- **Execution:** choose the unique whole-UTC-second midpoint
  `M = L + floor((U - L) / 2)` while the interval exceeds the configured
  resolution. A `PRESENT` midpoint advances the lower bound. An `ABSENT`
  midpoint advances the upper bound only after the existing A2/B4 support
  contract supplies `policy.absence_confirmation_frames` distinct canonical
  decoded frames at the persisted cadence. Gaps, aliases, decode/provenance disagreement, and operational
  failures stop safely without moving a bound.
- **Outputs:** an ordered in-memory `NarrowedBracket` at the configured
  precision, or a typed safe result. Evidence is reused through the existing
  schema-2/schema-3 repositories; no new manifest schema, terminal state, or
  public route is introduced. The run remains `RUNNING`.
- **Files:** `recording_search_d1_models.py`,
  `recording_search_d1_planner.py`, `recording_search_d1_support.py`,
  `recording_search_d1_repository.py`, and
  `recording_search_d1_service.py`.
- **Tests:** deterministic floor midpoint and identity, finite iteration bound,
  monotonic shrink, distinct-frame absence support, A2/B4 reuse, interruption,
  acquisition failure, and safe re-entry.
- **Exclude:** terminal persistence, `FOUND`/`INDETERMINATE` publication,
  Phase 8 media, user judgment, and automatic restart/resume.

#### Phase 7D-2: terminal persistence

- **Status:** D2-2 interpretation is implemented as a pure increment; D2-3
  implements schema-4 persistence/publication and the canonical lock order;
  D2-4 implements strict process-restart reopen validation and safe status
  projection.
- **Prerequisite:** retain reconstructible source-C2 and visually blocking
  evidence in the D1 in-memory handoff as specified above. Do not change D1
  midpoint or A2/B4 behavior.
- **Inputs:** one strict active handle and a closed found/coarse/visual terminal
  candidate reconstructed from the existing C1/C2/D1 path.
- **Outputs:** one atomic schema-4 `FOUND`, `NOT_FOUND`, or lifecycle
  `INDETERMINATE`/result `INCONCLUSIVE` record. Only `FOUND` is eligible for a
  separate deterministic Phase 8 request.
- **Files/tests:** the D2 models, identity, validator, repository, and service
  modules plus narrow existing repository/service/status integration and the
  acceptance matrix above.
- **Exclude:** Phase 8 media, user judgment, automatic restart/resume, new
  transport, parallel repositories, and any identity/ownership/theft claim.

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
