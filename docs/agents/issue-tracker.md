# Issue tracker: Local Markdown

Issues and specs for this repo live as markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The spec is `.scratch/<feature-slug>/spec.md`
- Implementation issues are one file per ticket at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`
- Tickets are numbered from `01` in dependency order
- Triage state is recorded as a `Status:` line near the top
- Comments and conversation history append under a `## Comments` heading

## Publishing

When a skill says to publish to the issue tracker, create or update the corresponding
file under `.scratch/<feature-slug>/`.

## Fetching

Read the referenced specification or ticket file in full before acting on it.

## Blocking

A ticket's `Blocked by:` line lists the ticket numbers that must be completed first.
The active frontier contains tickets whose blockers are all done.
