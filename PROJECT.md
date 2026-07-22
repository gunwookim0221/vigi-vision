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
analysis, reports, and event reasoning downstream.

## Current priorities

1. Preserve the public SDK / Vision ownership boundary in subsequent work.
2. Retain the completed live inspection pipeline and profile registry baseline.
3. Keep recording retrieval bounded and credential-safe while preserving its
   narrow boundary with the shared local-video analysis workflow.
4. Preserve the Investigation Plan, Collection, and Artifact boundaries while
   the next increment decides how investigation artifacts enter existing
   analysis.

## High-level roadmap

1. **Foundation (complete):** establish the project harness and boundaries.
2. **First Working Slice (complete):** choose and prove one user-visible
   workflow with its acceptance checks.
3. **Vertical implementation (complete):** connect the CLI, AI workflow, and
   SDK in the smallest working path.
4. **Hardening:** improve tests, errors, documentation, and operability based on
   observed MVP needs.
5. **Expansion:** consider additional investigation capabilities only after the
   MVP proves demand.
