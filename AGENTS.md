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

## Documentation policy

Documentation maintenance is part of feature completion.

Whenever implementation changes behavior, APIs, architecture, integrations,
workflows, CLI behavior, artifact formats, configuration, project phases, or
other durable project knowledge:

- inspect the corresponding documentation before considering the task complete;
- update every stale document so that documentation matches the implementation;
- update `PROJECT.md` whenever project status, roadmap, completed phases, or
  repository boundaries change;
- update existing routed documentation instead of creating new documents unless
  the implementation introduces durable new knowledge;
- update `docs/README.md` only when documentation routes change;
- keep documentation links relative;
- avoid placeholder documents;
- explicitly report which documentation files were updated, or explain why no
  documentation changes were required.

Documentation that no longer matches the implementation is considered a defect.

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
