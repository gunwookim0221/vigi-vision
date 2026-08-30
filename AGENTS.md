# Codex Working Guide

Read [PROJECT.md](PROJECT.md) before inspecting code or planning changes. It is
the authority for mission, scope, phase, and repository boundaries.

## Documentation routing

Read [docs/README.md](docs/README.md) next only when the task needs deeper
project documentation. Follow its category routes rather than scanning every
document. Create a document only when current implementation or a concrete
decision needs a durable home.

Treat [PROJECT.md](PROJECT.md) as a living charter. After implementation work,
verify that it still describes the current phase, completed sessions, priorities,
roadmap, repository boundaries, and project status; update it when any of those
become stale. Completed work must not remain described as future work.

## Working rules

- Deliver the smallest complete, observable MVP increment.
- Do not add abstractions, modules, dependencies, or future-facing interfaces
  without a present requirement.
- Keep AI orchestration and all AI-specific behavior in this repository.
- The neighboring `../tp-link-vigi-sdk` repository may be inspected for its
  public API and conventions, but must not be modified from this repository.
- Prefer using the SDK as published. If application work exposes a genuine SDK
  gap, document a concrete change request under
  `docs/integrations/sdk-change-requests/`; do not work around the boundary by
  moving AI logic into the SDK.

## Implementation and review gates

The mandatory project-wide implementation and review workflow is in
[docs/development/implementation-review-gates.md](docs/development/implementation-review-gates.md).
Before editing, an implementation agent must:

1. Read every applicable `AGENTS.md` and every routed authoritative contract.
2. Establish repository, branch, ancestry, worktree, index, and Git-operation
   preconditions.
3. Identify the exact predecessor production artifact or state and required
   successor state.
4. Trace the intended production call path before implementation.
5. Check that the requested composition is supported by existing contracts and
   typed boundaries.
6. Stop before editing when a contract conflict requires an unapproved
   semantic, schema, or identity decision.
7. Never invent a parallel algorithm to bypass an incompatible approved
   boundary.
8. For a demonstrated defect or missing path, add a deterministic test from the
   real predecessor boundary and prove the expected failure before correction.
9. Never treat a manually fabricated terminal-ready state as proof of an
   end-to-end production path.
10. Persist and strictly reopen evidence at every required boundary.
11. Run focused, related, full, static, documentation, and diff validation for
    the affected scope.
12. Perform a pre-commit self-review using the independent review's
    BLOCKER/MAJOR criteria.
13. Commit and push only when required gates pass and no contract conflict
    remains.
14. Preserve unrelated user changes and never weaken tests to hide failures.
15. Keep implementation, correction, review, and later-phase scopes separate.

## Documentation policy

Documentation maintenance is part of feature completion.

Whenever implementation changes behavior, APIs, architecture, integrations,
workflows, CLI behavior, artifact formats, configuration, project phases, or
other durable project knowledge:

- inspect the corresponding documentation before considering the task complete;
- update every stale document so that documentation matches the implementation;
- update `PROJECT.md` whenever project status, roadmap, completed phases, or
  repository boundaries change;
- update existing routed documentation whenever appropriate;
- create a new document only when the implementation introduces durable
  knowledge that is not already covered by existing documentation;
- update `docs/README.md` only when documentation routes change;
- keep documentation links relative;
- avoid placeholder documents;
- explicitly report which documentation files were updated, or explain why no
  documentation changes were required.

Documentation that no longer matches the implementation is considered a defect.

Use the existing documentation category whenever possible.

If implementation introduces durable architectural knowledge and no appropriate
document exists, create one under `docs/architecture/`.

If implementation introduces durable user-visible behavior, workflows, or
feature contracts and no appropriate document exists, create one under
`docs/design/`.

Do not create documentation for routine implementation details, bug fixes,
refactoring, or temporary implementation notes.

## Architecture Decision Records (ADR)

Do not create an ADR for routine implementation work.

Create or update an ADR under `docs/adr/` only when the task introduces a
durable architectural or design decision, such as:

- architecture changes;
- public API contract changes;
- artifact or manifest format changes;
- persistence or storage strategy changes;
- core algorithm or processing policy changes;
- technology adoption or replacement;
- repository boundary changes;
- decisions that future contributors would otherwise need to rediscover.

Prefer updating an existing ADR when appropriate instead of creating duplicates.

## Completion standard

Before handing off, exercise the changed behavior through its real surface when
practical and report:

- changed files;
- tests and validation performed;
- known limitations and unverified assumptions;
- remaining work that is required, without implementing unrelated next steps;
- documentation files reviewed and updated;
- whether an ADR was created, updated, or not required, with justification;
- confirmation that `PROJECT.md` and all relevant routed documentation are
  synchronized with the implementation.

Do not commit or push unless the user explicitly requests it.
