# Domain Docs

How engineering skills consume this repository's domain documentation.

## Before exploring

- Read `CONTEXT.md` at the repository root when it exists.
- Read relevant decisions under `docs/adr/` when they exist.
- If either location is absent, proceed without creating speculative documentation.

## Layout

This is a single-context repository:

```text
/
├── CONTEXT.md
├── docs/adr/
└── core/
```

## Vocabulary

Use terms defined in `CONTEXT.md` consistently in specifications, tickets, tests,
and implementation. If a required concept is missing, avoid silently inventing a
competing synonym.

## Architecture decisions

Surface any conflict with an existing ADR explicitly before changing the affected
architecture.
