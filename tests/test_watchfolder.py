"""Тесты pull-модели Node 2 ↔ Node 1.

orchestrator_sender: подстановка промптов и атомарная запись job-файла.
job_server: GET-список, GET-файл, DELETE-файл, защита от traversal.
node1_watchdog: pull-цикл list → fetch → submit → delete.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from core.orchestrator_sender import load_env, send_job
from scripts.job_server import Handler as JobHandler
from scripts.node1_watchdog import delete_job, fetch_job, list_jobs, submit

LTX_TEMPLATE = Path("КОД COMFY/LTX-2.3_Triple_Sampler_DevMode.json")


def _serve(handler: type[BaseHTTPRequestHandler]) -> HTTPServer:
    """Поднимает HTTP-сервер в daemon-потоке на свободном порту."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --- Sender -------------------------------------------------------------------

def test_send_job_injects_prompts(tmp_path: Path) -> None:
    """Позитив идёт в Text Multiline 5314, негатив — в CLIPTextEncode 5040."""
    out = send_job(
        job_id="t1", positive="man waves hand", negative="blurry",
        template_path=str(LTX_TEMPLATE), out_dir=str(tmp_path),
    )
    assert out.name == "job_t1.json"
    workflow = json.loads(out.read_text(encoding="utf-8"))
    assert workflow["5314"]["inputs"]["text"] == "man waves hand"
    assert workflow["5040"]["inputs"]["text"] == "blurry"


def test_send_job_empty_negative_keeps_stock(tmp_path: Path) -> None:
    """Без негатива стоковый негативный промпт шаблона не затирается."""
    original = json.loads(LTX_TEMPLATE.read_text(encoding="utf-8"))["5040"]["inputs"]["text"]
    out = send_job(
        job_id="t2", positive="p",
        template_path=str(LTX_TEMPLATE), out_dir=str(tmp_path),
    )
    workflow = json.loads(out.read_text(encoding="utf-8"))
    assert workflow["5040"]["inputs"]["text"] == original


def test_send_job_no_tmp_files_left(tmp_path: Path) -> None:
    """Атомарная запись: после os.replace .tmp-файлов не остаётся."""
    send_job(job_id="t3", positive="p", template_path=str(LTX_TEMPLATE), out_dir=str(tmp_path))
    assert list(tmp_path.glob("*.tmp")) == []


def test_load_env_parses_key_value(tmp_path: Path) -> None:
    """load_env: KEY=VALUE, комментарии и пустые значения."""
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\n# comment\nB = two\nEMPTY=\n", encoding="utf-8")
    assert load_env(str(env_file)) == {"A": "1", "B": "two", "EMPTY": ""}


# --- Job-server (Node 2) ------------------------------------------------------

def test_job_server_list_get_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET / → список; GET /job → контент; DELETE /job → файл удалён."""
    monkeypatch.setattr("scripts.job_server.JOBS_DIR", tmp_path)
    (tmp_path / "job_a.json").write_text('{"a": 1}', encoding="utf-8")
    server = _serve(JobHandler)
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        listing = json.loads(urllib.request.urlopen(f"{base}/", timeout=5).read())
        assert listing == ["job_a.json"]
        body = json.loads(urllib.request.urlopen(f"{base}/job_a.json", timeout=5).read())
        assert body == {"a": 1}
        req = urllib.request.Request(f"{base}/job_a.json", method="DELETE")
        urllib.request.urlopen(req, timeout=5)
        assert not (tmp_path / "job_a.json").exists()
    finally:
        server.shutdown()


def test_job_server_rejects_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Файлы без префикса job_ и с путями не отдаются (защита от traversal)."""
    monkeypatch.setattr("scripts.job_server.JOBS_DIR", tmp_path)
    (tmp_path / "secret.json").write_text("{}", encoding="utf-8")
    server = _serve(JobHandler)
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base}/secret.json", timeout=5)
        assert exc_info.value.code == 404
    finally:
        server.shutdown()


# --- Watchdog (Node 1) --------------------------------------------------------

class _Node2Stub(BaseHTTPRequestHandler):
    """Заглушка job-server'а: отдаёт список/файл, записывает DELETE."""

    files: dict[str, Any] = {}
    deleted: list[str] = []

    def do_GET(self) -> None:
        if self.path == "/":
            body = json.dumps(sorted(self.files)).encode("utf-8")
        else:
            body = json.dumps(self.files.get(self.path.lstrip("/"), {})).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:
        _Node2Stub.deleted.append(self.path.lstrip("/"))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _ComfyUIStub(BaseHTTPRequestHandler):
    """Заглушка ComfyUI: записывает тела POST-запросов."""

    payloads: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _ComfyUIStub.payloads.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


def test_watchdog_pull_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """list → fetch → submit → delete: файл уходит в ComfyUI и удаляется с Node 2."""
    workflow = {"9": {"class_type": "SaveImage"}}
    _Node2Stub.files = {"job_t.json": workflow}
    _Node2Stub.deleted = []
    _ComfyUIStub.payloads = []
    node2 = _serve(_Node2Stub)
    comfy = _serve(_ComfyUIStub)
    try:
        monkeypatch.setattr("scripts.node1_watchdog.NODE2_URL", f"http://127.0.0.1:{node2.server_port}")
        monkeypatch.setattr("scripts.node1_watchdog.COMFYUI_URL", f"http://127.0.0.1:{comfy.server_port}/prompt")
        assert list_jobs() == ["job_t.json"]
        fetched = fetch_job("job_t.json")
        submit(fetched, "job_t.json")
        delete_job("job_t.json")
        assert _ComfyUIStub.payloads == [{"prompt": workflow}]
        assert _Node2Stub.deleted == ["job_t.json"]
    finally:
        node2.shutdown()
        comfy.shutdown()
