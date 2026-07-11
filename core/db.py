"""Операционная память контент-завода (SQLite).

Легковесная база для логов Оркестратора, статусов медиа-генерации (Node 1)
и многоуровневых метрик постов. Тонкая обертка над встроенным sqlite3 —
без ORM (Ponytail-минимализм). Путь к БД задаётся через Settings/.env.

Долговременная семантическая память (стилистика, тренды) живёт отдельно —
в Graphify + Vector DB; здесь только операционное состояние.
"""
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL DEFAULT (datetime('now')),
    level    TEXT NOT NULL,
    agent    TEXT NOT NULL,
    message  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL UNIQUE,
    status     TEXT NOT NULL,
    file_path  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS metrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id      TEXT NOT NULL,
    period       TEXT NOT NULL CHECK (period IN ('1d', '1w', '1m')),
    likes        INTEGER NOT NULL DEFAULT 0,
    comments     INTEGER NOT NULL DEFAULT 0,
    reach        INTEGER NOT NULL DEFAULT 0,
    collected_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Database:
    """Операционная БД: логи, медиа-задания, метрики.

    Одно соединение на экземпляр, коммиты через контекстный менеджер
    (`with self._conn` — commit при успехе, rollback при исключении).

    Example:
        >>> db = Database("data/factory.db")
        >>> db.init_schema()
        >>> db.log("INFO", "orchestrator", "run started")
        >>> db.close()
    """

    def __init__(self, db_path: str) -> None:
        """Открывает (или создаёт) файл БД; родительская папка создаётся автоматически.

        check_same_thread=False + Lock: соединением можно делиться между
        потоками (Webhook-сервер обрабатывает запросы в своих потоках).
        """
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def init_schema(self) -> None:
        """Создаёт таблицы, если их ещё нет (идемпотентно)."""
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    # --- Логи ------------------------------------------------------------------

    def log(self, level: str, agent: str, message: str) -> int:
        """Пишет запись в журнал. Returns: id созданной строки."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO logs (level, agent, message) VALUES (?, ?, ?)",
                (level, agent, message),
            )
        return cur.lastrowid or 0

    def fetch_logs(self, limit: int = 50) -> list[sqlite3.Row]:
        """Последние записи журнала, от новых к старым."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
            )
            return list(cur.fetchall())

    # --- Медиа-задания (Node 1) -------------------------------------------------

    def upsert_job(self, job_id: str, status: str, file_path: str | None = None) -> None:
        """Создаёт задание или обновляет его статус.

        `file_path` перезаписывается только если передан — повторный upsert
        статуса не затирает уже сохранённый путь к файлу.
        """
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO media_jobs (job_id, status, file_path)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    file_path = COALESCE(excluded.file_path, media_jobs.file_path),
                    updated_at = datetime('now')
                """,
                (job_id, status, file_path),
            )

    def fetch_jobs_by_status(self, status: str) -> list[sqlite3.Row]:
        """Все задания с заданным статусом, по порядку создания (Kanban-выборка)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM media_jobs WHERE status = ? ORDER BY id", (status,)
            )
            return list(cur.fetchall())

    def fetch_job(self, job_id: str) -> sqlite3.Row | None:
        """Задание по job_id или None, если его нет."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM media_jobs WHERE job_id = ?", (job_id,)
            )
            return cur.fetchone()

    # --- Метрики ----------------------------------------------------------------

    def add_metric(
        self, post_id: str, period: str, likes: int, comments: int, reach: int
    ) -> None:
        """Сохраняет метрику поста за период ('1d' / '1w' / '1m').

        Raises:
            sqlite3.IntegrityError: период вне CHECK-ограничения.
        """
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO metrics (post_id, period, likes, comments, reach)
                VALUES (?, ?, ?, ?, ?)
                """,
                (post_id, period, likes, comments, reach),
            )

    def fetch_metrics(self, post_id: str) -> list[sqlite3.Row]:
        """Все метрики поста, от новых к старым."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM metrics WHERE post_id = ? ORDER BY id DESC", (post_id,)
            )
            return list(cur.fetchall())

    def close(self) -> None:
        """Закрывает соединение с БД."""
        self._conn.close()
