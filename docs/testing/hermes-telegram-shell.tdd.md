# TDD evidence: Hermes Telegram shell

## Source and journeys

Source: `.scratch/hermes-content-factory-mvp/issues/01-hermes-telegram-shell.md`
and its parent specification.

- An administrator can use five Russian Telegram commands through one bot.
- A non-administrator is ignored without a response or state change.
- Each clean event run performs at most one transition and repeated updates or
  callbacks do not repeat it.
- Status reports Hermes health and the active key number without secret values.

## RED and GREEN

RED was captured before production code:

```text
$ uv run pytest -q tests/test_hermes_shell.py
ModuleNotFoundError: No module named 'core.hermes_shell'
```

The failure was the intended missing-contract failure and is preserved in commit
`f0fa281`.

GREEN for the minimal implementation:

```text
$ uv run pytest -q tests/test_hermes_shell.py
8 passed
$ uv run mypy core/hermes_shell.py tests/test_hermes_shell.py
Success: no issues found in 2 source files
```

The first six passing contract tests and implementation are preserved by GREEN
commit `e2e8de9`; two additional characterization tests cover env fallback and
the one-event CLI.

## Test specification

| Guarantee | Test type | Evidence |
|---|---|---|
| Exactly two distinct admin IDs are loaded from env | unit | `test_config_requires_two_distinct_admins_and_two_named_key_slots` |
| Missing/duplicate admins fail closed | unit | `test_config_rejects_duplicate_admins`, `test_config_requires_both_admins_and_selects_available_key` |
| Both Zen key slots are env-backed and status exposes only a slot number | unit | config and safe-status tests |
| Only the five requested commands have Russian descriptions | contract | `test_only_five_russian_commands_are_registered` |
| Unknown Telegram IDs are silently ignored | security contract | `test_unknown_user_is_silently_ignored` |
| Update and callback IDs survive clean runner instances | integration | `test_update_and_callback_are_idempotent_across_clean_runs` |
| The CLI consumes one event and exits | integration | `test_cli_processes_one_event_and_exits` |

## Coverage and gaps

`pytest-cov` is not installed and no dependency was added. The available stdlib
measurement was run with:

```text
$ uv run python -m trace --count --summary --coverdir /tmp/hermes-shell-cover \
    --module pytest -q tests/test_hermes_shell.py
8 passed
```

The generated annotated `core.hermes_shell.cover` shows all ticket paths used by
the harness tests; deliberately defensive malformed-event returns and the
`__main__` import guard remain outside the acceptance flow. Branch coverage is
not available from stdlib `trace`. Real Telegram transport and Hermes installation
are intentionally deferred; this ticket supplies their tested workspace/harness
contract without downloading either system.

Final regression:

```text
$ uv run pytest -q
118 passed in 1.69s
$ uv run mypy
Success: no issues found in 28 source files
```
