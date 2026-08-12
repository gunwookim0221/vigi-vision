# ADR-0007: Single-Site Coarse-to-Binary Recording Search

## Status

Accepted as the implementation decision for the Phase 7 MVP.

## Context

Phase 6 publishes one immutable confirmed reference frame, channel, source-pixel
ROI, reference time, and truthful timing evidence. Phase 7 must search later
recordings for a candidate interval in which that object stopped being visible
in the confirmed region.

The expected deployment is one restaurant with one local application host, one
NVR, and normally one user. A second user may double-click or open the same
investigation, but there is no distributed worker fleet, multi-host ownership,
or automatic takeover requirement.

An earlier design expanded the problem into leases, fencing epochs, compatible
resume, cross-process takeover, full crash recovery, and a large event-sourced
manifest. That complexity was disproportionate to the current product need and
made the core recording-search feature harder to implement and verify.

## Decision

Phase 7 adopts a single-host, single-active-run model:

1. Each attempt receives a unique `search_run_id` and its own artifact
   directory.
2. One per-investigation OS-backed exclusive lock protects active execution.
3. A concurrent start is rejected deterministically as `ALREADY_RUNNING`; it
   does not create another run.
4. Refresh and reopen observe status and never start work.
5. A nonterminal manifest found after its process lock has been released is
   marked `INTERRUPTED`.
6. An interrupted run is not resumed or automatically taken over. The user may
   explicitly create a new run with a new ID and directory.
7. Evidence from a prior failed or interrupted run is never silently merged
   into a new run.

The search policy is deliberately small:

1. Require the Phase 6 schema 3 `ConfirmedInvestigationInput`, recompute and
   compare its persisted JPEG SHA-256 and byte size, fully decode dimensions,
   and reject schema 2 with direction to Phase 6C's explicit **Reconfirm for
   recording search** action. That action creates a new immutable schema 3
   identity and leaves schema 2 unchanged.
2. Sample chronologically from the confirmed requested time to the search end
   at a persisted five-minute coarse interval, always including the search end.
3. Phase 7A-2 first performs acquisition only: one bounded continuous multi-target
   operation writes one strict `ProbeFrameRequestRecord` per requested target and
   one immutable `CanonicalProbeFrameRecord` per distinct authoritative decoded
   source frame. It exposes
   the trusted segment identity, a decoder-proven physical replay origin, raw
   source/container PTS and positive source time base, normalized decoded UTC,
   replay-local time base/PTS, attempt-local ordinal, dimensions, image digest,
   and a canonical frame ID derived from the exact stable segment/frame-position
   tuple. The current Phase 7A-1 decoder does not provide this source-time
   capability; the implemented A2 decoder boundary fails safely when it is absent
   or unverifiable. No recording-session identifier is invented. A request
   alias may reference the same canonical frame, but never counts as an
   independent frame or observation.
4. Phase 7B, separately, classifies each acquired canonical frame through the
   versioned production EfficientSAM-Ti mask plus luma NCC over all pixels of
   the aligned source-pixel ROI as exactly `PRESENT`, `ABSENT`, or
   `INDETERMINATE`. Mask coverage is segmented pixels inside the clipped ROI
   divided by total clipped-ROI pixels; empty or zero-area input and coverage at
   least 95% are `INDETERMINATE`. Persist the model/checkpoint, complete
   calculation policy, thresholds, and acquisition policy. Visual uncertainty
   may publish `INDETERMINATE`; only successfully decoded and evaluated visual
   evidence may do so. Unsupported media, decode/RGB/preprocessing failure,
   corrupt input, classifier/runtime failure, invalid output, ownership loss,
   and persistence failure publish no RawComparison or observation and never
   masquerade as a visual state. The linked Phase 7B matrix requires the
   `effective_comparison_area` field and closes its overlap/area failure rows.
5. Confirm absence with three distinct canonical frames in increasing normalized
   decoded UTC order at one-second requested-target cadence. Each later observation alias
   must resolve to an indexed canonical recording observation in the same
   manifest and never counts as evidence; an A2 request alias is not evidence at
   all.
6. Find the first supported `PRESENT -> confirmed ABSENT` bracket using the
   exhaustive support transition table. The uncertainty counter counts
   consecutive unusable coarse targets: one event at most per target, zero for
   aliases, and reset by a valid canonical PRESENT. Acquisition, decode, or
   classifier failure, indeterminate evidence, insufficient distinct frames, or
   invalid order cannot confirm absence and increments the target once.
7. Narrow that bracket with deterministic whole-second binary midpoints until
   the persisted stopping resolution is reached.
8. Treat missing recording, per-target acquisition/decode failure, a valid
   visual `INDETERMINATE`, or classifier operational failure as one coarse-target
   uncertainty event, never `ABSENT`; an operational classifier failure has no
   fabricated observation. Invalid baseline geometry, corrupt immutable input,
   manifest corruption, and unexpected storage or persistence failure remain
   fixed operational failures, not visual uncertainty.
9. Persist the candidate interval and a separate Phase 8 handoff request.

The compact run manifest stores schema 3 Phase 6 facts, the complete policy
snapshot, and phase-appropriate strict indexes. Schema 1 remains the exact
readable Phase 7A-1 form. Schema 2 is exclusively the Phase 7A-2 acquisition
form: it indexes `acquisition_operation_ids`, `probe_request_ids`, and unique
`canonical_frame_ids`, contains no observation, classifier, candidate, `FOUND`,
`NOT_FOUND`, or Phase 8 fields, and rejects unsupported future versions. A v1
load never infers A2 collections. The active A1 OS lock remains continuously
held by the run handle; one per-run in-process A2 mutex serializes the complete
v1 reload, v2 successor construction, and atomic replacement. Readers see either
valid v1 or valid v2, never a partial promotion.

Phase 7B promotes one valid active v2 manifest to schema 3 only as part of the
same successful classification publication transaction, beneath the same
continuously held OS lock and handle-owned mutation mutex. Before timed
classifier success, the active `RecordingSearchRunHandle.baseline_bytes` is the
only baseline byte source and no Phase 7B operation, observation, alias, or
schema-3 child is authoritative. Schema 3 preserves all A2 fields and indexes,
adds one immutable confirmed-baseline record and ordered
classification-operation, canonical-observation, and alias indexes, and still
excludes terminal search and Phase 8 fields. Its atomic manifest replacement is
the sole promotion and observation publication commit point.

Strict A2 loading validates every request/frame/JPEG relationship, ownership,
the durable operation-record index, stable canonical identity, source/replay
PTS/time-base/ordinal provenance, and digest/size/dimension/path check. It
rejects future observation/classifier/result fields and any foreign or
incomplete child. Phase 7B schema-3 records require a successful request/frame
pair; acquisition failures remain failed requests and cannot become
observations. The later integrated search contract requires finite policy-valid
PRESENT/ABSENT metrics, safe INDETERMINATE reasons, and exactly three distinct
ordered ABSENT frames for `FOUND`; those rules are not accepted by the A2 or
Phase 7B loader.

All A2 child-record, index, and manifest mutations use that same per-run mutex
beneath the continuously held A1 OS lock. Phase 7B uses the mutex to validate
and snapshot the handle-owned baseline/probe bytes without admitting a durable
operation, releases the mutex while the existing bounded classifier worker
runs, and reacquires it for complete prepublication revalidation. A cancelled
worker may continue briefly, but its revoked attempt token can never publish or
mutate authoritative state. Only a timely result that passes handle/state/
operation-input/OS-lock ownership checks may prepare the operation and any
schema-3 successor, stage owned children, publish without overwrite, and commit
the atomic manifest replacement. Owner B therefore reloads A's
committed result and reuses its frame identity when the trusted segment and
normalized decoded UTC match; it may add only its own request relationship.
There is no lost-update or silent merge path. The one canonical identity tuple
is `(investigation_id, search_run_id, channel_id, source_segment_id,
decoded_frame_utc)`, serialized as compact UTF-8 JSON in that order and hashed
with SHA-256 as `frame-<digest>`. Replay-local PTS, time base, attempt-local
ordinal, requested time, acquisition ID, operation ID, invocation token, JPEG
digest, and dimensions are provenance or operational metadata, not identity
inputs. Ambiguous duplicate normalized positions fail safely. Frame publication
operation ownership and request operation ownership are distinct; both operation
IDs must resolve to immutable server-created `AcquisitionOperationRecord` values
through the same run's ordered operation index, but they need not be equal. The
normalized UTC uses the physical replay origin plus raw source PTS and positive
source time base, rounded once to six fractional digits with ties-to-even; if
that mapping cannot be proven or overlapping acquisitions disagree, acquisition
fails rather than substituting segment start, extraction start, or requested time.

Each operation is admitted as one closed immutable
`AcquisitionOperationRecord` at `operations/{operation_id}.json` with fixed
`record_type`, operation ID, investigation ID, run ID,
`operation_kind=recording_probe_acquisition_v1`, `state=ADMITTED`, and a
server-generated `admitted_at_utc`. The ordered unique manifest index is
updated only by atomic replacement beneath the same lock and mutex; frame and
request records cannot reference an operation before that index commit. Strict
reopening requires one indexed record per ID and matching back-references, and
rejects missing, foreign, duplicate, malformed, orphaned, or merely inserted
operation IDs. Schema 1 to schema 2 promotion commits empty A2 indexes first;
operation admission is the next atomic successor, followed by child publication.

Phase 7 stops before review-media generation. Phase 8 creates boundary images,
timeline evidence, and review video. Phase 9 leaves the final decision to the
user. No phase identifies people, infers ownership, or declares theft.

The normative field, lifecycle, search, persistence, and implementation-slice
contract is in
[Phase 7 Object-Disappearance Recording Search MVP](../design/object-disappearance-recording-search.md).
The exact Phase 7B classifier and schema-3 observation contract is in
[Phase 7B Recording-Probe Object-Presence Classification](../design/object-presence-classification.md).

## Deferred resilience analysis

Lease expiry, fencing epochs, ownership-generation transfer, automatic
recovery, compatible cross-process resume, multi-host coordination, complete
crash-safe publication, strict full terminal manifests, and stable source
binding are not Phase 7 MVP requirements.

Existing analysis is preserved in
[Recording Search Resilience: Future Reference](../future/recording-search-resilience.md).
It is non-normative and cannot expand current implementation, review, or
completion criteria.

Future adoption requires:

- a demonstrated multi-process, automatic-takeover, or crash-recovery need;
- a supported storage and host model;
- resolution of the documented ownership, durability, race, fixture, and source-
  binding defects; and
- a separate ADR that explicitly replaces this decision.

## Alternatives considered

### Keep the advanced resilience protocol in the current contract

Rejected. The deployment does not need multiple mutable owners or automatic
takeover, and the unresolved protocol would delay the useful search feature.

### Use only coarse sampling

Rejected. Coarse sampling is bounded and simple, but it leaves an unnecessarily
wide candidate interval. Binary narrowing after a supported bracket provides
useful precision without scanning every second.

### Use pure binary search without a coarse scan

Rejected. Visibility is not inherently monotonic. A chronological coarse pass
first establishes actual evidence and a candidate bracket; uncertainty stops
narrowing instead of being hidden.

### Treat one absent observation as disappearance

Rejected. One frame may reflect occlusion, poor image quality, compression, or
an alias returned for several requested times. The MVP requires three distinct
ordered absent frames.

### Resume interrupted runs automatically

Rejected for the MVP. Explicit new runs are easier to explain, isolate, test,
and clean up on one local host.

## Consequences

- One local developer can implement Phase 7 without distributed coordination.
- Duplicate starts and refreshes have deterministic behavior.
- A reboot cannot silently complete or resume a run.
- Interrupted and failed evidence remains isolated and inspectable.
- Search results remain requested-time intervals with explicit decoder limits.
- A handoff failure cannot retroactively change valid Phase 7 evidence.
- The selected conservative classifier and numeric policy still require
  representative real-NVR validation before deployment; tuning creates a new
  policy version and cannot turn uncertainty or infrastructure failure into
  `ABSENT`.
- The SDK may not prove that a channel number still maps to the same physical
  camera; the MVP exposes this limitation and stops on explicit mismatch.
