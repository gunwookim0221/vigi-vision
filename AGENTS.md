# Codex Working Guide

Read [PROJECT.md](PROJECT.md) before inspecting code or planning changes. It is
the authority for mission, scope, phase, and repository boundaries.

## Documentation routing

Read [docs/README.md](docs/README.md) next only when the task needs deeper
project documentation. Follow its category routes rather than scanning every
document. Create a document only when current implementation or a concrete
decision needs a durable home.

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
- Update `PROJECT.md` when project direction, phase, priorities, or boundaries
  change.
- Update routed documentation when an implemented architecture, integration,
  durable decision, design contract, or submission requirement changes.
- Avoid placeholder documents and keep documentation links relative.
- Do not commit or push unless the user explicitly requests it.

## Completion standard

Before handing off, exercise the changed behavior through its real surface when
practical and report:

- changed files;
- tests and validation performed;
- known limitations and unverified assumptions;
- remaining work that is required, without implementing unrelated next steps.
