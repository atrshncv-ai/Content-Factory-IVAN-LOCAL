"""Управляемый lifecycle разовых Telegram-генераций (ticket 04)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.db import Database
from core.hermes_shell import HermesShell
from core.task_control import NodeUnavailable, TaskQueue

NOW = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def queue(tmp_path: Path) -> TaskQueue:
    db = Database(str(tmp_path / "tasks.db"))
    db.init_schema()
    return TaskQueue(db)


def test_max_three_manual_tasks_and_manual_or_auto_approval(
    queue: TaskQueue,
) -> None:
    for number in range(1, 4):
        queue.create(f"tg-{number}", "manual", NOW)
    with pytest.raises(ValueError, match="трёх"):
        queue.create("tg-4", "manual", NOW)

    queue.plan("tg-1", "сценарий", "prompt", NOW)
    assert queue.get("tg-1")["status"] == "awaiting_approval"
    queue.approve("tg-1", NOW)
    assert queue.get("tg-1")["status"] == "queued"

    queue.cancel("tg-2", confirmed=False, now=NOW)
    queue.create("tg-4", "auto", NOW)
    queue.plan("tg-4", "сценарий", "prompt", NOW)
    assert queue.get("tg-4")["status"] == "queued"


def test_only_one_render_and_status_has_position(queue: TaskQueue) -> None:
    for number in (1, 2):
        job_id = f"tg-{number}"
        queue.create(job_id, "auto", NOW)
        queue.plan(job_id, "s", "p", NOW)
    submitted: list[str] = []

    assert queue.tick(NOW, lambda job: submitted.append(str(job["job_id"]))) == {
        "stage": "submitted",
        "job_id": "tg-1",
    }
    assert queue.tick(NOW, lambda job: submitted.append(str(job["job_id"]))) == {
        "stage": "active",
        "job_id": "tg-1",
    }
    assert submitted == ["tg-1"]
    status = queue.status(NOW)
    assert "tg-1: generating" in status
    assert "tg-2: queued, позиция 1" in status


def test_cancel_waiting_immediately_but_active_requires_confirmation(
    queue: TaskQueue,
) -> None:
    queue.create("tg-1", "auto", NOW)
    queue.plan("tg-1", "s", "p", NOW)
    queue.tick(NOW, lambda _job: None)

    assert queue.cancel("tg-1", confirmed=False, now=NOW) == "confirm"
    cancelled: list[str] = []
    assert queue.cancel(
        "tg-1", confirmed=True, now=NOW, cancel_render=cancelled.append
    ) == "cancelled"
    assert cancelled == ["tg-1"]
    assert queue.get("tg-1")["status"] == "cancelled"


def test_notice_timeout_retry_and_second_failure_needs_human(
    queue: TaskQueue,
) -> None:
    queue.create("tg-1", "auto", NOW)
    queue.plan("tg-1", "s", "p", NOW)
    queue.tick(NOW, lambda _job: None)

    assert queue.tick(NOW + timedelta(minutes=30), lambda _job: None)["stage"] == "notice"
    assert queue.tick(NOW + timedelta(minutes=31), lambda _job: None)["stage"] == "active"
    assert queue.tick(
        NOW + timedelta(minutes=60),
        lambda _job: None,
        health=lambda: True,
        cancel_render=lambda _job_id: None,
    )["stage"] == "retry_queued"
    queue.tick(NOW + timedelta(minutes=61), lambda _job: None)
    assert queue.tick(
        NOW + timedelta(minutes=121),
        lambda _job: None,
        health=lambda: True,
        cancel_render=lambda _job_id: None,
    )["stage"] == "human_action"
    assert queue.get("tg-1")["attempts"] == 2


def test_node_unavailable_spends_no_attempt_and_notifies_on_schedule(
    queue: TaskQueue,
) -> None:
    queue.create("tg-1", "auto", NOW)
    queue.plan("tg-1", "s", "p", NOW)

    def unavailable(_job: object) -> None:
        raise NodeUnavailable

    assert queue.tick(NOW, unavailable)["stage"] == "node_unavailable"
    assert queue.get("tg-1")["attempts"] == 0
    assert queue.tick(NOW + timedelta(minutes=30), unavailable)["stage"] == "node_unavailable"
    assert queue.tick(NOW + timedelta(hours=1), unavailable)["stage"] == "waiting_node"
    assert queue.tick(NOW + timedelta(hours=6, minutes=30), unavailable)["stage"] == (
        "node_unavailable"
    )


def test_result_actions_keep_inputs_and_limit_user_rerenders(
    queue: TaskQueue,
) -> None:
    queue.create("tg-1", "auto", NOW, seed=10)
    queue.plan("tg-1", "scene", "prompt", NOW)
    queue.mark_result("tg-1", NOW)

    queue.result_action("tg-1", "regenerate", NOW)
    assert queue.get("tg-1")["seed"] == 11
    queue.mark_result("tg-1", NOW)
    queue.result_action("tg-1", "revise", NOW, feedback="больше движения")
    revised = queue.get("tg-1")
    assert revised["concept"] == "scene\nПравка: больше движения"
    assert revised["video_prompt"] == "prompt\nRevision: больше движения"
    queue.mark_result("tg-1", NOW)
    with pytest.raises(ValueError, match="две"):
        queue.result_action("tg-1", "regenerate", NOW)

    queue.result_action("tg-1", "accept", NOW)
    assert queue.get("tg-1")["status"] == "accepted"


def test_shell_exposes_status_cancel_and_current_task_actions(
    queue: TaskQueue, tmp_path: Path
) -> None:
    queue.create("tg-1", "manual", NOW)
    queue.plan("tg-1", "scene", "prompt", NOW)
    shell = HermesShell(
        frozenset({101, 202}), tmp_path / "state.json", task_queue=queue
    )

    status = shell.handle(
        {"update_id": 1, "message": {"from": {"id": 101}, "text": "/status"}}
    )
    assert status is not None and "tg-1: awaiting_approval" in status
    cancel = shell.handle(
        {"update_id": 2, "message": {"from": {"id": 101}, "text": "/cancel"}}
    )
    assert cancel is not None and "tg-1" in cancel
    approved = shell.handle(
        {
            "callback_query": {
                "id": "approve-1",
                "from": {"id": 202},
                "data": "task:approve:tg-1",
            }
        }
    )
    assert approved == "tg-1: утверждено и поставлено в очередь."
