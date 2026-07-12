"""Тесты Главного Оркестратора (core.orchestrator).

Проверяют ИСКЛЮЧИТЕЛЬНО логику переходов машины состояний Kanban:
все субагенты (Parser, Planner, Prompter, Evaluator, Publisher) и отправка
на Node 1 замоканы. Оркестратор не содержит while-циклов: один запуск =
один шаг по доске + чистый возврат summary (Clean Context, exit(0)).
"""
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.db import Database
from core.orchestrator import Orchestrator, _build_node1_command
from core.publisher import PublishError
from tests.conftest import IsolatedSettings


class _FakeParser:
    """Парсер-заглушка: отдаёт заданные тренды, считает вызовы."""

    def __init__(self, trends: list[dict[str, object]]) -> None:
        self._trends = trends
        self.calls = 0

    def fetch_trends(self, query: str, limit: int = 10) -> list[dict[str, object]]:
        self.calls += 1
        return self._trends


class _FakePlanner:
    """Сценарист-заглушка: записывает концепцию в БД, считает вызовы."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self.calls: list[str] = []

    def plan_job(self, job_id: str) -> dict[str, str]:
        self.calls.append(job_id)
        concept = f"Виральная концепция для {job_id}"
        self._db.upsert_job(job_id, "planned", concept=concept)
        return {"job_id": job_id, "concept": concept, "status": "planned"}


class _FakePrompter:
    """Промпт-инженер-заглушка: записывает промпты в БД, считает вызовы."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self.calls: list[str] = []

    def create_prompts(self, job_id: str) -> dict[str, str]:
        self.calls.append(job_id)
        video = f"Video prompt for {job_id}"
        photo = f"Photo prompt for {job_id}"
        self._db.upsert_job(job_id, "ready_for_render", video_prompt=video, photo_prompt=photo)
        return {"job_id": job_id, "video_prompt": video, "photo_prompt": photo, "status": "ready_for_render"}


class _FakeEvaluator:
    """Evaluator-заглушка: записывает вызовы и двигает задание в approved."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self.calls: list[str] = []

    def evaluate_job(self, job_id: str) -> dict[str, Any]:
        self.calls.append(job_id)
        self._db.upsert_job(job_id, "approved")
        return {"approved": True}


class _FakePublisher:
    """Publisher-заглушка: отдаёт post_id; падает на файлах с меткой fail_on."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self._fail_on = fail_on

    def publish(self, media_path: str, caption: str) -> str:
        self.calls.append(media_path)
        if self._fail_on and self._fail_on in media_path:
            raise PublishError("CLI down")
        return "post-1"


class _FakeSubmit:
    """Отправка задания на Node 1 (cli-anything-comfyui) — заглушка."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, job_id: str, video_prompt: str = "", photo_prompt: str = "") -> None:
        self.calls.append(job_id)
        self.prompts.append((video_prompt, photo_prompt))


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(str(tmp_path / "ops.db"))
    database.init_schema()
    yield database
    database.close()


@pytest.fixture
def media(tmp_path: Path) -> Path:
    file = tmp_path / "final.png"
    file.write_bytes(b"\x89PNG fake")
    return file


def make_orchestrator(
    db: Database,
    parser: _FakeParser | None = None,
    planner: _FakePlanner | None = None,
    prompter: _FakePrompter | None = None,
    evaluator: _FakeEvaluator | None = None,
    publisher: _FakePublisher | None = None,
    submit: _FakeSubmit | None = None,
) -> Orchestrator:
    """Оркестратор со всеми замоканными субагентами."""
    return Orchestrator(
        db=db,
        parser=parser or _FakeParser([]),
        planner=planner or _FakePlanner(db),
        prompter=prompter or _FakePrompter(db),
        evaluator=evaluator or _FakeEvaluator(db),
        publisher=publisher or _FakePublisher(),
        submit=submit or _FakeSubmit(),
    )


TRENDS = [
    {"title": "T1", "url": "https://ig.example/p/1", "engagement": 10,
     "source": "scrape_creators"},
    {"title": "T2", "url": "https://ig.example/p/2", "engagement": 20,
     "source": "scrape_creators"},
]


# --- Переходы состояний (cron-тик) --------------------------------------------

def test_tick_new_goes_to_planner(db: Database) -> None:
    """new → Planner → planned."""
    db.upsert_job("job-c", "new")
    planner = _FakePlanner(db)
    summary = make_orchestrator(db, planner=planner).run_once()
    assert planner.calls == ["job-c"]
    assert summary["planned"] == 1
    job = db.fetch_job("job-c")
    assert job is not None and job["status"] == "planned"
    assert job["concept"] is not None


def test_tick_planned_goes_to_prompter(db: Database) -> None:
    """planned → Prompter → ready_for_render."""
    db.upsert_job("job-d", "planned", concept="Test concept")
    prompter = _FakePrompter(db)
    summary = make_orchestrator(db, prompter=prompter).run_once()
    assert prompter.calls == ["job-d"]
    assert summary["prompts"] == 1
    job = db.fetch_job("job-d")
    assert job is not None and job["status"] == "ready_for_render"
    assert job["video_prompt"] is not None
    assert job["photo_prompt"] is not None


def test_tick_ready_goes_to_node1(db: Database) -> None:
    """ready_for_render → submit → generating."""
    db.upsert_job("job-e", "ready_for_render")
    submit = _FakeSubmit()
    summary = make_orchestrator(db, submit=submit).run_once()
    assert submit.calls == ["job-e"]
    assert summary["submitted"] == 1
    job = db.fetch_job("job-e")
    assert job is not None and job["status"] == "generating"


def test_submit_forwards_prompts_to_node1(db: Database) -> None:
    """Node 1 получает video/photo промпты из БД (регресс: раньше слали только job_id)."""
    db.upsert_job(
        "job-p", "ready_for_render",
        video_prompt="static portrait, soft light",
        photo_prompt="flat lay, warm tones",
    )
    submit = _FakeSubmit()
    summary = make_orchestrator(db, submit=submit).run_once()
    assert submit.calls == ["job-p"]
    assert submit.prompts == [("static portrait, soft light", "flat lay, warm tones")]
    assert summary["submitted"] == 1


def test_node1_command_includes_prompt_flags() -> None:
    """cli-anything-comfyui получает промпты флагами; regenerate — без них."""
    settings = IsolatedSettings(comfyui_command="cli-anything-comfyui")
    command = _build_node1_command(settings, "submit", "job-1", "VP", "PP")
    assert command == [
        "cli-anything-comfyui", "submit", "job-1",
        "--video-prompt", "VP", "--photo-prompt", "PP",
    ]
    regen = _build_node1_command(settings, "regenerate", "job-1")
    assert "--video-prompt" not in regen


def test_tick_evaluates_done_jobs(db: Database, media: Path) -> None:
    """done → Evaluator → approved."""
    db.upsert_job("job-a", "done", str(media))
    evaluator = _FakeEvaluator(db)
    summary = make_orchestrator(db, evaluator=evaluator).run_once()
    assert evaluator.calls == ["job-a"]
    assert summary["evaluated"] == 1
    job = db.fetch_job("job-a")
    assert job is not None and job["status"] == "approved"


def test_tick_publishes_approved_jobs(db: Database, media: Path) -> None:
    """approved → Publisher → published."""
    db.upsert_job("job-b", "approved", str(media))
    publisher = _FakePublisher()
    summary = make_orchestrator(db, publisher=publisher).run_once()
    assert publisher.calls == [str(media)]
    assert summary["published"] == 1
    job = db.fetch_job("job-b")
    assert job is not None and job["status"] == "published"


def test_tick_creates_jobs_from_trends_when_queue_empty(db: Database) -> None:
    """Пустая доска → Парсер ищет тренды → создаются задания new с метаданными."""
    parser = _FakeParser(TRENDS)
    summary = make_orchestrator(db, parser=parser).run_once()
    assert parser.calls == 1
    assert summary["created"] == 2
    jobs = db.fetch_jobs_by_status("new")
    assert len(jobs) == 2
    assert {j["trend_title"] for j in jobs} == {"T1", "T2"}
    assert {j["trend_url"] for j in jobs} == {
        "https://ig.example/p/1", "https://ig.example/p/2",
    }


def test_tick_full_pipeline_one_pass(db: Database, media: Path) -> None:
    """Один тик двигает ВСЕ стадии разом; Парсер не дёргается."""
    db.upsert_job("job-a", "done", str(media))
    db.upsert_job("job-b", "approved", str(media))
    db.upsert_job("job-c", "new")
    db.upsert_job("job-d", "planned", concept="Concept for D")
    db.upsert_job("job-e", "ready_for_render")
    parser = _FakeParser(TRENDS)
    summary = make_orchestrator(db, parser=parser).run_once()
    assert parser.calls == 0
    assert summary["created"] == 0
    # Проверяем финальные статусы
    job_a = db.fetch_job("job-a")
    job_b = db.fetch_job("job-b")
    job_c = db.fetch_job("job-c")
    job_d = db.fetch_job("job-d")
    job_e = db.fetch_job("job-e")
    assert job_a is not None and job_a["status"] == "approved"
    assert job_b is not None and job_b["status"] == "published"
    assert job_c is not None and job_c["status"] == "planned"
    assert job_d is not None and job_d["status"] == "ready_for_render"
    assert job_e is not None and job_e["status"] == "generating"


# --- Маршрутизация по Webhook ---------------------------------------------------

def test_webhook_trigger_evaluates_single_job(db: Database, media: Path) -> None:
    """Триггер от Node 1: оценивается ТОЛЬКО указанный job, остальное не трогается."""
    db.upsert_job("job-x", "done", str(media))
    db.upsert_job("job-b", "approved", str(media))
    evaluator = _FakeEvaluator(db)
    publisher = _FakePublisher()
    parser = _FakeParser(TRENDS)
    summary = make_orchestrator(
        db, parser=parser, evaluator=evaluator, publisher=publisher,
    ).run_once(trigger_job_id="job-x")
    assert evaluator.calls == ["job-x"]
    assert publisher.calls == []
    assert parser.calls == 0
    assert summary["evaluated"] == 1


# --- Отказоустойчивость ----------------------------------------------------------

def test_tick_continues_on_agent_error(db: Database, media: Path) -> None:
    """Падение одного субагента не роняет тик: остальные задания обрабатываются."""
    db.upsert_job("job-bad", "approved", "/nonexistent/bad.png")
    db.upsert_job("job-good", "approved", str(media))
    publisher = _FakePublisher(fail_on="bad")
    summary = make_orchestrator(db, publisher=publisher).run_once()
    assert summary["errors"] == 1
    assert summary["published"] == 1
    good = db.fetch_job("job-good")
    bad = db.fetch_job("job-bad")
    assert good is not None and good["status"] == "published"
    assert bad is not None and bad["status"] == "approved"  # ждёт следующего тика
