# Implementation and Review Gates

This is the mandatory project-wide workflow for implementation and correction
tasks, and the minimum workflow for reviewing their resulting changes. It
complements the concise rules in [AGENTS.md](../../AGENTS.md), the project
charter in [PROJECT.md](../../PROJECT.md), and the authoritative documents
routed by [docs/README.md](../README.md). It does not replace a feature
contract, an ADR, or a phase-specific acceptance plan.

The rules below are mandatory. Examples and phase notes are applicability
guidance, not alternate specifications. Tests and reviews provide evidence for
the claimed path; they do not guarantee correctness.

## Gate 1 — Preconditions

Before editing, record:

- the resolved repository path and the applicable instruction scope;
- the current branch, expected branch, current `HEAD`, expected starting
  `HEAD` when supplied, ancestry, upstream, and the relevant remote ref;
- whether the worktree and index are clean, or which changes are explicitly
  approved and in scope;
- whether a merge, rebase, cherry-pick, revert, bisect, or other Git operation
  is in progress;
- whether `.env` is ignored, untracked, and unchanged without reading,
  printing, copying, modifying, or staging it;
- the requested scope and the explicit out-of-scope files, phases, and
  behaviors.

Fetch remote refs with prune when that capability is available, then verify the
branch and remote relationship again. If the fetch cannot be performed, record
that limitation and use the strongest available local evidence. A supplied
starting commit must be on the expected ancestry; an exact-`HEAD` mismatch must
be surfaced rather than silently treated as a clean baseline. Stop before
editing for a divergent branch, an ancestry mismatch, an unexpected dirty
worktree or index, unrelated unpublished work, or an unresolved Git operation.
Preserve approved user changes; do not make a destructive Git operation to
manufacture a clean precondition.

## Gate 2 — Contract compatibility before editing

Read every applicable `AGENTS.md` from the repository root through the target
path, then read `PROJECT.md`, `README.md`, `docs/README.md`, and every routed
architecture, design, integration, testing, development, review, or ADR
document relevant to the change. Use repository-local links and `rg --files` to
discover guidance; do not scan unrelated documentation by default.

Identify every authoritative contract involved and map each producer's output
to the consumer's input. Compare, as applicable:

- required and forbidden keys, enums, ranges, and nullability;
- canonical identities, ownership, ordering, and duplicate/alias behavior;
- time zones, timing precision, interval direction, and boundary semantics such
  as half-open intervals;
- persistence, atomic publication, strict readback, restart, and immutability;
- cancellation, timeout, cleanup, concurrency, security, and path confinement;
- the actual public or internal API and typed boundary available today.

Stop before editing when composition would require an unapproved decision to:

- reinterpret persisted meaning;
- change a published identity;
- invalidate golden vectors or compatibility fixtures;
- weaken strict validation;
- create a parallel version of an approved algorithm; or
- silently change legacy behavior.

Record the conflict and the smallest decision or contract update required. Do
not resolve it by inventing semantics in implementation code.

## Gate 3 — Production-path proof

Before implementation, write down the complete intended predecessor-to-successor
path:

1. the real predecessor artifact or state and its strict loader;
2. the public or internal production entry point;
3. every service, adapter, repository, decoder, classifier, or other boundary
   involved;
4. each persistence, atomic publication, and strict-readback point;
5. the required successor artifact or terminal output; and
6. failure, cancellation, timeout, concurrency, interruption, and restart
   behavior at each boundary.

The path is not proven by imports, helpers, an isolated adapter, a unit test of
one component, or terminal publication alone. When a task claims end-to-end
composition, at least one deterministic production-path test must start at the
real predecessor boundary and reach the reviewed successor boundary.

## Gate 4 — Reproducing the gap

For a correction or missing integration, first add a deterministic,
production-shaped test that begins with the real predecessor boundary. Run it
before implementing the correction and confirm that it fails for the expected
reason. Record that expected failure in the task report; do not commit the
deliberately failing state as a separate change.

If safely demonstrating the failure is impractical, document why and use the
closest deterministic production-shaped probe available. A fabricated state
that skips the predecessor is not an adequate substitute merely because it
reaches the terminal code.

## Gate 5 — Implementation

Implement the smallest approved path. Reuse existing domain services and typed
boundaries; do not add a parallel policy or algorithm to evade an incompatibility.
Where the contract requires it, derive decisions from persisted and strictly
reopened state rather than from an in-memory shortcut. Preserve ownership,
timing, cancellation, cleanup, security, path confinement, immutability, and
compatibility behavior. Keep implementation, correction, review, and later
phase scopes separate, and avoid unrelated refactors.

## Gate 6 — Test quality

Tests must make their boundary and evidence strength clear. Cover the relevant
categories separately:

- unit or model validity;
- repository persistence and strict reopen;
- service integration;
- the actual predecessor-to-successor production path;
- restart and idempotency;
- concurrency and conflicting operations;
- timeout, cancellation, and cleanup;
- security and path confinement;
- compatibility and golden vectors.

A terminal-ready fixture proves only that terminal interpretation or publication
accepts that fixture. It does not prove that production code can generate the
fixture from the predecessor phase. Whenever the task claims end-to-end
composition, at least one production-path test must begin at the real
predecessor boundary and reach the reviewed successor boundary.

## Gate 7 — Validation

Run the validation categories required by the affected scope and report each
result separately:

- new focused tests;
- affected subsystem tests;
- predecessor and successor contract regressions;
- the full test suite;
- lint;
- a format check that does not mutate files;
- changed-file type checking;
- full type checking;
- documentation and relative-link checks;
- golden-vector or identity checks where applicable;
- `git diff --check`; and
- `git diff --cached --check` before committing.

Identify pre-existing or unrelated failures separately. Do not hide them with
unrelated edits, weakened assertions, skipped tests, or scope expansion.

## Gate 8 — Pre-commit self-review

Before committing, inspect the complete diff as an adversarial reviewer using
the same BLOCKER/MAJOR criteria expected from independent review. Ask:

- Is the production entry point actually connected?
- Can the real predecessor output reach the new code?
- Does any test bypass production objects or the required boundary?
- Are calculated timeouts actually passed and enforced?
- Can read-only status work during live ownership?
- Can cleanup mask the primary error?
- Can aliases or duplicates inflate evidence?
- Can restart or concurrency redefine immutable state?
- Can a later phase or out-of-scope behavior leak into the diff?
- Is every claimed invariant demonstrated by a test or a direct production
  trace?

Resolve every demonstrated BLOCKER or MAJOR defect before commit. A BLOCKER is
a demonstrated path break, contract/integrity/ownership/security violation,
false success, or data-loss risk. A MAJOR is a demonstrated failure of a
required boundary, strict reopen, timeout/cleanup/concurrency invariant, or
claimed production composition that prevents a trustworthy completion verdict.

## Gate 9 — Independent review

Independent review is read-only and evaluates the committed range. It requires
a reachable production path and concrete evidence. Its verdict-blocking scope
is limited to demonstrated BLOCKER and MAJOR defects; style, optional
refactoring, speculative risks, and future-phase work are not blockers.

The review stops when no demonstrated BLOCKER or MAJOR remains. It must not
rewrite the implementation, broaden the task, invent a contract decision, or
turn a fixture-only result into an end-to-end claim.

## Gate 10 — Completion report

The completion report must include:

- initial and final Git state, including branch, `HEAD`, upstream, and remote
  relationship;
- changed files and the production path implemented;
- contract decisions made, and any conflict that blocked work;
- the expected pre-fix failure demonstration, or why the closest probe was
  used;
- validation results and separately identified pre-existing failures;
- a scope audit confirming that unrelated changes were preserved;
- commit, push, and ref results when those actions were required;
- deferred work and known limitations; and
- one exact readiness verdict that matches the evidence.

Also report which documentation files were reviewed and updated. Confirm that
`PROJECT.md` and all relevant routed documentation remain synchronized with the
implementation. If no documentation change is required, say why. Do not claim
that tests guarantee correctness.

## Phase 7E applicability note

This note applies the gates without restating the Phase 7E contract. The
authoritative specification is the Phase 7E section of
[object-disappearance-recording-search.md](../design/object-disappearance-recording-search.md),
with the durable executor decision in
[ADR-0007](../adr/ADR-0007-validated-recording-search.md).

For a Phase 7E-1D claim:

- start from the real strict Schema 6 state created by 7E-1C;
- prove the production C1 → C2 → D1 → D2 path creates the evidence required
  for Schema 7;
- treat a fabricated terminal-ready Schema 6 fixture as publication coverage
  only, never as proof of predecessor-to-successor production composition; and
- check contract compatibility for support direction, the half-open `[S,E)`
  interval, canonical identity bindings, strict reopen, cumulative deadline,
  and ownership before editing.

Use the routed Phase 7E documents for exact fields, matrices, vectors, and
failure semantics. Do not duplicate or weaken those normative rules here.

## Scope and completion boundary

Commit or push only when the requested scope is complete, the applicable gates
pass, and no contract conflict remains. Preserve the repository rule that
routine implementation work does not require an ADR; create or update one only
for a durable architectural or design decision. Do not use history rewriting,
force-push, reset, or other destructive Git actions to conceal a failed gate.
