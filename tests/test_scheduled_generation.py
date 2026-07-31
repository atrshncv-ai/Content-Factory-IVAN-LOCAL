"""Плановые фото и генерации по утверждённому недельному плану (ticket 06)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.db import Database
from core.task_control import TaskQueue
from core.weekly_plan import ScheduledPlan, WeeklyPlanController, CandidateStore

SAMARA = timezone(timedelta(hours=4))
NOW = datetime(2026, 8, 2, 10, 0, tzinfo=SAMARA)  # Sunday
PROFILE = {"timezone": "Europe/Samara", "brand": "Иван"}


def _plan() -> dict[str, Any]:
    return {
        "profile": PROFILE,
        "candidates": [],
        "cards": [
            {
                "publish_at": value,
                "title": f"card-{index}",
                "trend_url": "https://example.com",
                "audience_fit": "fit",
                "hook": "hook",
                "idea": "idea",
                "photo_recommendation": "portrait",
            }
            for index, value in enumerate(
                (
                    "2026-08-04T18:00:00+04:00",
                    "",
                    "bad strategic value",
                ),
                1,
            )
        ],
        "active_index": 3,
        "revision": 8,
        "phase": "approved",
        "mode": "manual",
    }


@pytest.fixture
def queue(tmp_path: Path) -> TaskQueue:
    db = Database(str(tmp_path / "scheduled.db"))
    db.init_schema()
    return TaskQueue(db)


def test_approval_creates_three_idempotent_tasks_and_fallback_schedule(
    queue: TaskQueue,
) -> None:
    state = {"weekly_plan": _plan()}
    scheduled = ScheduledPlan(queue)

    assert scheduled.connect_approved(state, NOW) == "Пришлите фото для карточки 1 из 3."
    scheduled.connect_approved(state, NOW)

    jobs = [queue.get(f"plan-8-{index}") for index in range(1, 4)]
    assert [job["status"] for job in jobs] == ["awaiting_photo"] * 3
    assert [job["publish_at"] for job in jobs] == [
        "2026-08-04T18:00:00+04:00",
        "2026-08-06T18:00:00+04:00",
        "2026-08-08T18:00:00+04:00",
    ]
    assert all(
        datetime.fromisoformat(job["due_at"])
        == datetime.fromisoformat(job["publish_at"]) - timedelta(hours=3)
        for job in jobs
    )


def test_photos_are_collected_sequentially_confirmed_and_latest_wins_before_start(
    queue: TaskQueue,
) -> None:
    state = {"weekly_plan": _plan()}
    scheduled = ScheduledPlan(queue)
    scheduled.connect_approved(state, NOW)

    assert scheduled.receive_photo(state, "photo-a", NOW) == (
        "Подтвердите фото для карточки 1."
    )
    assert scheduled.receive_photo(state, "photo-b", NOW) == (
        "Подтвердите фото для карточки 1."
    )
    assert scheduled.confirm_photo(state, NOW) == "Пришлите фото для карточки 2 из 3."
    assert queue.get("plan-8-1")["source_path"] == "photo-b"

    scheduled.receive_photo(state, "photo-c", NOW)
    scheduled.confirm_photo(state, NOW)
    scheduled.receive_photo(state, "photo-d", NOW)
    scheduled.confirm_photo(state, NOW)
    assert scheduled.receive_photo(state, "replacement", NOW) == (
        "Подтвердите замену фото для карточки 1."
    )
    scheduled.confirm_photo(state, NOW)
    assert queue.get("plan-8-1")["source_path"] == "replacement"

    queue.tick(datetime(2026, 8, 4, 15, 0, tzinfo=SAMARA), lambda _job: None)
    queue.tick(datetime(2026, 8, 4, 15, 1, tzinfo=SAMARA), lambda _job: None)
    with pytest.raises(ValueError, match="отмен"):
        scheduled.receive_photo(
            state, "too-late", datetime(2026, 8, 4, 15, 2, tzinfo=SAMARA)
        )


def test_due_without_photo_reminds_once_and_auto_continues_when_photo_arrives(
    queue: TaskQueue,
) -> None:
    state = {"weekly_plan": _plan()}
    scheduled = ScheduledPlan(queue)
    scheduled.connect_approved(state, NOW)
    due = datetime(2026, 8, 4, 15, 0, tzinfo=SAMARA)

    assert queue.tick(due, lambda _job: None) == {
        "stage": "missing_photo",
        "job_id": "plan-8-1",
    }
    assert queue.tick(due + timedelta(minutes=1), lambda _job: None)["stage"] == "idle"
    scheduled.receive_photo(state, "late-photo", due + timedelta(minutes=2))
    scheduled.confirm_photo(state, due + timedelta(minutes=3))
    assert queue.get("plan-8-1")["status"] == "awaiting_approval"


def test_mode_override_and_schedule_edit_stop_on_due_day(queue: TaskQueue) -> None:
    state = {"weekly_plan": _plan()}
    scheduled = ScheduledPlan(queue)
    scheduled.connect_approved(state, NOW)

    scheduled.set_mode(state, 1, "auto", NOW + timedelta(days=1))
    scheduled.reschedule(
        state, 1, datetime(2026, 8, 5, 18, tzinfo=SAMARA), NOW + timedelta(days=1)
    )
    assert queue.get("plan-8-1")["approval_mode"] == "auto"
    assert queue.get("plan-8-1")["publish_at"] == "2026-08-05T18:00:00+04:00"
    with pytest.raises(ValueError, match="дня генерации"):
        scheduled.set_mode(
            state, 1, "manual", datetime(2026, 8, 5, 10, tzinfo=SAMARA)
        )


def test_overdue_and_nearest_scheduled_priority_over_manual_without_preemption(
    queue: TaskQueue,
) -> None:
    for job_id in ("tg-1", "plan-old", "plan-near"):
        if job_id.startswith("tg-"):
            queue.create(job_id, "auto", NOW)
            queue.plan(job_id, "s", "p", NOW)
        else:
            publish = NOW + (timedelta(hours=1) if job_id == "plan-old" else timedelta(hours=2))
            queue.create_scheduled(job_id, "auto", publish, "s", "p")
            queue.set_photo(job_id, job_id)

    submitted: list[str] = []
    first = queue.tick(NOW, lambda job: submitted.append(str(job["job_id"])))
    assert first["job_id"] == "plan-old"
    assert queue.tick(NOW, lambda _job: None)["job_id"] == "plan-old"


def test_sunday_draft_guard_and_single_monday_reminder(tmp_path: Path) -> None:
    candidates = [
        {
            "source": "youtube",
            "title": str(i),
            "url": f"https://example.com/{i}",
            "published_at": "2026-08-01",
            "metrics": {},
        }
        for i in range(10)
    ]
    controller = WeeklyPlanController(
        CandidateStore(tmp_path / "analytics.json"),
        lambda _source: candidates,
        lambda _profile, pool: [_plan()["cards"][0] | {"trend_url": pool[i]["url"]}
                                for i in range(3)],
        lambda _profile, pool, _cards, _active: _plan()["cards"][0]
        | {"trend_url": pool[0]["url"]},
        lambda _profile, card, _feedback: card,
    )
    state: dict[str, Any] = {}

    assert controller.cron(state, PROFILE, NOW).startswith("Создан черновик")
    assert controller.cron(state, PROFILE, NOW) is None
    monday = NOW + timedelta(days=1)
    assert controller.cron(state, PROFILE, monday) == (
        "Напоминание: утвердите недельный план."
    )
    assert controller.cron(state, PROFILE, monday + timedelta(hours=1)) is None
