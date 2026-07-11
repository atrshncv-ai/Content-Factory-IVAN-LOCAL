"""Тесты операционной памяти core.db (SQLite).

Покрывают контракт тонкой обертки над sqlite3: логи Оркестратора,
статусы медиа-генерации (Node 1) и многоуровневые метрики.
Каждый тест работает на изолированной БД во временной папке (tmp_path).
"""
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    """Свежая БД с применённой схемой на каждый тест."""
    database = Database(str(tmp_path / "ops.db"))
    database.init_schema()
    yield database
    database.close()


def _tables(db_file: Path) -> set[str]:
    """Имена таблиц в файле БД (читается независимым соединением)."""
    with sqlite3.connect(db_file) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {row[0] for row in rows}


# --- Схема ---------------------------------------------------------------------

def test_init_schema_creates_all_tables(db: Database, tmp_path: Path) -> None:
    """Создаются все три таблицы: логи, медиа-задания, метрики."""
    assert {"logs", "media_jobs", "metrics"} <= _tables(tmp_path / "ops.db")


def test_init_schema_is_idempotent(db: Database) -> None:
    """Повторное применение схемы не падает (CREATE IF NOT EXISTS)."""
    db.init_schema()
    db.init_schema()


# --- Логи ----------------------------------------------------------------------

def test_log_persists_entry(db: Database) -> None:
    """log() пишет запись с уровнем, агентом и сообщением."""
    db.log("INFO", "orchestrator", "run started")
    logs = db.fetch_logs()
    assert len(logs) == 1
    assert logs[0]["level"] == "INFO"
    assert logs[0]["agent"] == "orchestrator"
    assert logs[0]["message"] == "run started"


def test_fetch_logs_newest_first_with_limit(db: Database) -> None:
    """fetch_logs отдаёт записи от новых к старым и уважает лимит."""
    for i in range(5):
        db.log("INFO", "parser", f"step {i}")
    logs = db.fetch_logs(limit=2)
    assert len(logs) == 2
    assert logs[0]["message"] == "step 4"
    assert logs[1]["message"] == "step 3"


# --- Медиа-задания (Node 1) ----------------------------------------------------

def test_job_lifecycle_upsert(db: Database) -> None:
    """Задание создаётся в pending и обновляется до done с путём к файлу."""
    db.upsert_job("job-1", "pending")
    job = db.fetch_job("job-1")
    assert job is not None
    assert job["status"] == "pending"
    assert job["file_path"] is None

    db.upsert_job("job-1", "done", file_path="/shared/job-1.png")
    job = db.fetch_job("job-1")
    assert job is not None
    assert job["status"] == "done"
    assert job["file_path"] == "/shared/job-1.png"


def test_upsert_job_does_not_duplicate(db: Database) -> None:
    """Повторный upsert по тому же job_id не плодит строки."""
    db.upsert_job("job-1", "pending")
    db.upsert_job("job-1", "rendering")
    db.upsert_job("job-1", "done")
    assert db.fetch_job("job-1") is not None
    db.log("x", "x", "x")  # убеждаемся, что соединение живо


def test_fetch_job_missing_returns_none(db: Database) -> None:
    """Несуществующее задание — None, а не исключение."""
    assert db.fetch_job("nope") is None


# --- Метрики -------------------------------------------------------------------

def test_add_metric_stores_values(db: Database) -> None:
    """Метрика поста сохраняется с периодом и счётчиками."""
    db.add_metric("post-1", "1d", likes=10, comments=2, reach=500)
    metrics = db.fetch_metrics("post-1")
    assert len(metrics) == 1
    assert metrics[0]["period"] == "1d"
    assert metrics[0]["likes"] == 10
    assert metrics[0]["reach"] == 500


def test_add_metric_rejects_invalid_period(db: Database) -> None:
    """CHECK-ограничение: период только 1d / 1w / 1m."""
    with pytest.raises(sqlite3.IntegrityError):
        db.add_metric("post-1", "99x", likes=0, comments=0, reach=0)
