"""Watchdog Node 1 (pull-модель): забирает job-файлы с Node 2 по HTTP.

Node 2 (мозг) раздаёт задачи своим job-server'ом (Tailscale, порт 8899).
Watchdog на ПК с ComfyUI раз в 2 секунды:
  1. GET    <node2>/           → список job-файлов;
  2. GET    <node2>/job_X.json → скачивает workflow;
  3. POST   workflow в локальный ComfyUI (127.0.0.1:8188/prompt);
  4. DELETE <node2>/job_X.json → чтобы не сгенерировать дважды.

Исходящее соединение Node 1 → Node 2 не блокируется брандмауэром Windows,
поэтому pull-модель работает там, где SMB/serve не проходили.

Изолированная CLI-утилита (syscall) на Node 1, не Оркестратор — polling-loop
допустим (запрет while True в AGENTS.md касается Node 2).

Ponytail: только stdlib (json, os, time, urllib).
"""
import json
import os
import time
import urllib.request
from typing import Any

NODE2_URL = os.environ.get("NODE2_URL", "http://100.123.244.99:8899")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188/prompt")


def _get_json(url: str) -> Any:
    """GET-запрос, ожидающий JSON."""
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_jobs() -> list[str]:
    """GET / → список имён job-файлов на Node 2."""
    return [str(name) for name in _get_json(f"{NODE2_URL}/")]


def fetch_job(name: str) -> Any:
    """GET /job_X.json → workflow ComfyUI."""
    return _get_json(f"{NODE2_URL}/{name}")


def delete_job(name: str) -> None:
    """DELETE /job_X.json — удалить отработанный файл на Node 2."""
    req = urllib.request.Request(f"{NODE2_URL}/{name}", method="DELETE")
    with urllib.request.urlopen(req, timeout=10):
        pass


def submit(workflow: Any, name: str) -> None:
    """POST workflow в локальный API ComfyUI."""
    payload = json.dumps({"prompt": workflow}).encode("utf-8")
    req = urllib.request.Request(
        COMFYUI_URL, data=payload, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"OK {name}: HTTP {resp.status}")


def main() -> None:
    """Polling-loop: каждые 2 секунды забирает новые job-файлы с Node 2."""
    print(f"Watchdog(pull): {NODE2_URL} -> {COMFYUI_URL}")
    while True:
        try:
            names = list_jobs()
        except Exception as exc:
            print(f"Node 2 недоступен: {exc}")
            names = []
        for name in names:
            try:
                workflow = fetch_job(name)
                submit(workflow, name)
                delete_job(name)
            except Exception as exc:
                # Файл остаётся на Node 2 — повтор на следующем тике.
                print(f"RETRY {name}: {exc}")
        time.sleep(2)


if __name__ == "__main__":
    main()
