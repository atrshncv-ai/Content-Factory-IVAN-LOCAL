# TDD evidence: task control and recovery

## Source

Specification: `.scratch/hermes-content-factory-mvp/spec.md`.
Ticket: `.scratch/hermes-content-factory-mvp/issues/04-task-control-and-recovery.md`.

## User journey

An administrator can queue at most three unfinished one-off jobs, approve or
cancel a selected job, observe one active render, recover once from a timeout,
and explicitly decide after a second failure or a completed result.

## RED → GREEN

- RED: `uv run pytest -q tests/test_task_control.py` failed during collection
  with `ModuleNotFoundError: core.task_control`. Checkpoint: `a5326a9`.
- GREEN: `uv run pytest -q` — `132 passed`.
- Types: `uv run mypy core tests` — `Success: no issues found in 30 source files`.
- Coverage: neither `pytest-cov` nor `coverage` is installed; no dependency was
  added solely for reporting. All ticket branches are exercised by 7 focused
  tests plus the full regression suite.

## Guarantees

| Guarantee | Evidence | Type |
|---|---|---|
| Three unfinished one-off jobs maximum; manual waits and auto queues | `test_max_three_manual_tasks_and_manual_or_auto_approval` | integration |
| One active render and deterministic queue positions | `test_only_one_render_and_status_has_position` | integration |
| Waiting cancel is immediate; active cancel requires confirmation | `test_cancel_waiting_immediately_but_active_requires_confirmation` | integration |
| One 30-minute notice, one automatic 60-minute retry, then human action | `test_notice_timeout_retry_and_second_failure_needs_human` | unit |
| Node 1 outage spends no attempt and uses immediate/30m/6h notices | `test_node_unavailable_spends_no_attempt_and_notifies_on_schedule` | unit |
| Accept/cancel/revise/regenerate and two-user-rerender limit | `test_result_actions_keep_inputs_and_limit_user_rerenders` | integration |
| Telegram shell exposes queue status, cancel selection and approval | `test_shell_exposes_status_cancel_and_current_task_actions` | contract |

## Boundaries

No live Telegram, Tailscale or ComfyUI call was made. Adapters/fakes are the
boundary. Weekly planning, ffprobe and Vision analysis remain outside ticket 04.
