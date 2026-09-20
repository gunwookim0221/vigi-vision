# Presence-first classifier — Phase S2-2 measurement report

Date: 2026-09-20

S2-1 baseline: `918e58f` (`Implement S2-1 PRESENT-only presence fast path`)

Status: measurement-only; no S2-2 production behavior was changed.

## Scope and method

This evaluation measures the committed S2-1 PRESENT-only gate against the
existing B3/v3 `classify_decoded_images` boundary. It does not add an ABSENT
fast path, tune thresholds, add a model, alter alignment, or change acquisition
or publication behavior.

The harness is `tools/measure_presence_s2_2.py`. It reuses the deterministic
32x32 fixture from `tests/test_presence_first_s2.py`, constructs a fixed
baseline reference and ten candidate observations, and runs each timed case
with nine repetitions after a warm-up. The v3 side uses a deterministic
two-call predictor proxy, so model-call counts and comparator work are
measurable without a checkpoint, device, network, or persisted evidence.

Fixtures exercised:

| Fixture | Cluster | Purpose |
| --- | --- | --- |
| unchanged | stable present | positive control for fast PRESENT |
| lighting variation | exposure/quality | benign global shift |
| local background patch | exposure/quality | local scene change |
| removed with reveal | absence | object removed; S2-1 must delegate |
| partial occlusion | occlusion | partial support loss |
| replacement-like | replacement | changed object appearance |
| camera translation | scene instability | small spatial movement |
| scene patch | scene instability | local background change |
| missing reference | prerequisite | no prepared reference |
| invalid reference coverage | prerequisite | reference rejected during preparation |

No preserved repository recording replay supplied a safe paired candidate and
labelled outcome for this isolated comparison, so the available deterministic
fixture is used and that limitation is explicit. The timing is a pure-Python
32x32 comparator versus the existing deterministic classifier boundary. It is
useful for relative work accounting, not a claim of production SAM latency.

## Results

The latest harness run produced:

| Metric | Result |
| --- | ---: |
| Total observations | 10 |
| Fast-path evaluations | 8 |
| Fast PRESENT hits | 2 |
| Fast PRESENT hit rate (evaluated) | 25.0% |
| Delegated cases | 8 |
| Delegation rate (all observations) | 80.0% |
| Candidate segmentation calls avoided | 2 |
| Full classifier mask calls avoided | 4 |
| Alignment comparisons avoided | 490 |
| Fast gate mean / median | 0.876 ms / 0.837 ms |
| Delegated v3 mean / median | 81.403 ms / 78.034 ms |

The two fast hits were `unchanged` and `lighting_variation`. Both agreed with
v3 `PRESENT`; fast PRESENT disagreements were 0 and false-PRESENT cases were
none. The eight non-hit observations were delegated, including the removed
object; S2-1 made no ABSENT decision. The delegated clusters were one local
background/exposure change, one ABSENT candidate, one partial occlusion, one
replacement-like candidate, two scene-instability cases, and two prerequisite
failures.

Per-observation v3 comparison:

| Observation | Fast route/result | Existing v3 outcome |
| --- | --- | --- |
| unchanged | fast PRESENT | PRESENT |
| lighting variation | fast PRESENT | PRESENT |
| local background patch | delegated | INDETERMINATE |
| removed with reveal | delegated | ABSENT |
| partial occlusion | delegated | INDETERMINATE |
| replacement-like | delegated | INDETERMINATE |
| camera translation | delegated | PRESENT |
| scene patch | delegated | INDETERMINATE |
| missing reference | prerequisite delegation | PRESENT |
| invalid reference coverage | prerequisite delegation | PRESENT |

## Interpretation and recommendation

S2-1 is conservative on this fixture set: it only short-circuits the two
stable/benign PRESENT cases, while absence, ambiguity, occlusion, replacement,
scene instability, and missing prerequisites remain on the existing path. No
false PRESENT was observed. On each fast hit, the prepared reference is reused
and the candidate segmentation/model inference, candidate mask generation,
translation/rotation alignment, and slow B4 classifier call are not entered.
The measured saving is two candidate segmentation calls (four total predictor
calls including the baseline-side calls that the full boundary would perform)
and 490 alignment-comparison iterations in this deterministic run. The
diagnostic counter is a comparison-iteration count, not a claim of 490 process
invocations.

Fast hits still perform acquisition-side decoding before this harness boundary,
ROI extraction, luma conversion, fixed-support luma/NCC/edge/foreground/change
metrics, and the stability checks required by the S2-1 gate. The measurement
therefore supports a meaningful classifier-side saving, not elimination of
the upstream decode or gate work.

Recommendation: **KEEP_PRESENT_ONLY_AND_COLLECT_MORE_DATA**.

The evidence does not justify `READY_TO_EVALUATE_FAST_ABSENT`: it has no real
paired production replay, only ten synthetic observations, and S2-1 intentionally
does not evaluate fast ABSENT. It also does not indicate
`FIX_S2_1_BEFORE_CONTINUING`, because every fast PRESENT agreed with v3. Gather
representative paired recordings and labeled ambiguity/absence cases before
considering S2-3; do not infer ABSENT behavior from this report.

## Reproduction and changed files

Run from the repository root:

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m ruff check tools/measure_presence_s2_2.py
.venv\Scripts\python.exe tools/measure_presence_s2_2.py
```

S2-2 leaves these two files uncommitted by design:

- `tools/measure_presence_s2_2.py` — deterministic measurement harness.
- `docs/design/presence-first-classifier-s2-2-report.md` — this report.

The worktree otherwise contains the committed S2-1 baseline and has not been
pushed.
