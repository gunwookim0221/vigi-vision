# Object Disappearance Investigation

## Status and purpose

**Status: approved Phase 1 design direction; Phase 5 implements the transient
source-space ROI workflow and Phase 6-1 now approves the confirmation and
durable persistence contract. The Phase 6 backend/web implementation, cropped
preview, and disappearance reasoning remain unimplemented.**

This document defines the first bounded use case for VIGI Vision's longer-term
Event Discovery direction: a user investigates one selected object on one NVR
channel and determines when it was no longer visible at its original location.
Shoes are a representative initial scenario, not a reusable domain type or a
product-specific architectural name.

The aim is a verifiable, review-oriented workflow that can validate reusable
recording, region-selection, temporal comparison, evidence, and review-clip
capabilities before the project attempts generic event discovery.

## Problem statement

Reviewing a long fixed-camera recording to find when a known object disappeared
from a known place is slow and prone to false precision. A user needs a bounded
way to identify a credible change interval, inspect the surrounding footage,
and retain enough evidence to decide whether further human review is needed.

The initial result is deliberately limited to the selected region. It can say
that the object appears to have left its original location or is no longer
visible in that region; it cannot say where it went or why.

## Relationship to generic Event Discovery

Object disappearance investigation is the first concrete slice of future
generic Event Discovery. It does not replace that direction. It gives Event
Discovery a measurable foundation for:

- retrieving a recording frame at a requested time;
- accepting and preserving a manually selected region of interest (ROI);
- evaluating regional presence over time;
- narrowing a candidate change interval;
- producing a human-review clip; and
- recording credential-free evidence and results.

Later work may use these capabilities for object relocation, entrance or exit
events, broader object types, automatic candidate discovery, and optional VLM
interpretation. None of those extensions are part of this design's MVP.

## User workflow

The approved intended workflow is:

1. The user chooses one fixed NVR channel, a reference time, and a forward
   search end time. The default source timezone is `Asia/Seoul`, consistent
   with current recording sampling and investigation input boundaries.
2. The system retrieves a frame at the reference time.
3. The user draws a rectangular ROI around one object, reviews a cropped
   preview, and confirms the selection.
4. The system samples forward through the selected time range, classifies the
   original region conservatively, and narrows a candidate change interval.
5. The system presents the last confirmed-present time, first confirmed-absent
   time, supporting evidence, confidence information, and a review-required
   indication.
6. The system produces a review clip beginning 10 seconds before and ending
   30 seconds after the candidate change, subject to recording availability and
   later implementation decisions.

## Current reusable capabilities

The following are **implemented today** and form the foundation; they do not
yet perform disappearance investigation:

- [Recording retrieval](../integrations/recording-retrieval.md) plans a
  credential-free replay request through the public SDK and extracts one
  bounded temporary MP4. It applies a client-side duration limit and bounded
  timeout, then removes partial output on failure.
- [Recording sampling](recording-sampling.md) resolves covered NVR ranges,
  divides them into bounded replay chunks, extracts timestamped JPEGs at a
  requested cadence, records gaps, and writes credential-free manifests.
- Local-MP4 sampling extracts representative temporary frames for the existing
  bounded video-analysis workflow. It is useful evidence that VIGI Vision has
  an ffmpeg frame-extraction boundary, but it is not a selectable recording
  reference-frame service or a presence comparison service.
- The current [Investigation Plan](investigation-plan.md) and artifact flow can
  preserve replay clips, create one local-MP4 anchor snapshot per collected
  clip, and write a credential-free manifest. That anchor snapshot is tied to
  the existing fixed multi-camera investigation package, not a user-selected
  object workflow.

Existing OpenAI profile analysis and business reports remain separate from
this proposed workflow. They must not be treated as an implementation of
object-presence or disappearance classification.

## Capabilities not yet implemented

The repository does **not** currently provide:

- an implemented API for persistent ROI selection, crop previews, or
  confirmation. The
  Phase 5-1/5-2 loopback shell now provides one transient rectangular ROI over
  a selected reference frame. It uses one Pointer Events path for mouse,
  touch, and pen, accepts one active pointer, scopes `touch-action: none` to
  the image, clamps and rounds original-image pixel coordinates, supports
  bounded movement, eight-handle resize, reset/recreate, keyboard edits, and
  an immutable Phase 6 handoff snapshot. It rejects rectangles below 4×4
  source pixels and clears state on candidate/result/image lifecycle changes.
  The [Phase 6-1 confirmation contract](investigation-confirmation.md) now fixes
  the future API, immutable package, integer source-pixel ROI, provenance, and
  Phase 7 loader boundary; implementation remains deferred.
- implemented storage for the canonical source-pixel ROI;
- regional presence classification, temporal comparison, or disappearance
  reasoning;
- coarse-to-fine search, confirmed change intervals, or review clips;
- a result schema/API for disappearance outcomes.

## Initial MVP scope

The approved MVP boundary is one user-selected object on one fixed NVR camera
channel. The user supplies the channel, reference time, and search end time;
the search proceeds only forward from the reference time. The ROI is a
rectangle on the reference frame.

The confirmation contract retains one canonical integer rectangle in original
source pixels with exact source dimensions. Normalized coordinates are derived
rather than persisted because a second rounded representation could disagree.
The inline review shows the selected image and ROI; it need not persist a
separate crop solely for confirmation.

The MVP detects only whether the selected object is no longer present at its
original location. It reports a bounded change interval rather than a falsely
precise instant.

## Explicit non-goals

The MVP does not promise to:

- locate the object elsewhere in the frame or prove relocation;
- determine theft, cause, intent, or a responsible person;
- identify, track, or correlate people across cameras;
- perform face recognition or automatic object-category recognition;
- make identity, payment, causal, or unsupported continuous-tracking claims;
- replace the existing recording retrieval, sampling, analysis, or
  investigation workflows.

`MOVED` and `OCCLUDED` are possible future refinements. They are not MVP
outcomes and must not be inferred from an `ABSENT` result.

## Domain terminology

- **Reference frame:** the recorded frame at the user-selected reference time.
- **ROI (region of interest):** the user-confirmed rectangular region on the
  reference frame that contains the selected object.
- **Original location:** the ROI's spatial position in the source frame; it is
  not a claim that the object remains stationary outside the observed evidence.
- **Observation:** one time-stamped evaluation of the original location.
- **Change interval:** the interval between the last confirmed `PRESENT`
  observation and the first confirmed `ABSENT` observation.
- **Review clip:** intended footage spanning 10 seconds before through 30
  seconds after the candidate change.

## Investigation lifecycle

The proposed lifecycle is:

```text
validated channel and times
  -> reference-frame retrieval
  -> ROI selection and confirmation
  -> recording coverage and temporal observations
  -> candidate interval refinement
  -> evidence and review-clip generation
  -> review-oriented result
```

The implemented replay and sampling boundaries may be reused at appropriate
steps, but later implementation must keep each responsibility explicit rather
than embedding comparison or event logic into those boundaries.

## Proposed temporal search strategy

The following are **configurable proposed defaults**, not irreversible API
contracts or validated accuracy claims:

1. Start with a coarse forward scan at five-minute intervals.
2. Find the first candidate `PRESENT -> ABSENT` interval.
3. Use binary-style interval refinement only where observations are sufficiently
   stable for it to be meaningful.
4. Inspect sequentially at approximately one-second intervals in the final
   minute.
5. Require three consecutive `ABSENT` observations before confirming a change.
6. Treat `INDETERMINATE` conservatively: it must not silently confirm absence.
7. Report the last confirmed `PRESENT` time and the first confirmed `ABSENT`
   time.

Pure binary search is unsafe as a general rule because visibility may be
non-monotonic. Occlusion, temporary movement, lighting changes, decode
variation, or an object reappearing can invalidate the assumption that every
later observation stays absent. The implementation must retain a sequential or
otherwise conservative path whenever that assumption does not hold.

## Presence-state model

The initial design uses three states:

| State | Intended meaning | Required handling |
| --- | --- | --- |
| `PRESENT` | The selected object is sufficiently supported as visible in its original region. | May support a last-confirmed-present bound. |
| `ABSENT` | The selected object is sufficiently supported as no longer visible in its original region. | Requires the configured consecutive-observation confirmation before a change is confirmed. |
| `INDETERMINATE` | Evidence is insufficient because of obstruction, image quality, lighting, framing, or comparison uncertainty. | Must preserve uncertainty and cannot silently become `ABSENT`. |

The thresholds, model outputs, and transition rules are unresolved experimental
decisions. `MOVED` and `OCCLUDED` remain future-state candidates only after
representative evidence demonstrates that they can be distinguished safely.

## Intended artifacts and evidence

The intended user-facing result contains:

- last confirmed-present time;
- first confirmed-absent time;
- a review clip from 10 seconds before to 30 seconds after the candidate
  change;
- evidence and confidence information; and
- an explicit review-required indication.

The exact result schema, manifest fields, artifact directory structure, and
HTTP/API contract are not finalized in Phase 1. Any future artifact design must
retain the repository's credential-free principles: no usernames, passwords,
hosts, authenticated RTSP/replay URLs, ffmpeg arguments, or raw subprocess
diagnostics in manifests or user-facing output.

## Failure and uncertainty handling

Planned behavior must distinguish unavailable recording, authentication or
replay extraction failure, frame retrieval failure, invalid ROI/source-frame
mapping, and insufficient or indeterminate visual evidence. A failure or
uncertain observation must not be rendered as a confirmed disappearance.

If a candidate interval cannot be supported, the result should remain
review-required or incomplete with a safe category. It must not fabricate a
time, location, cause, identity, or confidence level. Cleanup rules for future
temporary clips and frames must remove only invocation-owned temporary files
and preserve no authenticated paths in diagnostics.

## Privacy and safety boundaries

This is a local-PC MVP direction for surveillance footage and must preserve the
project's existing safety posture:

- human review remains required;
- no face recognition, person identity, payment, theft, causality, or
  continuous tracking claims;
- artifacts and manifests remain credential-free;
- surveillance footage and derived frames remain local, ignored artifacts and
  must not be committed;
- uncertainty is displayed rather than hidden behind a categorical result.

## Technical direction

The intended implementation sequence is a Python internal service boundary,
then a minimal FastAPI backend, then a React + TypeScript + Vite frontend.
Konva.js or an equivalent canvas library is a future candidate for rectangular
ROI selection. Local-PC deployment is the initial target.

The image-comparison method is deliberately unresolved. SSIM, color
histograms, ORB, learned embeddings, or another technique may be evaluated
against representative fixed-camera footage. No method, threshold, or accuracy
claim is selected by this document.

## Phased delivery plan

1. **Phase 1 (this document):** scope, terminology, safety boundaries, and
   experimental success criteria.
2. **Phase 2:** detailed reference-frame extraction service and minimal HTTP
   boundary.
3. **Phase 3:** reference-frame display, manual ROI selection, source-pixel
   coordinate mapping, review, and confirmation.
4. **Phase 4:** presence-classification experiment using representative
   fixed-camera footage.
5. **Phase 5:** coarse and refined temporal search with conservative state
   handling.
6. **Phase 6:** review-clip generation and result presentation.
7. **Phase 7:** object relocation, automatic detection, broader event types,
   generic Event Discovery, and optional VLM interpretation.

These phases describe intended dependency order, not a public delivery
commitment. In the current repository delivery sequence, Phase 6-1 is the
approved confirmation/persistence design, Phase 6-2 is backend publication,
Phase 6-3 is the inline web flow, Phase 6-4 is real-NVR validation, and Phase 7
consumes only the resulting confirmed typed input.

## Success criteria

The MVP should eventually demonstrate, with representative fixed-camera
evidence, that:

- a valid requested channel and time can retrieve a recorded reference frame;
- a user can select and confirm one object region;
- the selection stays correctly mapped to source-frame resolution;
- a confirmed-present reference can be distinguished from a clearly absent
  case;
- uncertain or obstructed cases do not silently become confirmed absence;
- the result is a bounded change interval, not unsupported timestamp precision;
- the review clip contains the relevant transition in the representative
  scenario;
- manifests and user-visible output contain no credentials; and
- existing recording, replay, sampling, and analysis workflows are unaffected.

Algorithm-specific accuracy thresholds remain open until representative
footage, annotation rules, and evaluation data exist.

## Known risks and open questions

- How accurately can future reference-frame and review-clip extraction map a
  requested source time to decoded media on the target NVR?
- What sampling cadence and maximum search duration are operationally reliable?
- How should ROI coordinates behave when future extraction outputs vary in
  resolution, rotation, or aspect ratio?
- Which comparison method and conservative thresholds perform acceptably under
  lighting shifts, reflections, occlusion, compression, and temporary object
  movement?
- How should the UI explain `INDETERMINATE` and request human review without
  implying a conclusion?
- What evidence is sufficient to label a transition candidate while preserving
  the distinction between observation and inference?

## Future expansion path

Once the MVP has demonstrated reliable, reviewable presence changes, future
work may add object relocation within a frame, broader object and event types,
automatic candidate selection, multi-camera correlation only where evidence
supports it, generic Event Discovery, and optional VLM interpretation. Each
extension requires its own contract, safety review, and evaluation evidence;
none follows automatically from an `ABSENT` classification.
