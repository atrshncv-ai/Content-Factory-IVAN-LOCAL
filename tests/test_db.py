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


def test_upsert_job_stores_trend_metadata(db: Database) -> None:
    """trend_title/trend_url сохраняются при создании и не затираются upsert'ом статуса."""
    db.upsert_job("t-1", "new", trend_title="Coffee art", trend_url="https://ig/p/1")
    job = db.fetch_job("t-1")
    assert job is not None
    assert job["trend_title"] == "Coffee art"
    assert job["trend_url"] == "https://ig/p/1"
    db.upsert_job("t-1", "planned")
    job = db.fetch_job("t-1")
    assert job is not None and job["trend_title"] == "Coffee art"


def test_fetch_jobs_by_status(db: Database) -> None:
    """Оркестратор опрашивает доску по статусам (Kanban-выборки)."""
    db.upsert_job("a", "new")
    db.upsert_job("b", "done")
    db.upsert_job("c", "new")
    jobs = db.fetch_jobs_by_status("new")
    assert {job["job_id"] for job in jobs} == {"a", "c"}


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


# --- P1: миграция legacy-БД ----------------------------------------------------

LEGACY_MEDIA_JOBS_SCHEMA = """
CREATE TABLE media_jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL UNIQUE,
    status     TEXT NOT NULL,
    file_path  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _make_legacy_db(db_path: Path) -> None:
    """Создаёт файл БД со старой схемой media_jobs (без новых колонок)."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.executescript(LEGACY_MEDIA_JOBS_SCHEMA)
    conn.execute(
        "INSERT INTO media_jobs (job_id, status) VALUES ('old-job', 'new')"
    )
    conn.commit()
    conn.close()


def test_init_schema_migrates_legacy_media_jobs(tmp_path: Path) -> None:
    """P1: legacy-БД без trend_title/trend_url/concept/prompts → ALTER TABLE."""
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)

    db = Database(str(db_path))
    db.init_schema()

    db.upsert_job(
        "old-job", "planned",
        concept="c", photo_prompt="p", trend_title="t", trend_url="u",
    )
    job = db.fetch_job("old-job")
    assert job is not None
    assert job["concept"] == "c"
    assert job["photo_prompt"] == "p"
    assert job["trend_title"] == "t"
    assert job["trend_url"] == "u"
    db.close()


def test_init_schema_migration_is_idempotent(tmp_path: Path) -> None:
    """Повторный init_schema на уже мигрированной БД не падает."""
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)

    db = Database(str(db_path))
    db.init_schema()
    db.init_schema()  # второй прогон — колонки уже есть
    assert db.fetch_job("old-job") is not None
    db.close()
