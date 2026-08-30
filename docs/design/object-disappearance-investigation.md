# Object Disappearance Investigation

## Status and purpose

**Status: approved object-disappearance direction; Phases 2-6 implement
reference-frame retrieval, candidate review, source-space ROI selection, and
immutable confirmation. Phase 7A-1 implements its validated local run lifecycle
and safe start/status boundary. Phase 7A-2 implements its acquisition-only
schema-2 request/frame persistence and strict reopen boundary. Phase 7B
implements bounded single-probe classification and immutable schema-3
observation persistence and strict reopening. Phase 7C-1 implements the
deterministic chronological coarse plan and sequential A2/B4 execution
foundation. Phase 7C-2 implements only the pure non-persistent interpretation
handoff. Phase 7D-1 now implements deterministic non-terminal binary narrowing
through the existing A2/B4 boundaries. Phase 7D-2 D2-2 now implements the pure
terminal outcome interpreter, strict reconstruction, and in-memory result
identities; D2-3 now implements canonical lock-ordered schema-4 terminal
publication and strict readback; D2-4 adds strict terminal reopen/status
projection; D2-5 adds the strict FOUND-only Phase 8 request handoff and durable
request persistence. The terminal path reloads post-D1 schema-3 evidence under
the mutation boundary and independently reconstructs persisted FOUND facts on
reopen. Phase 7E feasibility work proves that current VIGI replay timestamps
 are request-relative and cannot supply authoritative physical frame UTC. The
 implemented Phase 7E-1C boundary now owns one bounded replay/decode session,
 durable media retention, exact local frame selection, and persisted-frame
 admission. Phase 7E-1D now owns the Phase 7E C1 adapter, adaptive evidence
 composition, schema-7 terminal publication, strict readback, and safe status.
 The single normative Phase 7E contract uses one replay/decode session, one SDK
segment, a five-minute default, a hard 600-second search maximum, an exact
2,520-second invocation ceiling, schemas 5→6→7, and a separate FOUND-only Phase
8 projection repository. The 7E-1C common-session and 7E-1D terminal
orchestration boundaries are implemented; CLI integration, Phase 8
review-media processing, and Phase 9 remain unimplemented; schemas 1–4 retain
their original strict meaning.**

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
5. Phase 7 persists the last supported-present and first confirmed-absent bounds
   plus supporting evidence and limitations.
6. Phase 8 creates review images and video around the candidate boundary.
7. Phase 9 presents that material and leaves the final decision to the user.

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
- [Investigation confirmation](investigation-confirmation.md) publishes immutable
  schema 3 confirmations with JPEG digest/size binding and a strict typed
  loader. Existing schema 2 packages remain readable but require explicit
  Phase 6C reconfirmation before search.

Existing OpenAI profile analysis and business reports remain separate from
this proposed workflow. They must not be treated as an implementation of
object-presence or disappearance classification.

## Capabilities not yet implemented

The repository does **not** currently provide:

- the schema 5-7 request-relative production path or synchronous search CLI;
- Phase 8 boundary images, evidence timeline, or review clip; or
- a Phase 9 operator review surface for recording-search results.

Phase 7B now provides a bounded single-probe classifier, deterministic outcomes,
schema-3 observation publication, and strict reopening. Those outcomes describe
one admitted probe only; they are not chronological disappearance-search
conclusions. Phase 7C-1 now provides the deterministic chronological target plan
and sequential A2/B4 execution foundation. Phase 7C-2 now provides only the
strict, non-persistent transition interpretation handoff. The provisional
thresholds are policy inputs for deterministic
processing, not accuracy claims.

The existing Phase 5 browser and Phase 6 backend already provide transient ROI
editing, strict confirmation, durable canonical source-pixel ROI storage, and
the typed `load_confirmed()` boundary. Phase 7 must consume that boundary rather
than recreate browser or confirmation state.

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
- **Observation:** one evaluation of the original location. Current VIGI
  production binds it to a requested target and a request-relative decoder
  estimate, not an authoritative physical UTC.
- **Requested-time change interval:** the requested-time interval between the
  last supported `PRESENT` bound and the first supported `ABSENT` group. It is
  not an exact disappearance instant or physical-time confidence interval.
- **Review clip:** intended footage spanning 10 seconds before through 30
  seconds after the candidate change.

## Investigation lifecycle

The proposed lifecycle is:

```text
validated channel and times
  -> reference-frame retrieval
  -> ROI selection and confirmation
  -> Phase 7A-2 recording acquisition
  -> Phase 7B single-probe observations
  -> Phase 7C/7D candidate interval
  -> Phase 7E durable source clip and handoff
  -> Phase 8 review images and video
  -> Phase 9 user decision
```

The implemented replay and sampling boundaries may be reused at appropriate
steps, but later implementation must keep each responsibility explicit rather
than embedding comparison or event logic into those boundaries.

## Approved temporal search strategy

[Phase 7 Object-Disappearance Recording Search MVP](object-disappearance-recording-search.md)
defines the current single-site policy. It samples chronologically from the
confirmed requested time at a five-minute coarse interval and includes one
logical end-boundary target. That target selects only the greatest eligible
same-session decoded frame strictly before the half-open end; other targets use
nearest-frame selection with earlier-tie preference. A supported
`PRESENT -> confirmed ABSENT` bracket then narrows at deterministic whole-second
midpoints until it is one second wide.

The complete half-open search interval must be no longer than 600 seconds and
must fit entirely inside one indexed SDK recording segment. The complete coarse
grid, all binary revalidation, support frames, terminal evidence, and the Phase
7E source clip belong to one immutable common replay/decode session. A request
that crosses a segment boundary, has ambiguous/touching coverage, or exceeds
600 seconds fails before visual interpretation; independent sessions cannot be
combined to establish a transition.

A single `ABSENT` frame never proves disappearance. For current VIGI production,
the absence rule acquires the policy count (three by default) at the requested
cadence (one second by default) from one wider replay and one continuous decode
session. Requested target, local PTS, normalized offset, decode ordinal, and
request-relative estimate must increase strictly; frame/observation identities
and canonical decoded RGB24 digests must be distinct. An alias, repeated
content, session change,
gap, incomplete support, or operational failure supplies no absence evidence.
Overlapping independent requests are never treated as physical-frame identity.
`INDETERMINATE` is visual only. The MVP does not introduce adaptive grids,
leases, fencing, automatic takeover, or resume.

## Presence-state model

The initial design uses three states:

| State | Intended meaning | Required handling |
| --- | --- | --- |
| `PRESENT` | The selected object is sufficiently supported as visible in its original region. | May support a last-confirmed-present bound. |
| `ABSENT` | The selected object is sufficiently supported as no longer visible in its original region. | Requires the configured consecutive-observation confirmation before a change is confirmed. |
| `INDETERMINATE` | Evidence is insufficient because of obstruction, image quality, lighting, framing, or comparison uncertainty. | Must preserve uncertainty and cannot silently become `ABSENT`. |

The initial production policy is `efficient-sam-ti-roi-ncc-v1`: EfficientSAM-
Ti supplies masks, while a local aligned-ROI comparator maps persisted mask-IoU
and luma-NCC thresholds deterministically to the three states. EfficientSAM does
not establish object identity by itself. The exact input, geometry, error,
identity, persistence, and state contract is in
[Phase 7B Recording-Probe Object-Presence Classification](object-presence-classification.md).
Phase 7E must validate this conservative policy on representative NVR frames
before deployment. Unsupported media, media-type mismatch, decode failure,
invalid decoded structure, unsupported channel layout, deterministic
RGB-normalization failure, and preprocessing input failure are operational
`invalid_media_input` failures: they publish no `RawComparison`, visual
observation, alias, Phase 7B operation, schema-3 promotion, or authoritative
manifest mutation, and they never produce `PRESENT`, `ABSENT`, or
`INDETERMINATE`. Only successfully decoded and evaluated visual evidence that
reaches a closed quality, comparability, or policy-gap outcome may be
`INDETERMINATE`. `MOVED` and `OCCLUDED` remain future states.

## Intended artifacts and evidence

The intended user-facing result is one closed `FOUND`, `NOT_FOUND`, or
`INCONCLUSIVE` result. Under request-relative provenance, `FOUND` contains the
last supported-present and first supported-absent requested-time bounds, labels
physical-origin error as unknown/unbounded, and is the only kind eligible for a
Phase 8 handoff request. It never claims an exact physical event time.
`NOT_FOUND` means only that the complete configured, nonduplicated requested
coarse grid remained `PRESENT`; it is not continuous-presence proof.
`INCONCLUSIVE` preserves a validated visual limitation or contradiction. All
kinds expose limitations and keep Phase 9 human review authoritative.
Operational failure is not a visual result.

Phase 7A-1 now defines and implements one run ID and directory, a compact
lifecycle manifest, strict baseline validation, duplicate/interruption handling,
and a safe start/status HTTP boundary. The contract-defined Phase 7A-2 slice adds
only bounded replay acquisition: immutable request records, canonical decoded
frame records, run-relative JPEGs, and distinct frame/request indexes. Phase 7B
now adds one-admitted-probe recording observations, deterministic outcomes, and
strict schema-3 publication/reopen. It does not create chronological search
conclusions or the Phase 8 handoff; those remain later slices.
Artifacts retain the repository's
credential-free principles: no usernames, passwords, hosts, authenticated
RTSP/replay URLs, ffmpeg arguments, raw subprocess diagnostics, or absolute
paths enter manifests or user-facing output.

## Failure and uncertainty handling

Planned behavior must distinguish unavailable recording, authentication or
replay extraction failure, frame retrieval failure, invalid ROI/source-frame
mapping, and insufficient or indeterminate visual evidence. A failure or
uncertain observation must not be rendered as a confirmed disappearance.

If a requested-time interval cannot be supported, the run is visual
`INCONCLUSIVE` only for a validated visual limitation; otherwise it is an
administrative safe failure or interruption. It must not fabricate a time,
location, cause, identity, confidence level, or physical error bound. Cleanup
rules remove only invocation-owned temporary files and preserve no authenticated
paths in diagnostics.

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

Phase 7 begins with a unique `search_run_id`, one run directory, one compact
manifest, and one per-investigation OS-backed lock. A concurrent start is
rejected. If a process exits, the next inspection marks its nonterminal run
`INTERRUPTED`; no process automatically resumes or takes it over, and an
explicit new attempt uses a new ID without adopting old evidence.

The implemented schemas 1-4 retain the original strict physical-origin
contract, classification, coarse interpretation, narrowing, terminal result,
  and FOUND-only Phase 8 request. Phase 7E feasibility work proved that the current VIGI
SDK/RTSP/FFmpeg boundary cannot provide the required physical frame UTC, so new
production runs must not enter or reinterpret that family.

The Phase 7E successor begins with pre-acquisition
`REQUEST_RELATIVE_ESTIMATE` schema 5, atomically creates zero-evidence schema 6
after common-session media admission, incrementally persists/reopens frames
before B4 observations, and atomically publishes immutable schema 7. Its decoder derives an estimate only from
replay requested start plus normalized session-local offset, with exact rational
conversion and unknown/unbounded physical-origin bias. A new domain-separated
identity family binds request/session provenance without claiming cross-window
physical-frame identity. The single-session support rule, strict distinctness,
complete-grid NOT_FOUND rule, and operational-versus-visual separation fail
closed when that evidence is insufficient.

The normative schema-5/6 matrices never resume partial work. Phase 7E binds the
full B4 classifier policy and typed evidence rather than a mutable label. Its
26-family identity graph and inline binary JPEG/MP4 fixture prove strict reopen.
Phase 8 uses exact closed state memberships and separates semantic source-clip
identity from encoded-byte integrity.

The Phase 6 baseline remains a historical `PRESENT` authorization and may seed
planning, but it is not recording-session evidence. A baseline plus a first
recording `ABSENT` cannot establish automatic `FOUND`; at least one recording
`PRESENT` and the same-session policy-count `ABSENT` support are required.
Otherwise the public outcome is non-FOUND `INCONCLUSIVE` with
`BASELINE_ONLY_LOWER_BOUND` and human review is required.

Execution will be one synchronous local `search-recordings` CLI invocation that
retains the existing process-owned handle through terminalization. The existing
GET status stays read-only; the existing POST will return a fixed HTTP 503 and
create no run until a separately designed HTTP executor exists. No Phase 7
frontend, background worker, lease, takeover, or resume behavior is part of the
approved direction.

The selected comparator uses the existing verified EfficientSAM-Ti point-mask
path plus aligned source-ROI mask IoU and mean-centered luma correlation. Its
exact policy and thresholds are normative in the Phase 7 design; they are
conservative initial values, not accuracy claims, and any tuning requires a new
version after Phase 7E evidence.

## Phased delivery plan

1. **Phases 1-5 (implemented foundation):** bounded scope, durable
   reference-frame resources and candidates, source-space ROI review, and
   assisted/manual editing.
2. **Phase 6 (schema 2 and schema 3 compatibility implemented):** strict
   immutable confirmation publication, explicit legacy reconfirmation, and
   digest-bound typed Phase 7 loading.
3. **Phase 7 (A-1 through 7D implemented; 7E design pending final approval):** single-host run lifecycle,
   interruption/new-run isolation, truthful baseline provenance, and
   acquisition-only request/frame records with canonical frame identities,
   run-relative JPEG publication, strict acquisition indexes, bounded
   single-probe three-state classification, and immutable schema-3 observations.
   Phase 7C-1 now adds the deterministic five-minute (policy-snapshot) coarse
   target plan and sequential acquisition/classification execution, plus the
   pure non-persistent transition interpretation handoff, and deterministic
   non-terminal binary narrowing to a one-second in-memory bracket. Phase 7D-2
   D2-2 now provides pure terminal interpretation and identity construction;
  D2-3 now provides atomic terminal persistence and strict readback, D2-4
  provides strict reopen/status projection, and D2-5 provides the durable
  FOUND-only Phase 8 handoff request. Phase 7E keeps its immutable schema-7
  result separate from the FOUND-only Phase 8 source-clip/handoff repository;
  Phase 8 owns later review-media processing. FOUND terminalization persists a
  validated lossless D1 reconstruction envelope and never downgrades schema 4
  after its commit point. The Phase 7E contract preserves one common session,
  one segment, a 600-second search ceiling, and a 2,520-second invocation
  ceiling. Ordered 7E-1A/1B/1C/1D, 7E-2, and 7E-3 implementation/validation
  are in progress: 1A and 1B are implemented, and 1C now implements one
  bounded replay/remux, durable `.media`, exact local target selection, RGB24,
  and persisted-frame A2/B4 admission. Slice ownership is: 1A
  identities/models/validation; 1B pre-acquisition schema 5 and incremental
  schema 6; 1C retained MP4,
  exact logical-target frame selection, RGB24, and A2/B4; 1D the Phase
  Phase 7E C1 adapter (`S`, logical `E`, shared explicit
  `BACKWARD_FROM_END` support mode with legacy default `FORWARD`), source
  reconstruction, and immutable schema 7; 2 synchronous CLI and separate
  Phase 8 projection/retention; 3
  bounded real-NVR acceptance and fault injection.
4. **Phase 8 (future):** boundary images, evidence timeline, and review video.
5. **Phase 9 (future):** user-facing review and final human decision.
6. **Later work:** object relocation, automatic detection, broader event types,
   generic Event Discovery, and optional VLM interpretation under separate
   contracts.

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
- Phase 8 can resolve the Phase 7 handoff into truthful review media or report
  unavailable coverage without constructing a gap-crossing clip;
- manifests and user-visible output contain no credentials; and
- existing recording, replay, sampling, and analysis workflows are unaffected.

The current versioned classifier thresholds are provisional operating values,
not accuracy claims. Phase 7E must validate them against representative footage,
annotation rules, and evaluation data before deployment; any tuning creates a
new policy version.

## Known risks and open questions

- Current VIGI replay provides no measured physical-origin accuracy. Validation
  may measure target-selection distance and operational consistency, but must
  retain `UNKNOWN_UNBOUNDED` physical-origin bias unless a new authoritative
  transport capability is independently proven.
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
