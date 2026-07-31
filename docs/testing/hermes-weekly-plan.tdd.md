# Hermes weekly plan — TDD evidence

Source: `.scratch/hermes-content-factory-mvp/issues/05-weekly-plan.md`.

## User journeys

- Administrator receives three brand-aware cards from at least five verified
  web candidates and sees a stale-cache warning when applicable.
- Administrator reviews cards sequentially, replacing or editing only the
  active card, then approves one mode without starting a render.
- A callback from an older revision cannot overwrite current plan state.

## RED and GREEN

- RED: `uv run pytest -q tests/test_weekly_plan.py` failed during collection
  with `ModuleNotFoundError: core.weekly_plan`; checkpoint `7ba989a`.
- GREEN: `uv run pytest -q tests/test_weekly_plan.py tests/test_hermes_shell.py`
  — `20 passed`; checkpoint `93852b7`.
- Regression: `uv run pytest -q` — `140 passed`.
- Types: `uv run mypy` — `Success: no issues found in 32 source files`.

| Guarantee | Test |
|---|---|
| Four named sources, real URL/date contract and available-only metrics | `test_candidate_contract_uses_all_sources_and_only_available_metrics` |
| 24-hour cache and warned fallback through seven-day boundary | `test_cache_is_fresh_for_24_hours_and_warned_fallback_expires_at_7_days` |
| Fewer than five valid candidates stops and reports sources | `test_invalid_or_fewer_than_five_candidates_stop_without_invention` |
| Brand profile reaches strategist; exactly three cards review sequentially | `test_strategist_gets_profile_and_ten_candidates_then_reviews_sequentially` |
| Replace/edit affects active card only; stale revision loses | `test_replace_and_text_edit_change_only_active_card_and_stale_actions_lose` |
| Final auto/manual decision does not schedule or render | `test_final_approval_sets_one_mode_without_scheduling_or_stale_overwrite` |
| Hermes shell persists one callback transition per clean event | `test_hermes_shell_routes_plan_clarify_callbacks_and_rejects_stale_revision` |

## Coverage and gaps

The repository has no installed pytest coverage plugin, so
`--cov=core.weekly_plan` was unavailable. No dependency was added merely to
produce a percentage. All public ticket-05 transitions are exercised by eight
offline tests; real website access remains an injected Hermes/CLI adapter and
belongs to deployment acceptance, not unit tests.
