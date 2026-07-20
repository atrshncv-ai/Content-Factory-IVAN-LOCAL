"""Watchdog Node 1 (Watch Folder): Shared Folder → локальный API ComfyUI.

Запускается локально на ПК с ComfyUI. Раз в 2 секунды проверяет папку
на новые job-файлы (job_*.json), отправляет их POST на 127.0.0.1:8188/prompt
и перемещает в processing/, чтобы не отправить дважды.

Это изолированная CLI-утилита (syscall) на Node 1, а не Оркестратор,
поэтому polling-loop здесь допустим (запрет while True в AGENTS.md п.2
касается главного Оркестратора на Node 2).

Ponytail: только stdlib (json, os, shutil, time, urllib, pathlib).
"""
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

WATCH_DIR = Path(os.environ.get("SHARED_FOLDER", "shared"))
DONE_DIR = WATCH_DIR / "processing"
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188/prompt")


def submit(job_path: Path) -> None:
    """Отправляет job-файл в локальный API ComfyUI (POST /prompt)."""
    workflow = json.loads(job_path.read_text(encoding="utf-8"))
    payload = json.dumps({"prompt": workflow}).encode("utf-8")
    req = urllib.request.Request(
        COMFYUI_URL, data=payload, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"OK {job_path.name}: HTTP {resp.status}")


def main() -> None:
    """Polling-loop: каждые 2 секунды забирает новые job-файлы."""
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Watchdog: слежу за {WATCH_DIR.resolve()}, API: {COMFYUI_URL}")
    while True:
        for job_path in sorted(WATCH_DIR.glob("job_*.json")):
            try:
                submit(job_path)
            except (json.JSONDecodeError, urllib.error.HTTPError) as exc:
                # Бракованный файл/запрос — в мертвую папку, не мучаем API.
                print(f"BAD {job_path.name}: {exc}")
                shutil.move(str(job_path), DONE_DIR / f"{job_path.name}.failed")
            except Exception as exc:
                # ComfyUI недоступен — файл остаётся, повтор на следующем тике.
                print(f"RETRY {job_path.name}: {exc}")
            else:
                shutil.move(str(job_path), DONE_DIR / job_path.name)
        time.sleep(2)


if __name__ == "__main__":
    main()
