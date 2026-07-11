"""Тесты субагента «Контролёр Качества» (core.evaluator).

Покрывают микро-Ralph Loop: Vision-аппрув с первого раза, аппрув после
браковок (перегенерация) и жёсткий выход по MAX_RETRIES (Safety Valve).
Vision API замокан фейком — сеть и реальные модели не используются.
"""
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.db import Database
from core.evaluator import QualityEvaluator


class _FakeLLM:
    """Подмена Vision-модели: отдаёт ответы из очереди, считает вызовы.

    Когда очередь пуста — вечно бракует (удобно для теста MAX_RETRIES).
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls = 0

    def chat(self, messages: list) -> str:
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return '{"approved": false, "reason": "очередь ответов пуста"}'


class _FakeRegenerate:
    """Подмена вызова перегенерации на Node 1: записывает job_id вызовов."""

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
def job(db: Database, tmp_path: Path) -> str:
    """Готовое к оценке задание: файл в Shared Folder + статус done в БД."""
    image = tmp_path / "job-1.png"
    image.write_bytes(b"\x89PNG\r\n fake image bytes")
    db.upsert_job("job-1", "done", str(image))
    return "job-1"


APPROVED = '{"approved": true, "reason": "артефактов нет"}'
REJECTED = '{"approved": false, "reason": "лишний палец на левой кисти"}'


def test_approved_on_first_attempt(db: Database, job: str) -> None:
    """Чистое изображение аппрувится с первого раза, перегенерации нет."""
    llm = _FakeLLM(['```json\n{"approved": true, "reason": "чисто"}\n```'])
    regen = _FakeRegenerate()
    evaluator = QualityEvaluator(llm, db, regen, max_retries=3)

    result = evaluator.evaluate_job(job)

    assert result["approved"] is True
    assert llm.calls == 1
    assert regen.calls == []
    job_row = db.fetch_job(job)
    assert job_row is not None
    assert job_row["status"] == "approved"


def test_approved_after_two_rejections(db: Database, job: str) -> None:
    """Брак → перегенерация → брак → перегенерация → аппрув (цикл работает)."""
    llm = _FakeLLM([REJECTED, REJECTED, APPROVED])
    regen = _FakeRegenerate()
    evaluator = QualityEvaluator(llm, db, regen, max_retries=3)

    result = evaluator.evaluate_job(job)

    assert result["approved"] is True
    assert llm.calls == 3
    assert regen.calls == [job, job]
    job_row = db.fetch_job(job)
    assert job_row is not None
    assert job_row["status"] == "approved"


def test_max_retries_exhausted_marks_failed(db: Database, job: str) -> None:
    """Safety Valve: постоянный брак → выход по MAX_RETRIES, статус failed."""
    llm = _FakeLLM([REJECTED])  # далее вечный брак из заглушки
    regen = _FakeRegenerate()
    evaluator = QualityEvaluator(llm, db, regen, max_retries=3)

    result = evaluator.evaluate_job(job)

    assert result["approved"] is False
    assert llm.calls == 4          # 1 оценка + 3 перегенерации
    assert len(regen.calls) == 3   # предохранитель сработал ровно на лимите
    job_row = db.fetch_job(job)
    assert job_row is not None
    assert job_row["status"] == "failed"
    assert any(log["agent"] == "evaluator" for log in db.fetch_logs())


def test_invalid_model_json_counts_as_rejection(db: Database, job: str) -> None:
    """Мусорный ответ модели = брак (судья не падает, цикл продолжается)."""
    llm = _FakeLLM(["<not json at all>", APPROVED])
    regen = _FakeRegenerate()
    evaluator = QualityEvaluator(llm, db, regen, max_retries=3)

    result = evaluator.evaluate_job(job)

    assert result["approved"] is True
    assert llm.calls == 2
    assert len(regen.calls) == 1
