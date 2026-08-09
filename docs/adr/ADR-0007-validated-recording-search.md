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
3. Extend the local decoder with one bounded continuous multi-target operation
   that exposes segment/acquisition/session identity, PTS, ordinal, image digest,
   and a session-scoped canonical frame ID without changing existing callers.
4. Classify each canonical probe through the versioned production EfficientSAM-
   Ti mask plus luma NCC over all pixels of the aligned source-pixel ROI as
   exactly `PRESENT`, `ABSENT`, or `INDETERMINATE`. Mask coverage is segmented
   pixels inside the clipped ROI divided by total clipped-ROI pixels; empty or
   zero-area input and coverage at least 95% are `INDETERMINATE`. Persist the
   model/checkpoint, complete calculation policy, thresholds, and acquisition
   policy.
5. Confirm absence with three distinct canonical frames in increasing decoded
   order at one-second requested-target cadence. Each alias must resolve to an
   indexed canonical recording probe in the same manifest and never counts as
   evidence.
6. Find the first supported `PRESENT -> confirmed ABSENT` bracket using the
   exhaustive support transition table. The uncertainty counter counts
   consecutive unusable coarse targets: one event at most per target, zero for
   aliases, and reset by a valid canonical PRESENT. Acquisition, decode, or
   classifier failure, indeterminate evidence, insufficient distinct frames, or
   invalid order cannot confirm absence and increments the target once.
7. Narrow that bracket with deterministic whole-second binary midpoints until
   the persisted stopping resolution is reached.
8. Treat missing recording, per-target acquisition/decode failure, geometry
   mismatch, corruption, and classifier failure as one coarse-target uncertainty
   event, never `ABSENT`. Unexpected storage or persistence failure outside
   target acquisition remains `FAILED`.
9. Persist the candidate interval and a separate Phase 8 handoff request.

The compact run manifest stores only schema 3 Phase 6 facts, the complete policy
snapshot, the closed baseline/probe/alias observation union, state, candidate
interval, and fixed safe reasons. It does not invent a Phase 6-confirmed
`source_identity` or persist internal paths.

Strict loading validates the union relationships. PRESENT and ABSENT probes
require finite policy-valid metrics and no failure reason; INDETERMINATE probes
require only the fixed current unavailable/insufficient-evidence reason set.
Aliases resolve only to indexed canonical probes in the same manifest. `FOUND`
must resolve to a later ABSENT upper bound after a canonical PRESENT lower bound
and exactly three distinct same-session ordered ABSENT support probes; aliases
and the baseline cannot fill those evidence positions. Any mismatch invalidates
the manifest rather than presenting `FOUND`.

Phase 7 stops before review-media generation. Phase 8 creates boundary images,
timeline evidence, and review video. Phase 9 leaves the final decision to the
user. No phase identifies people, infers ownership, or declares theft.

The normative field, lifecycle, search, persistence, and implementation-slice
contract is in
[Phase 7 Object-Disappearance Recording Search MVP](../design/object-disappearance-recording-search.md).

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
