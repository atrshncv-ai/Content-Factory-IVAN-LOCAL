"""Детерминированная очередь разовых рендеров: один вызов — один переход."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from core.db import Database

TERMINAL = {"accepted", "cancelled"}


class NodeUnavailable(RuntimeError):
    """Node 1 не готов принять рендер; попытка не расходуется."""


class TaskQueue:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        job_id: str,
        mode: str,
        now: datetime,
        *,
        seed: int = 0,
    ) -> None:
        if mode not in {"manual", "auto"}:
            raise ValueError("Режим должен быть manual или auto")
        unfinished = [
            row for row in self._manual_jobs() if str(row["status"]) not in TERMINAL
        ]
        if len(unfinished) >= 3:
            raise ValueError("Допускается не более трёх незавершённых задач")
        self._db.upsert_job(job_id, "received")
        self._db.update_job_control(
            job_id, approval_mode=mode, seed=seed, render_started_at=None,
            job_kind="manual",
        )

    def create_scheduled(
        self,
        job_id: str,
        mode: str,
        publish_at: datetime,
        scenario: str,
        prompt: str,
    ) -> None:
        """Идемпотентно создаёт плановую задачу, не запуская её."""
        if self._db.fetch_job(job_id) is not None:
            return
        if mode not in {"manual", "auto"}:
            raise ValueError("Режим должен быть manual или auto")
        self._db.upsert_job(job_id, "awaiting_photo")
        self._db.update_job_control(
            job_id,
            approval_mode=mode,
            job_kind="scheduled",
            publish_at=publish_at.isoformat(),
            due_at=(publish_at - timedelta(hours=3)).isoformat(),
            concept=scenario,
            video_prompt=prompt,
        )

    def set_photo(self, job_id: str, source_path: str, now: datetime) -> None:
        job = self._required(job_id)
        if job["status"] in {"generating", "result_ready", *TERMINAL}:
            raise ValueError("После старта отмените задачу и создайте новую")
        due = self._parse(job["due_at"])
        status = str(job["status"])
        if due is not None and now >= due:
            status = "queued" if job["approval_mode"] == "auto" else "awaiting_approval"
        elif job["job_kind"] == "scheduled":
            status = "scheduled"
        self._db.update_job_control(
            job_id, source_path=source_path, status=status
        )

    def update_schedule(
        self, job_id: str, publish_at: datetime, *, mode: str | None = None
    ) -> None:
        values: dict[str, object] = {
            "publish_at": publish_at.isoformat(),
            "due_at": (publish_at - timedelta(hours=3)).isoformat(),
        }
        if mode is not None:
            if mode not in {"manual", "auto"}:
                raise ValueError("Режим должен быть manual или auto")
            values["approval_mode"] = mode
        self._db.update_job_control(job_id, **values)

    def plan(
        self, job_id: str, scenario: str, prompt: str, now: datetime
    ) -> None:
        job = self._required(job_id)
        status = "awaiting_approval" if job["approval_mode"] == "manual" else "queued"
        self._db.update_job_control(
            job_id, status=status, concept=scenario, video_prompt=prompt
        )

    def approve(self, job_id: str, now: datetime) -> None:
        job = self._required(job_id)
        if job["status"] != "awaiting_approval":
            raise ValueError("Задача не ожидает утверждения")
        self._db.update_job_control(job_id, status="queued")

    def revise_plan(
        self, job_id: str, feedback: str, now: datetime
    ) -> None:
        """Сохраняет ручную правку до первого запуска, не расходуя ререндер."""
        job = self._required(job_id)
        if job["status"] != "awaiting_approval" or not feedback.strip():
            raise ValueError("Задача не ожидает текстовой доработки")
        self._db.update_job_control(
            job_id,
            concept=f"{job['concept']}\nПравка: {feedback.strip()}",
            video_prompt=f"{job['video_prompt']}\nRevision: {feedback.strip()}",
        )

    def tick(
        self,
        now: datetime,
        submit: Callable[[Any], None],
        *,
        health: Callable[[], bool] = lambda: True,
        cancel_render: Callable[[str], None] = lambda _job_id: None,
        generation_allowed: Callable[[], bool] = lambda: True,
    ) -> dict[str, str]:
        jobs = self._jobs()
        active = next(
            (row for row in jobs if row["status"] == "generating"),
            None,
        )
        if active is not None:
            return self._tick_active(active, now, health, cancel_render)
        due_missing = next(
            (
                row for row in self._scheduled_jobs()
                if row["status"] == "awaiting_photo"
                and (due := self._parse(row["due_at"])) is not None
                and due <= now
                and row["photo_notice_at"] is None
            ),
            None,
        )
        if due_missing is not None:
            job_id = str(due_missing["job_id"])
            self._db.update_job_control(job_id, photo_notice_at=now.isoformat())
            return {"stage": "missing_photo", "job_id": job_id}
        due_ready = next(
            (
                row for row in self._scheduled_jobs()
                if row["status"] == "scheduled"
                and (due := self._parse(row["due_at"])) is not None
                and due <= now
            ),
            None,
        )
        if due_ready is not None:
            job_id = str(due_ready["job_id"])
            status = (
                "queued"
                if due_ready["approval_mode"] == "auto"
                else "awaiting_approval"
            )
            self._db.update_job_control(job_id, status=status)
            return {"stage": status, "job_id": job_id}
        queued = next(
            (row for row in self._priority_jobs(now) if row["status"] == "queued"),
            None,
        )
        if queued is None:
            return {"stage": "idle"}
        if not generation_allowed():
            return {"stage": "blocked_disk", "job_id": str(queued["job_id"])}
        return self._submit(queued, now, submit)

    def _submit(
        self, job: Any, now: datetime, submit: Callable[[Any], None]
    ) -> dict[str, str]:
        job_id = str(job["job_id"])
        try:
            submit(job)
        except NodeUnavailable:
            last = self._parse(job["node_notice_at"])
            count = int(job["node_notice_count"])
            interval = timedelta(minutes=30) if count == 1 else timedelta(hours=6)
            due = last is None or now - last >= interval
            if due:
                self._db.update_job_control(
                    job_id,
                    node_notice_at=now.isoformat(),
                    node_notice_count=count + 1,
                )
                return {"stage": "node_unavailable", "job_id": job_id}
            return {"stage": "waiting_node", "job_id": job_id}
        self._db.update_job_control(
            job_id,
            status="generating",
            attempts=int(job["attempts"]) + 1,
            render_started_at=now.isoformat(),
            notice_at=None,
            node_notice_at=None,
            node_notice_count=0,
        )
        return {"stage": "submitted", "job_id": job_id}

    def _tick_active(
        self,
        job: Any,
        now: datetime,
        health: Callable[[], bool],
        cancel_render: Callable[[str], None],
    ) -> dict[str, str]:
        job_id = str(job["job_id"])
        started = self._parse(job["render_started_at"]) or now
        elapsed = now - started
        if elapsed >= timedelta(hours=1):
            health()
            cancel_render(job_id)
            if int(job["attempts"]) >= 2:
                self._db.update_job_control(job_id, status="needs_action")
                return {"stage": "human_action", "job_id": job_id}
            self._db.update_job_control(
                job_id, status="queued", render_started_at=None
            )
            return {"stage": "retry_queued", "job_id": job_id}
        if elapsed >= timedelta(minutes=30) and job["notice_at"] is None:
            self._db.update_job_control(job_id, notice_at=now.isoformat())
            return {"stage": "notice", "job_id": job_id}
        return {"stage": "active", "job_id": job_id}

    def cancel(
        self,
        job_id: str,
        *,
        confirmed: bool,
        now: datetime,
        cancel_render: Callable[[str], None] = lambda _job_id: None,
    ) -> str:
        job = self._required(job_id)
        if job["status"] == "generating" and not confirmed:
            return "confirm"
        if job["status"] == "generating":
            cancel_render(job_id)
        self._db.update_job_control(job_id, status="cancelled")
        return "cancelled"

    def mark_result(self, job_id: str, now: datetime) -> None:
        self._required(job_id)
        self._db.update_job_control(job_id, status="result_ready")

    def failure_action(self, job_id: str, action: str, now: datetime) -> None:
        job = self._required(job_id)
        if job["status"] != "needs_action":
            raise ValueError("Задача не ожидает решения после сбоя")
        if action not in {"retry", "cancel"}:
            raise ValueError("Выберите retry или cancel")
        self._db.update_job_control(
            job_id, status="queued" if action == "retry" else "cancelled"
        )

    def result_action(
        self,
        job_id: str,
        action: str,
        now: datetime,
        *,
        feedback: str = "",
    ) -> None:
        job = self._required(job_id)
        if action == "accept":
            self._db.update_job_control(job_id, status="accepted")
            return
        if action == "cancel":
            self._db.update_job_control(job_id, status="cancelled")
            return
        if action not in {"regenerate", "revise"}:
            raise ValueError("Неизвестное действие")
        rerenders = int(job["user_rerenders"])
        if rerenders >= 2:
            raise ValueError("Разрешены только две пользовательские перегенерации")
        values: dict[str, object] = {
            "status": "queued",
            "user_rerenders": rerenders + 1,
            "seed": int(job["seed"]) + 1,
        }
        if action == "revise":
            if not feedback.strip():
                raise ValueError("Для доработки нужен комментарий")
            values["concept"] = f"{job['concept']}\nПравка: {feedback.strip()}"
            values["video_prompt"] = (
                f"{job['video_prompt']}\nRevision: {feedback.strip()}"
            )
        self._db.update_job_control(job_id, **values)

    def status(self, now: datetime) -> str:
        queued_position = 0
        lines: list[str] = []
        for job in self._manual_jobs():
            status = str(job["status"])
            suffix = ""
            if status == "queued":
                queued_position += 1
                suffix = f", позиция {queued_position}"
            lines.append(
                f"{job['job_id']}: {status}{suffix}, с {job['created_at']} UTC"
            )
        return "\n".join(lines) or "Очередь пуста."

    def get(self, job_id: str) -> Any:
        return self._required(job_id)

    def _manual_jobs(self) -> list[Any]:
        return [
            row for row in self._db.fetch_jobs()
            if row["job_kind"] == "manual"
        ]

    def _scheduled_jobs(self) -> list[Any]:
        return [row for row in self._db.fetch_jobs() if row["job_kind"] == "scheduled"]

    def _jobs(self) -> list[Any]:
        return self._db.fetch_jobs()

    def _priority_jobs(self, now: datetime) -> list[Any]:
        def key(row: Any) -> tuple[int, datetime, int]:
            due = self._parse(row["due_at"])
            if row["job_kind"] == "scheduled" and due is not None:
                return (0, due, int(row["id"]))
            return (1, now, int(row["id"]))

        return sorted(self._jobs(), key=key)

    def _required(self, job_id: str) -> Any:
        job = self._db.fetch_job(job_id)
        if job is None:
            raise ValueError("Задача не найдена")
        return job

    @staticmethod
    def _parse(value: object) -> datetime | None:
        return datetime.fromisoformat(str(value)) if value else None
