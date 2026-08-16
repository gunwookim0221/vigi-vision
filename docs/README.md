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
  contracts. Read when implementing or revising a product experience. The
  implemented pure multi-camera planning contract is in
  [design/investigation-plan.md](design/investigation-plan.md). The public CLI
  inventory is in [design/cli-workflows.md](design/cli-workflows.md), and the
  planned generic NVR frame-sampling contract is in
  [design/recording-sampling.md](design/recording-sampling.md). Read
  [design/object-disappearance-investigation.md](design/object-disappearance-investigation.md)
  for the bounded object-change product scope and safety constraints. Read
  [design/object-disappearance-recording-search.md](design/object-disappearance-recording-search.md)
  for the current normative Phase 7 single-site MVP: one active local run,
  interruption and explicit restart, the required Phase 6 schema 3 integrity
  handoff, the implemented Phase 7A-1 local lifecycle/API, Phase 7A-2 canonical
  multi-target acquisition boundaries, the implemented Phase 7B single-probe
  classifier/schema-3 observation boundary, and the Phase 7C-1 deterministic
  chronological coarse execution foundation and the non-persistent deterministic
  transition interpretation handoff. Binary search,
  terminal disappearance persistence, and the Phase 8 request boundary remain
  planned. Read
  [design/object-presence-classification.md](design/object-presence-classification.md)
  for the normative Phase 7B single-probe input, geometry, classifier, outcome,
  schema-3 observation, publication, idempotency, and strict-reopen contract.
  Read
  [design/reference-frame-service.md](design/reference-frame-service.md)
  when implementing or reviewing the proposed recorded reference-frame service,
  its timing evidence or durable artifacts. Read
  [design/reference-frame-api.md](design/reference-frame-api.md) for the
  implemented synchronous FastAPI transport contract, safe JPEG retrieval, and
  Phase 3B operational boundary. Read
  [design/reference-frame-candidates.md](design/reference-frame-candidates.md)
  for the implemented Phase 4A bounded candidate-set contract that reuses those
  single-frame resources without making absolute timing claims. Read
  [design/investigation-confirmation.md](design/investigation-confirmation.md)
  for the implemented schema 2 Phase 6 confirmation and the approved Phase 6C
  schema 3 JPEG-integrity/reconfirmation compatibility increment, immutable package,
  source-pixel ROI, idempotency, and typed handoff contract. Read
  [design/assisted-roi-selection.md](design/assisted-roi-selection.md) for the
  completed Phase 5-3A tap-assisted ROI feasibility decision, implemented
  Phase 5-3B-2 optional backend API/model lifecycle, and remaining frontend
  acceptance boundary.
- **Integrations — `integrations/`:** contracts and operating notes for external
  systems. Use `integrations/sdk-change-requests/` only for specific,
  evidence-backed changes requested of the separate VIGI SDK.
  The live-stream capability request and its validation status are in
  [sdk-change-requests/live-rtsp-url-builder.md](integrations/sdk-change-requests/live-rtsp-url-builder.md).
  The implemented NVR recording-retrieval boundary is in
  [integrations/recording-retrieval.md](integrations/recording-retrieval.md).
- **ADR — `adr/`:** durable decisions whose alternatives and consequences matter.
  Read when revisiting a recorded choice.
- **Future reference — `future/`:** explicitly non-normative analysis that is
  not part of current implementation, review, or completion criteria. The
  deferred lease, fencing, takeover, resume, crash-recovery, full-manifest, and
  source-binding analysis is in
  [future/recording-search-resilience.md](future/recording-search-resilience.md).
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
