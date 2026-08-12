# Phase 7B Recording-Probe Object-Presence Classification

## Status and authority

**Status: normative Phase 7B design, ready for initial review. Runtime code and
tests are not implemented.**

This document owns the single-probe classification, observation identity,
schema-3 publication, and strict-reopen contract for Phase 7B. The broader
search order and phase boundaries remain in
[Phase 7 Object-Disappearance Recording Search MVP](object-disappearance-recording-search.md),
and the single-host ownership decision remains in
[ADR-0007](../adr/ADR-0007-validated-recording-search.md). If a summary in
either document conflicts with this Phase 7B contract, this document is
authoritative only for Phase 7B classification and schema-3 observation data.

Phase 7B answers one question:

> Does the confirmed reference object appear to be present in this acquired
> probe frame within the confirmed comparison area?

It returns exactly one closed state for every successfully completed semantic
classification: `PRESENT`, `ABSENT`, or `INDETERMINATE`. An infrastructure,
ownership, or corrupt-input failure publishes no observation and is not a
fourth visual state.

## Scope and non-goals

Phase 7B includes:

- strict loading of one confirmed baseline and one successful Phase 7A-2
  request/frame pair;
- deterministic image and ROI preparation;
- one narrow object-comparison adapter;
- conservative mapping to the three public states;
- immutable baseline, probe-observation, alias, and operation records;
- schema-2 to schema-3 promotion, atomic publication, idempotency, and strict
  reopening; and
- one internal synchronous service contract used by later search
  orchestration.

Phase 7B does not choose probe times, scan recordings, confirm three-frame
absence, build or narrow an interval, create a final search result, extract
Phase 8 media, expose a new HTTP route, add a CLI or UI, or make any person,
identity, ownership, theft, intent, payment, cause, or responsibility claim.
`ABSENT` is one bounded visual observation, not a disappearance result.

## Existing boundaries reused unchanged

| Existing boundary | Phase 7B use | Rule |
| --- | --- | --- |
| `InvestigationConfirmationService.load_confirmed()` during run start | Resolve the schema-3 reference resource and metadata; the existing Phase 7A-1 baseline gate reads the confined bytes once and captures them in the start result. | Phase 7B never accepts a baseline path, digest, dimensions, ROI, timing, or provenance from its caller. |
| `RecordingSearchRunHandle` | Prove the current process continuously owns the per-investigation OS-backed lock and retain the invocation-owned `baseline_bytes` captured by the existing Phase 7A-1 gate. | Classification cannot start or publish through a detached or terminal handle; baseline bytes are authoritative only through this active handle. |
| Existing `validate_successful_request()` | Preserve the existing request/frame validation and public behavior for current Phase 7A callers. | It remains acquisition-only and does not supply bytes to classification. |
| Narrow Phase 7A read-side validation extension | Return the admitted request, admitted canonical frame, exact probe JPEG bytes, and immutable frame metadata after one confined-path/integrity read. | A pending, failed, unindexed, foreign, or corrupt request/frame is not classifiable; classification cannot reopen the path. |
| The handle-owned per-run A2 mutation mutex | Serialize validation snapshots, successful operation admission, duplicate detection, prepublication revalidation, child publication, and manifest replacement. | Phase 7B reuses this mutex; it does not create another writer lock or hold it across an abandoned worker. |
| `LazyEfficientSamPredictor` mask path | Produce one validated point-prompt mask for each image using the verified source/checkpoint. | Phase 7B wraps the predictor through a classifier adapter; it does not call the HTTP ROI-suggestion service or persist its transient preview API. |
| Existing durable JSON, confined-path, no-overwrite, staging, and atomic-manifest utilities | Publish strict run-owned records. | Phase 7B extends the same run repository and never creates a second persistence root. |

The approved MVP has no lease, fencing epoch, takeover, ownership generation,
or multi-host protocol. References to a "stale owner" below mean a caller whose
active run handle was retired or whose OS-backed lock is no longer held. The
deferred resilience document is non-normative and cannot add Phase 7B
acceptance criteria.

## Classification command and authoritative inputs

The only Phase 7B domain entry point in this slice is an internal synchronous
service method:

```text
classify(RecordingSearchRunHandle, ClassifyRecordingProbeRequest) -> ClassificationResult

ClassifyRecordingProbeRequest
  investigation_id
  search_run_id
  probe_request_id
```

The handle is the first authoritative parameter; it is never reconstructed from
the request. All three request fields are lookup keys in their existing strict
grammars and must match `handle.investigation_id`, `handle.search_run_id`, and
the admitted request relationship. A mismatch fails as `foreign_input` or
`stale_run_owner` before any classifier work. The request contains no channel,
requested time, frame ID, JPEG path or bytes, dimensions, ROI, mask, digest,
classifier/model selection, thresholds, operation ID, timestamp, or outcome.

The handle supplies run authority, investigation/run binding, immutable baseline
bytes and baseline contract, access to the existing OS lock and handle-owned
mutation mutex, and publication authority. There is no global handle lookup,
authority reconstruction from IDs, implicit run reopening, or second ownership
model. Every validation snapshot, operation admission, duplicate check,
publication, and lifecycle decision uses this same active handle.

The service derives one immutable `ClassificationInput` only after all of these
facts agree:

```text
ClassificationInput
  investigation_id
  search_run_id
  channel_id
  baseline_observation_id
  reference_frame_resource_id
  baseline_jpeg_bytes
  reference_jpeg_sha256
  reference_jpeg_size_bytes
  source_width, source_height
  confirmed_roi
  roi_provenance
  probe_request_id
  canonical_frame_id
  probe_jpeg_bytes
  probe_jpeg_sha256
  probe_jpeg_size_bytes
  canonical recording provenance by frame reference
  acquisition_policy_version
  classifier_policy_snapshot
```

`baseline_jpeg_bytes` is the exact immutable `handle.baseline_bytes` captured by
the Phase 7A-1 start gate. Phase 7B adds a narrow in-memory baseline validator;
it receives those bytes plus the immutable schema-3 confirmation metadata,
bounds the byte length, computes the digest, validates JPEG structure and
dimensions, decodes the bytes, and returns the decoded RGB value. It does not
call the general path-based `compute_jpeg_integrity()` helper and then reopen
the baseline path. The Phase 7A read-side extension returns the exact
`probe_jpeg_bytes` used to verify the admitted frame digest and size, together
with the frame's immutable dimensions and recording provenance. Hashing, size
validation, media validation, full decode, preprocessing, and classification all
consume the same in-memory `bytes` values; a second path-based classifier read
is forbidden. Baseline and probe bytes remain semantically distinct: the
baseline is Phase 6 evidence captured by the active handle, while the probe is
the admitted Phase 7A recording frame and retains its recording provenance.

The handle owns the baseline byte value for its lifetime. A classification call
owns the probe byte value from validation through publication or discard; no
byte value is persisted in a classifier record. Both are bounded by the existing
image/byte-size limits, and unsupported, truncated, malformed, or oversized
media fails closed without attempting a second read. A baseline or probe digest
mismatch publishes no observation.

### Eligibility and safe disposition

| Condition | Classification eligibility | Disposition |
| --- | --- | --- |
| Active handle carries schema-3 confirmation bytes that passed exact digest/size, full JPEG decode, exact dimensions, and valid source-pixel ROI at start | Eligible baseline. | Continue. |
| Strict indexed `SUCCEEDED` request, indexed canonical frame, admitted same-run operations, and the Phase 7A extension's exact frame JPEG digest/size/decode/dimensions and provenance | Eligible probe. | Continue. |
| Schema 2 confirmation or missing confirmed ROI | Ineligible. | Publish no observation; return `reconfirmation_required` or `invalid_baseline`. |
| Reference JPEG missing, unreadable, path-unsafe, digest/size mismatched, dimension mismatched, or corrupt | Ineligible and corrupt. | Publish no baseline or probe observation; return `baseline_corrupt`; the run cannot continue automatically. |
| Probe JPEG digest/size mismatch, path escape, missing file, or disagreement with its canonical frame record | Ineligible and corrupt. | Publish no observation; return `probe_artifact_corrupt`; strict reopen rejects the run. |
| Authenticated baseline or probe bytes with unsupported media, media-type mismatch, failed decode, invalid decoded structure, unsupported channel layout, RGB normalization failure, or preprocessing failure | Operational input-validation failure; no visual input exists for classification. | Publish no `RawComparison`, visual observation, alias, operation, or schema-3 promotion; return `invalid_media_input` and never emit `PRESENT`, `ABSENT`, or `INDETERMINATE`. |
| Invalid, missing, clamped, rounded, non-source-pixel, or out-of-bounds ROI | Ineligible baseline. | Publish no observation; return `invalid_baseline`. Phase 7B does not repair geometry. |
| Valid ROI smaller than a classifier policy's minimum comparison area | Eligible but visually unusable. | Publish `INDETERMINATE` with `insufficient_comparison_area`. |
| Failed or pending acquisition request | Ineligible lifecycle state. | Publish no observation; return `probe_not_ready`. |
| Unadmitted operation, unindexed request/frame, missing provenance, or mismatched request/frame relationship | Ineligible and corrupt. | Publish no observation; return `acquisition_state_corrupt`. |
| Cross-investigation, cross-run, or cross-channel reference | Ineligible and foreign. | Publish no observation; return `foreign_input`. |
| Classifier runtime unavailable, timed out, raised, or returned an invalid contract | Input remains eligible but the processing attempt failed. | Publish no observation; return a fixed operational error. A safe retry may start a new operation while the run remains valid. |

The confirmed baseline is not a recording probe. Its record contains no
recording coverage, source segment, recording session, acquisition ID, source
PTS, time base, decoded ordinal, or invented absolute decoded time. The
recording probe retains those facts only through its immutable
`CanonicalProbeFrameRecord`; the observation does not duplicate or reinterpret
them. Expiry of the historical segment used to make the reference JPEG does not
invalidate an intact schema-3 baseline.

## Comparison geometry and preprocessing

The confirmed Phase 6 rectangle is the only authoritative comparison area:

- origin is the top-left source pixel;
- coordinates are integer source pixels;
- the rectangle is half-open: `[x, x + width) x [y, y + height)`;
- `x >= 0`, `y >= 0`, `width >= 4`, and `height >= 4`;
- it must be fully within the exact confirmed source dimensions; and
- it is neither normalized, rounded, clamped, expanded, nor silently repaired.

Phase 7A-2 already requires every canonical probe frame to have the same source
dimensions as the confirmation. Phase 7B repeats that equality check. It does
not resize, rotate, letterbox, pad, crop, register, or aspect-ratio-correct one
image to make a mismatch appear valid. A stored mismatch is corrupt input and
publishes no observation. If equal outer dimensions still hide camera movement,
orientation change, obstruction, blur, lighting failure, or framing that makes
the measured comparison unusable, the adapter returns `INDETERMINATE`.

The prompt point for both images is deterministic:

```text
x_point = roi.x + floor((roi.width - 1) / 2)
y_point = roi.y + floor((roi.height - 1) / 2)
```

The predictor must return a finite source-sized mask that contains that point.
The mask is clipped to the exact half-open ROI. Phase 6 never persisted an
assisted mask, so Phase 7B cannot reuse or invent one. `manual`, `assisted`, and
`assisted_then_adjusted` remain audit provenance only; in every case the final
confirmed rectangle overrides any earlier assisted proposal.

For each clipped mask, compute integer mask-pixel count and ROI-relative
coverage. Empty masks, point-missing masks, malformed masks, fewer than the
persisted minimum clipped-mask pixels, or coverage greater than or equal to the
persisted maximum are unusable. The current approved maximum is `0.95`; equality
is rejected. A full-frame or background-dominant mask cannot create `ABSENT`.

`mask_iou` uses the two clipped masks. `roi_luma_ncc` uses all aligned pixels in
the exact ROI after deterministic RGB-to-luma conversion; masks do not select
NCC pixels. The policy snapshot owns the RGB conversion coefficients, numeric
dtype, mask-logit threshold, comparison formula, metric quantization, and
minimum pixel rules. Zero variance, non-finite arithmetic, or structurally
invalid output cannot satisfy an outcome threshold.

## Classifier boundary

Phase 7B separates six responsibilities:

```text
strict input loader
  -> deterministic image/ROI preparation
  -> ObjectPresenceClassifier.compare()
  -> bounded RawComparison measurements
  -> ObjectPresenceDecisionPolicy.decide()
  -> immutable observation publication
```

The classifier protocol is replaceable but narrow:

```text
ObjectPresenceClassifier.compare(ClassifierInput) -> RawComparison

RawComparison
  baseline_mask_pixel_count: integer | null
  probe_mask_pixel_count: integer | null
  roi_pixel_count: positive integer
  mask_intersection_pixel_count: integer | null
  mask_union_pixel_count: integer | null
  baseline_mask_coverage: finite decimal | null
  probe_mask_coverage: finite decimal | null
  mask_iou: finite decimal | null
  effective_comparison_area: positive integer | null
  roi_luma_ncc: finite decimal | null
  visual_status: comparable | unusable
  unusable_reason: closed reason | null
```

Every `RawComparison` key is required; a field that is not valid for the selected
variant is JSON `null` (the schema has no optional omission). Counts are bounded
by `roi_pixel_count`. Ratios are bounded to `[0, 1]`; NCC is bounded to `[-1, 1]`.
The classifier evaluates gates in this exact order: (1) exact-byte/media
validation, (2) media decode and RGB normalization, (3) ROI geometry, (4) mask
decoding and domain validation, (5) aligned comparison-domain construction,
(6) mask IoU computation and overlap-gate validation, (7) effective comparison-area
computation and validation, (8) luma normalization, (9) variance/NCC
preconditions, (10) NCC computation with finite/range checks, and (11) policy
mapping. A malformed tensor, non-finite value, wrong shape, source-size
mismatch, impossible geometry, or other contract failure is the operational
`invalid_classifier_output` category and publishes no observation; it is not a
`RawComparison` reason.

The closed persisted matrix is:

| `visual_status` / inner `unusable_reason` | Required non-null fields | Fields that must be `null` | Allowed outer outcome/reason and publication |
| --- | --- | --- | --- |
| `comparable` / `null` | `roi_pixel_count`, both mask counts and coverages, intersection, union, `mask_iou`, `effective_comparison_area`, `roi_luma_ncc` | None | `PRESENT` or `ABSENT` with outer `reason_code: null` when its thresholds pass; otherwise `INDETERMINATE` with outer `reason_code: insufficient_visual_evidence`; publish one observation. |
| `unusable` / `invalid_mask` | `roi_pixel_count` | All mask/count/coverage/IoU/effective-area/NCC fields | `INDETERMINATE` with the identical outer reason; publish one observation. This is only an empty/degenerate mask after a valid predictor contract; malformed output is operational `invalid_classifier_output`. |
| `unusable` / `background_dominant` | `roi_pixel_count`, both mask counts and coverages | intersection, union, `mask_iou`, `effective_comparison_area`, `roi_luma_ncc` | `INDETERMINATE` with the identical outer reason; publish one observation. |
| `unusable` / `insufficient_mask_overlap` | `roi_pixel_count`, both mask counts and coverages, intersection, union, `mask_iou` | `effective_comparison_area`, `roi_luma_ncc` | `INDETERMINATE` with the identical outer reason; publish one observation. The finite IoU must fail the configured minimum-overlap gate. |
| `unusable` / `insufficient_comparison_area` | `roi_pixel_count`, both mask counts and coverages, intersection, union, `mask_iou`, `effective_comparison_area` | `roi_luma_ncc` | `INDETERMINATE` with the identical outer reason; publish one observation. The finite IoU must have passed the minimum-overlap gate. |
| `unusable` / `zero_luma_variance` | `roi_pixel_count`, both mask counts and coverages, intersection, union, `mask_iou`, `effective_comparison_area` | `roi_luma_ncc` | `INDETERMINATE` with the identical outer reason; publish one observation. |

For `insufficient_mask_overlap`, mask IoU is finite but fails the required,
versioned minimum-overlap policy gate; effective area and NCC are not computed.
For `insufficient_comparison_area`, the comparison-area gate is evaluated after
valid mask counts and IoU pass the overlap gate; the finite integral effective
area is below the persisted minimum. For `background_dominant`, coverage is
evaluated before pairwise metrics. For `zero_luma_variance`, IoU and effective
area are valid but NCC is undefined.
The separate comparable policy-gap row is the only way to persist finite
measurements that satisfy neither terminal threshold pair. The outer
`RecordingProbeObservationRecord.reason_code` is `null` for `PRESENT`/`ABSENT`,
`insufficient_visual_evidence` for comparable `INDETERMINATE`, and exactly equal
to the inner reason for every unusable row. An observation with any other
outcome/reason combination is invalid.

Strict reopening rejects omitted or unknown keys, duplicate keys, mismatched
inner/outer reasons, forbidden non-null fields, missing required fields,
non-finite or out-of-domain numbers, inconsistent counts/ratios, an invalid
outcome/reason pair, or a comparable/unusable status contradiction. Measurements
that were computed before a later gate but are not listed as required in the
selected row must be discarded and persisted as `null`. In particular,
`insufficient_mask_overlap` requires a finite IoU that fails the overlap gate and
null effective area/NCC; `insufficient_comparison_area` requires a finite IoU
that passes the overlap gate plus a finite integral effective area below policy,
with null NCC. Any later field on either row is corruption.

The production adapter is `efficient-sam-ti-roi-ncc-v1`. It reuses the verified
EfficientSAM-Ti point-mask mechanics at source commit
`d525f622e6f640acf5a0fc37c7ca1f243da5bde0` and checkpoint SHA-256
`dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a`, then
applies the aligned-ROI comparison above. EfficientSAM supplies a
category-agnostic mask, not object identity or correspondence. A general-purpose
VLM is not an authoritative fallback.

The Phase 7B adapter must consume the already decoded RGB arrays derived from
the invocation-owned byte strings. It may add a byte/array input method beside
the existing path-based predictor method, reusing the same lazy model,
checkpoint verification, tensor construction, output-shape checks, logit
threshold, score-to-mask alignment, and mask guards. Existing reference-frame
callers remain unchanged. It must not pass the original shared artifact path to
a predictor that reopens it after integrity validation, and it must not create
a second independently validated image representation.

Production composition reuses the existing one-at-a-time inference limiter and
bounded execution boundary. The handle-owned mutation mutex is held while the
service validates ownership, captures the immutable baseline/probe bytes, and
snapshots all classifier inputs; it does not admit or persist an operation at
this point. It is released for the bounded mask/comparator call because the
existing cancellation boundary can return while an underlying worker is still
running. The initial positive finite timeout is the existing `30.0` seconds. A
timeout or execution exception revokes the in-memory attempt token, publishes no
operation or observation, and releases the invocation's busy/active marker; an
abandoned worker is explicitly non-authoritative and may finish only in memory.

If classification completes before the timeout, the caller reacquires the same
handle-owned mutation mutex and revalidates the active handle, held OS lock,
`RUNNING` lifecycle, manifest generation/digest, request/frame, baseline/probe
digests and bytes, preprocessing/classifier/policy identities, and absence of a
canonical duplicate. Only that successful
revalidation may stage or publish an observation or alias. The worker never
writes repository state directly; a late completion whose token was revoked is
discarded. Loss of authority or changed state returns a fixed ownership/conflict
outcome and publishes nothing. The timeout value is operator/runtime
configuration, not visual evidence or an observation-identity input.

The shared predictor and inference limiter remain process-local, one-at-a-time
resources owned by the service composition. A timeout does not claim that the
worker was terminated; the predictor's existing synchronization protects the
cache while the abandoned call settles. A deterministic retry starts a new
operation only after the prior attempt has been marked inactive and strict
reload proves that no observation was committed.

The model's candidate score is used only inside the already verified
score-to-mask selection step. It is not a Phase 7B outcome threshold and is not
persisted as authoritative domain evidence because the current production
predictor does not expose it through its stable mask boundary. The selected
mask, counts, policy identity, and bounded comparison measurements are the
auditable facts.

Schema 3 extends the existing immutable policy snapshot with exactly these
determinism fields:

```text
classifier_preprocessing_version: phase7b-roi-luma-v1
mask_logit_threshold: 0.0
luma_integer_coefficients: [299, 587, 114]
luma_integer_divisor: 1000
luma_rounding_rule: add_500_then_floor
comparison_dtype: float64
metric_decimal_places: 6
metric_rounding_rule: half_even
minimum_mask_overlap_for_comparison: finite decimal in [0, 1]
```

For each RGB uint8 pixel, luma is
`floor((299 * R + 587 * G + 114 * B + 500) / 1000)`. NCC is the float64 sum of
mean-centered products divided by the square root of the two float64 sums of
squared centered values. Mask IoU is exact integer intersection divided by
exact integer union before quantization. The overlap gate is deterministic:
finite `mask_iou < minimum_mask_overlap_for_comparison` fails with
`insufficient_mask_overlap`, while `mask_iou >= minimum_mask_overlap_for_comparison`
passes to the effective-comparison-area gate. The field is a finite decimal in
the closed domain `[0, 1]`, is owned by the immutable versioned Phase 7B policy,
and participates in its canonical policy identity/digest. No deployment value is
chosen here; Phase 7E must supply and validate it before enabling the classifier.
Schema-3 policy parsing rejects a missing or different determinism field unless
a new classifier policy version defines that complete alternative. The
classifier execution timeout is bounded operator configuration, not visual
evidence or semantic identity; timeout publishes no observation.

## Decision policy and public states

Quality and structural gates run before threshold mapping. The closed
`INDETERMINATE` reasons are:

| Reason | Meaning |
| --- | --- |
| `insufficient_comparison_area` | The valid confirmed ROI or clipped mask is below the persisted minimum pixel rule. |
| `insufficient_mask_overlap` | Finite mask IoU failed the required versioned minimum-overlap gate; effective area and NCC are not produced. |
| `invalid_mask` | A selected mask is empty or degenerate after the predictor's output contract and prompt-containment guards pass. Malformed, source-size-inconsistent, or non-finite output is operational `invalid_classifier_output`. |
| `background_dominant` | Either ROI-relative clipped-mask coverage is at least the persisted maximum. |
| `zero_luma_variance` | NCC is undefined because either aligned ROI has zero luma variance. |
| `insufficient_visual_evidence` | All structural checks passed, but finite measurements support neither terminal threshold pair. |

`minimum_mask_overlap_for_comparison` is a required field of the versioned policy
snapshot. This design does not claim a production value; Phase 7E must supply and
validate the deployment value before enabling the classifier.

The current approved policy snapshot uses these provisional, versioned values:

| Rule | Value | Boundary |
| --- | ---: | --- |
| Minimum ROI pixels | `64` | Fewer is indeterminate. |
| Minimum clipped-mask pixels | `64` | Fewer in either mask is indeterminate. |
| Maximum ROI-relative mask coverage | `0.95` | Greater than or equal is indeterminate. |
| Minimum mask overlap for comparison | Phase 7E deployment value | IoU below the value is indeterminate; equality or greater passes. |
| PRESENT mask IoU | `0.50` | Greater than or equal passes. |
| PRESENT luma NCC | `0.60` | Greater than or equal passes. |
| ABSENT mask IoU | `0.10` | Less than or equal passes. |
| ABSENT luma NCC | `0.20` | Less than or equal passes. |

After all gates:

```text
if mask_iou >= 0.50 and roi_luma_ncc >= 0.60:
  PRESENT
else if mask_iou <= 0.10 and roi_luma_ncc <= 0.20:
  ABSENT
else:
  INDETERMINATE(insufficient_visual_evidence)
```

The PRESENT and ABSENT ranges must remain disjoint. Policy construction rejects
overlapping ranges. Exact threshold equality belongs to the stated inclusive
branch. No confidence-based tie-break or execution-order fallback is allowed.

Every ratio and NCC value is computed in the policy's fixed float64 arithmetic,
rejected if non-finite, rounded once to six decimal places with round-half-even,
then compared and persisted. Canonical JSON emits the shortest decimal form of
that six-place value and rejects an input with more than six fractional digits.
Counts remain exact integers. Strict reopen recomputes state from the persisted
quantized metrics and policy snapshot; a mismatch is corruption.

These are conservative initial operating values already approved by ADR-0007,
not a production-accuracy claim. Phase 7E must evaluate them on representative
local NVR evidence before deployment. Any threshold, preprocessing, checkpoint,
model, prompt, coverage, quantization, or comparison change requires a new
`classifier_policy_version`; it never mutates observations from the prior
version.

`PRESENT` means adequate positive evidence within this ROI. `ABSENT` means
adequate negative evidence for this one probe only. `INDETERMINATE` means the
visual comparison completed but could not support either terminal state. A
classifier timeout, missing checkpoint, invalid output, storage fault, or
corrupt input is an operational error with no observation, not
`INDETERMINATE` or `ABSENT`.

Phase 7B permits no manual correction. Phase 9 may later publish a separate
human decision, but it cannot rewrite machine measurements or state.

## Observation and operation identity

### Confirmed baseline identity

Schema-3 promotion creates exactly one
`ConfirmedReferenceBaselineRecord`. Its `observation_id` is
`baseline-<sha256>`, where the digest covers the compact UTF-8 canonical JSON
object with these keys in this exact order:

```text
record_type, investigation_id, search_run_id, channel_id,
reference_frame_resource_id, reference_requested_time_utc,
source_width, source_height, roi, jpeg_sha256, jpeg_size_bytes
```

`roi` contains exactly `x`, `y`, `width`, `height`, `coordinate_space`, and
`provenance` in that order. This makes the baseline run-owned while binding it
to the immutable Phase 6 resource, bytes, and final ROI. It has state `PRESENT`
and reason `user_confirmed_reference`; this is user confirmation, not a model
comparison.

### Canonical probe observation identity

One canonical frame produces at most one probe observation under one run policy.
`observation_id` is `observation-<sha256>`, where the digest covers compact
UTF-8 canonical JSON with these keys in this exact order:

```text
record_type, investigation_id, search_run_id, channel_id,
baseline_observation_id, canonical_frame_id, classifier_policy_version
```

The semantic identity includes investigation, run, channel, canonical frame,
confirmed baseline, and classifier policy. It excludes requested time,
request/operation/attempt/invocation ID, filesystem path, completion time,
metric values, confidence, retry order, and mutable state. The successful
request referenced by the record is the first manifest-indexed request that
caused publication; another request for the same frame resolves through an
alias and cannot create another semantic observation.

### Alias identity

When a different successful probe request references an already classified
canonical frame, Phase 7B publishes or reuses one `TargetAliasRecord` rather
than reclassifying. `alias_id` is `observation-alias-<sha256>` over the exact
ordered tuple `search_run_id`, `probe_request_id`, and
`canonical_observation_id`. It records the request's authoritative target time
and reason `same_decoded_frame`. An alias exposes the canonical observation's
state to its caller but is never independent evidence.

### Processing ownership identities

- `observation_id` is semantic and deterministic.
- `classification_operation_id` is server-created durable admission authority
  for one successful attempt to publish a semantic observation. It is prepared
  only after the bounded classifier succeeds and final revalidation passes; it
  never affects semantic identity.
- `classification_attempt_id` is an in-memory execution token used to bind
  classifier completion to the current call. It is not persisted or returned.
- the invocation identity is the active `RecordingSearchRunHandle` plus its
  handle-owned mutation mutex. It is process-local and never persisted.
- model/checkpoint identity and `classifier_policy_version` are persisted policy
  facts, not owner identities.

The immutable `ClassificationOperationRecord` contains exactly:

```text
record_type: classification_operation
classification_operation_id
investigation_id
search_run_id
operation_kind: recording_probe_classification_v1
state: ADMITTED
probe_request_id
canonical_frame_id
baseline_observation_id
classifier_policy_version
admitted_at_utc
```

`ADMITTED` means admitted by the same atomic manifest replacement that publishes
the validated observation (and, when needed, the schema-3 baseline successor).
There is no durable operation record or operation index before classifier
success.

`classification_operation_id` uses
`classification-op-[a-z0-9-]{1,96}` and is at most 114 characters. Baseline,
observation, and alias IDs use their fixed prefix followed by exactly 64
lowercase hexadecimal SHA-256 characters. `admitted_at_utc` and every
`published_at_utc` use canonical UTC with exactly six fractional digits. These
administrative timestamps never enter a semantic identity hash.

The record is admitted only in the atomic publication of a successful
classification. A failed, timed-out, cancelled, abandoned, or invalid attempt
leaves no indexed operation and no observation; the authoritative schema-2/A2
tree remains unchanged when applicable. A retry receives a new operation ID.

Rerunning identical semantic input after successful publication reuses the
existing observation and creates no new operation. A different classifier or
policy version is forbidden inside the immutable run policy snapshot. It
requires an explicit new run with a new snapshot and therefore a new baseline
and observation identity. No in-place invalidation or supersession exists in
the MVP.

## Persisted record shapes

`ConfirmedReferenceBaselineRecord` contains exactly:

```text
record_type: confirmed_reference_baseline
observation_id
investigation_id
search_run_id
channel_id
reference_frame_resource_id
reference_requested_time_utc
source_width, source_height
roi: x, y, width, height, coordinate_space, provenance
jpeg_sha256, jpeg_size_bytes
timing_precision_status, warnings
state: PRESENT
reason_code: user_confirmed_reference
published_at_utc
```

It contains no recording or classifier provenance.

`RecordingProbeObservationRecord` contains exactly:

```text
record_type: recording_probe
observation_id
investigation_id
search_run_id
channel_id
classification_operation_id
baseline_observation_id
canonical_frame_id
primary_probe_request_id
primary_requested_time_utc
classifier_policy_version
state: PRESENT | ABSENT | INDETERMINATE
reason_code: null | closed INDETERMINATE reason
classifier_evidence: RawComparison
published_at_utc
```

`PRESENT` and `ABSENT` require the comparable/null row of the closed
`RawComparison` matrix and null outer reason. `INDETERMINATE` requires either an
explicit unusable matrix row whose outer reason exactly matches the inner reason,
or the comparable finite policy-gap row with outer reason
`insufficient_visual_evidence`. The record references the canonical frame for
complete recording provenance; it does not duplicate segment, PTS, time-base,
ordinal, extraction-window, or JPEG-path fields.

`TargetAliasRecord` contains exactly:

```text
record_type: target_alias
alias_id
investigation_id
search_run_id
channel_id
probe_request_id
requested_time_utc
canonical_observation_id
reason_code: same_decoded_frame
published_at_utc
```

Every JSON model is strict, frozen, and variant-closed. Missing, duplicate,
unknown, coerced, non-canonical, cross-variant, or relationally inconsistent
fields are rejected.

## Provenance and auditability

An auditor can reconstruct a committed decision from the strict schema-3
manifest, baseline record, observation record, Phase 6 reference resource, A2
request/frame/operation records, the two immutable JPEGs, and the versioned
policy snapshot. The durable chain binds:

- Phase 6 investigation, reference resource, JPEG digest/size, source
  dimensions, requested time, ROI geometry, and ROI provenance;
- Phase 7 run, request, canonical frame, source segment, physical replay origin,
  source PTS/time base, normalized decoded UTC, replay-local PTS/time base,
  decoded ordinal, acquisition policy, and probe JPEG digest/size;
- deterministic preprocessing, prompt, mask threshold/coverage rules, minimum
  areas, arithmetic, quantization, classifier/model/checkpoint identity, and
  outcome thresholds; and
- exact counts, bounded measurements, public state, closed visual reason, and
  producing classification operation.

`reference_requested_time_utc`, the canonical frame's decoder-proven time and
recording provenance, JPEG digests, ROI, counts, measurements, and policy are
evidence. `admitted_at_utc` and `published_at_utc` are administrative audit
times only; they do not define observation identity, decoded time, search order,
or visual state. The in-memory attempt token and invocation/handle identity are
ownership controls and are not evidence.

No free-text diagnostic, model tensor, embedding, logit array, mask image,
stack trace, native error, or environment dump is authoritative evidence. A
closed reason code must explain every `INDETERMINATE` record. Operational errors
remain separate fixed categories and publish no visual record.

## Manifest schema 3 and artifact layout

Phase 7B promotes a valid active schema-2 acquisition manifest to this exact
additive schema-3 shape:

```text
RecordingSearchManifestV3
  schema_version: 3
  investigation_id, search_run_id
  state: RUNNING | FAILED | INTERRUPTED
  created_at_utc, started_at_utc, completed_at_utc
  confirmation
  policy
  acquisition_operation_ids
  probe_request_ids
  canonical_frame_ids
  baseline_observation_id
  classification_operation_ids
  canonical_observation_ids
  target_alias_ids
  failure_reason | null
```

`RUNNING` requires null `completed_at_utc` and `failure_reason`. `FAILED`
requires a completion time and exactly one of `artifact_failure` or
`unexpected_error`, both of which presuppose valid immutable baseline evidence.
`baseline_corrupt` is never a schema-3 failure reason. `INTERRUPTED` requires a
completion time and `process_lock_released`. Schema 3 cannot be created from
`PENDING` or from a baseline that has not passed the single-read integrity gate,
and it cannot contain a Phase 7C/7D terminal result.

Promotion preserves every schema-2 value and index in order, adds one validated
baseline record, and initializes the three Phase 7B indexes empty. A schema-1
or schema-2 loader remains exact and never infers Phase 7B fields. A schema-3
loader validates the complete A2 tree plus every Phase 7B relationship. Phase
7B does not add `FOUND`, `NOT_FOUND`, candidate-interval, or Phase 8 fields;
those remain a later schema decision.

The run layout becomes:

```text
{search_run_id}/
  manifest.json
  operations/{acquisition_operation_id}.json
  classification-operations/{classification_operation_id}.json
  frames/{canonical_frame_id}.json
  requests/{probe_request_id}.json
  observations/{baseline_or_observation_or_alias_id}.json
  evidence/frames/{canonical_frame_id}.jpg
```

The baseline JPEG remains owned by the Phase 6 reference resource and is not
copied. The probe JPEG remains the immutable A2 evidence file. Phase 7B does not
persist crops, masks, overlays, embeddings, tensors, logits, general model
diagnostics, or a second image. The bounded classifier evidence inside the
observation record is sufficient to reproduce and audit the decision from the
two referenced JPEGs and policy snapshot without adding privacy-heavy derived
artifacts.

`classification_operation_ids` are unique in server-admission order.
`canonical_observation_ids` are unique in manifest publication order.
`target_alias_ids` follow their source `probe_request_ids` order; equal request
times use the existing request-index order. The baseline has its dedicated
single ID and never appears in the probe index.

All records use the existing canonical UTF-8 JSON convention with one trailing
newline. Hash identities use compact JSON with the explicitly ordered keys
above. Persisted paths are run-relative POSIX paths derived from validated IDs.
No absolute path, drive, URI, `.`/`..`, backslash, symlink, junction, reparse
point, credential, hostname, authenticated URL, command, arguments, stderr,
traceback, or raw exception is allowed.

## Publication and strict reopening

### Schema promotion

Schema-3 promotion is not a preflight step and never occurs before a successful
timed classification. For a run that starts from schema 2, the classifier first
uses the active handle's exact baseline bytes and immutable confirmation metadata
in memory. Only after the classifier succeeds and the final revalidation passes,
the caller, while holding the A1 OS lock and handle-owned mutation mutex:

1. strictly reloads the current schema-2/A2 tree;
2. revalidates the active handle, `RUNNING` state, held OS lock, exact baseline
   bytes/digest/size/dimensions/ROI, and immutable run policy;
3. constructs the complete schema-3 baseline successor and the successful
   classification operation/observation (and any alias);
4. writes the complete successor children to invocation-owned staging;
5. publishes each child without overwrite; and
6. atomically replaces `manifest.json` with the schema-3 successor and all
   classification indexes appended exactly once.

That manifest replacement is the sole promotion and classification commit point.
Before it, schema 2 remains authoritative and all staged children are residue;
after it, the exact schema-3 manifest and children are authoritative. A retry
reuses byte-identical children; any conflicting child is `publication_conflict`.

If the active handle cannot validate its immutable baseline before this
replacement, Phase 7B publishes no baseline child, schema-3 successor, or
observation. The admitted schema-2/A2 manifest remains authoritative and the
caller receives operational `baseline_corrupt`; repair or explicit human
intervention is required. A schema-3 `FAILED` manifest is never fabricated to
describe that corruption.

### Classification operation and observation

For one request, beneath the same OS lock and handle-owned mutex only for the
validation snapshots and publication boundaries:

1. strictly reload the current schema-2/A2 or schema-3 tree and all indexed
   children;
2. return an existing canonical observation or alias when semantic identity is
   already committed;
3. while holding the mutex, revalidate the active handle, lock, `RUNNING` state,
   request/frame, exact baseline/probe bytes and digests, policy, and absence of
   conflicting paths; capture immutable classifier inputs and an ephemeral
   attempt token, but write no operation, observation, alias, or schema-3 child;
4. release the mutex and execute the classifier with the captured immutable
   inputs under the existing bounded timeout/limiter;
5. on timeout, cancellation, abandonment, invalid output, or operational
   failure, revoke the token under the mutex, clear the active marker, publish
   nothing, and return a fixed safe error. The authoritative schema-2/A2 tree
   remains byte-for-byte unchanged when the call began there. A worker that
   continues after cancellation has no write authority;
6. on timely success, reacquire the same mutex and revalidate the active handle,
   held lock, `RUNNING` state, current manifest generation/digest, request/frame,
   exact bytes/digests, preprocessing/classifier/policy identities, and absence
   of a canonical duplicate;
7. if revalidation fails, revoke the token, discard the in-memory result, and
   return the fixed ownership/conflict/lifecycle outcome without writing;
8. otherwise prepare and validate the complete observation or alias, and, when
   the run began at schema 2, the complete schema-3 successor and baseline child;
9. publish the invocation-owned children without overwrite; and
10. atomically replace the manifest with every new index appended exactly once,
    then clear the active marker.

The final manifest replacement is the observation or alias commit point. A
child file without a manifest index is never evidence. A manifest index without
a complete valid child is corruption.

Interruption outcomes are closed:

| Window | Authoritative state after interruption | Reopen behavior |
| --- | --- | --- |
| Before classifier execution | The authoritative schema-2/A2 or existing schema-3 manifest has no new Phase 7B operation, observation, alias, or promotion. | Lost OS lock makes the run `INTERRUPTED`; no classifier result is inferred. |
| During classifier execution | The authoritative tree is unchanged; partial in-memory measurements have no authority. A cancelled worker may still be running but has a revoked attempt token. | Inspection first tries the OS lock. If it is still held, it returns `RUNNING` without mutation; only after the lock is acquired and no active handle remains may it mark the nonterminal run `INTERRUPTED`. |
| After measurements/staging but before final child publication | The authoritative tree is still unchanged; invocation-owned staging is not evidence and no operation is admitted. | Clean only proven owned staging, then mark `INTERRUPTED`; otherwise reject ambiguous residue. |
| After final observation child publication but before manifest replacement | The child is unindexed and non-authoritative. | Remove only when the invocation marker proves ownership; otherwise reject as corruption. Never adopt it. |
| After manifest replacement | The indexed observation is committed and immutable. | Validate it strictly, then mark the still-nonterminal run `INTERRUPTED` after lock loss; never delete or reuse it in another run. |

The mutex is deliberately released while the bounded classifier worker runs,
because the existing cancellation boundary may abandon a worker after the
caller returns. Only the active handle-owned caller can reacquire the mutex and
publish, and complete prepublication revalidation is mandatory. The OS lock
remains continuously held by the run handle. Phase 7B adds no lease, fence, or
takeover path.

### Cleanup and reopen

Before a commit point, an invocation removes only its own staging and final
unindexed children when ownership is proven by its private staging marker. It
never deletes a committed child, a pre-existing path, another invocation's
file, an ambiguous entry, or anything behind path indirection. If ownership
cannot be proven, reopening treats the residue as corruption and preserves it
for operator inspection.

On strict schema-3 reopen, the reader must:

- validate the manifest, indexes, path confinement, and complete A2 tree;
- resolve the one baseline record and recompute its identity and every Phase 6
  relationship;
- require every indexed classification operation and observation/alias child
  exactly once and reject every unindexed non-recoverable child;
- recompute every observation and alias identity;
- verify request/frame/baseline/operation ownership and policy equality;
- revalidate reference and probe JPEG bytes, digest, size, format, and
  dimensions;
- validate bounded evidence, quantization, state/reason combinations, and
  recompute state from persisted metrics and thresholds; and
- reject missing, malformed, foreign, duplicate, path-mismatched, contradictory,
  or unsupported data as manifest corruption.

The loader never repairs, reclassifies, infers an observation from a JPEG,
adopts an unindexed file, or downgrades schema 3. If the process lock was lost
while the manifest remained nonterminal, the existing lock-safe A1 rule marks
the run `INTERRUPTED`; it does not resume classification.

If a later strict reopen finds that the indexed Phase 6 baseline no longer
matches its committed digest, size, dimensions, or confined resource identity,
the reader fails closed as artifact/manifest corruption even if the stored
lifecycle state says `FAILED` or `INTERRUPTED`. It does not mutate that state,
infer a visual outcome, or create a replacement schema-3 record. Explicit repair
or restoration of the authoritative bytes is required before the run can be
treated as operational again.

## Idempotency, retries, and concurrency

- A duplicate call before terminal publication enters through the same active
  handle. If the handle-owned in-memory attempt is still active, it waits for
  that attempt or returns the fixed `classification_in_progress` outcome; it
  never admits a competing operation. After the first call commits, it reloads
  and returns `reused`. If the first call times out or fails operationally, its
  token and active marker are revoked before a retry may admit a new operation.
- A duplicate call after terminal observation publication returns the exact
  immutable observation and creates no new operation or record.
- A different request that resolves to the same canonical frame creates or
  reuses one alias and returns the canonical state without classifier execution.
- A conflicting child at a deterministic path is never overwritten or treated
  as equal merely because its filename matches.
- A retired handle, released OS lock, terminal/interrupted run, or invalid
  operation cannot publish. Classifier success does not preserve ownership.
- A new policy or model requires a new run. Existing observations remain
  immutable historical evidence and are never rewritten or superseded in place.

## Closed error taxonomy

| Category | Observation | Safe retry | Run/reopen effect |
| --- | --- | --- | --- |
| `invalid_classification_request` | None | After caller correction | Run unchanged. |
| `probe_not_ready` | None | After acquisition succeeds; failed acquisition follows the existing explicit retry rule | Run unchanged unless A2 already made it terminal. |
| `invalid_baseline` | None | After caller correction or explicit reconfirmation | No schema-3 promotion; the authoritative schema-2/A2 manifest remains unchanged. |
| `baseline_corrupt` before schema-3 promotion | None | No automatic retry of unchanged input | No schema-3 manifest or observation is published. Return the fixed operational error and require repair or human intervention. |
| `baseline_corrupt` discovered while reopening schema 3 | None | No automatic retry | Strict reopen fails as `manifest_corrupt`/`baseline_corrupt`; do not mutate lifecycle state or bypass evidence checks. Existing committed records remain immutable but the run is not safely operational until repaired. |
| `foreign_input` / `acquisition_state_corrupt` / `probe_artifact_corrupt` | None | No automatic retry | Strict reopen rejects corruption; human inspection is required. |
| `invalid_media_input` | None | After correcting the input or adapter | Run remains valid; no visual record or schema-3 promotion is published. |
| `invalid_mask`, `background_dominant`, `insufficient_mask_overlap`, `insufficient_comparison_area`, `zero_luma_variance`, `insufficient_visual_evidence` | `INDETERMINATE` | A later distinct frame may be classifiable | Run remains valid. |
| `classifier_unavailable` | None | Yes after runtime/checkpoint restoration | Run remains valid if ownership is retained; no false visual state. |
| `classifier_timeout` / `classifier_execution_failed` | None | Yes with a new operation | Run remains valid unless orchestration chooses a safe terminal result later. |
| `classification_in_progress` | None | After the active handle-owned attempt settles | No new operation is admitted; the caller reloads the canonical result or retries after safe token revocation. |
| `invalid_classifier_output` | None | Only after correcting/versioning the adapter | No observation; repeated unchanged output must not be retried indefinitely. |
| `stale_run_owner` | None | Only through the still-valid owner or an explicit new run | Late work cannot publish. |
| `publication_conflict` / `artifact_failure` | None | Only after strict reload proves a safe retry | Unexpected storage failure may transition the run to `FAILED`. |
| `manifest_corrupt` | None | No automatic retry | Reopen fails closed and preserves artifacts for human inspection. |

`insufficient_mask_overlap` and `insufficient_comparison_area` are visual
comparability reasons, not operational categories. For the former, media decode,
preprocessing, ROI/mask/aligned-domain validation, and finite in-domain IoU have
completed; IoU is below the versioned overlap minimum, `effective_comparison_area`
and NCC are null, and the inner and outer reason are exactly
`insufficient_mask_overlap` with outcome `INDETERMINATE`. For the latter, IoU is
finite and passes that overlap minimum, the effective comparison area is valid
but below the existing area minimum, later variance/NCC values are null, and the
inner and outer reason are exactly `insufficient_comparison_area` with outcome
`INDETERMINATE`. Strict reopening rejects any other field, reason, or outcome
combination. Operational failures remain outside this taxonomy and publish no
observation.

Raw exceptions and native diagnostics never cross the service boundary. The
service returns fixed categories only.

## Service and API behavior

Phase 7B is internal-only. It adds one synchronous
`ObservationClassificationService.classify(handle, request)` entry point and no HTTP endpoint,
CLI, background job, polling resource, or UI. Phase 7C/7D will call it through
the active `RecordingSearchService` orchestration.

`ClassificationResult` contains exactly:

```text
outcome: CREATED | REUSED
observation_id
alias_id | null
probe_request_id
canonical_frame_id
state: PRESENT | ABSENT | INDETERMINATE
reason_code | null
```

An alias call returns `REUSED`, the canonical observation ID/state, its own
request ID, and the created or reused alias ID. A direct canonical observation
has a null alias ID. The result exposes no metric payload by default, path, baseline
bytes, probe bytes, operation/attempt/invocation ID, model path, checkpoint
path, host, URL, command, stderr, or exception text. Internal repository readers
can load the strict evidence record for tests and later search policy.

Because one caller holds the run lock and the handle-owned active-attempt marker
serializes publication, there is no separately observable Phase 7B `PENDING`
resource. The existing run remains `RUNNING` during classification. Pending
operation state is represented only by the live owner and its in-memory attempt
marker; process loss follows the existing `INTERRUPTED` rule. Existing start/status
endpoints remain unchanged in Phase 7B.

## Deterministic traces

Each trace identifies the active handle/lock state, the exact immutable byte or
record snapshot, classifier authority, write set and manifest commit point. A
strict schema-3 baseline exists unless stated otherwise; `handle.baseline_bytes`
is the only baseline byte source and the Phase 7A extension's returned bytes are
the only probe byte source.

| Trace | Authoritative state and lock | Files/records and commit point | Observable result | Retry or recovery |
| --- | --- | --- | --- | --- |
| 1. Valid `PRESENT` | `RUNNING`; active handle holds the OS lock; caller obtains the shared mutex; request/frame are indexed. | After timed classifier success and revalidation, atomically publish `classification-operations/{id}.json`, `observations/{id}.json`, and the manifest successor together. | `CREATED/PRESENT`; no interval. | Identical retry returns `REUSED`. |
| 2. Valid `ABSENT` | Same as trace 1; both quantized metrics satisfy the inclusive ABSENT maxima. | Same successful operation/observation publication transaction; observation-manifest replacement commits. | `CREATED/ABSENT` for one probe only; no disappearance result. | Identical retry returns `REUSED`; Phase 7C later owns support. |
| 3. Low quality or obstruction | `RUNNING`; valid inputs; active owner holds both lock levels. | After successful comparison, publish one admitted operation and one `INDETERMINATE` observation with bounded evidence in the manifest transaction. | Closed visual reason; never `ABSENT`. | A later distinct frame may be classified. |
| 4. Unsupported media or decode failure | Active handle holds the OS lock, but exact baseline or probe bytes fail media-type, decode, decoded-structure, channel-layout, RGB, or preprocessing validation before any visual comparison. | No RawComparison, operation, observation, alias, schema-3 successor, or manifest replacement. | `invalid_media_input`; no visual state. | Correct the input/adapter or start a new valid invocation; the authoritative schema-2/A2 tree is unchanged. |
| 5. Baseline digest mismatch before schema-3 promotion | Active handle holds the OS lock; schema 2/A2 is authoritative; baseline bytes from the handle fail the committed digest/size gate before any operation admission. | No baseline child, schema-3 successor, observation, or manifest replacement. | `baseline_corrupt`; no observation and no fabricated `FAILED` schema-3 state. | Repair/reconfirmation is required; the unchanged schema-2/A2 evidence is not retried automatically. |
| 6. Baseline corruption while reopening schema 3 | No caller may bypass strict reopen; the stored baseline child/resource no longer matches its committed digest. | No lifecycle mutation, replacement baseline, or observation write. | `manifest_corrupt`/`baseline_corrupt`; existing committed observations remain immutable but the run is not operational. | Restore authoritative bytes or require human repair; never infer a visual state. |
| 7. Duplicate before publication | Worker A owns the active handle and attempt marker; worker B uses the same handle and sees the marker while A's classifier runs outside the mutex. | B writes nothing; A alone may publish after revalidation. | B waits or returns `classification_in_progress`, then receives `REUSED` after A commits. | If A times out, token/marker revocation completes first; B may then admit a new operation. |
| 8. Duplicate after publication | Strict schema-3 state already indexes the valid semantic observation; caller holds the valid handle for reload. | No new operation, child, evidence, or manifest bytes. Existing manifest is the prior commit point. | `REUSED` with identical state. | No reclassification under the same policy. |
| 9. Interruption before observation commit | The authoritative schema-2/A2 or schema-3 manifest contains no new Phase 7B operation; process loses the OS lock before the successful publication transaction. | Staging or an unindexed child may exist, but no operation or observation index is committed. | Next inspection first attempts the OS lock; only after acquiring it with no active handle marks `INTERRUPTED`; residue is not evidence. | Clean only provably invocation-owned residue; explicit new run required. |
| 10. Interruption after observation commit | Observation is indexed and valid; overall run is still nonterminal when process lock is lost. | Observation and manifest successor are authoritative; manifest replacement already committed. | Inspection marks the run `INTERRUPTED` without deleting the observation. | Explicit new run; never adopt old evidence. |
| 11. Two competing workers | One process owns the OS lock. In-process A owns the handle marker/mutex at each publication boundary; B waits or reloads. Another process cannot own the run. | Exactly one writer creates each operation/observation successor and commits each manifest replacement. | Waiter reloads the winner; no lost update. | Retry follows the committed state. |
| 12. Stale worker after ownership loss or changed state | No fence exists. A retired handle, released OS lock, changed manifest generation, or changed policy makes A's token stale after classifier work. | Prepublication revalidation rejects; only owned staging may be removed; no final manifest commit. | `stale_run_owner`/conflict; no observation and no invented lease/fence data. | Only the current valid owner, or an explicit new run, may proceed. |
| 13. Classifier succeeds after ownership loss | Measurements exist only in memory; handle/lock validation fails before final publication. | No observation child or manifest index; owned staging is removed when provable. | `stale_run_owner`; classifier success is discarded. | A valid new run must classify again. |
| 14. Malformed/foreign unindexed residue | Reopen holds the OS lock and mutex; manifest does not index the residue. | No commit point exists for the residue. Owned staging-marker recovery may delete only its own file; otherwise preserve it. | `manifest_corrupt`; nothing is adopted or returned. | Human inspection unless ownership-safe cleanup fully restores the exact tree. |
| 15. New classifier or policy version | Current run has an immutable different policy snapshot; its existing observation remains indexed. | No write in the current run. A separately started run creates its own baseline, operations, observations, and commits. | Current call is rejected; new run gets new identities. | Explicit new run only; old records remain immutable. |
| 16. Historical reference segment missing | `handle.baseline_bytes`, digest/size/dimensions/ROI remain valid; current probe has full A2 provenance; active owner holds the OS lock. | Normal operation and observation publication; no reference segment record is created. | Classification proceeds normally. | Normal duplicate rules apply. |
| 17. Probe bytes read once | Active handle owns immutable baseline bytes; the A2 read-side extension reads the confined probe JPEG once, verifies digest/size, and returns those bytes plus frame metadata. | No second path read; only successful operation/observation records and the manifest successor are written after successful revalidation. | Normal `PRESENT`, `ABSENT`, or visual `INDETERMINATE`; any mismatch publishes nothing. | A digest/decode failure is `probe_artifact_corrupt`; repair or a new acquisition is required. |
| 18. Timeout and late completion | Handle owns the OS lock; classifier runs outside the mutation mutex under the bounded timeout. Timeout revokes the attempt token and active marker while the worker may still finish. | No operation, observation, alias, or manifest write by the late worker; schema-2/A2 remains byte-for-byte unchanged when applicable. | `classifier_timeout`; no visual state. | Strict reload proves no committed result, then a new operation with a distinct invocation may retry; the late result is discarded. |
| 19. Classifier succeeds and revalidation passes | Handle still owns the OS lock; the caller reacquires the mutex after the worker completes and all manifest, byte, policy, request/frame, and duplicate checks match the captured snapshot. | Stage and publish one complete operation/observation/alias (and schema-3 successor if needed), then replace the manifest as the sole commit point. | `CREATED` with the validated state; active marker clears. | Identical retry returns `REUSED`. |
| 20. Insufficient mask overlap | Schema 2/A2 or schema 3 is `RUNNING`; the active `RecordingSearchRunHandle` holds the OS lock. Under the handle mutex, the caller captures the exact immutable baseline/probe bytes and all policy/request/frame inputs, then releases the mutex for the timely classifier. Media decode, RGB preprocessing, mask validation, and aligned-domain construction succeed; finite `mask_iou` is in `[0, 1]` but is below the versioned `minimum_mask_overlap_for_comparison`. | Reacquire the same mutex and revalidate the handle, lock, lifecycle, manifest generation, exact bytes/digests, policy identity, request/frame, and duplicate absence. Persist `RawComparison` as `unusable/insufficient_mask_overlap` with required ROI/mask counts, coverages, intersection, union, and finite IoU; retain null effective area and NCC. Publish the operation, observation (and schema-3 baseline successor if needed), then replace the manifest as the admission commit point. | Timely `INDETERMINATE/insufficient_mask_overlap`; no operational failure is implied. Strict reopen accepts the exact matrix row and recomputes the same state. | Identical retry returns the canonical committed result without reclassification. |
| 21. Insufficient comparison area | Same `RUNNING` authority, active handle, OS lock, mutex capture/release, exact bytes, successful media/preprocessing, valid masks, aligned domain, and final revalidation as trace 20. Finite `mask_iou` passes the versioned overlap minimum; the required finite integral `effective_comparison_area` is below the existing minimum comparison-area rule. | Persist `RawComparison` as `unusable/insufficient_comparison_area` with all required counts, coverages, intersection, union, finite IoU, and effective area; retain null NCC. Publish the operation, observation (and schema-3 baseline successor if needed), then replace the manifest as the admission commit point. | Timely `INDETERMINATE/insufficient_comparison_area`; this is a successfully evaluated visual result, not an operational failure. Strict reopen accepts only this matrix row and recomputes the same state. | Identical retry returns the canonical committed result without reclassification. |

## Phase boundaries and implementation slices

Phase 7B implementation may be split into Luna-sized units without changing the
contract:

1. strict observation/operation models, identity helpers, and schema-3 reader;
2. schema promotion, operation admission, atomic observation/alias publication,
   cleanup, and strict reopen;
3. deterministic image/ROI preparation, comparator, decision policy, and
   production EfficientSAM-Ti adapter composition; and
4. the one internal classification service plus focused contract, concurrency,
   interruption, corruption, redaction, and real-predictor fixture tests.

Phase 7C may consume only committed canonical observations and aliases to run
the chronological coarse policy. It cannot alter Phase 7B state or treat an
alias, acquisition failure, or operational classifier error as independent
visual evidence. Phase 7D owns binary narrowing and terminal search persistence.
Phase 8 owns review media, Phase 9 owns human judgment, and neither may rewrite
Phase 7B records.

## Validation requirements for implementation

The implementation review must prove:

- all eligibility, foreign-input, digest, size, path, dimension, ROI, strict
  JSON, identity, and policy checks through production service/repository paths;
- every threshold equality and gap, non-finite value, malformed output, mask
  gate, zero-variance case, and closed state/reason combination;
- score-to-mask alignment and the real EfficientSAM predictor plus comparator on
  local non-sensitive fixtures;
- no infrastructure or corrupt-input failure can publish `ABSENT` or an
  `INDETERMINATE` observation that hides the failure;
- deterministic duplicate, alias, promotion, interruption, lost-owner,
  publication-conflict, cleanup, and strict-reopen behavior using hooks/events,
  not sleeps;
- no credential, path, command, stderr, traceback, or uncontrolled model output
  is persisted or returned; and
- Phase 7A-1/A-2, Phase 6, reference-frame, assisted-ROI, CLI, and frontend
  behavior remain unchanged.

Representative real-NVR accuracy and operational tuning remain Phase 7E. Until
that evidence exists, the current thresholds are a versioned conservative
policy to validate, not a production-accuracy claim.
