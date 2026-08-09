# Phase 7 Object-Disappearance Recording Search MVP

## Status and normative authority

**Status: normative design for the current single-site Phase 7 MVP. No Phase 7
runtime, transport, classifier, required Phase 6C schema 3 compatibility
increment, or Phase 8 review-media generator is implemented.**

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

The implemented schema 2 `ConfirmedInvestigationInput` currently provides
exactly:

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
eligible for automatic Phase 7 search. Before Phase 7 search, Phase 6C must
publish schema 3 confirmations and extend `ConfirmedInvestigationInput`
with these server-owned fields:

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

Before creating a baseline or recording observation, Phase 7 must:

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
      observations/
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

### Interruption and explicit restart

If the process exits or the PC reboots, the operating system releases the lock.
On the next status inspection or start request:

1. acquire the per-investigation lock;
2. strictly inspect the latest nonterminal run;
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

### Closed observation union

Strict persistence parses exactly this discriminated union:

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

`RecordingProbeObservationRecord` contains:

```text
record_type: recording_probe
observation_id
primary_requested_time_utc
actual_decoded_time_utc | null
acquisition_id
source_segment_id
decode_session_id
decoded_pts_seconds       # canonical decimal text, not binary float JSON
decoded_ordinal
canonical_frame_id
image_sha256
source_width, source_height
state: PRESENT | ABSENT | INDETERMINATE
reason_code: null | recording_unavailable | insufficient_visual_evidence
classifier_evidence:
  classifier_policy_version
  mask_iou | null
  roi_luma_ncc | null
```

All recording provenance is mandatory. Strict validation permits only these
state/evidence combinations:

| State | Metrics | `reason_code` |
| --- | --- | --- |
| `PRESENT` | Both metrics are finite and satisfy the persisted PRESENT thresholds. | `null` |
| `ABSENT` | Both metrics are finite and satisfy the persisted ABSENT thresholds. | `null` |
| `INDETERMINATE` | Either or both metrics may be finite or null diagnostic facts, but they cannot satisfy a terminal mapping. | Exactly `recording_unavailable` or `insufficient_visual_evidence` |

`recording_unavailable` covers a required gap and replay, acquisition, or decode
failure for that coarse target. `insufficient_visual_evidence` covers classifier
failure, invalid or empty geometry/mask, zero variance, non-finite comparison,
insufficient distinct frames, and invalid decoded order. No other probe reason
code is valid in the MVP.

```text
TargetAliasRecord
  record_type: target_alias
  alias_id
  requested_time_utc
  canonical_observation_id
  reason_code: same_decoded_frame
```

It never repeats image or classification evidence. Alias IDs are indexed
separately from canonical observation IDs. Every `canonical_observation_id` must
resolve within the same manifest to an indexed `RecordingProbeObservationRecord`;
it cannot resolve to the baseline, another alias, a missing record, or a probe
outside that manifest.

### Phase 7A-2 batch decoder extension

Phase 7A-2 adds a local bounded operation without changing existing single-target
callers:

```text
decode_targets(acquisition, ordered_requested_targets)
  -> ordered DecodedTargetResult values
```

One operation opens one replay/decode session and selects all requested targets
chronologically. Each result exposes requested UTC, nullable actual decoded UTC,
credential-free acquisition and segment IDs, decode-session ID, exact PTS,
ordinal within that session, dimensions, JPEG digest, and canonical frame ID.
The operation retains no URL, credentials, command, stderr, or temporary path.

`source_segment_id` is SHA-256 of canonical JSON containing channel ID and the
trusted segment start/end epoch seconds. `acquisition_id` hashes that segment ID,
the bounded decode-window UTC endpoints, and acquisition-policy version.
`decode_session_id` is one safe unique ID for that invocation. Within that
session, `canonical_frame_id` is SHA-256 of canonical JSON containing
`acquisition_id`, `decode_session_id`, exact PTS text, decoded ordinal,
dimensions, and image SHA-256.

Canonical identity is guaranteed only within one continuous decode session.
IDs from different sessions do not prove that frames differ. Every rule that
needs frame distinctness, including `[t, t+1s, t+2s]`, therefore uses one batch
operation. If cross-session alias ambiguity would affect a bound, reacquire the
relevant targets together or return `INDETERMINATE`; requested time alone never
proves identity.

One canonical frame produces one `RecordingProbeObservationRecord`. Additional
targets selecting it optionally produce `TargetAliasRecord` values. Aliases do
not update bounds, count as support, or increment the coarse-target uncertainty
counter.

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
   strictly increasing PTS/ordinal order, all classified `ABSENT`; only then is
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
   or decoded PTS/ordinal order is not strictly increasing:
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
| Required gap or replay/acquisition/decode failure for a coarse target | Count one unusable coarse target; terminal `INDETERMINATE` at the configured limit or when the scan ends with unresolved evidence | `recording_unavailable` |
| Classifier failure, invalid evidence, missing canonical PRESENT lower probe, insufficient distinct frames, or invalid order for a coarse target | Count one unusable coarse target; terminal `INDETERMINATE` at the configured limit or when the scan ends with unresolved evidence | `insufficient_visual_evidence` |
| Confirmed JPEG or Phase 6 input fails integrity validation | `FAILED` | `baseline_validation_failed` |
| Process/PC exits during a nonterminal run | `INTERRUPTED` | `run_interrupted` |
| Unexpected storage or persistence failure outside a coarse-target acquisition | `FAILED` | fixed stage-safe reason |

## Minimal persistence contract

### Manifest

`manifest.json` is one strict UTF-8 JSON object with duplicate and unknown keys
rejected. Timestamps are canonical whole-second UTC. The compact schema is:

```text
RecordingSearchManifest
  schema_version: 1
  investigation_id
  search_run_id
  state
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
  canonical_observation_ids: ordered baseline/probe record references
  target_alias_ids: ordered alias record references
  candidate_interval | null:
    last_present_observation_id and requested time
    first_absent_observation_id and requested time
    absence_support_observation_ids
  failure_reason | null
  phase8_handoff_status: NOT_APPLICABLE | PENDING | READY | FAILED
  phase8_failure_reason | null
```

This is a complete terminal manifest. The separately stored observation records
are not inlined here; snippets elsewhere are explicitly illustrative:

```json
{
  "schema_version": 1,
  "investigation_id": "object-disappearance-v3-ch1-20260720T033418Z",
  "search_run_id": "search-run-9d764020",
  "state": "FOUND",
  "created_at_utc": "2026-08-09T04:00:00Z",
  "started_at_utc": "2026-08-09T04:00:01Z",
  "completed_at_utc": "2026-08-09T04:03:12Z",
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
  "canonical_observation_ids": [
    "observation-reference",
    "observation-last-present",
    "observation-first-absent",
    "observation-first-absent-plus-1",
    "observation-first-absent-plus-2"
  ],
  "target_alias_ids": [],
  "candidate_interval": {
    "last_present_observation_id": "observation-last-present",
    "last_present_requested_time_utc": "2026-07-20T11:42:10Z",
    "first_absent_observation_id": "observation-first-absent",
    "first_absent_requested_time_utc": "2026-07-20T11:42:11Z",
    "absence_support_observation_ids": [
      "observation-first-absent",
      "observation-first-absent-plus-1",
      "observation-first-absent-plus-2"
    ]
  },
  "failure_reason": null,
  "phase8_handoff_status": "READY",
  "phase8_failure_reason": null
}
```

Observation records are immutable strict JSON below `observations/`; the two
ordered ID lists are their only authoritative indexes. Every record rejects
unknown, duplicate, missing, or cross-variant fields.

Strict manifest loading also validates relationships, not only JSON shape:

| Manifest state | Required candidate/evidence relationship |
| --- | --- |
| `FOUND` | `candidate_interval` is present and `failure_reason` is null. Its lower-bound ID resolves to an indexed canonical `RecordingProbeObservationRecord` in `PRESENT`; its upper-bound ID resolves to a later indexed canonical probe in `ABSENT`; stored requested times exactly match those records. `absence_support_observation_ids` resolves to exactly three distinct indexed canonical `ABSENT` probes from one decode session in strictly increasing PTS/ordinal order, includes the upper-bound probe, and satisfies the persisted cadence/policy. No alias or baseline record can fill any evidence position. |
| Every state other than `FOUND` | `candidate_interval` is null and no Phase 8 handoff is `READY`. State-specific fixed failure fields follow the state table above. |

All resolved probes must belong to the same run manifest and match its
investigation, channel, acquisition/classifier policy, dimensions, and indexed
record identity. A missing, mismatched, aliased, out-of-order, threshold-invalid,
or semantically inconsistent record makes the manifest invalid. It must never be
returned, rendered, or handed to Phase 8 as `FOUND`.

No manifest, observation, alias, evidence file, or file name contains
credentials, hostnames, usernames,
authenticated URLs, ffmpeg arguments, subprocess commands, raw stderr,
tracebacks, or absolute paths. `jpeg_path` is runtime-only.

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
  invalid input or abandoned state fails without creating search evidence.

### Phase 7A-2: bounded multi-target acquisition

- **Inputs:** one active run, coverage/planner/replay boundaries, ordered target
  set, existing single-target decoder behavior, and acquisition policy.
- **Outputs:** bounded batch-decoder results, canonical frame identities, exact
  order/timing/digests, aliases, and run-owned frame records.
- **Tests:** ordered selection, distinct/aliased targets, session scope,
  cross-session rejection, gaps, timeouts, cleanup, and credential redaction.
- **Complete:** one real/fixture batch proves distinct versus aliased frames while
  existing single-target callers remain unchanged.

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
