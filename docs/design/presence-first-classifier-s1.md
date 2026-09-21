# Phase S1 Presence-First Object Classifier Design

## Status and authority

**Status: S1 remains the classifier design baseline. Phase S2 completed the
run-scoped PRESENT-only optimization, synthetic and preserved-data measurement,
disagreement RCA, and approved conservative scene-guard correction. Local S3
shadow work now computes separate reference-relative search evidence and
bounded process-local diagnostics; it adds no schema, public evidence, API,
UI, migration, or publication change. Every non-fast-hit case still delegates to
the existing Schema 8/v3 classifier unchanged, and there is no fast ABSENT
path. S4's internal candidate formation/narrowing and S5's internal
candidate-local verification are now implemented and approved; future S6-S7
work follows the separate
[disappearance-candidate-first search design](disappearance-candidate-search-design.md).**

This document records the implementation design for simplifying new successor
runs. It does not reinterpret legacy Schema 5--7 observations or existing
Schema 8 evidence. The implemented contracts remain authoritative in
[Phase 7B Recording-Probe Object-Presence Classification](object-presence-classification.md)
and
[Phase 7 Object-Disappearance Recording Search MVP](object-disappearance-recording-search.md)
until a later implementation phase adopts a new versioned classifier policy.
S2-1 uses the already approved successor policy and evidence shape; its
process-local reference context and bounded counters are not durable identity.

The design keeps the product question narrow: whether the selected object is
visually present in its fixed source-pixel ROI. It does not identify a person,
determine theft or ownership, track a person, or replace final human review.

## 1. Current-state classifier flow

The current successor path is not a simple mask-IoU classifier. The actual
production-shaped path is:

```text
strict Phase 6 confirmation
  -> decode and integrity-check the confirmed baseline JPEG
  -> build SuccessorClassificationAuthority (baseline RGB, dimensions, ROI,
     confirmation identities)
  -> make one optional run-scoped reference-only B4 call
  -> retain the validated baseline support and fixed-support descriptors
  -> plan and acquire an anchor/coarse/midpoint target
  -> choose the target JPEG, or a bounded neighboring fallback after
     occlusion/decode failure
  -> integrity-check and decode the candidate JPEG to source-sized RGB
  -> evaluate the fixed-support PRESENT fast gate
  -> return PRESENT immediately on a decisive fast hit
  -> otherwise delegate to the existing B4 classifier
  -> spawn one bounded B4 classifier process
  -> verify/load EfficientSAM-Ti
  -> infer a source-sized center-point mask for the baseline image
  -> infer a source-sized center-point mask for the candidate image
  -> validate both masks and clip them to the fixed ROI
  -> use the baseline mask as immutable object support; retain the candidate
     mask as diagnostic evidence
  -> derive fixed support and background/stability regions
  -> normalize candidate luma against local background
  -> compute fixed-coordinate support similarity, NCC, edge similarity,
     change, foreground retention, and background change
  -> evaluate bounded translation/rotation alignment and recompute support
     signals when the alignment is confident
  -> evaluate scene-stability, registration, replacement, occlusion,
     PRESENT, and empty-background ABSENT gates
  -> return PRESENT / ABSENT / INDETERMINATE plus RawComparison
  -> persist the observation and classifier evidence in successor evidence
  -> stop coarse sampling at the first adjacent PRESENT -> ABSENT bracket
  -> classify binary midpoints through the same path
  -> atomically publish FOUND / NOT_FOUND / INCONCLUSIVE and evidence
```

### Reference and candidate preparation

`SuccessorExecutionService.prepare()` reads and decodes the immutable confirmed
JPEG once and constructs the authority object. S2-1 then makes one optional
run-scoped reference-only B4 call through the existing process boundary and
retains the validated baseline mask plus fixed-support descriptors in memory.
If preparation is unavailable or invalid, the authority remains usable and the
existing slow path is used. For every candidate,
`SuccessorCoarseClassificationService` verifies acquisition facts, decodes the
candidate JPEG, and calls the classifier with the full baseline RGB image,
candidate RGB image, source dimensions, fixed ROI, and correlation ID.

The fast gate evaluates only fixed-support luma, NCC, edge, foreground,
background-change, and scene-stability signals. A decisive match returns
`PRESENT` before candidate segmentation or alignment. A failed, ambiguous, or
unavailable fast evaluation delegates to the existing classifier, including all
`ABSENT` decisions. Process-local counters and bounded log facts are diagnostic
only.

On the delegated slow path, `classify_decoded_images()` calls
`predict_masks_for_images()`, which invokes the predictor once for the baseline
and once for the candidate. The fast path performs no candidate segmentation or
alignment. The spawned-worker boundary also performs model readiness and request
reconstruction per slow-path invocation.

The successor candidate B4 invocation uses a separate bounded 60-second
inference budget because measured CPU EfficientSAM execution can exceed the
legacy 30-second ceiling. Worker startup and run-scoped reference preparation
retain their existing 30-second budgets; the legacy Schema 5--7 classifier
policy remains unchanged. This is an execution-policy correction only: it
does not alter thresholds, segmentation geometry, retry behavior, evidence
schemas, or terminal outcome mapping. The existing successor cancellation
authority is propagated through the B4 process wait/cleanup boundary and is
rechecked before any normal terminal publication, so cancellation retains
precedence over a late classifier result. Successor evidence frames are
materialized with an in-memory non-authoritative payload and made visible as a
manifest only after that same guard; cancelled runs discard the staged payload
and therefore cannot expose normal terminal evidence alongside an
`INTERRUPTED` terminal. Reopen and public evidence reads also require the
committed terminal to confirm the manifest status and reason; recovered or
failed terminals fail closed even if an earlier manifest exists on disk.

### Comparison and decision behavior

New successor runs use `baseline_support_v3`. The baseline mask defines target
support. The independently generated candidate mask cannot establish target
identity and does not gate the successor PRESENT or ABSENT decisions; it is
diagnostic. The slow comparator nevertheless requires both inference calls
before entering the baseline-support comparison; the S2-1 fast gate has already
established a conservative PRESENT result without entering that comparator.

The comparator calculates both fixed-coordinate signals and a bounded local
rigid alignment. The current alignment considers translations limited by ROI
fraction/pixel caps and rotations of `-10`, `-5`, `0`, `5`, and `10` degrees.
Current diagnostics exercise 49 translation candidates and five rotations,
or 245 comparisons, for a representative search. Confident alignment is
required for PRESENT, but deliberately not for ABSENT. Scene-stability and a
registration-motion veto protect both decisions. Replacement, partial
occlusion, conflicting evidence, broad scene change, insufficient support, and
ambiguous positive identity remain INDETERMINATE.

The policy currently combines these mechanisms:

- baseline-support luma similarity and NCC;
- support edge similarity and change ratio;
- local-background foreground retention;
- fixed-background scene-stability counts and veto reasons;
- bounded translation/rotation alignment, overlap, score, and margin;
- candidate-mask coverage/IoU diagnostics;
- whole-ROI NCC diagnostics;
- PRESENT, empty-background ABSENT, replacement, occlusion, conflict, and
  registration vetoes; and
- a strict evidence matrix with additive decision booleans and reasons.

### Search, evidence, and terminal integration

Acquisition and classifier failures are target-local operational states, never
ABSENT. A valid visual INDETERMINATE may cause bounded neighboring-frame
fallback when the closed reason is ROI occlusion. Coarse classification orders
observations by actual frame time and creates a bracket only from adjacent
PRESENT then ABSENT observations. Narrowing uses the same classifier and stops
safely on an indeterminate or operational midpoint. Evidence publication
stores immutable baseline/candidate JPEGs, ROI crops, classifier policy
identity, outcome, reason, comparison measurements, timing, and acquisition
provenance before terminal publication. Final human review remains downstream.

## 2. Problems motivating simplification

The current logic contains useful safeguards, but its execution order obscures
which work contributes to a decision:

1. The unchanged baseline is segmented for every candidate.
2. Candidate segmentation is paid for even though its mask is diagnostic in
   the successor policy.
3. Alignment is evaluated before knowing whether fixed-coordinate evidence is
   already decisive.
4. Presence, identity consistency, scene quality, registration, replacement,
   and occlusion are evaluated in one coupled comparison/policy pass.
5. Multiple threshold families interact, so a correction for exposure,
   revealed background, motion, or segmentation-edge behavior can affect
   unrelated cases.
6. Repeated correction tests show that removal, lighting/noise, small object
   displacement, large camera motion, partial occlusion, and replacement need
   different evidence, but the current path computes most signals for all of
   them.
7. The process boundary reports model, segmentation, and 245-candidate
   alignment work per invocation even for an obviously unchanged fixed ROI.

The simplification objective is not to weaken these safety cases. It is to
order them so cheap decisive evidence terminates first and ambiguity alone
activates the expensive identity path.

## 3. Proposed presence-first architecture

```text
run preparation
  confirmed baseline JPEG + fixed ROI
       |
       +--> validate/decode once
       +--> prepare reusable reference support once
            (validated baseline mask when available, luma/support/background
             templates, geometry, policy identity)

each acquired candidate JPEG
  -> validate/decode
  -> exact fixed ROI crop
  -> cheap quality and scene guards
       | unsafe/unusable --------------------------> INDETERMINATE
       v
  -> fixed-coordinate presence gate (no candidate model inference,
     no transform search, no embeddings)
       | clearly unchanged occupancy --------------> PRESENT
       | strongly revealed empty background -------> ABSENT
       | otherwise ---------------------------------> slow path
                                                       |
                                                       +-> candidate segmentation
                                                       +-> bounded alignment
                                                       +-> identity-consistency,
                                                           replacement, occlusion,
                                                           and similarity signals
                                                       |
                                                       +-> PRESENT / ABSENT /
                                                           INDETERMINATE
  -> existing observation, coarse/bracket, narrowing, evidence, terminal,
     and human-review boundaries
```

The cascade is asymmetric. A fast PRESENT needs strong evidence that the same
fixed support remains visually occupied in substantially the same way. A fast
ABSENT needs stronger evidence: the reference support must have disappeared,
the newly visible pixels must be consistent with the local background, and the
surrounding scene must be stable. Everything else escalates. Failure of a gate
is not evidence for its opposite result.

### Reusable reference preparation

For one run, prepare an immutable in-memory `ReferencePresenceContext` from the
already authoritative baseline bytes, dimensions, and ROI:

- exact ROI geometry and luma crop;
- one validated baseline target-support mask, generated with the existing
  verified EfficientSAM path when that mask is used;
- clipped support indices and pixel count;
- the existing adaptively dilated background/stability exclusion;
- fixed background indices and minimum valid-area facts;
- baseline support/background luma and edge descriptors; and
- the complete preprocessing/model/policy identity.

This context is derived once and never becomes a new source of authority. It is
valid only while bound to the baseline JPEG digest, ROI identity, dimensions,
and classifier policy identity. A mismatch discards it. S1 does not require
persisting it; S2 should prefer a run-scoped immutable cache so artifact formats
remain unchanged. If safe reuse across the current spawned-process boundary
would require a broad worker-lifecycle rewrite, S2 may first use a
process-isolated reference-preparation call and pass the bounded derived context
to subsequent calls. It must not silently reuse context across runs.

## 4. Fast-path semantic contract

The fast gate answers only:

> Is fixed-coordinate evidence sufficiently clear to terminate this
> observation without candidate segmentation, transform search, or semantic
> identity recognition?

It does not recognize an object category, compare a person, use a general VLM,
or prove real-world identity. Its inputs are the immutable reference context
and one decoded candidate ROI. Its output is one of `CONFIDENT_PRESENT`,
`CONFIDENT_ABSENT`, or `ESCALATE`.

### Mandatory preconditions

Before either terminal branch, all of these must hold:

- exact expected dimensions and source-pixel ROI geometry;
- valid finite reference context bound to the current confirmation and policy;
- enough validated baseline support and fixed-background pixels;
- successful deterministic luma/edge preprocessing;
- no broad scene-change or registration-motion veto from the existing
  fixed-background stability logic; and
- no non-finite, malformed, or contradictory measurement.

A failed precondition returns `ESCALATE` when the slow path could add decision
value. Corrupt input, invalid geometry, invalid reference context, or an
operational preprocessing failure stays an operational failure under the
existing contract and publishes no fabricated visual observation.

### Confident PRESENT

Return fast PRESENT only when fixed-coordinate evidence falls wholly within a
strict unchanged-occupancy band:

- the fixed scene is stable;
- baseline-support luma similarity and NCC pass the positive band;
- support edge similarity passes the positive band;
- support change stays below the positive-change maximum;
- foreground retention stays above the positive-occupancy minimum; and
- the empty-background predicate, replacement band, occlusion band, and any
  other negative/conflicting predicate are false.

S2 should initially reuse the current v3 metric definitions and current
positive thresholds where the definitions are identical. It must give the
cascade a new classifier/preprocessing policy identity; it must not claim that
old observations were produced by the cascade. A frame requiring translation
or rotation to satisfy the positive band is not fast PRESENT; it escalates.

### Confident ABSENT

This subsection preserves the original S1 safety analysis. Fast ABSENT was not
implemented in S2 and is no longer an active S3-S7 milestone; the adopted
candidate-first direction is authoritative for future planning.

Return fast ABSENT only when all existing empty-background safety evidence is
available at fixed coordinates:

- the reference support is valid and sufficiently large;
- sufficient unaffected fixed-background area remains valid;
- the surrounding scene is stable and has no registration-motion veto;
- baseline-support NCC is in the negative band;
- foreground retention is at or below the empty-support maximum;
- support change proves that the former object support did not merely remain
  unchanged;
- the revealed support has the required background/edge consistency; and
- replacement, partial-occupancy/occlusion, conflict, and quality-guard
  predicates are false.

This is a conjunction, not a score. Lack of any term returns `ESCALATE` or
INDETERMINATE; it never relaxes into ABSENT. S2 should reuse the existing v3
empty-background metric definitions and thresholds before considering new
values. If measurement shows that fast ABSENT cannot meet the false-ABSENT
safety target, S2 must ship a PRESENT-only fast gate and send every potential
absence to the existing slow path. That is a valid minimum cascade, not a
reason to weaken ABSENT.

### Escalate / INDETERMINATE

Return `ESCALATE` for small displacement, rotation, intermediate similarity,
possible replacement, partial occupancy, possible occlusion, uncertain
foreground/background separation, local change that is not clearly empty, or
any condition for which alignment or candidate segmentation can add value.

Escalation is internal and is not a fourth public state. If the slow path
cannot resolve the frame, the public result is INDETERMINATE with the existing
safe reason vocabulary. If slow-path execution fails operationally, the
existing operational state is preserved rather than converted to visual
INDETERMINATE.

## 5. Slow-path semantic contract

The slow path answers:

> Given that fixed-coordinate occupancy was not decisive, is visible content
> sufficiently consistent with the selected reference object, clearly empty,
> or still ambiguous?

Invoke it for:

- plausible small translation or rotation;
- object-like occupancy with changed shape or appearance;
- a possible replacement object;
- partial occlusion;
- uncertain foreground segmentation;
- exposure or compression change outside the fast bands;
- scene instability for which bounded registration can distinguish object
  motion from camera motion; or
- conflicting fast signals.

S2 preserves the current `baseline_support_v3` computation as the slow path as
far as practical. It should accept the prepared baseline support rather than
regenerating it. Candidate EfficientSAM inference remains a slow-path
supporting/diagnostic signal until measurement shows that it changes decisions.
Bounded alignment remains useful for PRESENT after small object displacement or
rotation. It must not turn alignment failure into ABSENT. The existing
scene-stability, replacement, occlusion, conflict, and empty-background rules
remain fail-closed.

Slow-path PRESENT requires positive identity-consistency evidence after a
confident bounded alignment. Slow-path ABSENT still requires independent strong
empty-background evidence and scene stability; alignment success is neither
necessary nor sufficient. Replacement and occlusion remain INDETERMINATE.

No embeddings, feature matcher, VLM, new segmenter, or continuous tracker is
added in S2. S3 may consider a heavier signal only for a measured ambiguity
cluster that existing alignment/segmentation cannot resolve.

## 6. Existing signal and component disposition

| Existing mechanism | S1 disposition | Reason |
| --- | --- | --- |
| Confirmation JPEG integrity, digest, dimensions, ROI identity | **KEEP IN FAST PATH** | It is the authority and geometry gate for every result. |
| One-time baseline RGB decode | **KEEP IN FAST PATH** | Required reference preparation; already performed once by successor preparation. |
| Candidate JPEG integrity/RGB decode | **KEEP IN FAST PATH** | Unavoidable before any visual decision and already target-local. |
| Exact half-open source-pixel ROI crop | **KEEP IN FAST PATH** | Fixed camera/ROI is the simplifying assumption. |
| Integer luma conversion and metric quantization | **KEEP IN FAST PATH** | Cheap, deterministic, and already evidence-compatible. |
| Baseline EfficientSAM mask | **REUSE AS SUPPORTING SIGNAL** | Generate once to define reference support; it is category-agnostic and not identity proof. |
| Candidate EfficientSAM mask | **KEEP IN SLOW PATH** | It is currently diagnostic and costs model inference; measure whether it changes slow decisions. |
| Mask area/coverage validation | **REUSE AS SUPPORTING SIGNAL** | Essential for reference-mask validity; candidate coverage is slow-path diagnostic. |
| Candidate/baseline mask IoU | **REMOVE FROM PRIMARY DECISION PATH** | Successor v3 already does not use it for PRESENT/ABSENT; retain for legacy and diagnostics. |
| Fixed baseline-support similarity/NCC/change/foreground retention | **KEEP IN FAST PATH** | These already express unchanged occupancy and empty-background evidence without candidate inference. |
| Support edge similarity | **KEEP IN FAST PATH** | Part of current positive and empty-background safeguards; retain pending measurement. |
| Fixed-background normalization and scene-stability profile | **KEEP IN FAST PATH** | Provides exposure tolerance and the mandatory ABSENT scene-safety veto. |
| Whole-ROI luma NCC | **REMOVE FROM PRIMARY DECISION PATH** | Too easily coupled to background dominance; retain only as compatible diagnostic evidence. |
| Translation/rotation alignment search | **KEEP IN SLOW PATH** | Demonstrated value for small displacement/rotation; unnecessary for obvious fixed-coordinate cases. |
| Alignment score/margin/overlap and registration veto | **KEEP IN SLOW PATH** | Required to prevent weak alignment from claiming PRESENT and to detect camera motion. |
| Replacement and occlusion heuristics | **KEEP IN SLOW PATH** | They distinguish occupied-but-not-consistent cases from safe ABSENT. |
| Broad scene-change and insufficient-stability vetoes | **KEEP IN FAST PATH** | A failed scene guard must block fast ABSENT and normally escalate. Slow path may refine but not bypass safety. |
| Additive decision booleans/reasons | **RETAIN ONLY FOR COMPATIBILITY / EVIDENCE** | Preserve auditability; simplify the internal order rather than deleting evidence in S2. |
| Spawned-process timeout, cancellation, and cleanup | **RETAIN ONLY FOR COMPATIBILITY / EVIDENCE** | Operational authority and resource safety, not a visual signal. |
| Neighboring-frame occlusion/decode fallback | **RETAIN ONLY FOR COMPATIBILITY / EVIDENCE** | Belongs to observation resolution outside the classifier cascade. |
| Coarse planning, bracket construction, and midpoint narrowing | **RETAIN ONLY FOR COMPATIBILITY / EVIDENCE** | Consume the same three public states and must remain unchanged. |
| Existing numeric thresholds as final production calibration | **UNDECIDED — requires measurement** | Reuse them initially for identical metrics, but do not claim they are optimal for stage-specific error rates. |
| Embeddings, feature matching, or a new model | **UNDECIDED — requires measurement** | No demonstrated ambiguity cluster currently justifies their cost or new failure modes. |

## 7. State transition rules

```text
PREPARE_REFERENCE
  invalid/corrupt authority ----------> operational failure; no observation
  valid context ----------------------> READY

READY + candidate
  corrupt/decode/geometry failure ----> existing operational/target-local state
  cheap guards unusable but valid ----> SLOW_PATH or INDETERMINATE
  confident unchanged occupancy ------> PRESENT
  confident empty stable support -----> ABSENT
  otherwise --------------------------> SLOW_PATH

SLOW_PATH
  aligned consistent object ----------> PRESENT
  strong independent empty background -> ABSENT
  replacement/occlusion/conflict/
  instability/insufficient evidence --> INDETERMINATE
  timeout/model/invalid output --------> existing operational state
```

The downstream transition rules do not change:

- only adjacent time-ordered PRESENT then ABSENT observations create a bracket;
- INDETERMINATE and operational states never create or bridge a bracket;
- narrowing replaces a boundary only with a visual PRESENT or ABSENT midpoint;
- an indeterminate or operational midpoint ends narrowing safely;
- NOT_FOUND still requires complete present coverage; and
- human review remains the final authority over published evidence.

## 8. Conservative ABSENT rules

ABSENT is prohibited when it depends on any of the following:

- failed, ambiguous, or unavailable alignment;
- failed or low-confidence segmentation;
- global/broad scene change or insufficient stable background;
- blur, occlusion, clipping, poor exposure, compression damage, or decode
  quality that defeats the required measurements;
- source dimension or ROI mismatch;
- a small displacement that moves the object partly outside fixed support;
- another object occupying the former support;
- a single weak similarity or mask-area score;
- model timeout, invalid output, missing checkpoint, cancellation, or any other
  operational failure; or
- absence of positive PRESENT evidence.

Fast or slow ABSENT requires positive empty-background evidence. If exposed
background cannot be distinguished from an occluder or replacement, the result
is INDETERMINATE. A future learned signal may support ABSENT only after replay
evaluation proves it lowers ambiguity without increasing false ABSENT.

## 9. Escalation and final INDETERMINATE rules

Escalate only when the slow path has a plausible discriminating signal to add.
Examples include alignment for small motion and segmentation/shape context for
replacement or occlusion. Do not invoke expensive work merely to repeat the
same fixed-coordinate metrics.

Return final INDETERMINATE when the slow path sees unstable scene, ambiguous
alignment, partial occlusion, replacement candidate, conflicting evidence,
invalid-but-structurally-completed mask evidence, insufficient comparison area,
zero variance, or insufficient visual evidence. Preserve operational failures
outside the visual vocabulary. This distinction is required by current
coarse/narrowing and terminal logic.

## 10. Binary-mask approach evaluation

The suggested white-foreground/black-background representation is useful, but
not as an exact-pixel or unconditional two-mask gate.

### Reference mask

A validated reference mask can be generated once and reused for the run. This
is the strongest part of the proposal: it removes repeated invariant baseline
inference and gives the cheap gate a stable support/background partition. It
must remain bound to the exact confirmation JPEG, ROI, dimensions, model, and
policy. The current point-prompt mask and mask guards can be reused.

### Candidate processing alternatives

| Candidate representation | Cost | Decision value | S1 conclusion |
| --- | --- | --- | --- |
| Second EfficientSAM mask | Model inference per frame | Area, tolerant IoU, center and contour change; weak correspondence/identity | Slow path only; not a cheap gate. |
| Thresholded RGB/luma difference mask | Cheap ROI CV | Highlights change but confounds exposure, shadow, compression, and camera motion | Supporting diagnostic after normalization; not ABSENT alone. |
| Reference-support occupancy against local background | Cheap ROI CV | Directly tests whether the former support still contains foreground | Preferred fast-path basis, with scene-stability and ambiguity guards. |
| Exact binary equality | Cheap | Brittle to edge noise, movement, and segmentation variation | Reject. |

If candidate segmentation is invoked, tolerant geometry should use mask area
ratio, overlap/IoU, center displacement, contour/occupancy change, and support
disappearance as separate signals rather than one exact match. No one signal
may claim ABSENT. A candidate mask that moves slightly should normally support
slow-path PRESENT after alignment; a similarly sized but differently shaped
mask should remain replacement/identity ambiguity; an empty or failed mask
must not become ABSENT without independent pixel/background evidence.

Candidate-time segmentation is the dominant reason the two-mask proposal is
not the S2 fast gate. The current code performs two model calls per candidate,
and the successor decision already treats the candidate mask as diagnostic.

## 11. Resource-efficiency strategy

### Cost classes

| Work | Current frequency | Cascade target | Relative character |
| --- | --- | --- | --- |
| Recording frame extraction/JPEG creation | Per acquired target/candidate | Unchanged | External decode/acquisition cost; outside classifier. |
| Baseline JPEG RGB decode | Once in successor preparation, then RGB copied into each worker request | Once | Required, bounded. |
| Candidate JPEG RGB decode | Per tested candidate, including fallback | Unchanged initially | Required before visual analysis. |
| ROI crop, luma, simple gradients/counts | Implicit full-ROI work per call | Per candidate, ROI only | Linear cheap CV. |
| Baseline segmentation inference | Once per candidate today | Once per run/context | Expensive model work removed from repetition. |
| Candidate segmentation inference | Once per candidate today | Escalated candidates only | Expensive model work. |
| Fixed-support metrics/stability | Per candidate | Per candidate | Linear ROI/support work and the fast gate. |
| Bounded alignment | Up to the bounded transform grid per candidate | Escalated candidates only | Multiplicative support comparisons; current diagnostics can reach 245 comparisons. |
| Embeddings/feature extraction | Not present | None in S2 | Do not add without measured value. |
| Process/model startup | Per candidate today | Reference preparation and slow calls only, subject to S2 boundary design | Material Windows/process/model cost. |

S2 should short-circuit before candidate segmentation and alignment. It should
avoid full-frame work after geometry validation when the exact ROI/support is
sufficient. It should not introduce an embedding model merely to replace cheap
deterministic signals.

### Required measurements

Record aggregate, credential-free counters/timings without changing outcome
semantics:

- total frames entering the classifier;
- reference preparation count and elapsed time;
- percentage/count resolved fast as PRESENT and ABSENT;
- percentage/count escalated;
- final slow-path PRESENT/ABSENT/INDETERMINATE counts;
- mean and percentile wall time per sampled frame, separated by stage;
- segmentation calls per run and per observation;
- alignment invocations and candidate comparisons;
- decoder calls and fallback candidate count;
- classifier timeout/failure rate;
- labeled false ABSENT, false PRESENT, and INDETERMINATE rates;
- coarse bracket agreement with the existing classifier;
- final narrowing completion and boundary agreement; and
- terminal FOUND/NOT_FOUND/INCONCLUSIVE agreement.

Do not invent targets or performance numbers in S1. S2 establishes a baseline
on deterministic fixtures and labeled replay. S4 decides acceptable thresholds
and cutover criteria before replacing the current policy.

## 12. Compatibility and migration

- PRESENT / ABSENT / INDETERMINATE meanings remain unchanged.
- Legacy schemas and `efficient-sam-ti-roi-ncc-v1` reopening remain untouched.
- Existing Schema 8 evidence remains immutable and reopens under its recorded
  policy identity.
- A cascade implementation requires a new classifier/preprocessing policy
  identity even if it reuses current numeric metric definitions.
- S2 should preserve the current `RawComparison` storage shape where truthful.
  Fast decisions can populate existing baseline-support metrics and use
  `alignment_state=not_required` with zero alignment counters. It must not
  fabricate candidate mask or alignment values. If the strict evidence model
  cannot represent a truthful fast result, stop and version the evidence shape
  explicitly; do not overload a field.
- Acquisition, target selection, fallback ordering, coarse sampling, actual
  frame-time ordering, bracket construction, narrowing, terminal publication,
  and Phase 8/human-review boundaries remain unchanged.
- Existing evidence provenance, strict reopen, atomic publication, and
  credential-free requirements remain in force.
- Reference context is an optimization, not new persisted authority. It must be
  recomputable from authoritative inputs and discarded on identity mismatch.
- Fast-path diagnostics should initially remain bounded process/run telemetry.
  Durable diagnostic additions require a separate versioned evidence decision.
- Rollout should support comparative replay of old and new policies without
  reinterpreting or overwriting old observations.

No artifact migration is required for S1. S2 must not silently use the new
policy identity for legacy runs or silently fall back to old semantics under a
new identity.

## 13. Failure-mode table

| Condition | Fast-path action | Slow/final action | ABSENT allowed? |
| --- | --- | --- | --- |
| Baseline bytes/digest/dimensions corrupt | Operational failure | No observation | No |
| Candidate decode or resolution failure | Existing target-local operational state | Existing bounded neighbor fallback where allowed | No |
| ROI invalid/out of bounds | Operational or existing invalid-frame handling | No fabricated visual state | No |
| Reference mask invalid/unavailable | Skip fast gate and delegate | Existing slow path and safe failure semantics | No |
| Candidate segmentation fails | Not invoked in fast path | Operational classifier failure, not visual absence | No |
| Scene/background unstable | Escalate | INDETERMINATE if unresolved | No |
| Blur or poor exposure defeats metrics | Escalate | INDETERMINATE if unresolved | No |
| Fixed support strongly unchanged | PRESENT | Not invoked | No absence branch |
| Fixed support strongly empty with stable background | Escalate (S2-1 has no fast ABSENT) | Existing slow ABSENT predicate remains authoritative | Yes, only through the existing slow predicate |
| Small translation/rotation | Escalate | Alignment may produce PRESENT | No solely from fast failure |
| Partial occlusion | Escalate | INDETERMINATE / bounded frame fallback | No |
| Similar or different replacement object | Escalate | INDETERMINATE unless measured identity evidence safely resolves PRESENT | No |
| Alignment fails or is ambiguous | No effect on fast ABSENT predicate beyond scene guards | INDETERMINATE unless independent empty-background evidence is complete | Not because alignment failed |
| Global camera motion | Escalate | INDETERMINATE | No |
| Conflicting PRESENT and empty-background evidence | Escalate | INDETERMINATE | No |
| Model timeout/cancellation/late result | Existing operational state; revoke authority | No observation from late work | No |
| Persistence or strict-readback failure | Existing publication failure behavior | No reinterpretation | No |
| Midpoint is INDETERMINATE | N/A | Stop narrowing safely; terminal INCONCLUSIVE as today | No |

## 14. Completed S2 boundary and adopted future direction

S2 established and approved:

- immutable run-scoped reference preparation bound to existing authority;
- exact-ROI fixed-support metrics plus the conservative wider scene guard;
- a PRESENT-only fast predicate with no fast ABSENT branch;
- delegation of every uncertain frame to the unchanged v3 slow path;
- truthful mapping into the current observation/evidence boundary;
- synthetic and preserved-data measurement; and
- correction of all three material fast-PRESENT/current-v3 disagreements.

The corrected preserved-data run produced seven safe fast PRESENT hits from 50
eligible observations. All seven replayed as current-v3 `PRESENT`; none replayed
as `ABSENT` or `INDETERMINATE`. Those observations are measurement evidence, not
an accuracy or prevalence claim.

The earlier idea of proceeding from fast PRESENT to fast ABSENT and general
slow-path cleanup is no longer the active roadmap. Fast ABSENT is deferred as
an optional post-validation optimization, not a required milestone. The
adopted future direction is documented in
[Disappearance-Candidate-First Search Design](disappearance-candidate-search-design.md):

- **S3:** define and measure cheap search evidence separately from visual state
  in shadow mode (implemented locally; no public behavior change);
- **S4:** form and narrow candidate intervals despite useful
  `INDETERMINATE` observations;
- **S5:** apply existing v3 evidence as internal candidate-local verification
  and rationalize slow work only from measured evidence (implemented without
  public/persistence changes);
- **S6:** run fresh manually labeled NVR validation centered on event
  containment and complete misses; and
- **S7:** perform reviewed integration, durable evidence/review-media changes
  where approved, and closure.

Current acquisition, fallback, coarse search, state-only narrowing, evidence,
terminal, API, UI, and legacy reopen behavior remain authoritative until a
later reviewed implementation changes them.

## 15. Continuing validation obligations

The completed S2 tests continue to protect fast-PRESENT safety, delegation,
run-scoped reuse, process boundaries, and absence of a fast-ABSENT path. Future
S3-S7 validation adds a separate search objective:

- deterministic search-signal behavior over existing metrics;
- candidate-event containment and interval width on independently labeled
  runs;
- complete missed-event count and false exclusion during narrowing;
- continuation through `INDETERMINATE` classifications when search evidence is
  usable;
- conservative widening for gaps, unusable evidence, and nonmonotonic change;
- selective v3, segmentation, and alignment invocation accounting;
- strict preservation of all definitive state and operational-failure rules;
- versioned persistence and strict reopen before candidate-only evidence can
  affect terminal output or review-media eligibility; and
- byte-for-value compatibility for legacy schemas and existing Schema 8 runs.

Lower runtime never justifies a missed labeled event or a fabricated visual
state.

## 16. Remaining measurement questions

The S2 reports answer the original fast-PRESENT safety question for the
evaluated corpus but do not provide human-labeled event-window ground truth.
The active measurement questions are now:

1. Which conjunction of foreground, similarity, edge, support-change, and
   scene evidence gives useful candidate recall without treating every benign
   variation as a transition?
2. How many adjacent degraded/change samples are needed to qualify a candidate,
   and how should a single tail sample be handled?
3. How often can evidence-based narrowing reduce a candidate window when the
   visual classifier remains `INDETERMINATE`?
4. Which v3 operations materially change verification outcomes inside candidate
   intervals?
5. What versioned evidence is minimally necessary to strictly reopen a
   candidate-only interval and make a review clip available without weakening
   `FOUND`?
6. What fraction of total decode, cheap-signal, model, alignment, and cleanup
   cost remains after v3 is restricted to candidate verification?

These questions drive S3-S6 measurement. They do not authorize schema, API,
terminal, or UI changes without separate review.
