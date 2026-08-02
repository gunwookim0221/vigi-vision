# Architecture Decision Records (ADR)

This directory records durable architectural and design decisions that are
expected to outlive individual implementations.

The goal of an ADR is to explain **why** a significant decision was made, not
to describe implementation details or replace source code documentation.

## When to create an ADR

Create an ADR only when a task introduces a long-lived decision that future
contributors would otherwise need to rediscover.

Typical examples include:

- architecture or component boundary changes;
- public API contract changes;
- artifact, manifest, or persistent data format changes;
- storage or persistence strategy changes;
- core algorithms or processing policies;
- technology adoption or replacement;
- repository boundary changes;
- significant design tradeoffs.

Routine implementation work, bug fixes, refactoring, tests, documentation-only
changes, or code cleanup should not create an ADR.

## Updating existing ADRs

If a decision evolves, update the existing ADR whenever practical instead of
creating a duplicate.

If a previous decision has been superseded, mark the old ADR accordingly and
reference the newer ADR.

## File naming

Use sequential numbering.

Examples:

- ADR-0001-reference-frame-policy.md
- ADR-0002-manifest-format.md
- ADR-0003-roi-storage.md

Keep filenames short, stable, and descriptive.

## Recommended structure

Each ADR should include:

- Title
- Status (Proposed, Accepted, Superseded)
- Context
- Decision
- Alternatives Considered
- Consequences

Focus on the reasoning behind the decision rather than implementation details.

## Index

- [ADR-0001: Repository Boundary Between AI and SDK](ADR-0001-repository-boundary.md)
- [ADR-0002: Bounded Recording Replay Extraction](ADR-0002-bounded-replay-extraction.md)
- [ADR-0003: Credential-Free Persisted Artifacts](ADR-0003-credential-free-artifacts.md)
- [ADR-0004: Direct Reference-Frame Decoding and Nearest-Frame Selection](ADR-0004-direct-decoder-and-nearest-frame.md)
- [ADR-0005: Reference Frame Artifact Lifecycle and Immutable Identity](ADR-0005-reference-frame-artifact-lifecycle.md)
- [ADR-0006: Immutable Investigation Confirmation Packages](ADR-0006-investigation-confirmation-persistence.md)

## Relationship with other documentation

Use ADRs together with the existing documentation.

- PROJECT.md describes overall project status and roadmap.
- docs/design/ describes user-visible behavior and feature contracts.
- docs/architecture/ describes implemented architecture.
- ADRs explain why important architectural decisions were made.

When implementation changes invalidate an ADR, update it as part of the same
task.

Documentation that no longer reflects the implementation should be treated as a
defect.
