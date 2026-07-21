"""Webhook-приёмник Node 1 (ComfyUI) — связь рендер-сервера с «Мозгом».

Node 1 по завершении генерации шлёт POST /webhook с JSON
{job_id, status, filename}. Модуль:
  1. валидирует payload (включая защиту от path traversal);
  2. проверяет наличие файла в Shared Folder (сетевой шаринг, путь из .env);
  3. обновляет статус в media_jobs (core/db.py) и пишет аудит-лог;
  4. при статусе 'done' событийно триггерит тик Оркестратора (on_done) —
     Ambient Processing из AGENTS.md: оценка задания по входящему событию,
     без ожидания cron-тика.

Архитектура (Ponytail): stdlib http.server, ноль зависимостей. Логика
(`handle_webhook`) отделена от транспорта — тестируется без сокетов.
Сервер работает в daemon-потоке (`serve_in_background`) и не блокирует
Оркестратор, либо как foreground-процесс: `python -m core.webhook`.
"""
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from core.config import Settings
from core.db import Database

REQUIRED_FIELDS = ("job_id", "status", "filename")


def handle_webhook(
    payload: dict[str, Any], db: Database, shared_folder: Path
) -> dict[str, Any]:
    """Обрабатывает Webhook от Node 1: проверяет файл, обновляет media_jobs.

    Args:
        payload: JSON-тело {job_id, status, filename}.
        db: Операционная БД (core.db).
        shared_folder: Локальный путь к Shared Folder.

    Returns:
        {"ok": True, ...} при успехе или {"ok": False, "error": ...}:
        invalid_payload | file_not_found. При отсутствии файла задание
        помечается status='error' (брак для Контролера Качества).
    """
    job_id = payload.get("job_id")
    status = payload.get("status")
    filename = payload.get("filename")
    if not (
        isinstance(job_id, str) and job_id
        and isinstance(status, str) and status
        and isinstance(filename, str) and filename
    ):
        return {"ok": False, "error": "invalid_payload"}
    if Path(filename).name != filename:
        return {"ok": False, "error": "invalid_payload"}

    file_path = shared_folder / filename
    if not file_path.is_file():
        db.upsert_job(job_id, "error")
        db.log("WARNING", "webhook", f"job {job_id}: файл не найден ({filename})")
        return {"ok": False, "error": "file_not_found", "job_id": job_id}

    db.upsert_job(job_id, status, str(file_path))
    db.log("INFO", "webhook", f"job {job_id}: {status} ({filename})")
    return {
        "ok": True, "job_id": job_id, "status": status, "file_path": str(file_path),
    }


def _run_on_done(
    on_done: Callable[[str], Any], db: Database, job_id: str
) -> None:
    """Событийный триггер Оркестратора; его падение не роняет приёмник."""
    try:
        on_done(job_id)
    except Exception as exc:
        db.log("ERROR", "webhook", f"on_done {job_id}: {exc}")


def _make_handler(
    db: Database,
    shared_folder: Path,
    on_done: Callable[[str], Any] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Фабрика HTTP-handler'а с замкнутыми db, shared_folder и on_done.

    on_done: коллбэк событийного триггера Оркестратора. Вызывается в
    отдельном daemon-потоке ПОСЛЕ ответа Node 1 и только для статуса
    'done' — медленная оценка не блокирует HTTP-ответ.
    """

    class WebhookHandler(BaseHTTPRequestHandler):
        """POST /webhook → handle_webhook → JSON-ответ → триггер на done."""

        def do_POST(self) -> None:
            if self.path != "/webhook":
                self._respond(404, {"ok": False, "error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("payload is not a JSON object")
            except (ValueError, UnicodeDecodeError):
                self._respond(400, {"ok": False, "error": "invalid_json"})
                return
            result = handle_webhook(payload, db, shared_folder)
            if result["ok"]:
                self._respond(200, result)
            elif result["error"] == "invalid_payload":
                self._respond(400, result)
            else:
                self._respond(404, result)
            if result["ok"] and result.get("status") == "done" and on_done is not None:
                threading.Thread(
                    target=_run_on_done,
                    args=(on_done, db, str(result["job_id"])),
                    daemon=True,
                ).start()

        def _respond(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # тишина в stdout; аудит ведётся в SQLite (agent=webhook)

    return WebhookHandler


def serve_in_background(
    db: Database,
    shared_folder: Path,
    host: str,
    port: int,
    on_done: Callable[[str], Any] | None = None,
) -> ThreadingHTTPServer:
    """Поднимает Webhook-сервер в daemon-потоке (не блокирует Оркестратор).

    Args:
        port: 0 = эфемерный порт (для тестов).
        on_done: триггер Оркестратора на статус 'done' (см. _make_handler).

    Returns:
        Запущенный сервер; остановка — `httpd.shutdown()`.
    """
    httpd = ThreadingHTTPServer(
        (host, port), _make_handler(db, shared_folder, on_done)
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> None:
    """Foreground-запуск приёмника как отдельного процесса (`python -m core.webhook`).

    При статусе 'done' событийно триггерит тик Оркестратора для оценки
    задания (AGENTS.md: Ambient Processing — запуск по входящему Webhook).
    """
    from core.orchestrator import build_orchestrator  # lazy: защита от цикла импортов

    settings = Settings()
    database = Database(settings.db_path)
    database.init_schema()
    orchestrator = build_orchestrator(settings, database)
    httpd = ThreadingHTTPServer(
        (settings.webhook_host, settings.webhook_port),
        _make_handler(
            database,
            Path(settings.shared_folder),
            on_done=lambda job_id: orchestrator.run_once(trigger_job_id=job_id),
        ),
    )
    database.log(
        "INFO", "webhook",
        f"listening on {settings.webhook_host}:{settings.webhook_port}",
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        database.close()


if __name__ == "__main__":
    main()
