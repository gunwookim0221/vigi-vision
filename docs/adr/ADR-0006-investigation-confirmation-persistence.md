# ADR-0006: Immutable Investigation Confirmation Packages

## Status

Accepted. Schema 2 is implemented; the schema 3 integrity extension required by
Phase 7 is approved as the Phase 6C compatibility increment.

## Context

Phase 5 holds candidate selection and canonical source-pixel ROI state only in
the browser. Phase 7 needs a durable, typed, credential-free input that survives
refresh and restart. Completed reference-frame resources are already immutable,
while `artifacts/investigations/` already owns investigation manifests. Candidate
sets deliberately have no durable identity or manifest.

Writing selection data into the reference-frame manifest would mix shared frame
evidence with user-specific state. Creating a separate persistence root would
duplicate investigation ownership. Updating a manifest in place would expose
partial writes and make retry/conflict behavior ambiguous.

## Decision

New confirmation creates an immutable schema 3 object-disappearance package under
`artifacts/investigations/`. Its deterministic identity includes scenario,
schema version, channel, and whole-second UTC anchor. The manifest references
the existing reference-frame resource by stable ID, snapshots its validated
safe facts, and stores one integer half-open source-pixel ROI with provenance.
After fully decoding and dimension-checking the trusted JPEG, Phase 6 also
persists its SHA-256 and byte size. It stores no JPEG filesystem path: the
backend resolves the JPEG from the trusted resource ID, and neither the client
nor confirmation duplicates or modifies that resource.

Publication uses an exclusive sibling claim, same-filesystem staging directory,
deterministic JSON, and atomic no-overwrite directory promotion. The
confirmation claim carries an operation ID and UTC heartbeat; only a claim
proven stale under the documented recovery lock and conservative threshold may
be recovered. The final package is checked before claim handling, so interrupted
cleanup cannot make a successful retry appear in progress. Identical submissions
reuse the completed package; materially different submissions for the same
identity conflict. No confirmed package is edited in place.

Legacy unversioned manifests remain schema 1. Existing schema 2 confirmations
remain immutable and readable but lack authoritative integrity facts, so they
cannot start Phase 7 automatically. Phase 6C's explicit **Reconfirm for recording
search** action reopens the schema 2 JPEG and source-pixel ROI read-only, shows
both for user confirmation, and uses a dedicated compatibility POST that accepts
no replacement selection or server-owned facts. The server strictly reloads the
existing package and trusted resource, validates the JPEG and ROI, computes
SHA-256 and byte size from the newly confirmed bytes, revalidates immediately
before publication, and creates a new schema 3 package in a versioned namespace.
Missing, corrupt, changed, path-unsafe, dimension-mismatched, or ROI-invalid input
fails safely. The action never updates the schema 2 package or silently blesses
its current path. The complete schema, API, lifecycle, and loader contract is in
[Investigation Confirmation and Durable Persistence](../design/investigation-confirmation.md).

## Alternatives considered

### Modify the immutable reference-frame manifest

Rejected because one frame may be reviewed more than once and user selection is
not frame-generation evidence. It would also violate the existing immutable
resource lifecycle.

### Persist a candidate-set manifest

Rejected because confirmation needs one selected child, not a durable batch.
Retroactively persisting candidate sets would broaden Phase 4A and duplicate
child resource facts.

### Create a separate confirmation storage root or database

Rejected for the local MVP because investigation artifacts already own durable
investigation state. A new root or database adds retention, migration, and
transaction infrastructure without current evidence.

### Overwrite or version files inside one final directory

Rejected because readers could observe partial state and concurrent retries
would need a more complex update protocol. Immutable directory publication has
an existing proven repository precedent.

### Persist normalized and pixel ROI coordinates

Rejected because two rounded representations can disagree. Exact source-pixel
coordinates plus source dimensions are canonical; normalized values are
derived when required.

## Consequences

- Phase 7 has one strict schema 3 loader boundary and never consumes browser
  state or a freshly hashed legacy path.
- Reference-frame resources, candidate APIs, and existing multi-camera packages
  remain unchanged.
- Confirmation is idempotent but immutable; editing or selecting a second object
  at the same channel/anchor needs a future explicit identity flow.
- The referenced JPEG is not duplicated. Manual deletion, replacement, or
  corruption after confirmation is detected by full decode plus digest, size,
  and dimension revalidation rather than prevented by a cross-package
  transaction.
- Existing legacy manifests remain usable by existing consumers but cannot start
  Phase 7 object-disappearance work.
- Phase 6C is a narrow compatibility path, not a general replacement,
  migration, or confirmation-version management system.
