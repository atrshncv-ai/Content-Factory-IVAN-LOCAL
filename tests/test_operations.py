"""Ticket 07: one-shot operations and durability contracts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.db import Database
from core.operations import (
    KeyPool,
    backup_once,
    cleanup_once,
    disk_guard_once,
    recover_once,
    status_report,
)
from core.task_control import TaskQueue

NOW = datetime(2026, 7, 31, 10, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(str(tmp_path / "factory.db"))
    database.init_schema()
    return database


def test_key_pool_rotates_deterministically_and_pauses_after_two_limits(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "keys.json"
    pool = KeyPool(state_path, ("one", "two"))

    assert pool.active_number(NOW) == 1
    assert pool.rate_limited(NOW) == 2
    assert pool.rate_limited(NOW + timedelta(minutes=1)) == 0
    assert pool.active_number(NOW + timedelta(minutes=59)) == 0
    assert pool.active_number(NOW + timedelta(minutes=60)) == 1
    assert "one" not in state_path.read_text(encoding="utf-8")
    assert "two" not in state_path.read_text(encoding="utf-8")


def test_status_is_composite_safe_and_never_contains_secret_or_prompt(
    db: Database,
) -> None:
    db.upsert_job("V-104", "error", video_prompt="SECRET PROMPT")
    report = status_report(
        db,
        active_key_number=2,
        checks={
            "Tailscale": lambda: True,
            "Node 1": lambda: False,
            "ComfyUI": lambda: True,
        },
        plan_summary="утверждён",
    )

    assert "Hermes: работает" in report
    assert "ключ: №2" in report
    assert "Node 1: недоступен" in report
    assert "Очередь:" in report and "План: утверждён" in report
    assert "Последняя безопасная ошибка:" in report
    assert "SECRET PROMPT" not in report


def test_recovery_checks_active_history_and_notifies_both_admins(
    db: Database, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"weekly_plan": {"phase": "approved"}}), encoding="utf-8")
    db.upsert_job("V-104", "generating")
    messages: list[tuple[int, str]] = []

    summary = recover_once(
        db,
        state,
        (101, 202),
        lambda job_id: {"status": "success", "filename": f"{job_id}.mp4"},
        lambda admin_id, text: messages.append((admin_id, text)),
    )

    assert summary == {"tasks": 1, "plans": 1, "history_checked": 1}
    assert db.fetch_job("V-104")["status"] == "done"  # type: ignore[index]
    assert [item[0] for item in messages] == [101, 202]
    assert all("система онлайн" in item[1].lower() for item in messages)


def test_audit_has_telegram_id_logs_are_redacted_and_retained_30_days(
    db: Database,
) -> None:
    db.audit(101, "approve", "V-104")
    db.log("ERROR", "agent", "token=abc prompt=full secret=xyz")
    with db._conn:  # test-only ageing
        db._conn.execute(
            "UPDATE logs SET ts = datetime('now', '-31 days') WHERE id = 1"
        )
    assert db.prune_logs(30) == 1
    audit = db.fetch_audit()
    assert audit[0]["telegram_id"] == 101
    serialized = json.dumps([dict(row) for row in db.fetch_logs()]).lower()
    assert "abc" not in serialized and "full" not in serialized and "xyz" not in serialized


def test_backup_keeps_seven_versions_and_excludes_env(tmp_path: Path) -> None:
    source = tmp_path / "state"
    source.mkdir()
    (source / "factory.db").write_text("db", encoding="utf-8")
    (source / "state.json").write_text("{}", encoding="utf-8")
    (source / ".env").write_text("SECRET=x", encoding="utf-8")
    backups = tmp_path / "backups"

    for day in range(8):
        backup_once(source, backups, NOW + timedelta(days=day))

    archives = sorted(backups.glob("*.zip"))
    assert len(archives) == 7
    import zipfile

    with zipfile.ZipFile(archives[-1]) as archive:
        assert ".env" not in archive.namelist()
        assert "factory.db" in archive.namelist()


def test_cleanup_removes_old_failed_cancelled_and_temp_but_keeps_accepted(
    db: Database, tmp_path: Path
) -> None:
    failed = tmp_path / "failed.mp4"
    cancelled = tmp_path / "cancelled.mp4"
    accepted = tmp_path / "accepted.mp4"
    temporary = tmp_path / "temp.bin"
    for path in (failed, cancelled, accepted, temporary):
        path.write_bytes(b"x")
    db.upsert_job("f", "failed", file_path=str(failed))
    db.upsert_job("c", "cancelled", file_path=str(cancelled))
    db.upsert_job("a", "accepted", file_path=str(accepted))
    with db._conn:
        db._conn.execute("UPDATE media_jobs SET updated_at = datetime('now', '-8 days')")
    old = (NOW - timedelta(days=8)).timestamp()
    temporary.touch()
    import os

    os.utime(temporary, (old, old))

    result = cleanup_once(db, tmp_path, NOW)

    assert result["results"] == 2 and result["temporary"] == 1
    assert accepted.exists()


def test_disk_guard_blocks_new_generation_and_notifies_both_admins(
    db: Database,
) -> None:
    queue = TaskQueue(db)
    notices: list[int] = []
    allowed = disk_guard_once(
        free_bytes=19 * 1024**3,
        admin_ids=(101, 202),
        notify=lambda admin_id, _text: notices.append(admin_id),
    )
    queue.create("V-105", "auto", NOW)
    queue.plan("V-105", "s", "p", NOW)

    assert allowed is False
    assert queue.tick(NOW, lambda _job: None, generation_allowed=lambda: allowed) == {
        "stage": "blocked_disk",
        "job_id": "V-105",
    }
    assert notices == [101, 202]
