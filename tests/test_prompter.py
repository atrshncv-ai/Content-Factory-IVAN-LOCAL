"""Тесты субагента «Промпт-инженер» (core.prompter).

Покрывают генерацию промптов, Gate-валидацию (пустые поля от LLM → задача
остаётся в planned до следующего тика) и жёсткое бизнес-ограничение
«только статичные фото» в системном промпте.
"""
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.db import Database
from core.prompter import PromptEngineer


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


@pytest.fixture
def planned_job(db: Database) -> str:
    """Задание, готовое к промптингу (planned + concept)."""
    db.upsert_job("job-1", "planned", concept="Виральная концепция о кофе")
    return "job-1"


VALID = '{"video_prompt": "static portrait, soft light", "photo_prompt": "flat lay, warm tones"}'
EMPTY_PHOTO = '{"video_prompt": "static portrait", "photo_prompt": ""}'
EMPTY_VIDEO = '{"video_prompt": "  ", "photo_prompt": "flat lay"}'


def make_prompter(db: Database, llm: _FakeLLM) -> PromptEngineer:
    return PromptEngineer(llm, db, niche="ai")


# --- Happy path -----------------------------------------------------------------

def test_create_prompts_success(db: Database, planned_job: str) -> None:
    """Валидный JSON → оба промпта в БД, статус ready_for_render."""
    llm = _FakeLLM([VALID])
    result = make_prompter(db, llm).create_prompts(planned_job)
    assert result["status"] == "ready_for_render"
    job = db.fetch_job(planned_job)
    assert job is not None
    assert job["status"] == "ready_for_render"
    assert job["video_prompt"] == "static portrait, soft light"
    assert job["photo_prompt"] == "flat lay, warm tones"


# --- Gate Pattern: валидация непустых промптов -----------------------------------

def test_gate_rejects_empty_photo_prompt(db: Database, planned_job: str) -> None:
    """Пустой photo_prompt → ValueError, статус остаётся planned, ERROR в логе."""
    llm = _FakeLLM([EMPTY_PHOTO])
    with pytest.raises(ValueError):
        make_prompter(db, llm).create_prompts(planned_job)
    job = db.fetch_job(planned_job)
    assert job is not None and job["status"] == "planned"
    assert any(
        log["level"] == "ERROR" and log["agent"] == "prompter"
        for log in db.fetch_logs()
    )


def test_gate_rejects_empty_video_prompt(db: Database, planned_job: str) -> None:
    """Пустой/пробельный video_prompt → ValueError, статус остаётся planned."""
    llm = _FakeLLM([EMPTY_VIDEO])
    with pytest.raises(ValueError):
        make_prompter(db, llm).create_prompts(planned_job)
    job = db.fetch_job(planned_job)
    assert job is not None and job["status"] == "planned"


def test_gate_rejects_garbage_json(db: Database, planned_job: str) -> None:
    """Мусорный ответ модели → брак (ValueError), а не мусорный промпт в ComfyUI."""
    llm = _FakeLLM(["<not json at all>"])
    with pytest.raises(ValueError):
        make_prompter(db, llm).create_prompts(planned_job)
    job = db.fetch_job(planned_job)
    assert job is not None and job["status"] == "planned"


# --- Бизнес-ограничение: только статичные фото -----------------------------------

def test_prompt_enforces_static_only(db: Database, planned_job: str) -> None:
    """Системный промпт содержит жёсткий запрет терминов видео/движения."""
    llm = _FakeLLM([VALID])
    make_prompter(db, llm).create_prompts(planned_job)
    text = llm.messages[0][0]["content"]
    assert "STATIC" in text
    assert "FORBIDDEN" in text


# --- Статусные гварды -------------------------------------------------------------

def test_requires_planned_status(db: Database) -> None:
    """Задание не в planned → ValueError (LLM не вызывается)."""
    db.upsert_job("job-2", "new")
    llm = _FakeLLM([VALID])
    with pytest.raises(ValueError):
        make_prompter(db, llm).create_prompts("job-2")
    assert llm.calls == 0


def test_requires_concept(db: Database) -> None:
    """Planned без концепции → ValueError (LLM не вызывается)."""
    db.upsert_job("job-3", "planned")
    llm = _FakeLLM([VALID])
    with pytest.raises(ValueError):
        make_prompter(db, llm).create_prompts("job-3")
    assert llm.calls == 0
