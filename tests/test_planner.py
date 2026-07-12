"""Тесты субагента «Сценарист» (core.planner).

Покрывают генерацию концепции, жёсткое бизнес-ограничение «только
статичные фото» в системном промпте и статусные гварды.
"""
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.db import Database
from core.planner import ContentPlanner


class _FakeLLM:
    """LLM-заглушка: отдаёт ответы из очереди, записывает полученные сообщения."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls = 0
        self.messages: list = []

    def chat(self, messages: list) -> str:
        self.calls += 1
        self.messages.append(messages)
        return self._responses.pop(0)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(str(tmp_path / "ops.db"))
    database.init_schema()
    yield database
    database.close()


def test_plan_job_success(db: Database) -> None:
    """Валидный JSON → концепция в БД, статус planned."""
    db.upsert_job("job-1", "new")
    llm = _FakeLLM(['{"concept": "Кофе на рассвете, уютный минимализм"}'])
    result = ContentPlanner(llm, db, niche="ai").plan_job("job-1")
    assert result["status"] == "planned"
    job = db.fetch_job("job-1")
    assert job is not None
    assert job["status"] == "planned"
    assert job["concept"] == "Кофе на рассвете, уютный минимализм"


def test_prompt_enforces_static_only(db: Database) -> None:
    """Системный промпт содержит жёсткий запрет терминов видео/движения."""
    db.upsert_job("job-1", "new")
    llm = _FakeLLM(['{"concept": "x"}'])
    ContentPlanner(llm, db).plan_job("job-1")
    text = llm.messages[0][0]["content"]
    assert "СТАТИЧНЫЕ ФОТО" in text
    assert "Запрещено" in text


def test_requires_new_status(db: Database) -> None:
    """Задание не в new → ValueError (LLM не вызывается)."""
    db.upsert_job("job-1", "planned")
    llm = _FakeLLM([])
    with pytest.raises(ValueError):
        ContentPlanner(llm, db).plan_job("job-1")
    assert llm.calls == 0


def test_unknown_job_raises(db: Database) -> None:
    """Несуществующее задание → ValueError."""
    with pytest.raises(ValueError):
        ContentPlanner(_FakeLLM([]), db).plan_job("nope")
