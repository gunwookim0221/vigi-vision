# S2-1 PRESENT-only disagreement RCA and correction

Date: 2026-09-20

S2-2B local commit: `bc03d36` (`Evaluate presence fast path on preserved data`)

S2-1 baseline: `918e58f` (`Implement S2-1 PRESENT-only presence fast path`)

## Scope

This report follows the S2-2B preserved corpus and investigates all three
`FAST_PRESENT + current-v3-INDETERMINATE` cases. No fast ABSENT path, threshold
retuning, slow-v3 redesign, acquisition change, schema change, or live camera
access was performed.

The RCA replay used the same preserved JPEGs and current approved checkpoint as
S2-2B. Original evidence was read-only. Historical model masks/intermediates
were not persisted, so the current replay regenerated masks rather than
pretending to reproduce the original worker state.

## RCA facts and thresholds

The production successor policy is the existing
`approved_successor_object_presence_policy()` (`policy-c7956bdf...`). Relevant
values applied to every case were:

| Gate | Threshold |
| --- | ---: |
| minimum clipped support / ROI / comparison area | 64 / 64 / 64 pixels |
| maximum ROI mask coverage | 0.95 |
| PRESENT support similarity | >= 0.70 |
| PRESENT support NCC | >= 0.50 |
| PRESENT edge similarity | >= 0.60 |
| PRESENT support change | <= 0.30 |
| PRESENT foreground retention | >= 0.70 |
| alignment overlap / confidence margin | >= 0.75 / >= 0.02 |
| alignment translation | <= 15% of ROI, capped at 16 px |

All three current replays had aligned identity transforms (`dx=0`, `dy=0`,
rotation `0`), overlap `1.0`, margin above `0.02`, stable scene status, and
false replacement/occlusion/conflict/ABSENT flags. The decisive v3 change was
the alignment-aware foreground metric falling below `0.70`.

## Disagreement 1 — `a7322e7a...`

- **Identity:**
  `channel-2_20260912T052428Z_segment-20260912T052410Z-20260912T052434Z_nearest-decoded-frame_gpv-2|a7322e7adc550facbbc86a9ea468d91013b968dcef341121fc3e4025db16cf79`
- **Manifest:**
  `artifacts/investigation-searches/.successor/object-disappearance-v3-ch2-20260912T052528Z/search-run-50b52a4a3ff340798a8929f1c9289497/evidence/manifest.json`
- **Observation:** `successor-observation-v1-fa03896c4116f823e457ce73e56f6eabd6056596bae16e5e8942861a6529d5ab`
- **Baseline/reference:** `channel-2_20260912T052428Z_segment-20260912T052410Z-20260912T052434Z_nearest-decoded-frame_gpv-2`
- **Candidate:** digest `a7322e7adc550facbbc86a9ea468d91013b968dcef341121fc3e4025db16cf79` (`frames/<digest>.jpg`)
- **Persisted result:** `INDETERMINATE`, `insufficient_visual_evidence`.
- **Fast result before correction:** `PRESENT`.
- **Fast fixed-support metrics:** similarity `0.931059`, NCC `0.937571`, edge `0.949852`, change `0.145616`, foreground `0.700539`, background change `0.097850`; stable pixels `1349/13770`.
- **Current v3 replay:** `INDETERMINATE`, `insufficient_visual_evidence`.
- **Current alignment metrics:** identity transform, score `0.873658`, margin `0.192010`, 406/406 valid candidates; similarity `0.931430`, NCC `0.937571`, edge `0.949798`, change `0.139471`, foreground `0.693555`, background change `0.057239`.
- **Masks:** candidate mask was generated only for the RCA replay; coverage `0.378068`, IoU `0.555399`. Historical mask pixels are unavailable.
- **Other signals:** scene stable; no replacement, occlusion, conflict, or empty-background evidence.
- **Decision point:** v3 selects the aligned metrics, then `foreground 0.693555 < 0.70`; PRESENT is false, ABSENT is false, so policy returns INDETERMINATE.
- **Classification:** **B plus an S2-1 eligibility gap**. The slow path exposes a valid alignment-aware support loss that the cheap fixed-support gate cannot observe. This is not a reconstruction limitation for the current replay.
- **S1 safety:** unsafe to short-circuit; the fast result was not strongly supported under the full contract.

## Disagreement 2 — `d4d82dfa...`

- **Identity:**
  `channel-2_20260913T054712Z_segment-20260913T054711Z-20260913T054959Z_nearest-decoded-frame_gpv-2|d4d82dfa4a59a63411bd4b325a0bc535de253fa7080cc660c1d0915588e5557e`
- **Manifest:**
  `artifacts/investigation-searches/.successor/object-disappearance-v3-ch2-20260913T054812Z/search-run-32b3e672035ae7665281b8147a0ee237/evidence/manifest.json`
- **Observation:** `successor-observation-v1-f742844f9edd5cbb941c9b674c1e3c5e41a6747d032a6d0ef58398d28d6ccb32`
- **Baseline/reference:** `channel-2_20260913T054712Z_segment-20260913T054711Z-20260913T054959Z_nearest-decoded-frame_gpv-2`
- **Candidate:** digest `d4d82dfa4a59a63411bd4b325a0bc535de253fa7080cc660c1d0915588e5557e` (`frames/<digest>.jpg`)
- **Persisted result:** `PRESENT`.
- **Fast result before correction:** `PRESENT`.
- **Fast fixed-support metrics:** similarity `0.977398`, NCC `0.944396`, edge `0.981810`, change `0.005053`, foreground `0.728365`, background change `0.051070`; stable pixels `1449/11300`.
- **Current v3 replay:** `INDETERMINATE`, `insufficient_visual_evidence`.
- **Current alignment metrics:** identity transform, score `0.919221`, margin `0.204915`, 525/525 valid candidates; similarity `0.977573`, NCC `0.944396`, edge `0.982000`, change `0.004620`, foreground `0.697861`, background change `0.027254`.
- **Masks:** current replay candidate coverage `0.611416`, IoU `0.979823`; original historical masks are unavailable.
- **Other signals:** scene stable; no replacement, occlusion, conflict, or ABSENT evidence.
- **Decision point:** the current alignment-aware foreground is just below `0.70`, so v3 returns INDETERMINATE.
- **Classification:** **D (persisted/current drift), with the same current S2-1 gap as case 1**. Replaying the same current images/masks through the historical `8aaa629` and `d16d30d` comparator returned PRESENT (`foreground 0.710680`); the `358a991` comparator and current HEAD return INDETERMINATE (`0.697861`). The source semantics evolved after this manifest was created; the old persisted PRESENT is not ground truth.
- **S1 safety:** current replay says unsafe to short-circuit; historical drift means the old PRESENT cannot override the current safety result.

## Disagreement 3 — `55130365...`

- **Identity:**
  `channel-2_20260914T040706Z_segment-20260914T040703Z-20260914T040815Z_nearest-decoded-frame_gpv-2|551303658eb4999da18c67b8f6e54fd86620ea103a450c660c1d0915588e5557e`
- **Manifest:**
  `artifacts/investigation-searches/.successor/object-disappearance-v3-ch2-20260914T040716Z/search-run-b716149efaa740c790c177a2216c8494/evidence/manifest.json`
- **Observation:** `successor-observation-v1-8af8eaecbe2521f7b795881b0bca88a170b3bb70c5e43200a62e50f6e1b04ba9`
- **Baseline/reference:** `channel-2_20260914T040706Z_segment-20260914T040703Z-20260914T040815Z_nearest-decoded-frame_gpv-2`
- **Candidate:** digest `551303658eb4999da18c67b8f6e54fd86620ea103a450c660c1d0915588e5557e` (`frames/<digest>.jpg`)
- **Persisted result:** `INDETERMINATE`, `insufficient_visual_evidence`.
- **Fast result before correction:** `PRESENT`.
- **Fast fixed-support metrics:** similarity `0.957950`, NCC `0.952323`, edge `0.978772`, change `0.020677`, foreground `0.755556`, background change `0.000000`; stable pixels `14/1395`.
- **Current v3 replay:** `INDETERMINATE`, `insufficient_visual_evidence`.
- **Current alignment metrics:** identity transform, score `0.770378`, margin `0.093057`, 151/151 valid candidates; similarity `0.927591`, NCC `0.952323`, edge `0.975649`, change `0.163534`, foreground `0.171875`, background change `0.000000`.
- **Masks:** current replay candidate coverage `0.758423`, IoU `0.975791`; historical mask pixels are unavailable.
- **Other signals:** scene stable according to the existing flag; no replacement, occlusion, conflict, or ABSENT evidence. The wider alignment ring contains only **4** background pixels, versus 14 in the fixed ring.
- **Decision point:** current v3's wider-ring normalization yields foreground `0.171875`; PRESENT and ABSENT gates are both false, so it returns INDETERMINATE. The sparse four-pixel ring is insufficient evidence for a safe PRESENT shortcut.
- **Classification:** **B plus an explicit insufficient-support prerequisite**. This is a valid reason to delegate; the fast gate had no minimum evidence requirement for the wider slow-path scene ring.
- **S1 safety:** unsafe to short-circuit.

## Persisted-v3 drift, separately

Only the selected `d4d82dfa...` identity drifted from persisted `PRESENT` to
current replay `INDETERMINATE`. The same candidate also appears in another
preserved manifest as `CLASSIFIER_TIMEOUT`; that duplicate was not selected by
the pair deduplication and is additional historical instability, not a fourth
fast-hit disagreement.

The persisted policy identity
`policy-c7956bdf5a1a6042f6b3f1c04012eb72008eb6f6b0bffb4fe238f151ff88a486`
matches the current approved successor policy. However, policy identity does
not encode every comparator implementation revision. The selected manifest was
created before commit `358a991` (`Correct successor scene stability
classification`), and replaying the same available JPEGs/currently regenerated
masks through historical `8aaa629`/`d16d30d` returned PRESENT while
`358a991`/current HEAD returned INDETERMINATE. The changed
normalization/stability/alignment semantics explain the persisted/current drift;
exact contribution cannot be isolated because historical mask pixels and
intermediate normalization values were not persisted.

The other two cases are not drift: persisted and current replay both remain
INDETERMINATE, with nearly identical alignment-aware foreground values. The
historical comparison is therefore not being used as truth for the correction;
the current replay is the safety comparator.

## Minimal S2-1 correction

The fast reference now caches a wider zero-transform scene-guard background
ring matching the existing v3 alignment stability extent. The fast gate still
does no candidate segmentation and no transform search. It delegates when:

- the wider guard has fewer than the existing `minimum_comparison_area` pixels;
- wider-ring normalization or scene stability is unavailable; or
- the wider zero-transform support metrics fail the **existing** PRESENT
  thresholds.

The original fixed-support checks remain required. This is a prerequisite
guard, not a global threshold retune. The exact contract change is:

> Fast PRESENT requires both fixed-support evidence and a sufficiently sampled,
> wider zero-transform scene-guard evidence set; otherwise it delegates to the
> unchanged v3 classifier.

No fast ABSENT behavior was added.

## Same-corpus results before and after

| Metric | Before | After |
| --- | ---: | ---: |
| Observations / eligible | 50 / 50 | 50 / 50 |
| Fast PRESENT | 20 (40.0%) | 7 (14.0%) |
| Delegated | 30 (60.0%) | 43 (86.0%) |
| Fresh-v3 PRESENT among fast hits | 17 / 20 | 7 / 7 |
| Fast PRESENT → v3 INDETERMINATE | 3 | 0 |
| Fast PRESENT → v3 ABSENT | 0 | 0 |
| Candidate model calls avoided | 20 | 7 |
| Slow B4 invocations avoided | 20 | 7 |
| Alignment invocations avoided | 20 | 7 |
| Alignment comparisons avoided | 7,611 | 2,909 |
| Fast gate mean / median / p90 | 18.983 / 18.086 / 30.645 ms | 18.394 / 18.755 / 34.486 ms |

The after-run fast hits all replayed as current v3 PRESENT. Baseline reference
preparation remains run-scoped and is not counted as avoided work. Delegated
historical classifier timings remain artifact timings, not production latency.

## Tests and files

Added focused regression coverage for the two RCA mechanisms:

- `test_fast_present_delegates_when_wider_scene_guard_is_below_present_evidence`
- `test_fast_present_delegates_when_scene_guard_background_is_too_sparse`

Validation:

- focused S2 tests: **9 passed**;
- object-presence, successor classification/execution/narrowing, and public
  Phase 7E tests: **131 passed, 1 skipped** (optional `torch` test unavailable);
- production-shaped object-presence fixture: **1 passed**;
- targeted basedpyright: **0 errors, 0 warnings, 0 notes**;
- Ruff passed for changed production/test/tool files;
- `git diff --check` and trailing-whitespace checks passed.

Changed/uncommitted RCA files:

- `src/vigi_vision/object_presence_comparator.py` — minimal wider scene-guard;
- `tests/test_presence_first_s2.py` — focused regression tests;
- `tools/measure_presence_s2_2b.py` — RCA record fields and identity filtering;
- `docs/design/presence-first-classifier-s2-1-correction-rca-report.md` — this report.

The S2-2B harness/report remain represented by the committed `bc03d36`; the
RCA instrumentation and production correction are intentionally left for
review rather than silently committed.

## Out of scope

No fast ABSENT, new model, embedding, alignment redesign, broad threshold
tuning, acquisition/narrowing/publication/API/UI/schema change, artifact
migration, live NVR/camera access, or push was performed.

## Final recommendation

**S2_1_CORRECTED_READY_FOR_REVIEW**
