"""Активный поллер результатов ComfyUI (Node 1) — замена webhook-приёмника.

На Node 1 работает чистый ComfyUI (нотификатор не установить), поэтому
Node 2 сам опрашивает историю очереди через cli-anything-comfyui (syscall,
AGENTS.md п.3). Поллер находит завершённые задачи по job_id — он вшит в
filename_prefix workflow (render_workflow: `LTX_Output_{job_id}_`) — и
переводит media_jobs: generating → done (или error при сбое в ComfyUI),
фиксируя имя готового файла из outputs истории (он же лежит в output/Shared
Folder на Node 1).

Вызов: один опрос на cron-тик Оркестратора (run_once, Clean Context) или
standalone: `python -m core.poller`.
"""
import json
import subprocess
from typing import Any

from core.config import Settings
from core.db import Database


def _run_command(command: list[str]) -> str:
    """Запуск cli-anything-comfyui как syscall. Returns: stdout.

    Raises:
        subprocess.CalledProcessError: CLI завершился с ошибкой.
        subprocess.TimeoutExpired: CLI не ответил вовремя.
    """
    result = subprocess.run(
        command, capture_output=True, text=True, check=True, timeout=120,
    )
    return result.stdout


def build_history_command(settings: Settings) -> list[str]:
    """cli-anything-comfyui --url <url> --json queue history (глобальные флаги первыми)."""
    return [
        settings.comfyui_command, "--url", settings.comfyui_server_url,
        "--json", "queue", "history",
    ]


def _iter_entries(history: Any) -> list[Any]:
    """Нормализует историю: dict {prompt_id: entry} или list[entry] → list."""
    if isinstance(history, dict):
        return list(history.values())
    if isinstance(history, list):
        return history
    return []


def _entry_status(entry: Any) -> str:
    """status_str записи истории ('success' | 'error' | ...)."""
    if not isinstance(entry, dict):
        return ""
    status = entry.get("status")
    if isinstance(status, dict):
        return str(status.get("status_str", ""))
    return str(status or "")


def _entry_first_output(entry: Any, job_id: str) -> str | None:
    """Первое имя выходного файла записи; приоритет — содержащее job_id."""
    if not isinstance(entry, dict):
        return None
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        return None
    names: list[str] = []
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        for kind in ("images", "gifs", "videos"):
            for item in node_out.get(kind) or []:
                if isinstance(item, dict) and item.get("filename"):
                    names.append(str(item["filename"]))
    for name in names:
        if job_id in name:
            return name
    return names[0] if names else None


def find_in_history(history: Any, job_id: str) -> dict[str, Any] | None:
    """Ищет запись истории ComfyUI по job_id (подстрока в JSON записи).

    Returns:
        {"status": str, "filename": str | None} или None, если записи нет
        (задача ещё в очереди/в работе на Node 1).
    """
    for entry in _iter_entries(history):
        if job_id in json.dumps(entry, ensure_ascii=False):
            return {
                "status": _entry_status(entry),
                "filename": _entry_first_output(entry, job_id),
            }
    return None


def poll_once(db: Database, settings: Settings) -> dict[str, int]:
    """Один опрос истории ComfyUI: generating → done/error для завершённых задач.

    Падение CLI не роняет Оркестратор: ошибка пишется в лог, summary['errors'] += 1,
    задания остаются generating и будут опрошены на следующем тике.
    """
    summary: dict[str, int] = {"checked": 0, "done": 0, "errors": 0}
    jobs = db.fetch_jobs_by_status("generating")
    if not jobs:
        return summary
    try:
        history = json.loads(_run_command(build_history_command(settings)))
    except Exception as exc:
        db.log("ERROR", "poller", f"history недоступна: {exc}")
        summary["errors"] += 1
        return summary
    for job in jobs:
        summary["checked"] += 1
        job_id = str(job["job_id"])
        found = find_in_history(history, job_id)
        if found is None:
            continue  # задача ещё в очереди/в работе на Node 1
        if found["status"] == "success":
            filename = found["filename"] or ""
            db.upsert_job(job_id, "done", file_path=filename)
            db.log(
                "INFO", "poller",
                f"{job_id}: done ({filename or 'файл в Shared Folder'})",
            )
            summary["done"] += 1
        elif found["status"] == "error":
            db.upsert_job(job_id, "error")
            db.log("ERROR", "poller", f"{job_id}: ошибка выполнения в ComfyUI")
            summary["errors"] += 1
    return summary


def main() -> None:
    """Standalone-опрос: `python -m core.poller` (один проход, exit 0)."""
    settings = Settings()
    db = Database(settings.db_path)
    db.init_schema()
    summary = poll_once(db, settings)
    db.log("INFO", "poller", f"опрос завершён: {summary}")
    db.close()


if __name__ == "__main__":
    main()
