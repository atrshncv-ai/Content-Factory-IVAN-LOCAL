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
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL,
    file_path    TEXT,
    source_path  TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    concept      TEXT,
    video_prompt TEXT,
    photo_prompt TEXT,
    trend_title  TEXT,
    trend_url    TEXT,
    approval_mode TEXT NOT NULL DEFAULT 'auto',
    render_started_at TEXT,
    notice_at    TEXT,
    node_notice_at TEXT,
    node_notice_count INTEGER NOT NULL DEFAULT 0,
    user_rerenders INTEGER NOT NULL DEFAULT 0,
    seed         INTEGER NOT NULL DEFAULT 0,
    job_kind     TEXT NOT NULL DEFAULT 'manual',
    publish_at   TEXT,
    due_at       TEXT,
    photo_notice_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
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

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    telegram_id INTEGER NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL
);
"""

# Колонки, добавленные после первой версии схемы. CREATE TABLE IF NOT EXISTS
# не изменяет существующую таблицу, поэтому для legacy-БД (data/factory.db
# в проде) колонки добавляются через ALTER TABLE при init_schema.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("media_jobs", "concept", "TEXT"),
    ("media_jobs", "video_prompt", "TEXT"),
    ("media_jobs", "photo_prompt", "TEXT"),
    ("media_jobs", "trend_title", "TEXT"),
    ("media_jobs", "trend_url", "TEXT"),
    ("media_jobs", "source_path", "TEXT"),
    ("media_jobs", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("media_jobs", "approval_mode", "TEXT NOT NULL DEFAULT 'auto'"),
    ("media_jobs", "render_started_at", "TEXT"),
    ("media_jobs", "notice_at", "TEXT"),
    ("media_jobs", "node_notice_at", "TEXT"),
    ("media_jobs", "node_notice_count", "INTEGER NOT NULL DEFAULT 0"),
    ("media_jobs", "user_rerenders", "INTEGER NOT NULL DEFAULT 0"),
    ("media_jobs", "seed", "INTEGER NOT NULL DEFAULT 0"),
    ("media_jobs", "job_kind", "TEXT NOT NULL DEFAULT 'manual'"),
    ("media_jobs", "publish_at", "TEXT"),
    ("media_jobs", "due_at", "TEXT"),
    ("media_jobs", "photo_notice_at", "TEXT"),
]


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
        потоками (фоновые процессы Node 2 работают в своих потоках).
        """
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def init_schema(self) -> None:
        """Создаёт таблицы, если их ещё нет, и мигрирует legacy-схему (идемпотентно)."""
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._apply_migrations()

    def _apply_migrations(self) -> None:
        """Добавляет недостающие колонки в существующие таблицы (legacy-БД).

        PRAGMA table_info → если колонки нет, ALTER TABLE ADD COLUMN.
        Вызывать только под self._lock внутри транзакции.
        """
        for table, column, column_type in _MIGRATIONS:
            cur = self._conn.execute(f"PRAGMA table_info({table})")
            existing = {row["name"] for row in cur.fetchall()}
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                )

    # --- Логи ------------------------------------------------------------------

    def log(self, level: str, agent: str, message: str) -> int:
        """Пишет запись в журнал. Returns: id созданной строки."""
        message = self._safe_log_message(message)
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

    @staticmethod
    def _safe_log_message(message: str) -> str:
        """Не допускает значения секретов и полные промпты в техжурнал."""
        lowered = message.lower()
        if any(word in lowered for word in ("token=", "secret=", "prompt=")):
            return "чувствительные технические данные скрыты"
        return message

    def prune_logs(self, days: int = 30) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM logs WHERE ts < datetime('now', ?)",
                (f"-{days} days",),
            )
        return cur.rowcount

    def audit(self, telegram_id: int, action: str, target: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO audit (telegram_id, action, target) VALUES (?, ?, ?)",
                (telegram_id, action, target),
            )
        return cur.lastrowid or 0

    def fetch_audit(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM audit ORDER BY id DESC"))

    # --- Медиа-задания (Node 1) -------------------------------------------------

    def upsert_job(
        self,
        job_id: str,
        status: str,
        file_path: str | None = None,
        source_path: str | None = None,
        concept: str | None = None,
        video_prompt: str | None = None,
        photo_prompt: str | None = None,
        trend_title: str | None = None,
        trend_url: str | None = None,
    ) -> None:
        """Создаёт задание или обновляет его статус и данные.

        Поля concept/video_prompt/photo_prompt/trend_title/trend_url
        перезаписываются только если переданы — повторный upsert статуса
        не затирает уже сохранённые данные.
        """
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO media_jobs (job_id, status, file_path, source_path, concept,
                                        video_prompt, photo_prompt, trend_title, trend_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    file_path = COALESCE(excluded.file_path, media_jobs.file_path),
                    source_path = COALESCE(excluded.source_path, media_jobs.source_path),
                    concept = COALESCE(excluded.concept, media_jobs.concept),
                    video_prompt = COALESCE(excluded.video_prompt, media_jobs.video_prompt),
                    photo_prompt = COALESCE(excluded.photo_prompt, media_jobs.photo_prompt),
                    trend_title = COALESCE(excluded.trend_title, media_jobs.trend_title),
                    trend_url = COALESCE(excluded.trend_url, media_jobs.trend_url),
                    updated_at = datetime('now')
                """,
                (job_id, status, file_path, source_path, concept, video_prompt, photo_prompt,
                 trend_title, trend_url),
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

    def fetch_jobs(self) -> list[sqlite3.Row]:
        """Все задания в устойчивом порядке создания."""
        with self._lock:
            return list(self._conn.execute("SELECT * FROM media_jobs ORDER BY id"))

    def update_job_control(self, job_id: str, **values: object) -> None:
        """Обновляет только разрешённые lifecycle-поля."""
        allowed = {
            "status", "attempts", "approval_mode", "render_started_at",
            "notice_at", "node_notice_at", "user_rerenders", "seed",
            "node_notice_count", "concept", "video_prompt",
            "job_kind", "publish_at", "due_at", "photo_notice_at", "source_path",
        }
        if not values or not set(values) <= allowed:
            raise ValueError("Unsupported media job control field")
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE media_jobs SET {assignments}, updated_at = datetime('now') "
                "WHERE job_id = ?",
                (*values.values(), job_id),
            )

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
