# Documentation Router

Documentation is part of the project harness: it preserves context that would
otherwise require repeated code archaeology or rediscovery. Keep this directory
small. Read only the category relevant to the current task, and do not create a
document until real implementation or a concrete decision gives it lasting
content.

Documentation maintenance is part of feature completion. After implementation,
verify `PROJECT.md` and the relevant routed document remain synchronized with
the code and current phase. Prefer updating an existing document; create a new
one only for durable architectural knowledge. Change this router only when a
documentation route is added, removed, or relocated.

## Directory purpose and routes

- **Architecture — `architecture/`:** implemented system boundaries, component
  relationships, and runtime data flow. Read when changing cross-component
  structure.
- **Design — `design/`:** user-visible behavior, workflows, and concrete feature
  contracts. Read when implementing or revising a product experience.
- **Integrations — `integrations/`:** contracts and operating notes for external
  systems. Use `integrations/sdk-change-requests/` only for specific,
  evidence-backed changes requested of the separate VIGI SDK.
  The live-stream capability request and its validation status are in
  [sdk-change-requests/live-rtsp-url-builder.md](integrations/sdk-change-requests/live-rtsp-url-builder.md).
  The implemented NVR recording-retrieval boundary is in
  [integrations/recording-retrieval.md](integrations/recording-retrieval.md).
- **ADR — `adr/`:** durable decisions whose alternatives and consequences matter.
  Read when revisiting a recorded choice.
- **Submission — `submission/`:** material required for an actual demo, review,
  release, or competition submission.

## When to create a document

Create one only when at least one of these is true:

- an implemented boundary or data flow needs explanation beyond the code;
- a feature has a stable user-facing contract worth preserving;
- an external integration has concrete setup, constraints, or failure modes;
- a consequential decision needs its context and trade-offs recorded;
- an active submission requires durable material.

Prefer updating the most relevant existing document. Do not create placeholders,
empty indexes, speculative roadmaps, meeting notes, or documents that merely
repeat `PROJECT.md`, `AGENTS.md`, source code, or tests. Add a route here when a
new document becomes important enough that future sessions must be able to find
it quickly.
