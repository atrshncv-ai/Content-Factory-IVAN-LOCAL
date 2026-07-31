# TDD evidence: Hermes brand profile

## Source and journeys

Source: `.scratch/hermes-content-factory-mvp/issues/02-brand-profile.md` and its
parent specification.

- An administrator can complete the brand profile through sequential `/setup`
  events, skipping only optional fields.
- Either administrator can reopen the current profile and edit one selected
  field without a stale callback overwriting a newer revision.
- Hermes agents can read the structured profile from workspace state, including
  the updater Telegram ID, while the configured knowledge directory may be empty.
- `/plan` explains that its required profile is absent; `/new_video` remains
  available without one.

## RED and GREEN

RED was captured before production changes:

```text
$ uv run pytest -q tests/test_hermes_shell.py
4 failed, 8 passed in 0.23s
```

The four intended failures were the missing knowledge-path/setup constructor,
missing sequential setup state, missing profile summary/revision edit, and
missing `/plan` profile gate. RED is preserved in commit `6dea1e1`.

GREEN for the minimal implementation:

```text
$ uv run pytest -q tests/test_hermes_shell.py
12 passed in 0.16s
$ uv run mypy core/hermes_shell.py tests/test_hermes_shell.py
Success: no issues found in 2 source files
```

The implementation and tests are preserved in GREEN commit `2157d42`.

## Test specification

| Guarantee | Test type | Result | Evidence |
|---|---|---|---|
| All nine fields are collected in order and structured state is persisted | integration | PASS | `test_setup_collects_and_persists_complete_brand_profile` |
| Required fields cannot be skipped; four optional fields can | contract | PASS | `test_setup_allows_optional_skips_but_reasks_required_fields` |
| Reopening shows current values and revision | integration | PASS | `test_repeat_setup_shows_profile_and_revision_protects_edits` |
| A stale callback is rejected and a current edit records the initiating Telegram ID | security contract | PASS | `test_repeat_setup_shows_profile_and_revision_protects_edits` |
| The configured knowledge directory exists and may remain empty | integration | PASS | `test_setup_collects_and_persists_complete_brand_profile` |
| `/plan` is gated but `/new_video` is not | contract | PASS | `test_plan_requires_profile_but_new_video_remains_available` |

## Coverage and final regression

No dependency was added. Stdlib `trace` measured the relevant target:

```text
$ uv run python -m trace --count --summary \
    --coverdir /tmp/hermes-brand-profile-cover \
    --module pytest -q tests/test_hermes_shell.py
12 passed in 1.30s
core.hermes_shell: 228/228 executable lines hit (100.0%)
```

`trace` reports line coverage, not branch coverage. Telegram transport and
button rendering remain outside this deterministic event-shell ticket.

Final repository checks:

```text
$ uv run pytest -q
122 passed in 1.65s
$ uv run mypy
Success: no issues found in 28 source files
$ scripts/update_graph.sh
Graphify: граф обновлён
```
