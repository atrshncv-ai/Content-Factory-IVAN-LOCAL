"""Тесты паттерна Watch Folder: core.orchestrator_sender + scripts.node1_watchdog.

Sender: подстановка промптов в текстовые ноды LTX-шаблона и атомарная
запись job-файла в Shared Folder. Watchdog: POST {"prompt": workflow}
на локальный API ComfyUI (мок через http.server в потоке).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from core.orchestrator_sender import load_env, send_job
from scripts.node1_watchdog import submit

LTX_TEMPLATE = Path("КОД COMFY/LTX-2.3_Triple_Sampler_DevMode.json")


# --- Sender -------------------------------------------------------------------

def test_send_job_injects_prompts(tmp_path: Path) -> None:
    """Позитив идёт в Text Multiline 5314, негатив — в CLIPTextEncode 5040."""
    out = send_job(
        job_id="t1", positive="man waves hand", negative="blurry",
        template_path=str(LTX_TEMPLATE), shared_folder=str(tmp_path),
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
        template_path=str(LTX_TEMPLATE), shared_folder=str(tmp_path),
    )
    workflow = json.loads(out.read_text(encoding="utf-8"))
    assert workflow["5040"]["inputs"]["text"] == original


def test_send_job_no_tmp_files_left(tmp_path: Path) -> None:
    """Атомарная запись: после os.replace .tmp-файлов не остаётся."""
    send_job(job_id="t3", positive="p", template_path=str(LTX_TEMPLATE), shared_folder=str(tmp_path))
    assert list(tmp_path.glob("*.tmp")) == []


def test_load_env_parses_key_value(tmp_path: Path) -> None:
    """load_env: KEY=VALUE, комментарии и пустые значения."""
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\n# comment\nB = two\nEMPTY=\n", encoding="utf-8")
    assert load_env(str(env_file)) == {"A": "1", "B": "two", "EMPTY": ""}


# --- Watchdog -----------------------------------------------------------------

class _CaptureHandler(BaseHTTPRequestHandler):
    """HTTP-заглушка ComfyUI: складывает тела POST-запросов в payloads."""

    payloads: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _CaptureHandler.payloads.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


def test_submit_posts_prompt_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """submit() шлёт на /prompt тело {"prompt": <workflow из job-файла>}."""
    _CaptureHandler.payloads.clear()
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(
            "scripts.node1_watchdog.COMFYUI_URL",
            f"http://127.0.0.1:{server.server_port}/prompt",
        )
        job = tmp_path / "job_x.json"
        job.write_text(json.dumps({"9": {"class_type": "SaveImage"}}), encoding="utf-8")
        submit(job)
        assert _CaptureHandler.payloads == [{"prompt": {"9": {"class_type": "SaveImage"}}}]
    finally:
        server.shutdown()
