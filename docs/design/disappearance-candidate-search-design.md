# Disappearance-Candidate-First Search Design

## Status and authority

**Status: the architecture was approved at Initial Review. Phase S3 has a
local shadow-only implementation and preserved-data measurement. Phase S4's
internal, unpublished candidate-formation/narrowing implementation and its
cancellation lifecycle correction are approved. Persistence and public
behavior remain unimplemented.**

The implemented Phase 7 and Schema 8 recording-search contracts remain
authoritative in
[Object-Disappearance Recording Search](object-disappearance-recording-search.md)
and
[Object-Presence Classification](object-presence-classification.md). Existing
`PRESENT`, `ABSENT`, and `INDETERMINATE` meanings, persisted evidence, terminal
states, APIs, acquisition behavior, and strict-reopen rules are unchanged.

This design follows the completed S1/S2 work in
[Presence-First Classifier Design](presence-first-classifier-s1.md), including
the run-scoped reference preparation, approved PRESENT-only fast path,
preserved-data evaluation, disagreement RCA, and conservative wider scene
guard. It changes the planned direction after S2; it does not rewrite those
completed records.

The local S3 implementation adds only a pure internal evidence evaluator,
process-local counters, and best-effort diagnostics at the existing successor
classification boundary. S4 carries the resulting band as a private,
process-local observation hint and derives bounded candidates at the execution
boundary; neither is serialized. It does not add persisted records, terminal
behavior, API/UI output, or a public candidate interval. The
preserved-data report is produced by
`tools/measure_search_evidence_s3.py`; its labels are historical/preserved
classifier results and it makes no disappearance-recall claim. Independent
manual event-window labels remain mandatory for S6. The S4 preserved-data
sanity report is produced by `tools/measure_candidate_search_s4.py`; it groups
available preserved rows for deterministic interval checks only and likewise
makes no disappearance-recall or accuracy claim.

The product remains a human-review aid. It does not determine theft or
ownership, identify a person, or continuously track a person.

## 1. Problem statement and optimization priority

The current successor search makes exact per-frame classification the gate for
search progress. A coarse candidate bracket requires adjacent `PRESENT` then
`ABSENT` observations. Binary narrowing requires the same endpoint states and
stops when a midpoint is `INDETERMINATE`. This is safe for definitive visual
claims, but it couples two different questions:

1. **Classification:** what definitive visual state can this individual frame
   support?
2. **Search:** where did reference-object evidence materially change enough to
   justify closer inspection?

The primary Phase 7 search objective is now:

> Return a bounded candidate interval that contains the selected object's
> disappearance or material change whenever the available recording supports
> such an interval, even when individual frames remain `INDETERMINATE`.

The priority order is:

1. avoid completely missing a supported disappearance/change event;
2. contain the event inside a truthful candidate interval;
3. reduce the interval width without excluding the event;
4. obtain definitive per-frame states where they add verification value; and
5. reduce expensive classifier work after the recall contract is met.

A wider candidate interval is preferable to no candidate when the wider window
truthfully represents uncertainty. That preference never permits weak change
evidence to become `ABSENT`, `FOUND`, theft, loss, or identity evidence.

## 2. Current implementation constraints

Repository inspection establishes these current boundaries:

- coarse acquisition and observation ordering are already bounded and use
  actual selected-frame time;
- `SuccessorCoarseClassificationService` creates a candidate only from an
  adjacent `PRESENT -> ABSENT` pair;
- `SuccessorExecutionService` invokes the full v3 path for every non-fast-
  PRESENT candidate and stops coarse search at the first state bracket;
- `SuccessorBinaryNarrowingService` requires `PRESENT` and `ABSENT` endpoints,
  moves its bounds only on those states, and terminates on an
  `INDETERMINATE` midpoint;
- current `FOUND` and Phase 8 eligibility require verified disappearance
  evidence, not merely a change candidate; and
- current Schema 8 cannot be silently reinterpreted to persist a candidate-only
  interval as if it were a confirmed disappearance.

The midpoint selection, actual-frame ordering, gap checks, cancellation,
iteration limit, no-progress handling, acquisition service, and final v3
classifier remain useful. The state-only bracket predicate is the part that
cannot be reused as-is.

## 3. Target architecture

```text
confirmed reference JPEG + source-pixel ROI
        |
        +--> one run-scoped reference preparation
        |
coarse target acquisition and decode
        |
        +--> Stage A: approved cheap PRESENT gate
        |       decisive hit -> classified PRESENT + strong-reference anchor
        |       non-hit      -> no negative classification inference
        |
        +--> Stage B: cheap search-evidence extraction
                fixed-support appearance / foreground / edge / change
                wider-scene normalization and stability
                relative evidence change over chronological samples
                        |
                        +--> no supported change -> continue coarse scan
                        +--> candidate change run -> candidate interval
                                                    |
                                                    v
                                      Stage C: evidence-based narrowing
                                      strong-reference vs change evidence
                                                    |
                                                    v
                                      Stage D: precise v3 verification
                                      segmentation / alignment / replacement /
                                      occlusion / scene interpretation
                                                    |
                         +--------------------------+------------------------+
                         |                                                   |
              verified visual states                              unresolved visual states
              PRESENT / ABSENT /                                  remain INDETERMINATE;
              INDETERMINATE                                       candidate is review evidence
                         |                                                   |
                         +--------------------------+------------------------+
                                                    v
                                  versioned evidence / review clip / human
                                  judgment after the required contract review
```

The final architecture is layered rather than classifier-first. Stage B is
not a weaker classifier and Stage C is not allowed to manufacture visual
states. Stage D remains the authority for expensive frame-level interpretation.

## 4. Classification state and search evidence are separate contracts

The invariant is:

```text
classification state != search evidence
```

### Observation classification

The existing closed vocabulary remains exactly:

- `PRESENT`: the applicable classifier policy supports presence;
- `ABSENT`: the applicable classifier policy supports absence; and
- `INDETERMINATE`: valid visual evidence cannot safely support either state.

Only the approved classifier policy may emit those values. A failed fast gate,
a weak search signal, a scene change, or a candidate interval never emits
`ABSENT`. Operational failures remain operational failures rather than visual
observations.

### Search evidence

Search evidence is a deterministic, policy-versioned interpretation of cheap
reference-relative measurements for one acquired frame. Initial S3 terms such
as **strong reference**, **degraded reference**, **material reference drop**,
**scene discontinuity**, and **insufficient search evidence** are internal
decision bands, not new public or persisted observation states.

A conceptual search-evidence sample binds:

- plan, target, actual selected-frame time, reference, ROI, and policy
  identities;
- whether decoding and cheap preprocessing were usable;
- fixed-support similarity, foreground retention, edge similarity, change,
  and NCC where available;
- normalized wider-background and scene-stability facts;
- comparison with the most recent usable strong-reference sample; and
- a closed reason when the sample cannot support search progression.

S3 should first keep this structure pure and run-scoped. Durable storage is a
separate versioned contract decision; existing `RawComparison` fields must not
be overloaded with different meaning merely to avoid a schema change.

An `INDETERMINATE` observation can therefore coexist with useful search
evidence. Conversely, an operationally unavailable frame has no visual state
and cannot contribute fabricated object-change evidence.

## 5. Search-signal policy

S3 should reuse current deterministic metrics before considering a new model:

- corrected fixed-support and wider-scene normalization;
- support luma similarity and NCC;
- foreground retention;
- support edge similarity and support change;
- background-change and scene-stability evidence; and
- chronological deltas from the most recent usable strong-reference sample.

The policy is banded and conjunctive, not a general-purpose learned score:

1. **Strong reference** uses the approved corrected fast-PRESENT predicate.
   This is both a safe `PRESENT` classification and a search lower anchor.
2. **Material reference drop** requires usable reference/ROI geometry and a
   meaningful loss of foreground/reference evidence. S3 must require more
   than a bare fast-gate miss: foreground loss is paired with at least one
   independent appearance, edge, or support-change degradation, or with a
   separately measured large chronological drop.
3. **Scene discontinuity** records broad camera/scene change separately from
   object-support loss. It can open a conservative review candidate but cannot
   establish object absence.
4. **Degraded reference** is evidence outside the strong band but not strong
   enough to qualify as a material drop. Adjacent degraded samples can provide
   persistence support; one degraded sample alone does not assert a transition.
5. **Insufficient search evidence** covers sparse background support,
   unavailable normalization, unusable quality, missing media, or malformed
   input. It does not move an evidence bound.

Exact delta and persistence thresholds are S3 policy values to be selected
from deterministic fixtures and labeled fresh runs. They must be versioned and
reviewed before production behavior changes. No threshold is selected in this
design document, and no current accuracy number is implied.

## 6. Candidate interval formation

Candidate formation is a chronological, explainable state machine over search
evidence, not over visual classification state.

1. Seed the search with the confirmed reference as a strong-reference anchor.
   This is search context; it does not fabricate a recording observation.
2. Maintain the most recent usable strong-reference sample.
3. Open a candidate run at the first later material reference drop or scene
   discontinuity.
4. Qualify the run when a later adjacent usable sample remains degraded or
   materially changed. An existing v3 `ABSENT` also qualifies the run, but is
   not required for candidate formation.
5. If the search ends after only one material-drop sample, retain a provisional
   tail candidate for verification instead of silently missing it.
6. Set the conservative core interval from the last strong-reference actual
   frame time to the first material-drop actual frame time. Later samples
   confirm the interval; they do not move its right edge later and hide the
   earliest evidence change.
7. A recording gap or operationally unavailable span between the anchors
   widens the interval across the unobserved region and marks coverage
   incomplete. It can never support `ABSENT`, `NOT_FOUND`, or a precise event
   time.
8. Recovery to strong-reference evidence closes the change run but does not
   erase it. Temporary occlusion, movement, or a reversible scene change may
   still be useful review evidence.
9. Merge overlapping candidate runs. Keep disjoint runs in chronological
   order and verify the earliest qualifying run first; if verification rejects
   it as the target event, proceed to the next within a bounded policy limit.

Examples:

```text
classification:  PRESENT  INDETERMINATE  INDETERMINATE  ABSENT
search evidence: strong   material-drop  degraded       material-drop
candidate:        [last strong ---------- first material drop]
```

```text
classification:  PRESENT  INDETERMINATE  INDETERMINATE  INDETERMINATE
search evidence: strong   material-drop  degraded       degraded
candidate:        [last strong ---------- first material drop]
```

The second sequence produces a search candidate even though it contains no
`ABSENT`. Its frames remain `INDETERMINATE` until and unless the classifier can
resolve them.

## 7. Narrowing semantics

The current binary narrowing implementation cannot be reused unchanged. Its
bounded mechanics should be retained, while its predicate becomes
search-evidence based:

- the left bound is a usable strong-reference sample;
- the right bound is a qualified material-drop/change sample;
- a strong-reference midpoint moves the left bound right;
- a material-drop midpoint moves the right bound left;
- an `INDETERMINATE` classification does not stop narrowing when its search
  evidence is usable and falls in one of those bands;
- degraded but non-decisive or insufficient search evidence moves neither
  bound and returns the current conservative interval rather than discarding
  the candidate;
- a gap, operational failure, duplicate actual frame, or no-progress midpoint
  preserves/widens the honest interval and records why precision stopped; and
- nonmonotonic evidence stops bisection and retains the enclosing coarse
  interval for Stage D instead of selecting a falsely precise boundary.

This is bounded evidence bisection, not a claim that visibility is globally
monotonic. The existing actual-frame-time, segment coverage, cancellation,
iteration, and no-progress safeguards remain required.

Stage D may classify selected endpoints and nearby frames through v3. Only a
verified `PRESENT -> ABSENT` result may enter the existing `FOUND` publication
path. A candidate-only interval that remains visually indeterminate must not be
adapted into the current `SuccessorBinaryNarrowingResult`, whose type truthfully
requires those states.

## 8. Role of the approved fast PRESENT path

The corrected S2-1 fast path is preserved without threshold relaxation.

- A successful hit remains a real `PRESENT` observation.
- The same hit supplies the strongest cheap search anchor.
- It continues to avoid candidate segmentation, candidate-mask generation,
  alignment search, and full B4/v3 classification.
- A non-hit means only "not safely fast PRESENT." Stage B evaluates separate
  search evidence; it cannot infer change or absence from the miss alone.
- Reference preparation remains immutable, run-scoped, identity-bound, and
  free of global mutable cache state.

Fast PRESENT is an optimization and a trustworthy anchor, not the correctness
mechanism for candidate detection.

## 9. Decision on fast ABSENT

Fast ABSENT is not a required S3-S7 milestone.

Candidate-first search can locate and narrow useful change intervals without
asserting absence at the cheap tier. The existing v3 policy remains the
authority for `ABSENT`, and human review remains authoritative for the event's
meaning. Avoiding a fast-ABSENT project reduces false-absence risk and keeps
effort focused on missed-event recall.

Fast ABSENT may be reconsidered only as an optional post-validation
optimization if S6 measurements show that Stage D absence verification is a
dominant cost and labeled evidence supports the existing false-absence safety
contract. It is not a dependency for any phase in this plan.

## 10. Role of the existing v3 classifier

The v3 classifier remains valuable as a medium/slow precise verifier rather
than the mandatory first response to every coarse fast-gate miss.

Reserve its expensive operations primarily for:

- candidate-interval endpoints and selected neighboring frames;
- evidence-narrowing points whose cheap signal is genuinely ambiguous and
  where v3 can change the next action;
- distinguishing object movement, replacement, and partial occlusion from
  stable support loss;
- resolving bounded local translation/rotation through alignment; and
- producing truthful final `PRESENT`, `ABSENT`, or `INDETERMINATE` evidence.

Candidate segmentation, mask diagnostics, alignment candidate search,
replacement detection, occlusion logic, and detailed scene interpretation
belong in this tier. A v3 failure remains an operational failure. A v3
`INDETERMINATE` does not erase a previously supported search candidate, but it
prevents that candidate from becoming a definitive absence claim.

Rollout must be staged. S3 initially computes search evidence in shadow while
the current v3 path remains intact. S5 may skip v3 on noncandidate coarse
samples only after S3/S4 evidence and a reviewed durable-contract plan prove
that search behavior and strict reopen remain trustworthy.

## 11. Failure and safety behavior

| Condition | Search-evidence action | Classification/terminal constraint |
| --- | --- | --- |
| Occlusion | May create degraded/change evidence and a candidate | Never `ABSENT` solely from occlusion; v3/human review resolves it |
| Object movement | Candidate-worthy material change; preserve wider interval | Alignment may recover `PRESENT`; movement is not disappearance |
| Object replacement | Candidate-worthy material change | Replacement remains `INDETERMINATE` unless existing policy safely resolves otherwise |
| Lighting/exposure change | Reuse normalization; persistent unresolved degradation may create a candidate | Never absence solely from exposure |
| Camera movement/scene cut | Record scene discontinuity and widen candidate | No object-absence claim; precise timing may be unavailable |
| Blur/compression | Degraded or insufficient search evidence | No bound movement without usable evidence |
| Missing recording/gap | Widen across the unobserved span and mark incomplete coverage | Operational/coverage limitation; never visual absence or NOT_FOUND |
| Insufficient background area | Insufficient search evidence | Delegate/verify; no cheap negative decision |
| Segmentation failure | Stage B remains independent if cheap evidence exists | Stage D operational failure; never converted to `INDETERMINATE` or `ABSENT` |
| Alignment failure/ambiguity | Candidate remains available for review | Final state remains `INDETERMINATE` if unresolved |
| Long `INDETERMINATE` run | Continue if search evidence shows a persistent drop | Candidate interval allowed; no absence assertion |
| Multiple change events | Keep bounded chronological candidates; verify earliest then next | Do not collapse distinct events or claim theft/loss |
| Conflicting/nonmonotonic evidence | Stop precision work and retain enclosing interval | Final visual result is conservative; no fabricated monotonic boundary |
| Persistence/strict-reopen failure | No publication or terminal reinterpretation | Fail closed under the existing authority rules |

Candidate detection means only that a bounded portion of the recording merits
closer review. It does not establish what happened or who caused it.

## 12. Resource-efficiency strategy

The expected work is separated by stage:

| Work | Coarse scan | Evidence narrowing | Precise verification |
| --- | --- | --- | --- |
| Recording acquisition and JPEG decode | Required for selected targets | Required for bounded midpoint targets | Reuse acquired evidence where authoritative; otherwise bounded |
| Run-scoped reference preparation | Once per bound reference context | Reused | Reused |
| Cheap luma/edge/foreground/scene metrics | Every usable target | Every usable midpoint | Available as context |
| Corrected fast PRESENT gate | Every eligible target | Every eligible midpoint | May terminate a frame cheaply |
| Candidate segmentation/model inference | Avoid by default | Avoid by default | Run on selected verification frames |
| Alignment comparisons | None | None for the search predicate | Run only when v3 verification needs them |
| Full v3 classification | Shadow baseline during S3; later selective | Only when it can change the action | Required for definitive non-fast states and ABSENT verification |

Decoding remains a real cost and is not described as avoided. Narrowing replay
is bounded by the reviewed policy. Performance is secondary to candidate
recall, and this design invents no latency or invocation-rate target.

## 13. Compatibility, evidence, and rollout boundary

- Existing schemas 1-8, observation values, classifier identities, evidence,
  APIs, UI, acquisition, terminal publication, and legacy reopen remain
  unchanged until a separately reviewed implementation changes them.
- S3 begins with a pure, run-scoped search-evidence calculation and shadow
  comparison. It must not affect public outcomes.
- Candidate-only evidence cannot be persisted by overloading `ABSENT`,
  `FOUND`, `last_present`, `first_absent`, `RawComparison`, or the current
  narrowing result.
- Before candidate-only intervals affect terminal output, restart recovery, or
  Phase 8 eligibility, the project must approve a versioned persistence and
  identity contract that strictly reopens the signal policy, ordered samples,
  interval derivation, limitations, and verifier result.
- A verified v3 `PRESENT -> ABSENT` bracket may continue through the existing
  terminal path without reinterpreting old records.
- If Stage D remains `INDETERMINATE`, current runtime behavior remains
  `INCONCLUSIVE`. Exposing a candidate interval and review clip in that case is
  desirable, but requires the explicit contract decision above; it must not be
  smuggled through FOUND-only Phase 8 eligibility.
- Old runs remain byte-for-value reopenable under their recorded policies.

## 14. Future phase plan

### S3 — search-evidence contract and signal

S3 is implemented locally as shadow-only behavior:

- the pure internal search-evidence sample and versioned signal family are
  defined in `recording_search_successor_search_evidence.py`;
- existing reference-relative cheap metrics drive deterministic strong,
  material-drop, usable-ambiguous, and insufficient bands;
- the evaluator reuses the classifier's minimum baseline-support and ROI
  prerequisites, returning insufficient evidence before any directional drop
  decision when support is undersized;
- scene-only discontinuity is explicitly prevented from becoming a directional
  material drop;
- the existing successor classifier records bounded process-local counters and
  best-effort diagnostics without changing its observation result; and
- `tools/measure_search_evidence_s3.py` reports preserved replay distribution
  separately from classifier state.

S3 does not choose candidate persistence counts, form intervals, move bounds,
or claim recall. S4 currently uses a provisional repeated-material persistence
rule and an internal maximum of eight ordered candidates; both remain review
inputs rather than durable/public contracts. Independent event-window labels
remain S6 work.

### S4 — candidate interval formation and evidence narrowing

- implemented locally as a pure chronological candidate state machine over
  only the four approved S3 bands;
- opens a provisional interval at the first material drop after a strong
  anchor, qualifies it only after repeated material evidence, and preserves it
  after strong recovery;
- reuses actual frame ordering, bounded midpoint iteration, cancellation,
  coverage checks, no-progress protection, and acquisition error semantics in
  the internal evidence-narrowing adapter;
- tolerates `INDETERMINATE` classifier states only when independent S3
  evidence is directional, while ambiguous/insufficient evidence leaves the
  enclosing interval unchanged;
- records recording gaps and nonmonotonic evidence as safe, unpublished
  uncertainty rather than fabricating precision;
- keeps deterministic chronological candidates, suppresses duplicate drops,
  and retains an explicit overflow count/identity list when the bounded
  internal candidate limit is exceeded; and
- deliberately leaves candidate-only intervals out of the Schema 8 terminal
  and evidence projections. The implementation and cancellation correction
  passed Initial and Follow-up Review; any production/persistence cutover
  remains a later reviewed phase.

### S5 — precise verification and slow-path rationalization

- apply v3 primarily to candidate endpoints, nearby frames, and ambiguous
  narrowing points;
- measure which segmentation, alignment, replacement, occlusion, and scene
  checks change decisions;
- remove no existing mechanism without comparative evidence;
- decide the minimal versioned persistence/terminal/Phase 8 change for a
  candidate that remains visually indeterminate; and
- retain current behavior until that contract receives approval.

### S6 — fresh real-run/NVR end-to-end validation

- execute newly labeled recording searches using current code and the proposed
  candidate path;
- prioritize event containment, candidate width, and complete misses;
- collect independent manual event windows and confounder labels;
- measure classifier and resource behavior without inventing targets; and
- include disappearance, movement, replacement, occlusion, lighting, camera
  movement, compression, gaps, and operational failures where available.

### S7 — integration and closure

- cut over coarse search and narrowing only after review of S3-S6 evidence;
- implement any approved versioned evidence/terminal/Phase 8 boundary;
- verify strict reopen, publication, review-clip generation, browser projection,
  cancellation, restart, compatibility, and regression coverage;
- preserve human final judgment and all legacy reads; and
- close or explicitly defer remaining slow-path and optional fast-ABSENT work.

## 15. Measurement and acceptance strategy

Primary evaluation measures are:

- disappearance/change candidate recall;
- whether the manually established event window is contained in the returned
  candidate interval;
- candidate interval width;
- complete missed-event count; and
- false exclusion, where narrowing removes part of the labeled event window.

Secondary measures are:

- `PRESENT` / `ABSENT` / `INDETERMINATE` distribution for frames that are
  actually classified;
- candidate count and provisional/qualified candidate distribution;
- full-v3 invocation rate;
- candidate and baseline segmentation invocation counts;
- alignment invocation and comparison counts;
- decode, cheap-signal, narrowing, verification, and total search time; and
- corrected fast-PRESENT rate.

Fresh-run ground truth must be created independently of the search result. For
each validation run, a human reviewer records the last clearly reference-like
frame, the first clearly disappeared or materially changed frame, and relevant
confounders from the source recording or retained review clip. The annotation
uses actual decoded-frame time within the available request-relative timing
contract, binds the source/run digest, and records uncertainty rather than
inventing exact physical UTC. When practical, annotation is completed before
the candidate output is shown. Disagreements or uncertain labels remain a
separate review category rather than being forced into success/failure.

No existing preserved corpus supplies complete human event-window ground
truth. It can test determinism and regressions, but it cannot establish current
candidate recall.

## 16. Open design questions for later phases

The approved architecture now exercises S3 in shadow mode and S4 in an
unpublished internal path. Later phase reviews must decide or constrain:

1. whether the proposed conjunction for material reference drop is the right
   starting policy family before S3 selects numeric deltas;
2. the maximum ordered candidate count and whether a tail-only material drop
   always enters verification;
3. when a candidate-only interval must become durable, and the minimum new
   versioned evidence/identity shape required for strict reopen;
4. whether candidate-only `INCONCLUSIVE` results should become eligible for a
   review clip through a future Phase 8 contract, without weakening FOUND; and
5. the exact production cutover gate after shadow S3 and pure S4 validation.

These are review decisions, not permission to implement schema, API, terminal,
or UI changes in S3.

## 17. Review recommendation

The S3 Initial Review was approved before its implementation and push. S4's
Initial Review approved its candidate/narrowing semantics after one required
cancellation correction; Follow-up Review approved that lifecycle correction
and the complete unpublished S4 implementation. No further S4 review is
required unless a new material regression appears. The current public/terminal
contracts remain unchanged, and S5 or any durable/public candidate boundary
requires its own phase-specific review.
