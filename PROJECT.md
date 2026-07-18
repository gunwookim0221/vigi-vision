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

Build Week should establish and then prove a narrow MVP through end-to-end
increments. This foundation session creates the charter, working guide,
documentation router, and Python project skeleton only.

## Repository boundary

This repository owns the AI application. The neighboring
`tp-link-vigi-sdk` repository owns authentication, OpenAPI communication,
camera interaction, snapshots, SDK tests, SDK documentation, and SDK CI.
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
- Designing now for recording search, receipt OCR, timestamp matching, event
  search, clip extraction, or multimodal investigation.
- Product functionality during the foundation session.

## Current phase

Sessions 2 and 3 implementation are complete. The First Working Slice has a
typed CLI, public-SDK NVR and standalone-IPC RTSP adapters, a one-frame ffmpeg
extraction boundary, and an OpenAI image-analysis boundary. Session 3 adds
profile-based analysis of previously captured frames for counter, dining, and
entrance tasks without changing the live capture pipeline.

## Current priorities

1. Preserve the public SDK / Vision ownership boundary in subsequent work.
2. Retain the completed live inspection pipeline and profile registry baseline.

## High-level roadmap

1. **Foundation (complete):** establish the project harness and boundaries.
2. **First Working Slice (next):** choose one user-visible workflow and its
   acceptance checks.
3. **Vertical implementation:** connect the CLI, AI workflow, and SDK in the
   smallest working path.
4. **Hardening:** improve tests, errors, documentation, and operability based on
   observed MVP needs.
5. **Expansion:** consider additional investigation capabilities only after the
   MVP proves demand.
