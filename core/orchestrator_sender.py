"""Отправитель задач на Node 1 (pull-модель).

Загружает шаблон workflow ComfyUI, подставляет промпты в текстовые ноды
и сохраняет готовый job-файл в локальную папку задач (data/jobs).
Папку раздаёт scripts/job_server.py по HTTP через Tailscale; watchdog
на Node 1 сам забирает файл исходящим соединением — брандмауэр Windows
и SMB не задействованы.

Текстовые ноды LTX-2.3 (инвентаризация workflow заказчика):
  5314 (Text Multiline) — позитивный промпт (источник для CLIPTextEncode 5039);
  5040 (CLIPTextEncode, "Negative Prompt") — негативный промпт.

Ponytail: только stdlib (json, os, sys, pathlib).
"""
import json
import os
import sys
from pathlib import Path

POSITIVE_NODE = "5314"  # Text Multiline — позитивный промпт
NEGATIVE_NODE = "5040"  # CLIPTextEncode — негативный промпт

DEFAULT_TEMPLATE = "КОД COMFY/LTX-2.3_Triple_Sampler_DevMode.json"


def load_env(path: str = ".env") -> dict[str, str]:
    """Читает KEY=VALUE из .env без внешних зависимостей."""
    env: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def send_job(
    job_id: str,
    positive: str,
    template_path: str,
    out_dir: str,
    negative: str = "",
) -> Path:
    """Подставляет промпты в текстовые ноды и атомарно пишет job-файл.

    Атомарная запись (.tmp → os.replace) защищает Node 1 от чтения
    недописанного файла, пока job-server раздаёт папку по HTTP.

    Returns:
        Путь к созданному job-файлу в папке задач.
    """
    workflow = json.loads(Path(template_path).read_text(encoding="utf-8"))
    workflow[POSITIVE_NODE]["inputs"]["text"] = positive
    if negative:
        workflow[NEGATIVE_NODE]["inputs"]["text"] = negative
    out_path_dir = Path(out_dir)
    out_path_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_path_dir / f"job_{job_id}.json"
    tmp_path = out_path_dir / f"job_{job_id}.tmp"
    tmp_path.write_text(json.dumps(workflow, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, out_path)
    return out_path


def main() -> None:
    """CLI: python -m core.orchestrator_sender JOB_ID "positive" ["negative"]."""
    if len(sys.argv) < 3:
        raise SystemExit("usage: orchestrator_sender.py JOB_ID POSITIVE [NEGATIVE]")
    env = load_env()
    out = send_job(
        job_id=sys.argv[1],
        positive=sys.argv[2],
        template_path=env.get("COMFYUI_WORKFLOW_TEMPLATE", DEFAULT_TEMPLATE),
        out_dir=env.get("JOBS_DIR", "data/jobs"),
        negative=sys.argv[3] if len(sys.argv) > 3 else "",
    )
    print(f"job отправлен: {out}")


if __name__ == "__main__":
    main()
