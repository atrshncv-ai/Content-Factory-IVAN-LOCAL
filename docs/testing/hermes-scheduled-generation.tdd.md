# Hermes scheduled generation — TDD evidence

Source: `.scratch/hermes-content-factory-mvp/issues/06-scheduled-generation.md`.

## User journeys

- An approved plan creates three persisted scheduled jobs and collects their
  confirmed photos sequentially, allowing replacement only before render start.
- Cron creates one guarded Sunday draft and one Monday reminder.
- A due job starts three hours before publication, waits and reminds without a
  photo, then continues automatically when the missing photo is confirmed.
- Scheduled overdue/nearest work outranks manual FIFO without preempting an
  active render; all result actions remain on the shared `TaskQueue`.

## RED and GREEN

- RED: `.venv/bin/pytest -q tests/test_scheduled_generation.py` failed during
  collection because `ScheduledPlan` did not exist; checkpoint `0e0fda4`.
- GREEN: `.venv/bin/pytest -q tests/test_scheduled_generation.py
  tests/test_task_control.py tests/test_weekly_plan.py` — `22 passed`.
- Regression: `.venv/bin/pytest -q` — `147 passed`.
- Types: `.venv/bin/mypy core tests` — no issues in 33 source files.

| Guarantee | Test |
|---|---|
| Approval creates exactly three idempotent jobs; invalid strategic times use Tue/Thu/Sat 18:00 brand time | `test_approval_creates_three_idempotent_tasks_and_fallback_schedule` |
| Photos confirm sequentially, latest replaces before start, started work rejects replacement | `test_photos_are_collected_sequentially_confirmed_and_latest_wins_before_start` |
| Missing photo reminds once and late confirmation resumes automatically | `test_due_without_photo_reminds_once_and_auto_continues_when_photo_arrives` |
| Plan mode can be overridden and schedule edited only before generation day | `test_mode_override_and_schedule_edit_stop_on_due_day` |
| Scheduled overdue/nearest work outranks manual FIFO and active work is not preempted | `test_overdue_and_nearest_scheduled_priority_over_manual_without_preemption` |
| Sunday draft is guarded and Monday reminder is emitted once | `test_sunday_draft_guard_and_single_monday_reminder` |
| Hermes routes the largest Telegram photo and idempotent confirmation into persisted collection state | `test_shell_routes_plan_photo_and_confirmation_once` |

## Coverage and known gaps

No coverage dependency was added. The ticket's persisted transitions are
covered offline through SQLite and controlled clocks. Real Telegram file
download and ComfyUI execution remain on the already tested adapters and are
deployment-acceptance concerns.
