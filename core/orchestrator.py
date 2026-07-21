"""Главный Оркестратор контент-завода (Supervisor).

Единая точка входа. Машина состояний Kanban над таблицей media_jobs —
БЕЗ while-циклов (запрет AGENTS.md): один запуск = один проход по доске,
каждое задание двигается максимум на один шаг, затем чистый выход
(Clean Context, exit 0). Повтор — на следующем cron-тике или Webhook'е.

Стадии:  new → planned → ready_for_render → generating →(Node 1, Webhook)
         → done →(Evaluator)→ approved →(Publisher)→ published.
         Терминальные: published / failed / error.

Маршрутизация:
  - cron-тик:  done → Evaluator | approved → Publisher | new → Planner,
               planned → Prompter | ready_for_render → Node 1.
               ТОЛЬКО при пустой доске — Parser создаёт new (backpressure).
  - webhook:   --trigger <job_id> оценивает ровно одно задание.

Отказоустойчивость: каждый job в try/except — падение субагента пишется
в errors, тик продолжается. Снимки очередей берутся ДО мутаций: задание
не проходит две стадии за один тик (ровно один шаг Kanban).

При старте поднимает Webhook-приёмник Node 1 в daemon-потоке (best-effort:
если порт занят отдельным `python -m core.webhook` — продолжаем без него).

Запуск: `python -m core.orchestrator [--trigger JOB_ID]`
"""
import argparse
import hashlib
import json
import logging
import subprocess
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

from core.config import Settings
from core.db import Database
from core.evaluator import QualityEvaluator
from core.llm import LLMClient
from core.parser import TrendParser
from core.planner import ContentPlanner
from core.prompter import PromptEngineer
from core.publisher import Publisher
from core.webhook import serve_in_background

ACTIVE_STATUSES = (
    "new", "planned", "ready_for_render",
    "generating", "done", "approved", "regenerating",
)

PROMPT_PLACEHOLDER = "{{POSITIVE_PROMPT}}"

LTX_VIDEO_PROMPT_NODE = "5314"
LTX_FIRST_FRAME_NODE = "5029"
LTX_RESOLUTION_NODE = "5330"
LTX_PRIMARY_VIDEO_COMBINE_NODE = "5294"


def _is_ltx_video_workflow(workflow: dict[str, Any]) -> bool:
    """True, если workflow — LTX-2.3 video (признак: LTXResolutionHelperNode)."""
    return any(
        node.get("class_type") == "LTXResolutionHelperNode"
        for node in workflow.values()
    )


def _is_ltx_video_template(template_path: Path) -> bool:
    """True, если файл шаблона — LTX-2.3 video workflow."""
    workflow = json.loads(template_path.read_text(encoding="utf-8"))
    return _is_ltx_video_workflow(workflow)


def _first_frame_image_name(job_id: str, photo_prompt: str) -> str:
    """Детерминированное имя файла стартового кадра (i2v) на основе промпта."""
    prompt_hash = hashlib.md5(photo_prompt.encode("utf-8")).hexdigest()[:8]
    return f"first_frame_{job_id}_{prompt_hash}.png"


def _patch_ltx_video_workflow(
    workflow: dict[str, Any],
    job_id: str,
    video_prompt: str,
    photo_prompt: str,
) -> None:
    """Программный патчинг LTX-2.3 video workflow перед отправкой на Node 1.

    Мутации:
      - video_prompt в ноду Text Multiline (5314)
      - first_frame в LoadImage (5029)
      - 9:16 Reels в LTXResolutionHelperNode (5330)
      - save_output=True + filename_prefix с job_id для VHS_VideoCombine
    """
    prompt_node = workflow.get(LTX_VIDEO_PROMPT_NODE)
    if prompt_node is None or prompt_node.get("class_type") != "Text Multiline":
        raise ValueError(
            f"LTX-нода позитивного промпта {LTX_VIDEO_PROMPT_NODE} не найдена "
            f"или имеет неверный тип"
        )
    prompt_node["inputs"]["text"] = video_prompt

    first_frame_node = workflow.get(LTX_FIRST_FRAME_NODE)
    if first_frame_node is None or first_frame_node.get("class_type") != "LoadImage":
        raise ValueError(
            f"LTX-нода стартового кадра {LTX_FIRST_FRAME_NODE} не найдена "
            f"или имеет неверный тип"
        )
    first_frame_node["inputs"]["image"] = _first_frame_image_name(job_id, photo_prompt)

    resolution_node = workflow.get(LTX_RESOLUTION_NODE)
    if resolution_node is None or resolution_node.get("class_type") != "LTXResolutionHelperNode":
        raise ValueError(
            f"LTX-нода разрешения {LTX_RESOLUTION_NODE} не найдена "
            f"или имеет неверный тип"
        )
    resolution_node["inputs"].update(
        {
            "aspect_ratio": "custom",
            "custom_ratio_width": 9,
            "custom_ratio_height": 16,
        }
    )

    saver_count = 0
    for node_id, node in workflow.items():
        if node.get("class_type") == "VHS_VideoCombine":
            node["inputs"]["save_output"] = True
            if node_id == LTX_PRIMARY_VIDEO_COMBINE_NODE:
                node["inputs"]["filename_prefix"] = f"LTX_Output_{job_id}_"
            else:
                node["inputs"]["filename_prefix"] = f"LTX_Output_{job_id}_preview_{node_id}_"
            saver_count += 1
    if saver_count == 0:
        raise ValueError("В LTX-шаблоне не найдена нода VHS_VideoCombine")

    for node in workflow.values():
        if node.get("class_type") == "LoadAudioUI":
            audio_file = node["inputs"].get("audio")
            logger.warning(
                f"Workflow expects audio file {audio_file!r} in Node 1 input directory"
            )


def _render_static_workflow(workflow: dict[str, Any], photo_prompt: str) -> None:
    """Подстановка photo_prompt в placeholder-ноду статичного workflow."""
    injected = False
    for node in workflow.values():
        inputs = node.get("inputs", {})
        if (
            node.get("class_type") == "CLIPTextEncode"
            and inputs.get("text") == PROMPT_PLACEHOLDER
        ):
            inputs["text"] = photo_prompt
            injected = True
    if not injected:
        raise ValueError(
            f"placeholder {PROMPT_PLACEHOLDER} не найден в шаблоне"
        )


def render_workflow(
    template_path: Path,
    job_id: str,
    photo_prompt: str,
    out_dir: str,
    video_prompt: str | None = None,
) -> Path:
    """Рендерит workflow-JSON: LTX-видео или статичное фото.

    Для LTX-шаблонов применяет программный патчинг на лету
    (video_prompt, стартовый кадр, 9:16, saver).
    Для статичных шаблонов — подстановка photo_prompt в placeholder.

    Args:
        template_path: Путь к исходному шаблону ComfyUI (API-граф).
        job_id: Идентификатор задания.
        photo_prompt: Текстовый промпт для статичного кадра / имя кадра i2v.
        out_dir: Каталог для записи готового workflow-JSON.
        video_prompt: Текстовый промпт для LTX-видео (None для статики).

    Raises:
        ValueError: не найдена обязательная нода или placeholder.
    """
    workflow = json.loads(template_path.read_text(encoding="utf-8"))
    if _is_ltx_video_workflow(workflow):
        _patch_ltx_video_workflow(
            workflow, job_id, video_prompt or "", photo_prompt
        )
    else:
        _render_static_workflow(workflow, photo_prompt)
    out_path = Path(out_dir) / f"job_{job_id}_workflow.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return out_path


class TrendSource(Protocol):
    """Интерфейс Парсера для Оркестратора."""
    def fetch_trends(self, query: str, limit: int = 10) -> list[dict[str, object]]: ...


class JobEvaluator(Protocol):
    """Интерфейс Контролёра Качества для Оркестратора."""
    def evaluate_job(self, job_id: str) -> dict[str, Any]: ...


class PostPublisher(Protocol):
    """Интерфейс SMM-Паблишера для Оркестратора."""
    def publish(self, media_path: str, caption: str) -> str: ...
    def collect_metrics(self, post_id: str, period: str) -> dict[str, Any]: ...


class JobPlanner(Protocol):
    """Интерфейс Сценариста для Оркестратора."""
    def plan_job(self, job_id: str) -> dict[str, str]: ...


class JobPrompter(Protocol):
    """Интерфейс Промпт-инженера для Оркестратора."""
    def create_prompts(self, job_id: str) -> dict[str, str]: ...


def _job_id_from_trend(trend: dict[str, object]) -> str:
    """Детерминированный job_id из тренда (md5 — стабилен между процессами).

    Ключ: URL → title → весь JSON тренда. Fallback-источники (crawl4ai/Jina)
    часто отдают пустой URL — без fallback на title все такие тренды
    схлопывались бы в один id (md5 пустой строки).
    """
    key = str(trend.get("url") or "") or str(trend.get("title") or "")
    if not key:
        key = json.dumps(trend, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]
    return f"trend-{digest}"


class Orchestrator:
    """Supervisor: двигает Kanban-доску media_jobs на один шаг за запуск.

    Args:
        db: Операционная БД (доска + журнал).
        parser: Парсер трендов (создание новых заданий).
        planner: Сценарист (new → planned).
        prompter: Промпт-инженер (planned → ready_for_render).
        evaluator: Контролёр Качества (done → approved/failed).
        publisher: SMM-Паблишер (approved → published).
        submit: Постановка workflow в очередь Node 1: (job_id, workflow_path).
        niche: Тематика для Парсера.
        workflow_template: Путь к шаблону workflow ComfyUI (assets/).
        workflow_dir: Каталог для сгенерированных workflow-файлов.

    Example:
        >>> summary = Orchestrator(db, parser, planner, prompter, evaluator, publisher, submit).run_once()
    """

    def __init__(
        self,
        db: Database,
        parser: TrendSource,
        planner: JobPlanner,
        prompter: JobPrompter,
        evaluator: JobEvaluator,
        publisher: PostPublisher,
        submit: Callable[[str, str], None],
        niche: str = "ai",
        workflow_template: str = "assets/comfyui_workflow_template.json",
        workflow_dir: str = "/tmp",
    ) -> None:
        self._db = db
        self._parser = parser
        self._planner = planner
        self._prompter = prompter
        self._evaluator = evaluator
        self._publisher = publisher
        self._submit = submit
        self._niche = niche
        self._workflow_template = workflow_template
        self._workflow_dir = workflow_dir

    def run_once(self, trigger_job_id: str | None = None) -> dict[str, int]:
        """Один проход по доске. Returns: summary — процесс завершается сразу после."""
        summary: dict[str, int] = {
            "created": 0, "planned": 0, "prompts": 0,
            "submitted": 0, "evaluated": 0, "published": 0, "errors": 0,
        }
        if trigger_job_id is not None:
            self._evaluate_triggered(trigger_job_id, summary)
        else:
            self._cron_tick(summary)
        self._db.log("INFO", "orchestrator", f"тик завершён: {summary}")
        return summary

    # --- Маршруты ----------------------------------------------------------------

    def _evaluate_triggered(self, job_id: str, summary: dict[str, int]) -> None:
        """Webhook-маршрут: оценить ровно одно готовое задание."""
        job = self._db.fetch_job(job_id)
        if job is None or job["status"] != "done":
            self._db.log(
                "WARNING", "orchestrator", f"trigger {job_id}: задание не в статусе done",
            )
            return
        try:
            self._evaluator.evaluate_job(job_id)
            summary["evaluated"] += 1
        except Exception as exc:
            summary["errors"] += 1
            self._db.log("ERROR", "orchestrator", f"trigger {job_id}: {exc}")

    def _cron_tick(self, summary: dict[str, int]) -> None:
        """Cron-маршрут: снимки очередей → один шаг по каждой стадии."""
        # Снимки ДО мутаций (Clean Context: одно задание = один шаг)
        done_jobs = self._db.fetch_jobs_by_status("done")
        approved_jobs = self._db.fetch_jobs_by_status("approved")
        new_jobs = self._db.fetch_jobs_by_status("new")
        planned_jobs = self._db.fetch_jobs_by_status("planned")
        ready_jobs = self._db.fetch_jobs_by_status("ready_for_render")

        for job in done_jobs:
            self._guard(summary, "evaluated", job["job_id"],
                        partial(self._evaluator.evaluate_job, job["job_id"]))
        for job in approved_jobs:
            self._guard(summary, "published", job["job_id"],
                        partial(self._publish_approved, job))
        for job in ready_jobs:
            self._guard(summary, "submitted", job["job_id"],
                        partial(self._submit_ready, job))
        for job in planned_jobs:
            self._guard(summary, "prompts", job["job_id"],
                        partial(self._prompter.create_prompts, job["job_id"]))
        for job in new_jobs:
            self._guard(summary, "planned", job["job_id"],
                        partial(self._planner.plan_job, job["job_id"]))

        if not any(self._db.fetch_jobs_by_status(s) for s in ACTIVE_STATUSES):
            self._create_from_trends(summary)

    def _create_from_trends(self, summary: dict[str, int]) -> None:
        """Backpressure: доска пуста → Парсер → задания new с метаданными тренда."""
        try:
            trends = self._parser.fetch_trends(self._niche)
            for trend in trends:
                self._db.upsert_job(
                    _job_id_from_trend(trend), "new",
                    trend_title=str(trend.get("title", "")),
                    trend_url=str(trend.get("url", "")),
                )
                summary["created"] += 1
        except Exception as exc:
            summary["errors"] += 1
            self._db.log("ERROR", "orchestrator", f"parser: {exc}")

    def _publish_approved(self, job: Any) -> None:
        """approved → published: постинг через CLI, статус, затем метрики 1d.

        Статус фиксируется ДО сбора метрик: сбой CLI метрик не должен
        откатывать публикацию (ошибка уйдёт в errors, пост не ре-постится).
        """
        caption = job["concept"] or self._caption(job["job_id"])
        post_id = self._publisher.publish(job["file_path"], caption)
        self._db.upsert_job(job["job_id"], "published")
        self._publisher.collect_metrics(post_id, "1d")

    def _submit_ready(self, job: Any) -> None:
        """ready_for_render → generating: workflow-JSON → очередь Node 1.

        Для статики video_prompt игнорируется и логируется.
        Для LTX-видео video_prompt подставляется в шаблон программно.
        """
        video_prompt = str(job["video_prompt"] or "")
        if video_prompt and not _is_ltx_video_template(Path(self._workflow_template)):
            self._db.log(
                "INFO", "orchestrator",
                f"job {job['job_id']}: video_prompt игнорируется (только статика)",
            )
        workflow = render_workflow(
            Path(self._workflow_template),
            job["job_id"],
            str(job["photo_prompt"] or ""),
            self._workflow_dir,
            video_prompt=video_prompt,
        )
        self._submit(job["job_id"], str(workflow))
        self._db.upsert_job(job["job_id"], "generating")

    # --- Хелперы ------------------------------------------------------------------

    def _guard(
        self, summary: dict[str, int], counter: str, job_id: str, action: Callable[[], object]
    ) -> None:
        """Изоляция ошибки одного job: инкремент counter или errors+лог."""
        try:
            action()
            summary[counter] += 1
        except Exception as exc:
            summary["errors"] += 1
            self._db.log("ERROR", "orchestrator", f"{counter} {job_id}: {exc}")

    def _caption(self, job_id: str) -> str:
        """Временная подпись к посту (если нет концепции)."""
        return f"{job_id} — AI контент #{self._niche}"


def _build_node1_command(settings: Settings, workflow_path: str) -> list[str]:
    """Реальный контракт cli-anything-comfyui: --url URL queue prompt --workflow PATH.

    --url — глобальная опция CLI (идёт ПЕРЕД подкомандой): без неё CLI
    стучится на localhost:8188, а ComfyUI живёт на Node 1 (Tailscale).
    """
    return [
        settings.comfyui_command,
        "--url", settings.comfyui_server_url,
        "queue", "prompt", "--workflow", workflow_path,
    ]


def _node1_call(settings: Settings, workflow_path: str) -> None:
    """Ставит workflow-файл в очередь рендера Node 1."""
    subprocess.run(
        _build_node1_command(settings, workflow_path), check=True, timeout=600,
    )


def build_orchestrator(settings: Settings, db: Database) -> Orchestrator:
    """Собирает Оркестратор с боевыми субагентами (единая точка wiring'а).

    Используется CLI (`python -m core.orchestrator`) и Webhook-приёмником
    (`python -m core.webhook`) для событийного триггера оценки задания.
    """
    llm = LLMClient(settings)

    def _regenerate(job_id: str) -> None:
        """Перегенерация = повторная постановка workflow в очередь ComfyUI."""
        job = db.fetch_job(job_id)
        prompt = str(job["photo_prompt"] or "") if job is not None else ""
        video_prompt = str(job["video_prompt"] or "") if job is not None else ""
        workflow = render_workflow(
            Path(settings.comfyui_workflow_template), job_id, prompt,
            settings.comfyui_workflow_dir,
            video_prompt=video_prompt,
        )
        _node1_call(settings, str(workflow))

    return Orchestrator(
        db=db,
        parser=TrendParser(settings),
        planner=ContentPlanner(llm, db, niche=settings.content_niche),
        prompter=PromptEngineer(llm, db, niche=settings.content_niche),
        evaluator=QualityEvaluator(
            llm, db,
            regenerate=_regenerate,
            max_retries=settings.evaluator_max_retries,
        ),
        publisher=Publisher(settings, db),
        submit=lambda job_id, wf_path: _node1_call(settings, wf_path),
        niche=settings.content_niche,
        workflow_template=settings.comfyui_workflow_template,
        workflow_dir=settings.comfyui_workflow_dir,
    )


def _start_webhook(settings: Settings, db: Database, orchestrator: Orchestrator) -> None:
    """Поднимает Webhook-приёмник Node 1 в daemon-потоке (best-effort).

    Порт занят отдельным процессом (`python -m core.webhook`) — не падаем:
    приёмник жив, событийный триггер работает через него.
    """
    try:
        serve_in_background(
            db, Path(settings.shared_folder),
            settings.webhook_host, settings.webhook_port,
            on_done=lambda job_id: orchestrator.run_once(trigger_job_id=job_id),
        )
        db.log(
            "INFO", "orchestrator",
            f"webhook слушает {settings.webhook_host}:{settings.webhook_port}",
        )
    except OSError as exc:
        db.log("WARNING", "orchestrator", f"webhook не поднят: {exc}")


def main() -> None:
    """Точка входа: `python -m core.orchestrator [--trigger JOB_ID]`."""
    args = argparse.ArgumentParser(description="Оркестратор контент-завода")
    args.add_argument("--trigger", default=None, help="job_id от Webhook Node 1")
    parsed = args.parse_args()

    settings = Settings()
    db = Database(settings.db_path)
    db.init_schema()
    orchestrator = build_orchestrator(settings, db)
    _start_webhook(settings, db, orchestrator)
    orchestrator.run_once(trigger_job_id=parsed.trigger)
    db.close()


if __name__ == "__main__":
    main()
