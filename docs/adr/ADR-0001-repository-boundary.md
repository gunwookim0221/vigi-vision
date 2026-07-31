# ADR-0001: Repository Boundary Between AI and SDK

## Status

Accepted

## Context

VIGI Vision aims to provide natural-language investigation workflows based on TP-Link VIGI camera data. The system requires complex interactions with OpenAI, media processing with FFmpeg, and domain-specific orchestration (investigation planning, reference-frame candidate sets). It also relies heavily on the underlying camera hardware's public SDK for authentication, recording metadata search, and live/replay stream URL construction.

Placing all AI orchestration, language processing, and media decoding inside the SDK would couple hardware communication with rapidly changing AI workflows and external media tools. Conversely, building SDK details into the AI layer would duplicate existing work and complicate testing.

## Decision

We maintain a strict repository boundary: `vigi-vision` owns all AI orchestration, media decoding, investigation workflows, and command-line interfaces in its own repository. All NVR/IPC authentication, SDK network communication, and stream URL construction remains in the neighboring `tp-link-vigi-sdk` repository.

Code in `vigi_vision` imports from the `vigi` SDK package but does not write to it. AI-specific logic, prompts, and ffmpeg integrations are never moved into the SDK.

## Alternatives Considered

- **Embedding AI logic in the SDK:** Enhancing the SDK itself to include video analysis or reference frame decoding. This was rejected because the SDK should remain a pure, un-opinionated interface to the VIGI hardware.

## Consequences

- **Clear separation of concerns:** The SDK remains ignorant of OpenAI, ffmpeg, investigation planning, and artifact generation.
- **Explicit dependency management:** Updates to the SDK contract must be deliberately adopted by VIGI Vision.
- **Focused testing:** The AI application can mock the SDK boundary (e.g., in `test_reference_frame_direct.py`) without requiring actual hardware for higher-level workflow tests.
