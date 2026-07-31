"""Контракт защищённой одношаговой Telegram-оболочки Hermes."""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from core.hermes_shell import COMMANDS, HermesShell, ShellConfig, main


def _message(update_id: int, user_id: int, command: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id},
            "text": command,
        },
    }


def _callback(callback_id: str, user_id: int) -> dict[str, object]:
    return {
        "update_id": 99,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "data": "noop",
        },
    }


def test_config_requires_two_distinct_admins_and_two_named_key_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ADMIN_ID_1", "101")
    monkeypatch.setenv("TELEGRAM_ADMIN_ID_2", "202")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY_1", "test-value-1")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY_2", "test-value-2")

    config = ShellConfig()

    assert config.admin_ids == frozenset({101, 202})
    assert config.active_key_number == 1
    assert config.opencode_zen_api_key_1 is not None
    assert config.opencode_zen_api_key_2 is not None


def test_config_rejects_duplicate_admins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ADMIN_ID_1", "101")
    monkeypatch.setenv("TELEGRAM_ADMIN_ID_2", "101")

    with pytest.raises(ValueError, match="distinct"):
        ShellConfig()


def test_config_requires_both_admins_and_selects_available_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="Two Telegram"):
        ShellConfig()

    monkeypatch.setenv("TELEGRAM_ADMIN_ID_1", "101")
    monkeypatch.setenv("TELEGRAM_ADMIN_ID_2", "202")
    assert ShellConfig().active_key_number == 0
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY_2", "test-value-2")
    assert ShellConfig().active_key_number == 2


def test_only_five_russian_commands_are_registered() -> None:
    assert COMMANDS == {
        "/setup": "Настроить профиль бренда",
        "/plan": "Подготовить недельный план",
        "/new_video": "Создать новое видео",
        "/status": "Показать состояние системы",
        "/cancel": "Отменить задачу",
    }


def test_unknown_user_is_silently_ignored(tmp_path: Path) -> None:
    shell = HermesShell(frozenset({101, 202}), tmp_path / "state.json")

    assert shell.handle(_message(1, 303, "/status")) is None
    assert not (tmp_path / "state.json").exists()


def test_status_is_safe_and_other_commands_are_pending(tmp_path: Path) -> None:
    shell = HermesShell(
        frozenset({101, 202}),
        tmp_path / "state.json",
        active_key_number=2,
    )

    status = shell.handle(_message(1, 101, "/status"))
    pending = shell.handle(_message(2, 202, "/plan"))

    assert status == (
        "Hermes работает.\n"
        "Активный OpenCode Zen ключ: №2.\n"
        "Telegram-оболочка: готова."
    )
    assert pending == "Сценарий /plan пока не настроен."
    serialized = json.dumps({"status": status}, ensure_ascii=False).lower()
    for forbidden in ("token", ".env", "secret", "prompt", "test-value-1"):
        assert forbidden not in serialized


def test_update_and_callback_are_idempotent_across_clean_runs(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"

    first_run = HermesShell(frozenset({101, 202}), state_path)
    assert first_run.handle(_message(7, 101, "/setup")) is not None
    assert first_run.handle(_callback("cb-1", 202)) == (
        "Этот сценарий пока не настроен."
    )

    clean_run = HermesShell(frozenset({101, 202}), state_path)
    assert clean_run.handle(_message(7, 101, "/setup")) is None
    assert clean_run.handle(_callback("cb-1", 202)) is None

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["processed_events"] == ["callback:cb-1", "update:7"]


def test_cli_processes_one_event_and_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TELEGRAM_ADMIN_ID_1", "101")
    monkeypatch.setenv("TELEGRAM_ADMIN_ID_2", "202")
    monkeypatch.setenv("HERMES_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(_message(1, 101, "/status"))))

    assert main() == 0
    assert capsys.readouterr().out.startswith("Hermes работает.")
