"""Тесты активного поллера результатов ComfyUI (core.poller).

Поллер ходит в cli-anything-comfyui (syscall) — он замокан. Проверяем:
парсинг истории ComfyUI, переход generating → done/error, устойчивость
к падению CLI и отсутствие лишних вызовов при пустой доске.
"""
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.db import Database
from core.poller import build_history_command, find_in_history, poll_once
from tests.conftest import IsolatedSettings


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    """Изолированная операционная БД."""
    database = Database(str(tmp_path / "ops.db"))
    database.init_schema()
    yield database
    database.close()


def _history(*, status: str = "success", filename: str = "LTX_Output_j1_00001_.mp4") -> dict:
    """Снимок истории ComfyUI в формате {prompt_id: entry}."""
    return {
        "prompt-id-1": {
            "prompt": {},
            "outputs": {
                "5294": {
                    "gifs": [
                        {"filename": filename, "subfolder": "", "type": "output"},
                    ],
                },
            },
            "status": {"status_str": status, "completed": True, "messages": []},
        },
    }


def _mock_cli(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    """Подменяет syscall cli-anything-comfyui заданным JSON-payload."""
    monkeypatch.setattr(
        "core.poller._run_command", lambda cmd: json.dumps(payload),
    )


SETTINGS = IsolatedSettings(
    comfyui_command="cli-anything-comfyui",
    comfyui_server_url="http://100.102.155.86:8188",
)


# --- Команда -------------------------------------------------------------------

def test_history_command_shape() -> None:
    """Контракт CLI: --url <url> --json queue history (глобальные флаги первыми)."""
    command = build_history_command(SETTINGS)
    assert command == [
        "cli-anything-comfyui", "--url", "http://100.102.155.86:8188",
        "--json", "queue", "history",
    ]


# --- Парсинг истории -----------------------------------------------------------

def test_find_in_history_success_with_filename() -> None:
    """Запись с success и outputs → статус и имя файла с job_id."""
    found = find_in_history(_history(), "j1")
    assert found is not None
    assert found["status"] == "success"
    assert found["filename"] == "LTX_Output_j1_00001_.mp4"


def test_find_in_history_missing_job() -> None:
    """job_id отсутствует в истории → None (задача ещё в работе)."""
    assert find_in_history(_history(), "unknown-job") is None


def test_find_in_history_list_format() -> None:
    """История в виде list[entry] тоже парсится."""
    found = find_in_history(list(_history().values()), "j1")
    assert found is not None and found["status"] == "success"


# --- poll_once -----------------------------------------------------------------

def test_poll_marks_done_and_records_filename(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generating + success в истории → done + file_path из outputs."""
    db.upsert_job("j1", "generating")
    _mock_cli(monkeypatch, _history())
    summary = poll_once(db, SETTINGS)
    assert summary == {"checked": 1, "done": 1, "errors": 0}
    job = db.fetch_job("j1")
    assert job is not None
    assert job["status"] == "done"
    assert job["file_path"] == "LTX_Output_j1_00001_.mp4"


def test_poll_keeps_generating_when_not_in_history(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Задачи нет в истории → статус остаётся generating."""
    db.upsert_job("j2", "generating")
    _mock_cli(monkeypatch, _history())
    summary = poll_once(db, SETTINGS)
    assert summary["done"] == 0
    job = db.fetch_job("j2")
    assert job is not None and job["status"] == "generating"


def test_poll_marks_error_on_failed_comfyui(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status=error в истории → задание уходит в error, а не висит в generating."""
    db.upsert_job("j1", "generating")
    _mock_cli(monkeypatch, _history(status="error"))
    summary = poll_once(db, SETTINGS)
    assert summary["errors"] == 1
    job = db.fetch_job("j1")
    assert job is not None and job["status"] == "error"


def test_poll_survives_cli_failure(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Падение CLI → errors += 1, задание не трогаем, в лог запись."""
    db.upsert_job("j3", "generating")

    def boom(cmd: list[str]) -> str:
        raise RuntimeError("comfyui unreachable")

    monkeypatch.setattr("core.poller._run_command", boom)
    summary = poll_once(db, SETTINGS)
    assert summary["errors"] == 1
    job = db.fetch_job("j3")
    assert job is not None and job["status"] == "generating"
    assert any("history недоступна" in log["message"] for log in db.fetch_logs())


def test_poll_skips_cli_when_no_generating_jobs(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пустая доска (нет generating) → CLI вообще не дёргается."""
    calls: list[list[str]] = []

    def fake_cli(cmd: list[str]) -> str:
        calls.append(cmd)
        return "[]"

    monkeypatch.setattr("core.poller._run_command", fake_cli)
    summary = poll_once(db, SETTINGS)
    assert summary == {"checked": 0, "done": 0, "errors": 0}
    assert calls == []
