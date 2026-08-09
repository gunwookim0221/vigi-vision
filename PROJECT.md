# VIGI Vision Project Charter

VIGI Vision is designed around Context Engineering and Harness Engineering so
humans and AI coding agents can recover the project's intent, boundaries, and
current direction from a small, durable set of documents. Documentation is part
of the development harness: it should reduce repeated discovery, stay aligned
with the implementation, and grow only when it earns its maintenance cost.

## Mission

Turn natural-language investigation requests into useful results from TP-Link
VIGI camera data while keeping AI concerns separate from the underlying SDK.

## One-line project pitch

VIGI Vision is a small AI-assisted workflow for asking questions about VIGI
camera data and receiving grounded, understandable results.

## Engineering philosophy

- Build the smallest working slice that can be understood and maintained.
- Prefer clear boundaries and direct code over speculative abstractions.
- Treat documentation, tests, and executable tooling as one development harness.
- Preserve extension points only when today's implementation makes them real.

## Project goals

- Own natural-language understanding, AI orchestration, OpenAI integration,
  image analysis, result generation, and the user-facing CLI workflow.
- Build on the existing TP-Link VIGI Python SDK rather than duplicating it.
- Keep each increment working, testable, and easy for future sessions to extend.

## Build Week objective

Build Week established and proved a narrow MVP through end-to-end increments.
The foundation session created the charter, working guide, documentation router,
and Python project skeleton; later sessions extended that foundation with the
working capture, analysis, reporting, and recording-retrieval slices.

## Repository boundary

This repository owns the AI application. The neighboring
`tp-link-vigi-sdk` repository owns authentication, documented OpenAPI
communication, camera and recording metadata access, stream URL construction,
SDK tests, SDK documentation, and SDK CI.
AI-specific logic must never be moved into the SDK.

## Success criteria

- A user can complete one useful camera-investigation workflow end to end.
- Results are grounded in retrieved camera data and failures are understandable.
- The application-to-SDK boundary remains explicit.
- A new contributor or coding agent can orient quickly from `PROJECT.md`,
  `AGENTS.md`, and the documentation router.

## Explicit non-goals

- Rebuilding or embedding the TP-Link VIGI SDK.
- Enterprise architecture, broad provider frameworks, or speculative services.
- Designing now for receipt OCR, timestamp matching, event search, or
  multimodal investigation.

## Current phase

Sessions 2–7 are complete. The First Working Slice has a typed CLI,
public-SDK NVR and standalone-IPC RTSP adapters, a one-frame ffmpeg extraction
boundary, and an OpenAI image-analysis boundary. Session 3 added profile-based
analysis of previously captured frames for counter, dining, and entrance tasks
without changing the live capture pipeline. Session 4 added explainable business
reports from the same single structured model response; the structured analysis
remains authoritative. Session 5 added bounded local-MP4 analysis: a 30-second
cap, 2–10 ordered samples, one OpenAI request, temporary frame cleanup, and
evidence-grounded temporal reports. Session 6B added a reusable recording
retrieval layer that plans UTC replay from public SDK recording search results,
extracts a bounded temporary MP4 with ffmpeg, and returns it without invoking
OpenAI, video analysis, reports, or a public CLI. Session 7 connected that
stable retrieval layer to the existing local-video analysis service through the
public `analyze-recording` command, preserving one temporal OpenAI workflow,
one explainable report format, and cleanup of both temporary replay clips and
sampled frames. Session 8B added a pure, deterministic Investigation Plan
contract: it converts the current Asia/Seoul product input to a canonical UTC
anchor, expands validated scenario role rules over assigned NVR channels, and
produces ordered existing `RecordingWindow` values without external I/O, media
collection, or AI work. Session 8C added the typed Investigation Collection
boundary, which processes every planned window independently through the
existing recording search and replay extraction interfaces, preserves plan
order, returns caller-owned successful replay clips, and isolates safe
per-item failures without analysis or reporting. Session 8D added the typed
Investigation Artifact boundary, which transfers successful replay clips into
deterministic durable investigation packages, creates one local-MP4 anchor
snapshot per clip with ffmpeg, writes a credential-free manifest, and leaves
analysis, reports, and event reasoning downstream. Session 8E added
`InvestigationService`, the single typed orchestration entry point that invokes
the existing planner, collector, and artifact builder once each without adding
media, AI, reporting, or storage behavior. Session 9A exposed that completed
workflow through the public `investigate` CLI command for the current fixed
restaurant-checkout deployment, preserving the service as the sole execution
path and keeping output credential-safe. Session 10 added the public NVR-only
`sample-recording` command: it resolves public-SDK recording coverage for a
source-time range, processes bounded replay chunks, writes generic timestamped
JPEGs and a credential-free manifest, records gaps, and preserves inspectable
partial artifacts on safe failures without OpenAI, semantic search, or SDK
changes.

Post-submission, recording retrieval and sampling are the existing foundation.
The user-selected object-disappearance investigation now has its Phase 1 design
and an internal Phase 2B reference-frame service: it selects one recorded NVR
segment, extracts a bounded replay, selects the exact ffprobe candidate by
decoded-frame index, validates and persists one credential-free reference JPEG
with truthful clip-relative timing evidence, and is covered by hermetic tests.
An initial real-NVR run produced a validated `2560x1440` JPEG while retaining
the conservative `measured_clip_relative` status. Phase 3B has implemented the
approved local synchronous FastAPI transport: it composes the existing service,
safely reuses compatible completed resources, and exposes a separate durable
JPEG endpoint without introducing frontend or background work. Phase 3C then
hardened that loopback boundary and validated it against the real NVR: an
initial request created a `2560x1440` JPEG and a repeated compatible request
reused the same resource, while timing remained conservatively
`measured_clip_relative`. Phase 4A added a bounded, loopback candidate-set API
around a user-entered reference time: it serially reuses the existing
single-frame service and child resource lifecycle, preserves partial results,
and does not change the single-frame API. Automated and observed real-NVR
validation are complete with conservative timing limitations retained. Phase 4B
adds a native browser shell at the same loopback application root: it submits
the default candidate request for a KST local time and renders ordered safe
result facts plus successful candidate thumbnails. Phase 4C-2 adds transient
exactly-one selection without durable persistence. Phase 4C-3 makes date/time
application explicit, shows whole-second timezone-aware summaries, and adds
accessible indeterminate generation feedback. Phase 5-1 adds one transient
source-pixel ROI drawn over the selected candidate through unified Pointer
Events, including scoped mobile touch behavior and interruption-safe reset.
Phase 5-2 adds transient move, eight-handle resize, reset/recreate, keyboard
editing, and a narrow Phase 6 handoff snapshot without persistence. Fixture
browser validation is complete; physical-device and real-NVR validation remain
pending. Phase 5-3A now records an acceptance-gated design for explicit
tap-assisted ROI suggestions through EfficientSAM-Ti. A disposable native
Windows CPU spike proved point-to-mask-to-bounded-box mechanics, but its public
fixture suggestion required manual correction and does not establish CCTV
shoe/bag quality. Phase 5-3B-1 adds a disposable offline validation harness
under `tools/` for real reference-frame artifacts, with explicit checkpoint
verification, resumable human classifications, source-space mask boxes, and
credential-free Markdown/session evidence; it does not add a suggestion API,
model dependency, UI control, or persistence to the product. An initial real-CCTV
run evaluated 16 of 55 discovered frames, with 15 useful successes and one
partial dense-shoe-rack merge (93.75% success among evaluated frames); the
harness remains evidence-volume limited at its 20-frame policy threshold. Phase
5-3B-2 adds the optional production assisted-ROI backend: a strict
resource-bound point suggestion route, lazy EfficientSAM-Ti lifecycle, verified
operator checkpoint configuration, dedicated inference limiter, bounded
timeouts, and safe unavailable/error categories. Phase 5-3B-3 now connects the
explicit frontend Tap-to-suggest interaction to that endpoint, validates
candidate-bound source-space responses, preserves the prior ROI while pending,
and keeps manual correction authoritative. Phase 5-3C now returns a bounded
exact source-row mask preview, paints the silhouette over the responsive image,
de-emphasizes assisted resize handles, and clears mask state on reset, failure,
or manual correction. The mask remains transient preview evidence and the
rectangle remains the canonical ROI. Neither increment adds ROI persistence or
Phase 6 behavior. The reference-frame path now
also
has a direct decoder for new resources. It retains structured clip-relative
timing evidence, stops after a validated selected JPEG, and uses generation
policy `gpv-2` without changing generic replay-clip behavior. Initial real-CCTV
validation is favorable; operator smoke validation and physical desktop/mobile
acceptance for assisted ROI remain pending. Phase 5-4A moves all general ROI
status feedback into the single external live region below the image, keeps the
image dedicated to source/mask/rectangle/tap evidence, adds explicit state
semantics, and preserves reset/stale-response cleanup plus manual fallback;
localization remains deferred to Phase 5-4C. Phase 6-1 approved the
confirmation contract: one reviewed candidate and canonical source-pixel ROI
currently publish an immutable schema 2 package under the existing investigation
artifact root, reference the immutable frame resource, preserve truthful nullable
absolute timing, and expose one strict loader boundary. Phase 6-2A now implements
the typed backend persistence foundation,
and Phase 6-2B exposes it through the existing safe FastAPI boundary. Phase
6-2C now connects the existing reference-frame/ROI page to that boundary with
inline Korean review, strict POST/GET confirmation, reopen restoration, and
immutable read-only success state. Phase 7 now has a deliberately small
single-site recording-search MVP design. Before search, Phase 6C adds the small
schema 3 compatibility increment: confirmation-time JPEG SHA-256 and byte size,
explicit read-only schema 2 reconfirmation into a new immutable identity, and
mismatch-safe loading. Search uses
one unique run ID and directory
per attempt, rejects concurrent starts with one local OS-backed lock, marks
abandoned nonterminal runs `INTERRUPTED`, and requires an explicit new run
instead of automatic resume or takeover. The search samples recordings every
five minutes, resolves absence support through one bounded multi-target decode,
deduplicates canonical frames, and uses the production EfficientSAM-Ti mask plus
aligned-ROI luma-NCC comparator before whole-second binary narrowing. Its compact
manifest persists the closed baseline/probe/alias union and full policy snapshot
without inventing stable source identity. Advanced lease, fencing, takeover,
resume, and crash-recovery analysis is preserved as non-normative future
reference. No Phase 7 runtime, schema 3 update, classifier, transport, Phase 8
review media, or Phase 9 result UI is implemented. Absolute source-time
calibration remains deferred; Phase 4C-2
provides transient frontend-only selection of one
successful candidate, and Phase 5-1/5-2 provide the transient source-space
editing surface without altering the API or evidence semantics.
Generic Event Discovery remains a longer-term direction rather than a current
capability.

## Current priorities

1. Preserve the public SDK / Vision ownership boundary in subsequent work.
2. Retain the completed live inspection pipeline and profile registry baseline.
3. Keep recording retrieval bounded and credential-safe while preserving its
   narrow boundary with the shared local-video analysis workflow.
4. Preserve the public investigation CLI and Investigation Service with their
   Plan, Collection, and Artifact boundaries while the next increment decides
   how completed investigation artifacts enter existing analysis.
5. Preserve the implemented bounded Phase 4A candidate API and Phase 5 ROI
   loopback review shell with explicit applied time, accessible generation
   feedback, ordered thumbnails, transient exactly-one selection, and one
   source-pixel ROI that can be moved, resized, reset, recreated, and edited
   with keyboard input. Preserve the Phase 5-3B-2 backend, the Phase 5-3B-3
   assisted-ROI request path, the Phase 5-3C silhouette preview, and the Phase
   5-4A external status boundary, then run operator smoke and physical
   desktop/mobile acceptance. Continue with Phase 6-4 real-NVR validation
   without changing recording or reference-frame
   ownership.
6. Implement the scope-reviewed single-site object-disappearance MVP in bounded
   slices: Phase 6C schema 3 compatibility, Phase 7A-1 local lifecycle, Phase
   7A-2 canonical multi-target acquisition, then Phase 7B-7E for the production
   three-state comparator;
   chronological coarse sampling and exhaustive support transitions; binary
   narrowing, terminal persistence, and Phase 8 handoff; then real-NVR
   validation. Preserve published Phase 6 package immutability, reject duplicate
   starts, never reuse interrupted-run evidence, and defer Phase 8 media plus
   Phase 9 judgment.

## High-level roadmap

1. **Foundation (complete):** establish the project harness and boundaries.
2. **First Working Slice (complete):** choose and prove one user-visible
   workflow with its acceptance checks.
3. **Vertical implementation (complete):** connect the CLI, AI workflow, and
   SDK in the smallest working path.
4. **Hardening:** improve tests, errors, documentation, and operability based on
   observed MVP needs.
5. **Expansion:** implement and validate the designed Phase 7 user-selected
   object-change search before Phase 8 evidence creation, Phase 9 user judgment,
   broader event types, object relocation, generic Event Discovery, or optional
   VLM interpretation.
