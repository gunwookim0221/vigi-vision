# ADR-0005: Reference Frame Artifact Lifecycle and Immutable Identity

## Status

Accepted

## Context

The reference-frame subsystem must handle concurrent requests for the same channel and time, avoid redundant NVR load, and safely persist the resulting JPEGs and manifests to disk.

Because the system is designed to be lightweight and stateless between executions, it does not use a relational database to track in-flight jobs or completed frames. It must rely on the filesystem while preventing race conditions, corrupt partial writes, and duplicate effort.

## Decision

We use the filesystem as the source of truth, employing a staging protocol and immutable resource identities:
1. **Immutable Identity:** The resource ID is deterministically derived from the request parameters and the selected recording segment (`reference_frame_resource_id`).
2. **Reuse:** Before extraction, the `ReferenceFrameResourceStore` checks if a compatible, completed artifact already exists for that ID. If so, it reuses it (`ReferenceFrameOutcome.REUSED`).
3. **Staging:** New extractions are written to a temporary staging directory associated with the resource ID. If another invocation is already staging the same resource, `begin()` raises a conflict error.
4. **Atomic Promotion:** Once extraction and decoding succeed, `finalize()` writes the manifest and atomically promotes the staging directory to the durable resource path.
5. **Cleanup:** On failure, the staging directory is discarded.

## Alternatives Considered

- **In-place overwriting:** Writing directly to the final artifact path. Rejected because a failed process or concurrent write could leave a corrupt JPEG or missing manifest.
- **UUIDs for every request:** Generating a unique ID for every request regardless of parameters. Rejected because it prevents reuse, forcing the NVR to serve redundant replay streams for identical requests.

## Consequences

- **Safe Concurrency:** Concurrent requests for the same frame are handled safely; one will process, and the other will fail with a conflict (and can subsequently reuse the completed artifact on retry).
- **Idempotency and Efficiency:** The NVR is protected from redundant load because identical requests reuse the completed durable artifact.
- **Data Integrity:** Artifacts are guaranteed to be complete (JPEG + valid manifest) once they appear at their durable path.
