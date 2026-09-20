# Presence-first classifier — Phase S2-2B preserved-data report

Date: 2026-09-20

S2-2 local commit: `1ee27a5` (`Add S2-2 presence fast-path measurement harness`)

S2-1 baseline: `918e58f` (`Implement S2-1 PRESENT-only presence fast path`)

Status: evaluation-only. No production classifier semantics, thresholds,
evidence schemas, acquisition behavior, or publication behavior were changed.
No fast ABSENT path was implemented, and nothing was pushed.

## 1. Data sources and method

The read-only harness is `tools/measure_presence_s2_2b.py`. It used the
preserved successor recording-search evidence already in the repository:

- 24 `evidence/manifest.json` files under
  `artifacts/investigation-searches/.successor/`;
- the linked preserved JPEG frames in those evidence directories; and
- the approved local EfficientSAM-Ti checkpoint at
  `D:\models\efficient_sam_vitt.pt` (SHA-256
  `dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a`).

The manifests supplied 74 anchor/observation entries. The harness deduplicated
them by `(reference_frame_resource_id, candidate_digest)`, yielding 50 unique
pairs and 15 reusable baseline contexts. Original manifests and frames were
not modified. No live NVR, camera, or new recording acquisition was used.

For every pair, the harness decoded/cropped the source-pixel ROI, prepared the
run-scoped baseline support with the approved model, and evaluated the
committed S2-1 PRESENT-only gate. Non-hits retained the persisted v3 outcome
from the artifact. Every fast hit received a fresh candidate model inference
and a replay of the existing `classify_decoded_images` v3 comparator using
those masks. Thus the safety comparison is against a current comparator replay
for all fast hits; delegated timing/outcomes remain the preserved artifact
record because the old manifests do not contain reusable mask pixels.

The fresh v3 replay is the ROI-cropped comparator boundary, not a new
end-to-end worker/NVR run. It is therefore suitable for semantic comparison
and work accounting, not production-latency claims.

## 2. Independent observations and supported categories

| Preserved category | Count | Evidence basis |
| --- | ---: | --- |
| `preserved_present` | 16 | persisted v3 `PRESENT` |
| `preserved_absent` | 13 | persisted v3 `ABSENT` |
| `preserved_ambiguous` | 20 | persisted v3 `INDETERMINATE`, all `insufficient_visual_evidence` |
| `preserved_operational_failure` | 1 | persisted `CLASSIFIER_FAILED` |
| **Total independent pairs** | **50** | deduplicated preserved pairs |

The artifacts do not carry reliable semantic tags for lighting variation,
occlusion strength, displacement, replacement, framing shift, or compression
quality. Those labels were not invented. The supported ambiguity clusters are
the 20 insufficient-evidence cases and the one classifier failure.

## 3. Fast-path and v3 results

| Metric | Result |
| --- | ---: |
| Independent observations evaluated | 50 |
| Fast-gate eligible | 50 |
| Fast PRESENT hits | 20 |
| Fast PRESENT hit rate (eligible) | 40.0% |
| Delegated observations | 30 |
| Delegation rate (all observations) | 60.0% |
| Fresh v3 `PRESENT` replays for fast hits | 17 / 20 |
| Fast-hit agreement with fresh v3 | 17 / 20 (85.0%) |
| Fresh v3 `ABSENT` replays for fast hits | 0 |
| Fresh v3 `INDETERMINATE` replays for fast hits | 3 |
| Fast-hit replay failures | 0 |
| Persisted-v3/exact-replay agreements for fast hits | 14 / 20 |
| Persisted-v3/replay outcome differences | 6 / 20 |

The six persisted/replay differences are historical artifact drift, not six
additional fast-path safety failures. The current safety criterion is the
fresh replay: three fast PRESENT decisions produced current v3
`INDETERMINATE`, and none produced current v3 `ABSENT`.

### Material disagreement review

The three material cases are:

1. Two preserved `INDETERMINATE` / `insufficient_visual_evidence` observations
   replayed as `INDETERMINATE` again. Their persisted comparisons show either
   low foreground retention or insufficient valid stability support; they are
   genuine ambiguity cases, not evidence for an ABSENT fast gate.
2. One observation persisted as `PRESENT` but replayed as `INDETERMINATE`.
   This is an unresolved persisted-v3/current-replay disagreement (the
   preserved comparison was strong, while the current replay did not satisfy
   the PRESENT gate). It is treated as unsafe until the model/runtime or
   comparator provenance is investigated; no threshold was changed to hide it.

There were no fresh fast-hit/v3 `ABSENT` disagreements. Because the required
safety rule treats `FAST_PRESENT + INDETERMINATE` as material, the observed
three cases are sufficient to stop before fast ABSENT work.

## 4. Expensive-operation savings

Measured at the S2-1 cascade boundary:

- 20 candidate segmentation/model calls were avoided (one for each fast hit);
- 20 slow B4 classifier invocations were avoided;
- 20 alignment invocations were avoided;
- 7,611 bounded alignment-comparison iterations were avoided; and
- the 15 unique baseline model calls still ran once per cached baseline
  context. Baseline preparation is not a fast-hit saving.

All eligible candidates still incurred decoding/ROI extraction, luma
conversion, fixed-support metrics, and the gate's scene/stability checks.
The harness measured a mean ROI decode/crop time of 22.006 ms (median
20.940 ms; p90 30.016 ms) and did not treat that upstream work as avoided.

## 5. Timing and limitations

| Work | Mean | Median | p90 | Notes |
| --- | ---: | ---: | ---: | --- |
| S2-1 fast gate | 18.983 ms | 18.086 ms | 30.645 ms | gate only, after ROI decode/crop |
| Persisted delegated v3 classifier | 28,877.280 ms | 28,036.000 ms | 33,922.100 ms | historical artifact timing, not current production latency |
| Fresh v3 comparator replay for fast hits | 4,331.997 ms | 4,180.428 ms | 8,044.520 ms | pure comparator with static already-generated masks |
| Baseline EfficientSAM inference | 3,951.174 ms | 3,774.785 ms | 4,252.481 ms | 15 unique baselines, local CPU checkpoint |
| Candidate EfficientSAM inference on replayed hits | 3,927.588 ms | 3,657.521 ms | 4,764.158 ms | 20 real candidate model calls |

These are local replay measurements. They do not include or estimate live
NVR/network, decoder process, worker startup, storage, or camera latency, and
the preserved delegated durations were produced by earlier runs. They should
not be extrapolated to production throughput.

## 6. Missing data and interpretation limits

- There is no human-labeled ground truth for object identity or scene
  condition; v3 is the comparator, not an independent truth set.
- Preserved manifests contain v3 outcomes and measurements but not reusable
  model mask pixels, so delegated observations were not redundantly rerun.
- The 50 pairs are representative preserved investigations, not a random
  sample and not a statistically significant prevalence estimate.
- The source artifacts do not support reliable per-case labels for lighting,
  occlusion, movement, replacement, framing, or quality; the report uses only
  persisted outcome/reason categories.
- The replay used the approved local CPU checkpoint and an ROI-cropped pure
  comparator. It did not exercise live acquisition or the full worker process.

## 7. Recommendation

**FIX_S2_1_BEFORE_CONTINUING**

The PRESENT-only gate is useful on this preserved set (40% fast hits and 20
candidate model/alignment paths avoided), but three fast PRESENT hits fail the
required current-v3 safety check as `INDETERMINATE`. Investigate the
persisted/current replay discrepancy and the two borderline ambiguity cases
before any S2-3 or fast ABSENT evaluation. No threshold tuning or semantic
change was made in this task.

## 8. Validation and changed files

Validation run:

- `ruff check tools/measure_presence_s2_2b.py` — passed;
- focused S2 tests — **7 passed**;
- relevant successor classification/execution/narrowing tests — **65 passed,
  1 skipped** because the test environment had no `torch` import at that
  optional test; and
- `git diff --check` — passed for tracked changes; the two untracked files also
  passed a trailing-whitespace scan.

S2-2B files intentionally left uncommitted:

- `tools/measure_presence_s2_2b.py` — read-only preserved-data measurement
  harness;
- `docs/design/presence-first-classifier-s2-2b-report.md` — this report.
