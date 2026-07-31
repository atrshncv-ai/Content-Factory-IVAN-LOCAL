"""Одноразовые операции надёжности для запуска по cron."""

from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.db import Database

GIB = 1024**3


class KeyPool:
    """Детерминированная ротация двух ключевых слотов без сохранения секретов."""

    def __init__(self, state_path: Path, keys: tuple[str | None, str | None]) -> None:
        self._state_path = state_path
        self._available = tuple(key is not None for key in keys)

    def active_number(self, now: datetime) -> int:
        state = self._load()
        for number, available in enumerate(self._available, start=1):
            cooldown = state.get(str(number))
            if available and (
                not isinstance(cooldown, str)
                or datetime.fromisoformat(cooldown) <= now
            ):
                return number
        return 0

    def rate_limited(self, now: datetime) -> int:
        active = self.active_number(now)
        if active:
            state = self._load()
            state[str(active)] = (now + timedelta(minutes=60)).isoformat()
            self._save(state)
        return self.active_number(now)

    def _load(self) -> dict[str, str]:
        if not self._state_path.exists():
            return {}
        value = json.loads(self._state_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def _save(self, state: dict[str, str]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(state, sort_keys=True) + "\n", encoding="utf-8"
        )


def status_report(
    db: Database,
    *,
    active_key_number: int,
    checks: Mapping[str, Callable[[], bool]],
    plan_summary: str,
) -> str:
    """Составляет безопасный `/status`; исключения health-check не утекают."""
    lines = [
        "Hermes: работает",
        f"Активный OpenCode Zen ключ: №{active_key_number}",
    ]
    for name, check in checks.items():
        try:
            healthy = bool(check())
        except Exception:
            healthy = False
        lines.append(f"{name}: {'доступен' if healthy else 'недоступен'}")
    jobs = db.fetch_jobs()
    active = [row for row in jobs if row["status"] not in {"accepted", "cancelled"}]
    lines.append(f"Очередь: {len(active)}")
    lines.append(f"План: {plan_summary}")
    successful = next(
        (row for row in reversed(jobs) if row["status"] in {"accepted", "delivered"}),
        None,
    )
    lines.append(
        "Последний успех: "
        + (str(successful["updated_at"]) if successful is not None else "нет")
    )
    error = next(
        (row for row in db.fetch_logs() if row["level"] == "ERROR"),
        None,
    )
    lines.append(
        "Последняя безопасная ошибка: "
        + (str(error["message"]) if error is not None else "нет")
    )
    return "\n".join(lines)


def recover_once(
    db: Database,
    state_path: Path,
    admin_ids: Sequence[int],
    history: Callable[[str], Mapping[str, Any] | None],
    notify: Callable[[int, str], None],
) -> dict[str, int]:
    """Восстанавливает persisted state и сверяет один активный Comfy job."""
    state: dict[str, Any] = {}
    if state_path.exists():
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state = loaded
    tasks = [
        row for row in db.fetch_jobs()
        if row["status"] not in {"accepted", "cancelled", "failed"}
    ]
    active = next((row for row in tasks if row["status"] == "generating"), None)
    checked = 0
    if active is not None:
        checked = 1
        job_id = str(active["job_id"])
        found = history(job_id)
        if found and found.get("status") == "success" and found.get("filename"):
            db.upsert_job(job_id, "done", file_path=str(found["filename"]))
        elif found and found.get("status") == "error":
            db.upsert_job(job_id, "error")
    for admin_id in admin_ids:
        notify(admin_id, "Система онлайн. Сохранённые задачи и планы восстановлены.")
    return {
        "tasks": len(tasks),
        "plans": int(isinstance(state.get("weekly_plan"), dict)),
        "history_checked": checked,
    }


def backup_once(source: Path, destination: Path, now: datetime) -> Path:
    """Создаёт локальный ZIP и оставляет семь последних копий."""
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"node2-{now:%Y%m%d}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in source.rglob("*"):
            if path.is_file() and path.name != ".env":
                output.write(path, path.relative_to(source))
    for stale in sorted(destination.glob("node2-*.zip"))[:-7]:
        stale.unlink()
    return archive


def cleanup_once(
    db: Database, temporary_dir: Path, now: datetime
) -> dict[str, int]:
    """Удаляет старые непринятые результаты и недельные temp-файлы."""
    result_count = 0
    cutoff = now - timedelta(days=7)
    for job in db.fetch_jobs():
        updated = datetime.fromisoformat(str(job["updated_at"])).replace(
            tzinfo=now.tzinfo
        )
        if job["status"] in {"failed", "cancelled"} and updated < cutoff:
            path = Path(str(job["file_path"])) if job["file_path"] else None
            if path is not None and path.exists():
                path.unlink()
                result_count += 1
    temp_count = 0
    for path in temporary_dir.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff.timestamp():
            if not any(
                row["status"] == "accepted"
                and row["file_path"]
                and Path(str(row["file_path"])) == path
                for row in db.fetch_jobs()
            ):
                path.unlink()
                temp_count += 1
    return {"results": result_count, "temporary": temp_count}


def disk_guard_once(
    *,
    free_bytes: int | None = None,
    path: Path | None = None,
    admin_ids: Sequence[int],
    notify: Callable[[int, str], None],
) -> bool:
    """Проверяет порог 20 GiB и уведомляет администраторов."""
    free = free_bytes
    if free is None:
        if path is None:
            raise ValueError("path or free_bytes is required")
        free = shutil.disk_usage(path).free
    if free >= 20 * GIB:
        return True
    for admin_id in admin_ids:
        notify(admin_id, "Node 1: свободно менее 20 ГБ. Новые генерации заблокированы.")
    return False
