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

Запуск: `python -m core.orchestrator [--trigger JOB_ID]`
"""
import argparse
import hashlib
import subprocess
from collections.abc import Callable
from functools import partial
from typing import Any, Protocol

from core.config import Settings
from core.db import Database
from core.evaluator import QualityEvaluator
from core.llm import LLMClient
from core.parser import TrendParser
from core.planner import ContentPlanner
from core.prompter import PromptEngineer
from core.publisher import Publisher

ACTIVE_STATUSES = (
    "new", "planned", "ready_for_render",
    "generating", "done", "approved", "regenerating",
)


class TrendSource(Protocol):
    """Интерфейс Парсера для Оркестратора."""
    def fetch_trends(self, query: str, limit: int = 10) -> list[dict[str, object]]: ...


class JobEvaluator(Protocol):
    """Интерфейс Контролёра Качества для Оркестратора."""
    def evaluate_job(self, job_id: str) -> dict[str, Any]: ...


class PostPublisher(Protocol):
    """Интерфейс SMM-Паблишера для Оркестратора."""
    def publish(self, media_path: str, caption: str) -> str: ...


class JobPlanner(Protocol):
    """Интерфейс Сценариста для Оркестратора."""
    def plan_job(self, job_id: str) -> dict[str, str]: ...


class JobPrompter(Protocol):
    """Интерфейс Промпт-инженера для Оркестратора."""
    def create_prompts(self, job_id: str) -> dict[str, str]: ...


def _job_id_from_trend(trend: dict[str, object]) -> str:
    """Детерминированный job_id из URL тренда (md5 — стабилен между процессами)."""
    digest = hashlib.md5(str(trend.get("url", "")).encode("utf-8")).hexdigest()[:8]
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
        submit: Отправка задания на Node 1 (cli-anything-comfyui).
        niche: Тематика для Парсера.

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
        submit: Callable[[str], None],
        niche: str = "ai",
    ) -> None:
        self._db = db
        self._parser = parser
        self._planner = planner
        self._prompter = prompter
        self._evaluator = evaluator
        self._publisher = publisher
        self._submit = submit
        self._niche = niche

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
        """Backpressure: доска пуста → Парсер → задания new."""
        try:
            trends = self._parser.fetch_trends(self._niche)
            for trend in trends:
                self._db.upsert_job(_job_id_from_trend(trend), "new")
                summary["created"] += 1
        except Exception as exc:
            summary["errors"] += 1
            self._db.log("ERROR", "orchestrator", f"parser: {exc}")

    def _publish_approved(self, job: Any) -> None:
        """approved → published: постинг через CLI, затем фиксация статуса."""
        caption = job["concept"] or self._caption(job["job_id"])
        self._publisher.publish(job["file_path"], caption)
        self._db.upsert_job(job["job_id"], "published")

    def _submit_ready(self, job: Any) -> None:
        """ready_for_render → generating: отправка промптов на Node 1."""
        self._submit(job["job_id"])
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


def _node1_call(settings: Settings, action: str, job_id: str) -> None:
    """Отправка/перегенерация задания на Node 1 через cli-anything-comfyui."""
    subprocess.run(
        [settings.comfyui_command, action, job_id], check=True, timeout=600,
    )


def main() -> None:
    """Точка входа: `python -m core.orchestrator [--trigger JOB_ID]`."""
    args = argparse.ArgumentParser(description="Оркестратор контент-завода")
    args.add_argument("--trigger", default=None, help="job_id от Webhook Node 1")
    parsed = args.parse_args()

    settings = Settings()
    db = Database(settings.db_path)
    db.init_schema()
    llm = LLMClient(settings)
    orchestrator = Orchestrator(
        db=db,
        parser=TrendParser(settings),
        planner=ContentPlanner(llm, db, niche=settings.content_niche),
        prompter=PromptEngineer(llm, db, niche=settings.content_niche),
        evaluator=QualityEvaluator(
            llm, db,
            regenerate=lambda job_id: _node1_call(settings, "regenerate", job_id),
            max_retries=settings.evaluator_max_retries,
        ),
        publisher=Publisher(settings, db),
        submit=lambda job_id: _node1_call(settings, "submit", job_id),
        niche=settings.content_niche,
    )
    orchestrator.run_once(trigger_job_id=parsed.trigger)
    db.close()


if __name__ == "__main__":
    main()
