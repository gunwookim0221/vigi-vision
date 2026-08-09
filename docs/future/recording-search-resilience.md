# Recording Search Resilience: Future Reference

> **This document is non-normative future reference.**
>
> It is not part of the current Phase 7 MVP contract, implementation scope,
> review criteria, or completion criteria. Its capabilities may be adopted only
> after a real multi-process, automatic-takeover, or crash-recovery requirement
> is demonstrated and a separate ADR explicitly promotes them into the product
> contract.

## Purpose

The current Phase 7 MVP is deliberately designed for one restaurant, one local
application host, one NVR, and one active recording-search run per
investigation. The normative contract is
[Phase 7 Object-Disappearance Recording Search](../design/object-disappearance-recording-search.md).

Earlier Phase 7 design work explored a substantially stronger durability and
coordination model. That analysis remains useful if the deployment later gains
multiple worker processes, automatic takeover, resumable long-running searches,
or stronger crash-durability requirements. It is preserved here so those ideas
do not need to be rediscovered, but none is approved for the MVP.

This document does not repair every unresolved protocol. It records what has
already been analyzed, the known defects, and the prerequisites for future
adoption.

## Ideas already analyzed

### Semantic search identity

The advanced design derived a stable search identity from all inputs that could
change the meaning of a result:

- immutable Phase 6 confirmation bytes and schema;
- reference-frame resource identity, JPEG digest, byte size, dimensions, and
  ROI;
- normalized search end and source-timezone provenance;
- search, acquisition, decoder, classifier, and decision-policy versions;
- every numeric search budget and timeout affecting evidence; and
- a canonical recording-coverage snapshot and digest.

Any material change created a new search identity or an explicit new lineage.
Evidence from incompatible searches could not be silently merged. Runtime-only
facts such as process ID, operation ID, temporary paths, heartbeat times, or
measured CPU usage were excluded.

This model is stronger than the MVP's independent `search_run_id` attempts. It
would become relevant only if compatible resume or shared result reuse becomes
a demonstrated requirement.

### Lease and fencing model

The advanced design gave each execution attempt a random `operation_id`, an
owner record, a renewable lease, and a monotonically increasing fencing epoch.
It separated two lock-protected branches:

1. **Normal publication** reloaded owner, lease, fence, terminal state, and
   immutable dependencies while holding one per-search OS-backed exclusive
   lock. Publication required the current operation ID, matching epoch, and an
   unexpired lease.
2. **Expired-lease recovery** acquired the same lock, proved that the durable
   lease had expired, reconciled pending state, assigned a new operation ID,
   advanced the fence, and published a new owner.

The intended invariant was that a stale operation could never publish after a
newer fencing epoch became authoritative. A pre-lock check was only an early
abort; every authorizing check had to be repeated while holding the shared
lock.

The MVP intentionally has none of this. It rejects a concurrent start, relies
on one OS-backed lock held by the local process, marks an abandoned `RUNNING`
manifest `INTERRUPTED`, and requires the user to start a new run.

### Automatic takeover and owner transfer

The advanced recovery path considered:

- takeover only after strict lease expiry;
- rejection while the current lease remained valid;
- serialization of normal publication and recovery under the same lock;
- two recoveries racing after expiry;
- terminal publication winning before recovery;
- a prior owner attempting to publish after transfer; and
- a process crash during owner/fence publication.

Recovery records were intended to be strict, operation-owned JSON. Ambiguous,
future-dated, malformed, foreign, or path-unsafe state failed closed rather than
being overwritten.

No current product requirement justifies automatic takeover. A future design
must first identify who or what may take over, how liveness is established, and
how the user distinguishes a recovered run from a new run.

### Resumable execution

The advanced design proposed a strict checkpoint that retained:

- the complete semantic identity;
- normalized policies and limits;
- Phase 6 and JPEG integrity facts;
- recording coverage and gaps;
- requested-time and decoded-frame visited maps;
- frame aliases and observation cache entries;
- current bracket, precision, queues, counters, and closed regions;
- immutable evidence references; and
- the current semantic result and Phase 8 handoff state.

Resume would reload and rehash every dependency, validate all evidence and
ordering relationships, and reject any changed confirmation, JPEG, coverage,
policy, acquisition implementation, or classifier version. No partial merge or
opportunistic adoption was allowed.

The MVP does not resume. An interrupted run remains historical, and a new
attempt gets a new ID and directory without adopting old observations.

### Evidence-first checkpoint publication

The advanced publication sequence was:

1. write an evidence JPEG under operation-owned temporary storage;
2. flush, close, probe, hash, and size it;
3. publish it immutably without overwrite;
4. verify the published dependency;
5. write and strictly read back the next checkpoint;
6. atomically replace the checkpoint while holding the shared lock; and
7. remove only proven operation-owned temporary data.

The goal was that a checkpoint could never reference a partial or unpublished
evidence file. Terminal publication repeated full dependency validation and
used no-overwrite promotion.

The MVP retains only the ordinary local convention that evidence files are
immutable and manifest updates use same-directory atomic replacement where
supported. It does not claim a complete crash-safe publication transaction.

### Orphan reconciliation

The advanced design classified interrupted state instead of deleting it
blindly:

| Observed state | Analyzed disposition |
| --- | --- |
| Operation-owned temporary evidence without a durable reference | Remove only when ownership is proven; otherwise quarantine. |
| Published evidence without a checkpoint/final reference | Preserve, validate, and quarantine; never adopt automatically. |
| Checkpoint referencing missing or invalid evidence | Mark state corrupt and require operator action. |
| Abandoned operation directory | Resume only for a proven compatible owner; otherwise quarantine. |
| Stale owner or recovery metadata | Reconcile under the shared lock after strict expiry checks. |
| Foreign, ambiguous, symlinked, junction, or reparse-point state | Do not traverse, mutate, or delete; fail closed. |

The MVP needs no orphan-adoption table because it never resumes or takes over an
old run. Its cleanup is limited to temporary files owned by the current
invocation.

### Strict full terminal manifest

The advanced schema separated probe requests, cadence-target selections,
acquisition attempts, source segments, decode sessions, stable decoded frames,
classifier observations, search decisions, monotonicity violations, policy
progress, publication metadata, and Phase 8 coverage plans.

That level of detail could support deterministic resume, forensic replay of
policy decisions, and cross-process publication. It is intentionally not the
MVP schema. The MVP keeps one compact run manifest and only the recording-frame
provenance needed to audit observations.

The earlier large JSON example was an illustrative excerpt, not a valid strict
manifest fixture. It omitted required records and used placeholder decision
content. A future strict schema must provide validator-backed complete fixtures
instead of describing a partial example as conforming.

### Stronger Phase 7 source binding

The advanced design considered a deployment-local source identity or a future
`SourceBindingRecord` so a channel number could be tied to the same physical
camera across configuration changes.

Phase 6 does not persist or expose such an identity. Its confirmed handoff
contains a positive `channel_id`, reference resource identity, dimensions, ROI,
timing evidence, and a trusted internal JPEG path. It does not contain camera
serial number, NVR identity, or a stable physical-camera binding.

The MVP therefore documents this limitation and stops only when an existing
inventory check explicitly detects a mismatch. It must not invent confirmed
source provenance. A stronger binding requires a separately designed capture-
and-confirmation change, not a Phase 7-only assertion.

### Multi-process and multi-host expansion

A future deployment might need multiple local workers or multiple hosts. That
would introduce questions absent from the MVP:

- shared-clock assumptions and lease evaluation;
- storage semantics across network filesystems;
- host identity and authenticated worker authority;
- cross-host lock availability and failure modes;
- duplicate work after partitions;
- operator visibility into takeover and replay; and
- retention and cleanup ownership across hosts.

The previous local-filesystem analysis is not a consensus protocol and must not
be presented as one.

## Known defects and incomplete areas

The advanced analysis is not implementation-ready. At minimum, future review
must resolve these issues:

1. **Owner/fence transfer is not crash-atomic.** The prior sequence replaced
   `fence.json` and the owner separately. A crash can expose a mismatched pair;
   the old-or-new complete-state property was not established.
2. **The recovery race catalogue is incomplete.** Six explicit races were
   listed although the review contract required seven, and the crash-transfer
   result depended on the torn state above.
3. **Durability ordering is underspecified.** File and parent syncing after
   multiple replacements does not prove which owner/fence combination survives
   power loss.
4. **The strict terminal example is not a conformance fixture.** Required
   acquisition, session, frame, observation, and decision fields were omitted.
5. **Baseline precedence was contradictory.** One terminal table allowed a
   recording gap to prevent the confirmed baseline, while the baseline contract
   correctly said an intact Phase 6/JPEG input remains valid after its original
   recording segment expires.
6. **Stable source binding is unresolved.** `source_identity` was described as
   confirmed even though Phase 6 does not persist it.
7. **Automatic recovery policy lacks a product owner.** The user experience,
   authorization, notification, and retention rules for an automatically
   recovered run were never approved.
8. **Cross-host semantics were not designed.** Native local locks and atomic
   renames do not establish correctness on a network filesystem or under host
   partitions.

These are preserved as known gaps, not current Phase 7 defects, because the MVP
does not adopt the underlying features.

## Prerequisites for future adoption

Before any capability in this document becomes normative, the project must
demonstrate all of the following:

- a measured operational need for multiple processes, automatic takeover, or
  compatible resume;
- an explicit supported-host and filesystem matrix;
- a stable source-binding fact available at confirmation time or a documented
  limitation accepted by the product owner;
- a single atomically published ownership generation, or an equivalent protocol
  with complete crash-state proofs;
- deterministic tests for every normal-publication, recovery, terminal, and
  crash interleaving;
- validator-backed schemas and complete fixtures;
- path-confinement and foreign-state cleanup rules on every supported platform;
- explicit user-visible recovery, retry, cancellation, and retention behavior;
  and
- representative real-NVR timing, storage, and interruption measurements.

## Separate ADR required

Adoption requires a new ADR that:

1. states the demonstrated deployment requirement;
2. identifies the supported coordination and storage environment;
3. replaces, rather than silently extends, the single-active-run MVP contract;
4. defines the complete owner-generation and publication protocol;
5. resolves the known defects above with executable race and crash tests;
6. specifies source binding and migration behavior;
7. defines operator-visible takeover and recovery semantics; and
8. updates the Phase 7 implementation and completion criteria explicitly.

Until that ADR is accepted, this document remains analysis only and cannot be
used to fail or expand a Phase 7 MVP implementation or review.
