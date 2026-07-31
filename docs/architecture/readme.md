# Architecture Documentation

This directory contains documentation describing the implemented system
architecture.

Architecture documents explain how the system is organized, how major
components interact, and how data flows through the application.

## Create or update architecture documentation when

Create or update a document when implementation introduces durable
architectural knowledge, including:

- component responsibilities;
- module boundaries;
- runtime data flow;
- processing pipelines;
- service interactions;
- artifact lifecycle;
- integration boundaries;
- deployment or execution architecture.

## Do not create architecture documentation for

Do not create architecture documents for:

- routine feature implementation;
- bug fixes;
- refactoring without architectural impact;
- implementation details better explained in code;
- temporary experiments.

## Relationship with other documentation

Architecture documents describe **how the system is organized**.

Use the other documentation categories for different purposes:

- `PROJECT.md` — overall project status and roadmap.
- `docs/design/` — user-visible behavior and feature contracts.
- `docs/adr/` — why important architectural decisions were made.

Prefer updating an existing architecture document instead of creating a new one
unless the implementation introduces a new architectural concern.

