# ADR-0007: Single-Site Coarse-to-Binary Recording Search

## Status

Accepted for Phase 7D and amended by the Phase 7E request-relative normative
contract. Phase 7D-2's strict physical-origin schema 1–4 family remains
implemented, immutable, and readable. The Phase 7E feasibility investigations
prove that production VIGI replay cannot supply authoritative frame UTC but can
sustain one common replay/decode session for the bounded MVP. The 7E-1C
common-session acquisition/local frame-admission and 7E-1D terminal schema-7
orchestration boundaries are implemented; CLI integration, terminal real-NVR
validation, separate Phase 8
projection repository, and Phase 8 review-media processing remain unimplemented.

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

The bounded Phase 7E-0A investigation established a second constraint. VIGI
recording metadata provides segment coverage, but replay starts at request-
relative NPT zero and RTP/remuxed MP4 timestamps are rebased independently for
each request. The exposed SDK/RTSP/FFmpeg path has no absolute frame UTC,
RTCP/NTP map, `Range: clock`, program-date-time, SDP clock reference, or other
verified frame-to-epoch relationship. Therefore `requested replay start + local
PTS` is not authoritative physical time, and the implemented schema 1-4
physical-origin admission contract cannot execute against current production
VIGI recordings.

### Phase 7E request-relative amendment

The current VIGI boundary exposes request-relative media timing but no verified
frame-to-epoch mapping. New production runs therefore use
`REQUEST_RELATIVE_ESTIMATE` schemas 5→6→7 with
`UNKNOWN_UNBOUNDED` physical-origin bias. Schemas 1–4 retain their strict
`AUTHORITATIVE_SOURCE_UTC` meaning and are never migrated, mixed, or used as a
fallback parser.

One synchronous CLI invocation owns one SDK segment, one replay/remux, one
retained MP4, and one common decode session. The five-minute default and exact
600-second maximum remain fixed. Baseline confirmation is historical only:
`FOUND` requires a recording-session `PRESENT` lower observation plus
same-session distinct `ABSENT` support. Operational failure cannot become
visual `INCONCLUSIVE`.

Schema 5 is the pre-acquisition RUNNING request/policy/plan binding and contains
no stream, session, frame, or observation facts. After successful replay,
ffprobe, media publication, and strict readback, one atomic transition creates
schema 6 with a `CommonSession` and zero evidence. Schema 6 then admits
target/decoder intent, an atomically persisted and reopened frame, B4
classification, and an atomically persisted observation in that order. Legal
intermediate frames without observations cannot prove presence or absence.
Schema 7 atomically publishes the complete reconstructed terminal evidence and
is immutable thereafter.

The exact schema-5/6 state matrices distinguish requested, decoding, admitted
frame, classifying, completed visual observation, operational failure, and
interruption. Restart never resumes or promotes a partial record. The B4
operation and observation bind an immutable classifier-policy identity and the
complete typed comparison evidence; a mutable implementation label is not an
identity.

Session media is stored outside the run tree. Optional FOUND-only source clip,
handoff, retry, status, and retention state are stored under a distinct Phase 8
repository and never mutate schema 7. A clip or handoff failure leaves FOUND
unchanged and retryable without reopening the NVR. The source-clip identity
binds the semantic request and media policy; actual MP4 digest, length, and
observed stream facts are separate integrity data. The routed design defines
all five exact Phase 8 state memberships and transitions, repository paths,
lock order, repair/reuse/deletion rules, closed failure mapping, 26 acyclic
identity families, 59 vectors, a byte-complete JPEG/MP4 fixture, and the exact
2,520-second invocation budget.

Production execution remains synchronous and CLI-only. The existing execution
POST validates its request and returns HTTP 503
`recording_search_execution_requires_cli` with zero side effects. No worker,
lease, takeover, resume, frontend, Phase 8 executor, or Phase 9 behavior is
authorized. Implementation order is 7E-1A identities/models/matrices/validation,
1B schema-5/6 persistence, 1C one-session media plus exact logical-target frame
selection and A2/B4 adapters, 1D the Phase 7E-specific C1 planner adaptation
plus terminal composition/schema 7, 2 CLI and separate Phase 8
projection/retention, then 3
bounded acceptance. The complete normative contract is
[Phase 7 Object-Disappearance Recording Search MVP](../design/object-disappearance-recording-search.md).

## Decision

Phase 7 adopts a single-host, single-active-run model for both provenance
families:

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

The following search-policy description records the implemented strict
schemas 1–4 path. For schemas 5–7, the Phase 7E request-relative amendment and
routed normative design replace every physical-origin, normalized-decoded-UTC,
canonical-frame, persistence, identity, status, and executor assumption while
retaining the common baseline, classifier, coarse-to-binary, terminal-kind,
locking, and safety rules.

The legacy strict search policy is deliberately small:

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
5. Confirm absence with the configured positive number of distinct canonical
   frames (default three) in increasing normalized decoded UTC order at the
   configured requested-target cadence. Each later observation alias
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
9. Persist a closed terminal result through the schema-4 contract below. Create
   a separate Phase 8 handoff request only for a valid `FOUND` result.

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

Phase 7D-2 introduces schema 4 because schema 3's closed lifecycle deliberately
cannot represent a terminal search result. One active-handle operation may
atomically replace a strictly reopened schema-3 `RUNNING` manifest with a
complete schema-4 terminal successor. V4 preserves every schema-3 fact and
ordered child index and adds exactly one immutable result from the closed union
`FOUND | NOT_FOUND | INCONCLUSIVE`. Result kind remains separate from lifecycle:
`INCONCLUSIVE` projects through the existing `INDETERMINATE` state, while
`FAILED` and `INTERRUPTED` remain administrative states with no result.

`FOUND` requires a policy-resolution requested-time interval bounded by a
`PRESENT` baseline/probe and exactly the configured number of distinct ordered
canonical `ABSENT` frames. `NOT_FOUND` requires the complete policy coarse grid
through the search end, with every target resolving to a distinct canonical
`PRESENT` observation and no gap, alias, uncertainty, or unsupported absence.
`INCONCLUSIVE` requires strictly reopened visual inadequacy or contradictory
visual evidence. Recording gaps, acquisition/decode/classifier failure,
timeouts, stale/corrupt input, interruption, and resource exhaustion never
become visual terminal results.

Terminal result identity is SHA-256 over canonical JSON binding result kind,
investigation/run, Phase 6 confirmation and baseline, complete policy and plan,
source manifest revision, source C2/D1 bracket identities where applicable,
requested interval/window, closed reason/limitations, and canonically ordered
evidence references. Publication time and the derived Phase 8 review anchor are
excluded. Exact duplicate evidence therefore reopens the same result without a
write; a materially different proposal conflicts and cannot replace it.

The active handle continuously holds the OS lock. D2 snapshots under the shared
mutation mutex, performs only pure expensive validation outside it, then
reacquires it to revalidate authority, schema-3 `RUNNING` state, complete
manifest/evidence digest, Phase 6/probe JPEG integrity, and every indexed child.
One same-directory atomic manifest replacement is the sole result/lifecycle
commit point. Interruption and publication are ordered by that mutex; whichever
commits first prevents the other. A concurrent child admission changes the
source digest and makes the terminal candidate stale rather than being lost.

For a `FOUND` successor, schema 4 also stores an exact-key `d1_reconstruction`
envelope containing the complete source C2 bracket, D1 input/final bracket,
ordered narrowing evidence, and lossless history. It is a validated snapshot,
not an authority: strict reopen recomputes the current schema-3 source digest,
reopens the indexed children, and runs the existing D1 state machine through a
read-only repository-backed replay before accepting the terminal result. A
changed D1 fact therefore fails closed even if an attacker recomputes
terminal/result hashes. The A2 loader also
requires exact child-directory membership for indexed frames, requests, and
evidence JPEGs; unindexed extras are corruption.

The active schema-2/3 admission loader is the only boundary allowed to perform
the narrowly authorized schema-3 crash recovery under its mutation lock. Every
schema-4 consumer instead uses the explicit read-only tree validator: status,
duplicate/conflict inspection, process-restart reopen, source-digest and D1
reconstruction, and the public Phase 8 handoff reject staging directories,
unindexed operations/requests/frames/JPEGs/classification records,
observations, aliases, symlinks, and foreign entries without repairing or
deleting anything. The Phase 8 service tests cover creation, delayed and
restart retry, deterministic concurrent reuse, conflict/corrupt request
handling, non-FOUND rejection, and terminal residue rejection.

For a closed-handle duplicate, the existing
`LocalInvestigationLock(repository.lock_path(investigation_id))` is reacquired
directly after validated `run_path(investigation_id, search_run_id)` resolution;
the exact terminal manifest is then strictly reopened while that OS-backed lock
is held. Schema 4 is a read-only duplicate/conflict branch and does not require
an active handle. Schema 3 `RUNNING` still requires the exact active handle and
its mutation mutex. The canonical order is validate IDs/run path -> existing OS
lock -> service guard -> active mutation mutex only for a live schema-3
mutation -> release guard -> release OS lock. Status and interruption use the
same order and recheck the active map after acquiring the OS lock, so a live
owner is never marked interrupted. D2-3 explicitly migrates the current
guard-first `start` and unowned `status` paths, plus any interruption/cleanup
path that can wait, so no code waits for the OS lock while holding the service
guard. Active-handle removal quiesces the mutation mutex before removing its
map entry. No new lock, registry, lease, takeover, or repository is introduced.

Only strict schema-4 `FOUND` is eligible for an immutable deterministic
`phase8-request.json`. Request creation happens after the result commit. Failure
therefore leaves `FOUND` unchanged and permits an explicit idempotent handoff
retry. Phase 8 revalidates the result and recording coverage and owns all review
media; Phase 9 remains authoritative for human judgment. The complete field,
strict-reopen, failure-matrix, status, and handoff contract is normative in the
linked Phase 7 design.

Strict A2 loading validates every request/frame/JPEG relationship, ownership,
the durable operation-record index, stable canonical identity, source/replay
PTS/time-base/ordinal provenance, and digest/size/dimension/path check. It
rejects future observation/classifier/result fields and any foreign or
incomplete child. Phase 7B schema-3 records require a successful request/frame
pair; acquisition failures remain failed requests and cannot become
observations. The later integrated search contract requires finite policy-valid
PRESENT/ABSENT metrics, safe INDETERMINATE reasons, and exactly the configured
number of distinct
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
recovery, compatible cross-process resume, multi-host coordination, generalized
cross-process crash-safe publication, event-sourced manifests, and stable source
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
an alias returned for several requested times. The MVP requires the configured
number of distinct ordered absent frames.

### Resume interrupted runs automatically

Rejected for the MVP. Explicit new runs are easier to explain, isolate, test,
and clean up on one local host.

## Consequences

- One local developer can implement Phase 7 without distributed coordination.
- Duplicate starts and refreshes have deterministic behavior.
- A reboot cannot silently complete or resume a run.
- Interrupted and failed evidence remains isolated and inspectable.
- Current VIGI search results are explicitly request-relative intervals with an
  unknown/unbounded physical-origin bias; they are useful for bounded human
  review but cannot establish an exact physical event time.
- The conservative single-session support rule may withhold FOUND or NOT_FOUND
  from static, aliased, gapped, or poorly aligned recordings. This is an
  intentional false-absence safeguard.
- Schemas 5-7 and v2 identities add implementation work, but prevent old strict
  evidence from being silently reinterpreted or colliding with weaker
  provenance.
- Disabling POST breaks the earlier start-over-HTTP behavior intentionally so a
  durable RUNNING run cannot be created without an executor capable of owning
  it to completion.
- A handoff failure cannot retroactively change valid Phase 7 evidence.
- The selected conservative classifier and numeric policy still require
  representative real-NVR validation before deployment; tuning creates a new
  policy version and cannot turn uncertainty or infrastructure failure into
  `ABSENT`.
- The SDK may not prove that a channel number still maps to the same physical
  camera; the MVP exposes this limitation and stops on explicit mismatch.
