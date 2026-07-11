"""Тесты Главного Оркестратора (core.orchestrator).

Проверяют ИСКЛЮЧИТЕЛЬНО логику переходов машины состояний Kanban:
все субагенты (Parser, Evaluator, Publisher) и отправка на Node 1 замоканы.
Оркестратор не содержит while-циклов: один запуск = один шаг по доске +
чистый возврат summary (Clean Context, exit(0)).
"""
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.db import Database
from core.orchestrator import Orchestrator
from core.publisher import PublishError


class _FakeParser:
    """Парсер-заглушка: отдаёт заданные тренды, считает вызовы."""

    def __init__(self, trends: list[dict[str, object]]) -> None:
        self._trends = trends
        self.calls = 0

    def fetch_trends(self, query: str, limit: int = 10) -> list[dict[str, object]]:
        self.calls += 1
        return self._trends


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

    def __call__(self, job_id: str) -> None:
        self.calls.append(job_id)


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
    evaluator: _FakeEvaluator | None = None,
    publisher: _FakePublisher | None = None,
    submit: _FakeSubmit | None = None,
) -> Orchestrator:
    """Оркестратор со всеми замоканными субагентами."""
    return Orchestrator(
        db=db,
        parser=parser or _FakeParser([]),
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


def test_tick_submits_new_jobs_to_node1(db: Database) -> None:
    """new → отправка на Node 1 (ComfyUI) → generating."""
    db.upsert_job("job-c", "new")
    submit = _FakeSubmit()
    summary = make_orchestrator(db, submit=submit).run_once()
    assert submit.calls == ["job-c"]
    assert summary["submitted"] == 1
    job = db.fetch_job("job-c")
    assert job is not None and job["status"] == "generating"


def test_tick_creates_jobs_from_trends_when_queue_empty(db: Database) -> None:
    """Пустая доска → Парсер ищет тренды → создаются задания new."""
    parser = _FakeParser(TRENDS)
    summary = make_orchestrator(db, parser=parser).run_once()
    assert parser.calls == 1
    assert summary["created"] == 2
    assert len(db.fetch_jobs_by_status("new")) == 2


def test_tick_moves_full_pipeline_in_one_pass(db: Database, media: Path) -> None:
    """Один тик двигает ВСЕ стадии разом и не застревает; Парсер не дёргается."""
    db.upsert_job("job-a", "done", str(media))
    db.upsert_job("job-b", "approved", str(media))
    db.upsert_job("job-c", "new")
    parser = _FakeParser(TRENDS)
    summary = make_orchestrator(db, parser=parser).run_once()
    assert summary == {
        "evaluated": 1, "published": 1, "submitted": 1, "created": 0, "errors": 0,
    }
    assert parser.calls == 0
    statuses = {
        row["job_id"]: row["status"]
        for row in db.fetch_jobs_by_status("approved")
        + db.fetch_jobs_by_status("published")
        + db.fetch_jobs_by_status("generating")
    }
    assert statuses == {"job-a": "approved", "job-b": "published",
                        "job-c": "generating"}


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
