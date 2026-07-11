"""Тесты субагента «SMM-Паблишер» (core.publisher).

Покрывают публикацию через внешнюю CLI-утилиту автопостинга и сбор
многоуровневых метрик (1d/1w/1m) в SQLite. subprocess замокан на шве
_run_command — реальная CLI не вызывается.
"""
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.db import Database
from core.publisher import PublishError, Publisher
from tests.conftest import IsolatedSettings


class _FakeRunner:
    """Подмена _run_command: отдаёт stdout или падает, записывает команды."""

    def __init__(self, stdout: str = "", error: Exception | None = None) -> None:
        self._stdout = stdout
        self._error = error
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> str:
        self.calls.append(command)
        if self._error:
            raise self._error
        return self._stdout


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(str(tmp_path / "ops.db"))
    database.init_schema()
    yield database
    database.close()


@pytest.fixture
def media(tmp_path: Path) -> Path:
    """«Отрендеренный» файл поста."""
    file = tmp_path / "final.png"
    file.write_bytes(b"\x89PNG fake")
    return file


def make_publisher(
    monkeypatch: pytest.MonkeyPatch, db: Database, runner: _FakeRunner
) -> Publisher:
    """Publisher с подменённым _run_command."""
    monkeypatch.setattr("core.publisher._run_command", runner)
    return Publisher(IsolatedSettings(), db)


# --- Публикация ----------------------------------------------------------------

def test_publish_success_returns_post_id(
    monkeypatch: pytest.MonkeyPatch, db: Database, media: Path,
) -> None:
    """Успешная публикация: CLI отдал post_id, команда содержит файл и текст."""
    runner = _FakeRunner(json.dumps({"post_id": "ig_123", "url": "https://ig/p/ig_123"}))
    publisher = make_publisher(monkeypatch, db, runner)

    post_id = publisher.publish(str(media), "Новый пост #ai")

    assert post_id == "ig_123"
    command = runner.calls[0]
    assert "autopost" in command[0]
    assert str(media) in command
    assert "Новый пост #ai" in command
    assert any(log["agent"] == "publisher" for log in db.fetch_logs())


def test_publish_cli_failure_raises_publish_error(
    monkeypatch: pytest.MonkeyPatch, db: Database, media: Path,
) -> None:
    """CLI упала (non-zero exit) → кастомная PublishError."""
    runner = _FakeRunner(error=subprocess.CalledProcessError(1, ["autopost"], stderr="boom"))
    publisher = make_publisher(monkeypatch, db, runner)
    with pytest.raises(PublishError):
        publisher.publish(str(media), "x")


def test_publish_timeout_raises_publish_error(
    monkeypatch: pytest.MonkeyPatch, db: Database, media: Path,
) -> None:
    """Таймаут CLI → PublishError (ретрай — на следующем cron-тике, не здесь)."""
    runner = _FakeRunner(error=subprocess.TimeoutExpired(["autopost"], 120))
    publisher = make_publisher(monkeypatch, db, runner)
    with pytest.raises(PublishError):
        publisher.publish(str(media), "x")


# --- Метрики -------------------------------------------------------------------

@pytest.mark.parametrize("period", ["1d", "1w", "1m"])
def test_collect_metrics_stores_each_period(
    monkeypatch: pytest.MonkeyPatch, db: Database, period: str,
) -> None:
    """Метрики за каждый период парсятся из CLI и пишутся в таблицу metrics."""
    runner = _FakeRunner(json.dumps({"likes": 42, "comments": 7, "reach": 1500}))
    publisher = make_publisher(monkeypatch, db, runner)

    publisher.collect_metrics("ig_123", period)

    metrics = db.fetch_metrics("ig_123")
    assert len(metrics) == 1
    assert metrics[0]["period"] == period
    assert metrics[0]["likes"] == 42
    assert metrics[0]["comments"] == 7
    assert metrics[0]["reach"] == 1500


def test_collect_metrics_cli_failure_raises(
    monkeypatch: pytest.MonkeyPatch, db: Database,
) -> None:
    """Сбой CLI при сборе метрик → PublishError, в БД ничего не пишется."""
    runner = _FakeRunner(error=subprocess.CalledProcessError(2, ["autopost"]))
    publisher = make_publisher(monkeypatch, db, runner)
    with pytest.raises(PublishError):
        publisher.collect_metrics("ig_123", "1d")
    assert db.fetch_metrics("ig_123") == []


def test_collect_metrics_rejects_invalid_period(
    monkeypatch: pytest.MonkeyPatch, db: Database,
) -> None:
    """Период вне 1d/1w/1m отклоняется ДО вызова CLI (CHECK в БД — 2-я линия)."""
    runner = _FakeRunner(json.dumps({"likes": 0, "comments": 0, "reach": 0}))
    publisher = make_publisher(monkeypatch, db, runner)
    with pytest.raises(ValueError):
        publisher.collect_metrics("ig_123", "2x")
    assert runner.calls == []
