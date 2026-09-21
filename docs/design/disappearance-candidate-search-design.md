# Disappearance-Candidate-First Search Design

## Status and authority

**Status: the architecture was approved at Initial Review. Phase S3 has a
local shadow-only implementation and preserved-data measurement. Phase S4's
internal, unpublished candidate-formation/narrowing implementation and its
cancellation lifecycle correction are approved. Phase S5 now adds internal
candidate-local verification and measured slow-path accounting; persistence and
public behavior remain unimplemented.**

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
cannot be reused as-is. S5 reuses the actual observations and S4 midpoint
samples already produced by those boundaries rather than inventing a second
classifier state machine.

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

### 7.1 S5 candidate-local verification contract

The internal S5 verifier is implemented in
`recording_search_successor_verification.py` and is called only after S4 has
formed candidates. It consumes coarse observations plus any actual-frame S4
midpoint samples, merges duplicate observation IDs, and orders them by the
decoded frame timestamp. Different observations are merged only when their
frame and run/plan, acquisition, authority, ROI, policy, ordinal, digest, and
classifier-result provenance agree; timestamp equality alone is never an
identity proof. Runtime verification never treats a requested timestamp as an
actual decoded-frame timestamp. It
returns a process-local report with one of:

- `VERIFIED`: only a qualified candidate with an observed `PRESENT` anchor and
  `ABSENT` drop whose actual decoded frame times are present, strictly ordered,
  and bound inside the candidate interval, with no recovery or
  operational/gap caveat;
- `PARTIAL`: useful directional evidence exists, but the candidate is
  provisional, recovered, ambiguous, or otherwise not safe to call a
  disappearance;
- `UNRESOLVED`: coverage, operational, or evidence limitations prevent a safe
  conclusion; and
- `CANCELLED`: lifecycle cancellation occurred before the report completed.

These labels are internal verification dispositions, not classifier states and
not terminal states. Missing, equal, reversed, or out-of-interval actual frame
times are `UNRESOLVED`, not `VERIFIED`. A provisional tail never becomes `VERIFIED` merely
because its two coarse endpoints happen to be visually different. Recovery
remains ambiguous, gap/nonmonotonic intervals are retained or widened, and
operational failures never become `ABSENT`. Multiple retained candidates are
processed in chronological order; overflow is reported as an explicit
measurement fact rather than silently discarded.

S5 deliberately reuses v3 results already present on the selected observations
and S4 midpoint samples. It therefore adds zero redundant v3 invocations in
the current path while measuring the reused precise work, actual frame count,
alignment work, replacement/occlusion evidence, and verification duration. A
future implementation may add bounded new v3 calls only when replay evidence
shows that they change the candidate disposition. Cancellation returns through
the existing `INTERRUPTED` publication path, which remains idempotent and
exactly-once; S5 never publishes an internal report.

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

The current S5 implementation does not yet skip the existing coarse v3 path;
it records that work as compatibility behavior and avoids a speculative
runtime cutover.

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

S5's preserved replay harness (`tools/measure_candidate_verification_s5.py`)
reports actual local replay v3 invocations, candidate-local reused v3 work,
candidate segmentation/model-predictor calls, alignment invocation and
comparison counts, and verifier duration. These are measured observations, not
acceptance targets.

The current component disposition is:

| Component | S5 disposition | Evidence basis |
| --- | --- | --- |
| Existing coarse v3 invocation | `COMPATIBILITY_ONLY` | Kept unchanged while the candidate path is internal |
| Candidate-local v3 re-invocation | `REDUNDANT_IN_S5_PATH` | S4/coarse actual observations are reused |
| Candidate segmentation/model inference | `REQUIRED_ONLY_FOR_SPECIFIC_CASES` | Needed when an unresolved candidate gains a new decisive frame |
| Alignment search/comparisons | `REQUIRED_ONLY_FOR_SPECIFIC_CASES` | Relevant to bounded movement/replacement cases |
| Replacement detection | `REQUIRED_ONLY_FOR_SPECIFIC_CASES` | Cannot be inferred from a stable support drop |
| Occlusion logic | `REQUIRED_ONLY_FOR_SPECIFIC_CASES` | Recovery/temporary obstruction remains ambiguous |
| Wider-scene normalization and support gates | `REQUIRED` | S3 safety prerequisite for directional evidence |
| Candidate-only persistence/terminal/API/UI | `NEEDS_MORE_DATA` | Requires fresh labeled S6 evidence and a later contract review |

No component is removed from the existing classifier based on the preserved
corpus alone.

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

S5 is implemented as an internal/process-local verification stage:

- it verifies every retained qualified, provisional, recovery, gap,
  nonmonotonic, multiple, and overflow candidate without changing the public
  terminal result;
- it reuses actual decoded-frame observations and S4 midpoint evidence, keeps
  existing classifier semantics authoritative, and preserves unresolved
  candidates instead of converting them to `ABSENT`/`FOUND`;
- it routes cancellation through the existing exactly-once `INTERRUPTED`
  lifecycle;
- it records actual-frame ordering and bounded per-candidate dispositions;
- `tools/measure_candidate_verification_s5.py` measures preserved replay v3,
  segmentation, model/predictor, alignment, and verification cost; and
- it classifies slow components using measured evidence. No speculative
  mechanism removal, schema change, public candidate projection, or Phase 8
  eligibility change is included.

The current S4 nonmonotonic rule remains authoritative: S4 stops at the
enclosing interval, and S5 reports the resulting candidate as partial or
unresolved rather than reordering or tightening it.

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

S5 additionally records candidate-local reused v3 invocations, actual local
segmentation/model calls, alignment invocation/comparison counts, replacement
and occlusion evidence, actual selected-frame counts, and verification
duration. The preserved corpus has no independent disappearance labels, so
these numbers are cost/distribution measurements only.

The current bounded preserved replay (`limit=50`) covered 17 search groups and
formed two qualified candidates. Both reused their preserved `PRESENT` to
`ABSENT` endpoint states and were internally `VERIFIED`; the verifier added no
v3, segmentation, predictor, or alignment calls and took 0.176 ms total
(0.088 ms mean in the latest run). This is a compatibility/reuse measurement, not a production
latency or accuracy claim; the rows contain no independent event-window labels
and do not carry decoded-frame UTC or full runtime provenance, so the tool uses
requested timestamps as a documented compatibility fallback; this does not
validate the runtime actual-frame VERIFIED contract. Runtime S5 observations
still preserve actual decoded frame timestamps. Of the 50 rows, 49 carried a preserved v3 state and zero
fresh local v3 replays were needed by this run; that distinction is why the
report exposes both counters.

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

The approved architecture now exercises S3 in shadow mode, S4 in an
unpublished candidate path, and S5 in an unpublished verification path. Later
phase reviews must decide or constrain:

1. whether the proposed conjunction for material reference drop is the right
   starting policy family before S3 selects numeric deltas;
2. the maximum ordered candidate count and whether a tail-only material drop
   always enters verification;
3. when a candidate-only interval must become durable, and the minimum new
   versioned evidence/identity shape required for strict reopen;
4. whether candidate-only `INCONCLUSIVE` results should become eligible for a
   review clip through a future Phase 8 contract, without weakening FOUND; and
5. which candidate-local segmentation/alignment/replacement/occlusion checks
   materially change dispositions on fresh labeled runs; and
6. the exact production cutover gate after S3-S5 validation.

These are review decisions, not permission to implement schema, API, terminal,
or UI changes in S3.

## 17. Review recommendation

The S3 Initial Review was approved before its implementation and push. S4's
Initial Review approved its candidate/narrowing semantics after one required
cancellation correction; Follow-up Review approved that lifecycle correction
and the complete unpublished S4 implementation. No further S4 review is
required unless a new material regression appears. S5 is implemented and
tested as an internal/process-local stage, but an Initial Review is recommended
before any S5 production cutover or new v3 invocation policy. The current
public/terminal contracts remain unchanged, and any durable/public candidate
boundary requires its own phase-specific review.
