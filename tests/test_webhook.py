"""Тесты модуля связи с Node 1 (core.webhook).

Покрывают логику Webhook-приёмника ComfyUI: валидацию payload, проверку
файла в Shared Folder и обновление media_jobs в SQLite. Shared Folder и БД
подменяются tmp_path; HTTP-слой проверяется на реальном сервере с
эфемерным портом (сеть — только loopback).
"""
import json
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.db import Database
from core.webhook import handle_webhook, serve_in_background


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    """Изолированная операционная БД."""
    database = Database(str(tmp_path / "ops.db"))
    database.init_schema()
    yield database
    database.close()


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    """Изолированная Shared Folder (сетевой шаринг Node 1)."""
    folder = tmp_path / "shared"
    folder.mkdir()
    return folder


def post_json(url: str, payload: dict[str, object]) -> tuple[int, dict]:
    """POST JSON на тестовый сервер. Returns: (HTTP-статус, тело ответа)."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


# --- Логика handle_webhook -----------------------------------------------------

def test_webhook_done_updates_job_with_file(db: Database, shared: Path) -> None:
    """Готовый файл в Shared Folder → задание обновляется до done с file_path."""
    (shared / "j1.png").write_bytes(b"pngdata")
    result = handle_webhook(
        {"job_id": "j1", "status": "done", "filename": "j1.png"}, db, shared,
    )
    assert result["ok"] is True
    job = db.fetch_job("j1")
    assert job is not None
    assert job["status"] == "done"
    assert job["file_path"] == str(shared / "j1.png")


def test_webhook_missing_file_marks_job_error(db: Database, shared: Path) -> None:
    """Файла нет в Shared Folder → ok=False, задание помечается error."""
    result = handle_webhook(
        {"job_id": "j2", "status": "done", "filename": "nope.png"}, db, shared,
    )
    assert result["ok"] is False
    assert result["error"] == "file_not_found"
    job = db.fetch_job("j2")
    assert job is not None
    assert job["status"] == "error"


def test_webhook_rejects_invalid_payload(db: Database, shared: Path) -> None:
    """Payload без обязательных полей отклоняется, БД не трогается."""
    result = handle_webhook({"status": "done"}, db, shared)
    assert result["ok"] is False
    assert result["error"] == "invalid_payload"
    assert db.fetch_job("j3") is None


def test_webhook_creates_job_for_unknown_id(db: Database, shared: Path) -> None:
    """Webhook о неизвестном job_id создаёт запись (upsert)."""
    (shared / "j4.mp4").write_bytes(b"mp4")
    handle_webhook({"job_id": "j4", "status": "done", "filename": "j4.mp4"}, db, shared)
    assert db.fetch_job("j4") is not None


def test_webhook_rejects_path_traversal(db: Database, shared: Path) -> None:
    """Имя файла с '../' отклоняется (защита от выхода из Shared Folder)."""
    result = handle_webhook(
        {"job_id": "j5", "status": "done", "filename": "../../etc/passwd"}, db, shared,
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_payload"
    assert db.fetch_job("j5") is None


def test_webhook_logs_event(db: Database, shared: Path) -> None:
    """Каждый Webhook оставляет запись в журнале Оркестратора (agent=webhook)."""
    (shared / "j6.png").write_bytes(b"png")
    handle_webhook({"job_id": "j6", "status": "done", "filename": "j6.png"}, db, shared)
    assert any(log["agent"] == "webhook" for log in db.fetch_logs())


# --- HTTP-слой -----------------------------------------------------------------

def test_http_endpoint_end_to_end(db: Database, shared: Path) -> None:
    """Сервер в фоновом потоке принимает POST /webhook и обновляет БД."""
    (shared / "j7.png").write_bytes(b"png")
    httpd = serve_in_background(db, shared, "127.0.0.1", 0)
    try:
        host = str(httpd.server_address[0])
        port = int(httpd.server_address[1])
        status, body = post_json(
            f"http://{host}:{port}/webhook",
            {"job_id": "j7", "status": "done", "filename": "j7.png"},
        )
        assert status == 200
        assert body["ok"] is True
        job = db.fetch_job("j7")
        assert job is not None
        assert job["status"] == "done"
    finally:
        httpd.shutdown()


# --- Событийный триггер Оркестратора (on_done) --------------------------------

def test_on_done_fires_after_done_status(db: Database, shared: Path) -> None:
    """Статус done → on_done вызывается в отдельном потоке после ответа."""
    (shared / "j8.png").write_bytes(b"png")
    fired = threading.Event()
    seen: list[str] = []

    def on_done(job_id: str) -> None:
        seen.append(job_id)
        fired.set()

    httpd = serve_in_background(db, shared, "127.0.0.1", 0, on_done=on_done)
    try:
        host = str(httpd.server_address[0])
        port = int(httpd.server_address[1])
        status, _ = post_json(
            f"http://{host}:{port}/webhook",
            {"job_id": "j8", "status": "done", "filename": "j8.png"},
        )
        assert status == 200
        assert fired.wait(timeout=5), "on_done не вызван"
        assert seen == ["j8"]
    finally:
        httpd.shutdown()


def test_on_done_not_fired_for_other_statuses(db: Database, shared: Path) -> None:
    """Не-done статусы триггер Оркестратора не вызывают."""
    (shared / "j9.png").write_bytes(b"png")
    fired = threading.Event()
    httpd = serve_in_background(
        db, shared, "127.0.0.1", 0, on_done=lambda job_id: fired.set(),
    )
    try:
        host = str(httpd.server_address[0])
        port = int(httpd.server_address[1])
        post_json(
            f"http://{host}:{port}/webhook",
            {"job_id": "j9", "status": "generating", "filename": "j9.png"},
        )
        assert not fired.wait(timeout=1), "on_done не должен вызываться"
    finally:
        httpd.shutdown()


def test_on_done_failure_does_not_break_server(db: Database, shared: Path) -> None:
    """Падение on_done логируется и не роняет приёмник: POST отвечает 200."""
    (shared / "j10.png").write_bytes(b"png")

    def broken(job_id: str) -> None:
        raise RuntimeError("boom")

    httpd = serve_in_background(db, shared, "127.0.0.1", 0, on_done=broken)
    try:
        host = str(httpd.server_address[0])
        port = int(httpd.server_address[1])
        status, _ = post_json(
            f"http://{host}:{port}/webhook",
            {"job_id": "j10", "status": "done", "filename": "j10.png"},
        )
        assert status == 200
        for _ in range(50):  # ждём отработки daemon-потока триггера
            if any("on_done j10" in log["message"] for log in db.fetch_logs()):
                break
            time.sleep(0.1)
        assert any("on_done j10" in log["message"] for log in db.fetch_logs())
    finally:
        httpd.shutdown()
