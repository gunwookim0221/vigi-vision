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

Foundation complete. The repository contains only the permanent project
guidance, documentation routes, packaging metadata, and importable package
shell. The First Working Slice is next.

## Current priorities

1. Define the First Working Slice and its acceptance checks.
2. Keep implementation focused on that one end-to-end MVP path.
3. Add documentation only alongside decisions or implementation that need it.

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
