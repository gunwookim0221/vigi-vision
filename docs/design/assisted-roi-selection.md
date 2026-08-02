# Tap-Assisted ROI Selection

## Status and decision

**Status: Phase 5-3A design, Phase 5-3B-1 disposable validation harness,
Phase 5-3B-2 production assisted-ROI backend, Phase 5-3B-3 frontend
integration, and Phase 5-3C silhouette preview complete. ROI persistence and
Phase 6 behavior remain intentionally unimplemented.**

Recommendation: **Proceed to manual desktop/mobile acceptance and a controlled
loopback backend smoke check before Phase 6, retaining the disposable harness as
the acceptance tool.**
EfficientSAM-Ti produced useful initial shoe selections on the deployed CCTV
inputs, but dense adjacent shoes can merge into one suggested ROI. Keep the
existing manual ROI editor as the authoritative correction and recovery path.

The automated result is always a **suggested ROI**. It does not establish object
class, identity, ownership, tracking, disappearance, theft, or a person’s
identity.

## Why this boundary exists

The implemented Phase 5 editor is precise on desktop and supports touch, but a
small object can be difficult to surround accurately with a finger on a narrow
screen. The proposed extension adds one explicit interaction:

1. Select one usable reference-frame candidate.
2. Activate **Tap to suggest ROI**.
3. Tap the object once.
4. Receive one server-generated rectangular suggestion.
5. Verify it and use the existing move, resize, reset, or manual redraw paths.

Ordinary taps must not run inference. Assisted mode is temporary and explicit,
so existing drawing, moving, resizing, focus, keyboard, and page-scroll behavior
remains predictable.

## Existing contract that remains authoritative

The design reuses the implemented Phase 5 state rather than adding a second ROI
representation.

| Concern | Existing contract | Assisted-selection rule |
| --- | --- | --- |
| Candidate identity | A successful candidate’s opaque `reference_frame.resource_id`. | Send only that resource ID as the route parameter; never send an image path or URL. |
| Image identity | `ReferenceFrameResourceStore.resolve_image()` resolves the fixed JPEG from a completed server-controlled resource. | Run inference only on that resolved JPEG and reject manifest/generation versions unsupported by the suggestion adapter. |
| Source dimensions | Loaded image `naturalWidth` and `naturalHeight`; the API also supplies positive dimensions. | Require the stored image and response dimensions to agree with the currently selected image. |
| ROI shape | `{source_width, source_height, x, y, width, height}` with integer source pixels. | Convert the chosen mask to this exact rectangle before committing it; retain only the bounded transient preview for visual review. |
| Bounds | Edges may reach `source_width` or `source_height`; width and height are at least 4. | Require `x >= 0`, `y >= 0`, `x + width <= source_width`, `y + height <= source_height`, and the existing 4×4 minimum. |
| Transient state | Draft edits are separate from the one committed ROI. | Preserve the committed ROI while a suggestion is pending; replace it only after a complete valid response. |
| Candidate lifecycle | Candidate/result/image replacement clears the ROI and active pointer state. | It also aborts the browser request, invalidates its sequence, and clears the tap marker. |
| Phase 6 handoff | `getPhase6Snapshot()` returns an immutable candidate-bound copy or `null`. | Keep its schema and behavior unchanged; a successful suggestion becomes the same committed ROI it already reads. |

The current candidate request uses `requestSequence` to reject stale responses
but does not use `AbortController`. Assisted inference needs both: abort the
transport when possible and independently reject any late result whose sequence,
resource ID, or source dimensions no longer match. Browser cancellation cannot
guarantee cancellation of synchronous model work already running in a worker
thread, so the server concurrency limit remains necessary.

One coordinate distinction is important. A rectangle edge may equal the source
width or height, but a point identifies a pixel. Suggestion request points must
therefore satisfy `0 <= x < source_width` and `0 <= y < source_height`. The
assisted adapter must clamp the existing rounded pointer conversion to
`source_width - 1` and `source_height - 1` without changing manual ROI geometry.

## Evaluated point-prompted models

Facts in this table come from the linked upstream projects and model files.
Latency and memory values marked **measured** are from the isolated Phase 5-3A
run described below. Other runtime values are upstream measurements and are not
VIGI Vision guarantees.

| Model | License and checkpoint | Runtime support | Size and performance evidence | Assessment |
| --- | --- | --- | --- | --- |
| [SAM 2.1 Hiera Tiny](https://github.com/facebookresearch/sam2) | Apache-2.0 code and checkpoint; `sam2.1_hiera_tiny`, 38.9M parameters; official safetensors file is 156 MB. | Upstream requires Python >=3.10, PyTorch >=2.5.1, and torchvision >=0.20.1. CPU and CUDA are available through PyTorch, but upstream strongly recommends WSL rather than native Windows. | Upstream reports 91.2 FPS on an A100 with PyTorch 2.5.1/CUDA 12.4; no VIGI CPU, Windows, load, or memory measurement exists. | Better-maintained and newer, but its native-Windows build path, custom CUDA extension, checkpoint, and memory burden are unnecessary for one static-image tap. Do not select it for Phase 5-3B yet. |
| [MobileSAM](https://github.com/ChaoningZhang/MobileSAM) | Apache-2.0; `mobile_sam.pt`, approximately 40.7 MB; 9.66M total parameters. | Upstream declares Python >=3.8, PyTorch >=1.7, torchvision >=0.8, CPU and CUDA. Native Windows is plausible through PyTorch but unverified here. | Upstream reports about 3 s on a Mac i5 CPU and about 12 ms on an unspecified single GPU. | Small and simple, but its published small-object quality trails EfficientSAM and its tested ONNX versions are old. Retain as a comparison candidate, not the default. |
| [EfficientSAM-Ti](https://github.com/yformer/EfficientSAM) | Apache-2.0; `efficient_sam_vitt.pt`, 40,982,470 bytes (41 MB), SHA-256 `dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a`; approximately 10M parameters. | Pure PyTorch point-prompt path. The spike ran on native Windows, Python 3.11, and PyTorch 2.10 CPU. GPU support is provided by the model code but was not measured. | **Measured:** 0.119 s model/checkpoint construction, 1.296 s first inference, 1.221 s second inference, and about 798 MiB final process RSS. The CVPR paper reports 54 images/s on its GPU benchmark, which is not comparable to this CPU run. | Smallest approach with a successful VIGI-environment mechanism test and stronger published small-object results than MobileSAM. Select for the acceptance-gated Phase 5-3B path. |

The Windows PyTorch 2.10 CPU wheel used by the spike was 113.7 MB and
torchvision was 4.0 MB before their transitive dependencies. This is still a
material optional runtime, even though the EfficientSAM checkpoint is small.
PyTorch supports Windows and Python 3.10 through 3.14 in the evaluated release
line; EfficientSAM itself does not publish a maintained PyPI release or a VIGI
compatibility matrix. Production packaging therefore remains a Phase 5-3B gate,
not an established fact.

### Quality risk

The spike proves loading, a positive point prompt, mask selection, and bounded
box conversion. It does not prove CCTV usefulness. The public fixture contains
large, well-lit dogs. Shoes, bags, occlusion, low contrast, compression noise,
and small image footprint can produce an incomplete object mask or include
nearby objects. Published aggregate metrics cannot replace a small
camera-representative acceptance set. The initial CCTV run found that dense
adjacent shoes can merge into one rectangle; this is a partial result, not a
harness failure.

## Isolated feasibility result

The disposable run used official EfficientSAM source commit
`d525f622e6f640acf5a0fc37c7ca1f243da5bde0` and the repository’s public
`figs/examples/dogs.jpg`; it did not read an NVR artifact.

| Measurement | Result |
| --- | --- |
| Device | CPU; the installed wheel reported `torch 2.10.0+cpu` |
| Host acceleration | An RTX 3080 was present, but no CUDA PyTorch runtime was downloaded; GPU was not tested |
| Image | 1072×603 RGB public fixture |
| Positive point | `(580, 350)` source pixels |
| Selected mask | 12,931 pixels; model score 0.5529 |
| Bounding ROI | `x=471, y=282, width=118, height=174` |
| Checkpoint construction | 0.119 s |
| First inference | 1.296 s |
| Second inference | 1.221 s |
| Approximate process RSS | 284 MiB before model, 358 MiB after load, 798 MiB after inference |
| Disposable on-disk footprint | About 1.16 GB for the unpacked isolated environment, source, checkpoint, and output before cleanup |

Visual review showed that the rectangle covered the tapped brown dog’s torso
but omitted its head and much of its legs. The result was useful as an editable
starting box, not as a verified whole-object boundary. No sensitive image,
durable artifact, model weight, result image, virtual environment, or cloned
source remains after cleanup.

The dependency and model run was isolated from the project environment. Exact
reproduction requires a disposable Python 3.11 environment, the pinned upstream
commit and checkpoint hash above, `torch==2.10.0`, `torchvision==0.25.0`,
`psutil==7.0.0`, the public `dogs.jpg`, one positive point tensor at `(580,
350)`, highest predicted-IoU non-empty mask selection, and an inclusive
min/max mask bounding box. Run the harness from the upstream source directory
with `PYTHONPATH=.`. A Phase 5-3B acceptance harness should record these same
fields and delete its temporary environment and rendered output after review.

## Alternatives

| Alternative | Decision | Reason |
| --- | --- | --- |
| Tap-centered fixed-size box | Manual recovery only, not an automatic default. | Fast and dependency-free, but arbitrary scale makes it poor for both shoes and larger bags and can imply knowledge it does not have. |
| Tap-centered adaptive box | Reject for now. | A heuristic still needs an unproven scale signal and adds tuning without learning object boundaries. |
| Local color/edge proposal | Reject. | CCTV compression, shadows, patterned floors, occlusion, and adjacent objects make deterministic region growing brittle. |
| Object-detector boxes | Defer. | A detector requires category coverage, model/licensing review, and likely CCTV tuning; it changes the interaction from category-agnostic point prompting. |
| Client magnifier | Retain as a possible manual usability enhancement. | It can improve tap placement and manual correction without changing evidence semantics, but it does not suggest an object region and should not block the first segmentation test. |
| SAM 2.1 Tiny | Defer. | The quality/maintenance upside does not yet justify its larger checkpoint and upstream WSL recommendation for this native-Windows service. |
| EfficientSAM-Ti point prompt | Acceptance-gated production default. | It preserves the desired one-tap workflow, has a small checkpoint, is Apache-2.0, ran successfully on native Windows CPU, and returns a mask that can be discarded after deterministic box conversion. |

When the optional capability is unavailable, the UI must say that automatic
suggestion is unavailable and leave the complete manual editor enabled. It must
not silently create a fixed-size box and present it as model output.

## Implemented HTTP boundary

Add one sibling resource operation; do not modify either existing
reference-frame response schema.

```text
POST /api/v1/reference-frames/{resource_id}/roi-suggestions
```

Request:

```json
{
  "point": {
    "x": 1234,
    "y": 720
  }
}
```

Response:

```json
{
  "resource_id": "opaque-existing-resource-id",
  "source_width": 2560,
  "source_height": 1440,
  "bbox": {
    "x": 1100,
    "y": 640,
    "width": 310,
    "height": 190
  },
  "mask_preview": {
    "width": 2560,
    "height": 1440,
    "rows": [[], "... bounded source-row runs ..."]
  }
}
```

Boundary rules:

- Parse a strict integer point and reject booleans, fractions, extra fields,
  negative values, and the exclusive right/bottom edge.
- Resolve `resource_id` only through a narrow `ReferenceFrameResourceStore`
  metadata/image boundary; require a completed resource with supported manifest
  and generation-policy versions. Do not accept a path, image URL, upload,
  replay URL, or remote URL.
- Read the fixed completed JPEG and its intrinsic dimensions server-side.
- Require the model output to be a finite two-dimensional mask with exactly
  the source image dimensions. This is the “oversized mask” safety check. The
  existing `95%` coverage guard rejects pathological background-dominant
  candidates; it is a bounded safety rule, not a general object-size heuristic.
- From model candidates, choose the highest-score non-empty mask that contains
  the positive prompt; retain upstream order as the deterministic tie-breaker.
- Convert true mask pixels to the minimal source-space integer rectangle and
  validate it against source bounds before serialization. Return the rectangle
  plus a bounded exact row-run mask preview; do not write the mask to disk.
- Reject a preview with more than `50,000` source-row runs or with runs whose
  derived bounds do not exactly match the rectangle.
- Do not update the reference-frame manifest, JPEG, candidate set, or any Phase
  6 state.

The implemented route is `POST /api/v1/reference-frames/{resource_id}/roi-suggestions`.
Its strict body is `{ "point": { "x": integer, "y": integer } }`; dimensions
come only from the completed server resource. The response contains only
`resource_id`, source dimensions, the bounded `bbox`, and an exact transient
`mask_preview` whose `rows[y]` entries are non-overlapping `[start, end)` source
pixel runs.

Fixed public errors are `invalid_point` (422), existing
`resource_not_found`/`resource_corrupt` categories, `no_valid_suggestion` (422),
`suggestion_unavailable` (503), `suggestion_timeout` (504),
`suggestion_failure` (500), and the existing safe internal fallback. Responses
never contain checkpoint paths, artifact paths, exception text, commands, URLs,
credentials, stderr, scores, filesystem paths, or model internals. The mask
preview is returned only to the requesting local browser and is not persisted.

## Server composition and model lifecycle

- Define one narrow predictor protocol and inject the suggestion service into
  the existing app factory. `None` means the optional capability is unavailable;
  reference-frame generation, candidate selection, image serving, and manual
  ROI remain active.
- Keep the route present and return fixed `suggestion_unavailable` when the
  provider is absent. No capability endpoint or frontend control is added in
  this phase.
- Resolve the artifact before invoking the model. The model service owns no NVR,
  recording, replay, decoder, artifact publication, or manifest behavior.
- Load the model lazily on the first valid suggestion so a missing optional
  runtime cannot prevent API startup. Reuse one loaded model for later requests.
- Protect first load with the same inference serialization primitive and run
  synchronous work off the event loop. Use a dedicated
  `anyio.CapacityLimiter(1)` for inference rather than borrowing the existing
  NVR/media limiter; each boundary remains independently bounded.
- Support `cpu`, `cuda`, and `auto` composition. `auto` chooses CUDA only when
  the installed runtime reports it available; otherwise it chooses CPU. An
  explicitly requested unavailable device returns fixed unavailable state and
  does not silently change device.
- After a load failure, cache an unavailable state until process restart so
  repeated taps do not repeat expensive initialization. Return only the fixed
  safe category.
- Provision the checkpoint out of band, verify its exact SHA-256 before use,
  and keep it outside Git and artifact roots. Never download it during app
  startup or an HTTP request.
- On application shutdown, drop predictor/model references. No background
  worker, queue, or persistence layer is needed.

Operator configuration is process-local and never client-controlled:

```text
VIGI_ASSISTED_ROI_ENABLED=true
VIGI_ASSISTED_ROI_CHECKPOINT=C:\models\efficient_sam_vitt.pt
VIGI_ASSISTED_ROI_CHECKPOINT_SHA256=dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a
VIGI_ASSISTED_ROI_DEVICE=cpu
VIGI_ASSISTED_ROI_TIMEOUT_SECONDS=30
```

The checkpoint must be provisioned outside Git and the reference-frame artifact
root. The exact hash is checked immediately before model construction. Missing
or invalid optional configuration leaves the route safely unavailable rather
than preventing the reference-frame API from starting. EfficientSAM, PyTorch,
torchvision, Pillow, and NumPy are lazy optional imports; they are not core
dependencies and no download occurs.

## Frontend state and accessibility

The control belongs beside the existing ROI actions, outside the pointer
surface. It is disabled until a successful candidate image is loaded and a
verified source size/resource ID are available.

1. **Tap to suggest ROI** enters assisted mode and announces “Tap the object to
   request an automatic suggestion.”
2. The next primary mouse, pen, or touch tap inside the displayed image converts
   the image-relative CSS position through the intrinsic image rectangle into
   one bounded integer source-pixel point, then shows a visible marker and a
   polite pending status.
3. While pending, a new assisted tap aborts the prior browser request before
   starting the next generation; the latest request wins. The button remains a
   keyboard-operable cancellation control.
4. Keep the previous committed ROI visible while pending. On a valid current
   response, render the exact transient mask silhouette, pass the validated
   source rectangle through the existing committed ROI state, announce
   “Suggested ROI ready. Verify and adjust,” and clear the marker.
5. On safe failure, timeout, cancellation, stale response, or invalid response,
   preserve the previous committed ROI, clear the marker, announce a concise
   retry message, and leave manual editing available.
6. Candidate change, new candidate results, selected-image failure, and ROI
   reset invalidate the assisted sequence. Reset ROI keeps its existing meaning
   and must not initiate inference.

The marker is a high-contrast shape positioned relative to the displayed image
and is cleared on resolution, failure, cancellation, or staleness. Pending and
error text uses the existing live status region; focus is not moved on success.
The mode button exposes an accessible name and pressed state, and keyboard users
retain the existing focused ROI Arrow/Shift/Alt editing after a suggestion.
The mask is painted to a source-sized canvas and scaled with the loaded image;
the mask fill and contrasting outline remain visible over bright or dark CCTV
frames. Assisted selections hide the small resize handles; Reset ROI clears the
mask, rectangle, marker, and status so a new tap or manual drag can start
immediately. Manual rectangle editing clears the assisted preview and remains
authoritative.
Pointer cancellation produces no request. Touch suppression remains scoped to
the image only while assisted point collection is active; scrolling outside the
image remains normal. Zoom/pan and a persistent magnifier are out of scope until
physical-device evidence shows they are needed.

## Security and privacy

- The browser supplies only an opaque resource ID and two bounded integers.
- The resource store remains the sole filesystem authority; inference cannot
  browse arbitrary local files or retrieve remote content.
- The model operates offline after operator-controlled dependency and checkpoint
  acquisition. No image, point, mask, or ROI is sent to a third party.
- The mask and tap marker are transient, returned only as a bounded row-run
  preview to the local browser, and are never logged, persisted, or added to a
  manifest.
- Existing loopback-only trust and fixed safe error behavior remain unchanged.
- The dedicated concurrency limit bounds CPU/GPU pressure and prevents repeated
  taps from creating uncontrolled inference work.

## Phase 5-3B acceptance and tests

For acceptance, run EfficientSAM-Ti on a small non-sensitive set that
represents the deployed camera resolution, compression, lighting, shoe/bag
size, partial occlusion, and adjacent objects. Record CPU and available-GPU
load time, p50/p95 warm latency, process RSS or device peak allocation, proposed
box, whether the prompt lies inside it, and a human “useful starting box” review.
Do not choose a numeric quality threshold until that labeled set exists.

Backend tests cover valid source-pixel points, exclusive point bounds,
unknown/corrupt resources, path/URL rejection by schema, empty and wrong-shaped
masks, signed-logit thresholding, deterministic mask/score alignment, source
dimension agreement, one-at-a-time inference, bounded timeout, cached
unavailable state, fixed safe errors, and proof that artifacts/manifests are
unchanged. The backend does not expand a rectangle to the frontend's existing
4×4 editing minimum; the eventual client may normalize or manually correct it.

Frontend tests cover explicit mode, one request per activation, transport abort
plus sequence rejection, candidate/resource/dimension stale checks,
candidate-change cancellation, prior-ROI preservation, canonical committed ROI
entry, subsequent move/resize/reset/manual redraw, source-coordinate authority,
touch/mouse/keyboard behavior, accessible pending/error state, safe unavailable
handling, and suggestion language without recognition claims.

Regression gates remain the existing candidate order and no auto-selection,
unchanged reference-frame schemas and direct decoder, all Phase 5 manual ROI
tests, unchanged `getPhase6Snapshot()`, no durable persistence, complete Python
quality gates, Node syntax/tests, `git diff --check`, and changed-file/rule
audits.

## Phase 5-3B-1 disposable validation harness

The repository now contains an offline, non-production validation harness at
`tools/validate_assisted_roi.py`. It reads completed reference-frame
directories recursively and never edits their JPEGs or manifests. It does not
add a model dependency, checkpoint, API route, frontend control, or production
ROI service.

Create a disposable environment outside the project environment and install
the optional runtime explicitly:

```text
uv venv .venv-assisted-roi --python 3.11
uv pip install --python .venv-assisted-roi/Scripts/python.exe torch==2.10.0 torchvision==0.25.0 pillow numpy
git clone https://github.com/yformer/EfficientSAM.git third-party/EfficientSAM
git -C third-party/EfficientSAM checkout d525f622e6f640acf5a0fc37c7ca1f243da5bde0
uv pip install --python .venv-assisted-roi/Scripts/python.exe -e third-party/EfficientSAM
```

Place the operator-provisioned `efficient_sam_vitt.pt` checkpoint outside Git.
The expected SHA-256 is
`dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a`.
The harness never downloads a checkpoint; `--verify-sha256` makes the hash
check explicit before the window opens.

Run the native Windows CPU workflow with:

```text
python tools/validate_assisted_roi.py \
  --input artifacts/reference-frames \
  --checkpoint C:\models\efficient_sam_vitt.pt \
  --output artifacts/validation/assisted-roi \
  --verify-sha256 --device cpu
```

Optional `--resume`, `--limit`, `--channel`, `--shuffle`, and `--seed` flags
bound or reproduce a run. The window opens one discovered `frame.jpg` at a
time. Click an object to run the lazily loaded, reused predictor; inspect the
mask and source-pixel box; then classify `success`, `partial`, `failure`, or
`skip`. Previous/next, retry, clear, notes, and quit controls save progress.
Partial is deliberately not counted as success.

The output directory contains only harness-owned `session.json`,
`summary.md`, and JPEG overlays under `overlays/`. The session stores relative
source paths, channel metadata, points, boxes, classifications, and inference
timings. The summary records total/evaluated counts, per-channel results,
timing percentiles, checkpoint metadata, and the fixed recommendation policy:

- fewer than 20 evaluated images: `insufficient_evidence`;
- at least 20 and at least 70% success: `proceed`;
- at least 20 and 50% to below 70% success: `proceed_with_limitations`;
- at least 20 and below 50% success: `do_not_proceed`.

## Real CCTV acceptance evidence

The operator ran the disposable harness on native Windows with EfficientSAM-Ti,
`efficient_sam_vitt.pt`, Python 3.11, PyTorch 2.10 CPU, and the existing
`artifacts/reference-frames/**/frame.jpg` inputs. The checkpoint SHA-256 was
verified as
`dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a`.

The recorded session discovered 55 frames and evaluated 16: 15 were classified
`success`, 1 was `partial`, and 0 were `failure` (93.75% success among
evaluated frames). The report recommendation remains `insufficient_evidence`
because the harness policy requires at least 20 evaluated frames; this is an
evidence-volume result, not a quality failure.

Qualitative review found that individual shoes, full shoe pairs, and slippers
were selected usefully; people in the frame did not prevent useful shoe
selection; and small source objects produced tight rectangles. One dense
shoe-rack case grouped neighboring shoes and was classified `partial`. A
single-shoe selection is acceptable for VIGI’s downstream disappearance target,
which does not require semantic completion of a shoe pair. The remaining
primary limitation is merging adjacent visually connected shoes in dense
shelving. Manual ROI correction remains authoritative.

This is favorable initial real-CCTV validation, not a substitute for frontend
acceptance. The production backend and frontend integration are implemented;
operator smoke validation and physical desktop/mobile acceptance remain.

Successful overlays and sessions remain for review. A failed overlay write
removes only the partial overlay created by that attempt. To discard all
disposable validation output after review, remove only the chosen validation
directory, for example `artifacts/validation/assisted-roi`; never remove the
reference-frame input root.

The harness proves discovery, coordinate conversion, deterministic mask
selection, bounded checkpoint handling, resumability, and human review
recording. The initial real-CCTV session provides favorable camera evidence but
does not establish broad CCTV quality, model licensing beyond the recorded
upstream source, GPU performance, or production API behavior. Those remain
packaging and production integration gates.

## Production predictor CPU validation (2026-08-01)

The production `LazyEfficientSamPredictor` and
`AssistedRoiSuggestionService` were exercised on native Windows x86-64 through
the real reference-frame resource store. No frontend, API server, OpenAI
component, or test double was used for the four inference cases. The isolated
environment used Python `3.11.9`, `torch==2.10.0+cpu`,
`torchvision==0.25.0+cpu`, Pillow `12.3.0`, and NumPy `2.4.6`; CUDA was not
available. The official EfficientSAM source was commit
`d525f622e6f640acf5a0fc37c7ca1f243da5bde0`. The existing checkpoint was
`efficient_sam_vitt.pt`, `40,982,470` bytes, with verified SHA-256
`dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a`.

Automated assertions recorded source/JPEG dimension agreement, bounded boxes,
boxes at least 4×4, prompt containment, and unchanged hashes for all 55 frame
JPEGs plus 55 manifests (110 files; aggregate digest unchanged). The production
service returns a validated rectangle plus a bounded transient mask preview;
candidate mask pixels and model score remain response data for frontend review,
not durable artifact fields.

| Case | Source-space tap | Cold/warm elapsed | RSS after call | Returned ROI | Automated result |
| --- | --- | ---: | ---: | --- | --- |
| Normal visible object | `(571, 224)` | `8,203 ms` cold load + inference | `897 MiB` | `(540, 186, 99×77)` | success; bounded, ≥4×4, contains tap |
| Small object | `(682, 270)` | `3,694 ms` warm | `930 MiB` | `(669, 235, 46×51)` | success; bounded, ≥4×4, contains tap |
| Adjacent/touching shoes | `(162, 361)` | `4,040 ms` warm | `929 MiB` | `(100, 293, 181×119)` | success; bounded, ≥4×4, contains tap |
| Background mis-tap | `(2559, 1439)` | `3,735 ms` warm | `933 MiB` | `(2548, 1422, 12×18)` | success; bounded, ≥4×4, contains tap |

Visual review of the disposable overlays found a useful rack-object starting
box in the normal case and a tight small-object box. The adjacent-shoe box
merged neighboring shoes and needs manual correction. The bottom-right
background mis-tap produced a tiny edge box; it is structurally valid but not a
useful object selection, so the operator must review and reject it when
necessary. These are visual observations, not automated quality assertions.

A controlled timeout seam added only to the validation driver delayed the real
predictor behind a `0.001 s` service timeout. The public result was
`RoiSuggestionTimeoutError` / safe category `timeout` in about `14 ms`; no
validation output was created by the timeout. After the worker settled, a normal
call succeeded in `3,371 ms` with the same bounded ROI, demonstrating recovery
without predictor-state corruption. Overlays and measurements are disposable
and are under `artifacts/validation/assisted-roi-production/`; source JPEGs and
manifests remain untouched.

This run establishes production CPU execution and the current resource/memory
cost, but it does not establish GPU behavior, broad camera/lighting quality, or
that every background tap will be rejected automatically. The approximately
`897–936 MiB` working-set range after model load is the primary deployment
risk; frontend integration should retain manual ROI authority and a visible
review step.

## Phase 5-3C silhouette preview

Phase 5-3C keeps the rectangle contract but returns the selected EfficientSAM
mask as a bounded, exact source-row run preview. The browser paints those runs
to a transient canvas aligned to the loaded JPEG, so responsive image scaling
does not change source coordinates. The mask is primary visual evidence and
the mask-derived rectangle is secondary compatibility state; both are replaced
as one current suggestion.

Reset ROI removes the mask, rectangle, marker, status, and assisted handle
state. A new tap can start immediately. A failed, timed-out, unavailable, or
stale request clears any assisted preview while preserving manual editing. A
manual drag, move, or keyboard adjustment clears the mask because the user's
rectangle is authoritative. Manual rectangle drawing remains the fallback when
the optional model is unavailable.

The preview is transient browser/API data only. It is not a manifest field,
durable artifact, or Phase 6 confirmation record. Dense adjacent shoes or other
visually connected objects may still merge into one silhouette and require
manual correction; the UI makes no identity or tracking claim.

## Phase 5-4A external ROI status

The reference image is reserved for selection evidence: the source JPEG,
transient assisted silhouette, secondary rectangle, and essential tap marker.
General workflow messages now use the single existing live status region below
the image, followed by assisted guidance and the ROI controls. The status keeps
explicit ready, active/loading, success, warning, error, unavailable, and
disabled state metadata so meaning is not conveyed by color alone, and its
polite live semantics announce asynchronous changes without duplicating status
regions. Status text remains in the current language; Phase 5-4C localization
is not part of this change.

Reset ROI clears the silhouette, rectangle, marker, pending assisted state, and
external status back to ready guidance. Existing generation checks prevent a
late response from restoring old visual or textual state. Manual mouse, touch,
pen, and keyboard editing remains authoritative and uses the same external
status region.

## Phase 5-3B scope

Phase 5-3B-1 is complete as the disposable validation boundary above,
Phase 5-3B-2 is complete as the optional backend boundary, Phase 5-3B-3 is
complete as the explicit frontend invocation boundary, and Phase 5-3C is
complete as the transient silhouette preview boundary. Phase 5-4A is complete
as the external status boundary described above. Remaining Phase 5 work
is manual desktop/mobile acceptance plus any controlled loopback backend smoke
validation. The backend and frontend must not add ROI confirmation or persistence,
manifest updates, investigation creation, tracking, recognition, disappearance
reasoning, multiple ROIs, polygon/mask editing, NVR/replay/decoder changes, or
automatic selection without a tap.

Phase 6 is not started. There is no durable ROI or manifest write, no automatic
investigation start, and no second authoritative ROI representation.
