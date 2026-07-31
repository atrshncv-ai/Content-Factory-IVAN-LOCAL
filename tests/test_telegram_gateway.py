"""Real Telegram transport contract for the one-shot Hermes shell."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from core.hermes_shell import COMMANDS, HermesShell
from core.telegram_gateway import TelegramPollTick, register_menu
from core.telegram_mvp import TelegramHTTP


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps({"ok": True, "result": self._payload}).encode()


class FakeTelegram:
    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self.pending = updates
        self.offsets: list[int] = []
        self.messages: list[tuple[int, str, list[list[dict[str, str]]] | None]] = []
        self.answers: list[str] = []
        self.commands: list[dict[str, str]] = []

    def set_commands(self, commands: list[dict[str, str]]) -> None:
        self.commands = commands

    def updates(self, offset: int = 0) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        return [item for item in self.pending if int(item["update_id"]) >= offset]

    def send_message(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        self.messages.append((chat_id, text, keyboard))

    def answer_callback(self, callback_id: str) -> None:
        self.answers.append(callback_id)


def _message(update_id: int, user_id: int, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id},
            "text": text,
        },
    }


def test_register_menu_sends_exact_five_commands_without_slashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def open_url(url: str, timeout: int) -> _Response:
        calls.append(url)
        return _Response(True)

    monkeypatch.setattr("urllib.request.urlopen", open_url)

    register_menu(TelegramHTTP("not-a-real-secret"))

    assert len(calls) == 1
    parsed = urllib.parse.urlparse(calls[0])
    assert parsed.path.endswith("/setMyCommands")
    payload = json.loads(urllib.parse.parse_qs(parsed.query)["commands"][0])
    assert payload == [
        {"command": name.removeprefix("/"), "description": description}
        for name, description in COMMANDS.items()
    ]
    assert [item["command"] for item in payload] == [
        "setup",
        "plan",
        "new_video",
        "status",
        "cancel",
    ]


def test_tick_routes_one_update_to_shell_and_persists_offset(tmp_path: Path) -> None:
    telegram = FakeTelegram(
        [_message(7, 101, "/status"), _message(8, 101, "/setup")]
    )
    tick = TelegramPollTick(
        telegram,
        HermesShell(frozenset({101, 202}), tmp_path / "state.json"),
        tmp_path / "offset",
    )

    assert tick.run_once() == "sent"
    assert len(telegram.messages) == 1
    assert telegram.messages[0][1].startswith("Hermes работает.")
    assert (tmp_path / "offset").read_text() == "8\n"

    clean_tick = TelegramPollTick(
        telegram,
        HermesShell(frozenset({101, 202}), tmp_path / "state.json"),
        tmp_path / "offset",
    )
    assert clean_tick.run_once() == "sent"
    assert telegram.offsets == [0, 8]
    assert len(telegram.messages) == 2


def test_bootstrap_advances_past_existing_history_without_replies(
    tmp_path: Path,
) -> None:
    telegram = FakeTelegram(
        [_message(7, 101, "/status"), _message(11, 101, "/setup")]
    )
    tick = TelegramPollTick(
        telegram,
        HermesShell(frozenset({101}), tmp_path / "state.json"),
        tmp_path / "offset",
    )

    assert tick.bootstrap_offset() == 12
    assert (tmp_path / "offset").read_text() == "12\n"
    assert telegram.messages == []
    assert telegram.answers == []
    assert not (tmp_path / "state.json").exists()

    telegram.pending.append(_message(12, 101, "/status"))
    assert tick.run_once() == "sent"
    assert telegram.offsets == [-1, 12]
    assert len(telegram.messages) == 1


def test_setup_summary_gets_real_inline_edit_buttons(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "brand_profile": {
                    "brand": "Иван",
                    "niche": "AI",
                    "audience": "Бизнес",
                    "products": "",
                    "geography": "",
                    "tone": "Эксперт",
                    "goals": "",
                    "excluded_topics": "",
                    "timezone": "Europe/Samara",
                    "revision": 4,
                }
            }
        )
    )
    telegram = FakeTelegram([_message(1, 101, "/setup")])

    TelegramPollTick(
        telegram,
        HermesShell(frozenset({101, 202}), state_path),
        tmp_path / "offset",
    ).run_once()

    keyboard = telegram.messages[0][2]
    assert keyboard is not None
    callbacks = {
        button["callback_data"] for row in keyboard for button in row
    }
    assert "setup:edit:tone:4" in callbacks
    assert all(row and len(row) <= 2 for row in keyboard)


def test_plan_and_task_states_get_callback_keyboards(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    shell = HermesShell(frozenset({101, 202}), state_path)
    state_path.write_text(
        json.dumps(
            {
                "weekly_plan": {
                    "phase": "review",
                    "revision": 3,
                }
            }
        )
    )
    review = shell.keyboard("Карточка 1 из 3")
    assert review == [[
        {"text": "Принять", "callback_data": "plan:accept:3"},
        {"text": "Заменить", "callback_data": "plan:replace:3"},
        {"text": "Изменить", "callback_data": "plan:edit:3"},
    ]]

    state_path.write_text(
        json.dumps({"weekly_plan": {"phase": "final", "revision": 8}})
    )
    final = shell.keyboard("Итоговый план готов")
    assert final == [
        [{"text": "Утвердить — автоматически", "callback_data": "plan:approve:auto:8"}],
        [{"text": "Утвердить — с проверкой", "callback_data": "plan:approve:manual:8"}],
    ]

    assert HermesShell.keyboard_for_task("V-104", "awaiting_approval") == [[
        {"text": "Утвердить и запустить", "callback_data": "task:approve:V-104"},
        {"text": "Отменить", "callback_data": "task:cancel:V-104"},
    ]]
    assert HermesShell.keyboard_for_task("V-104", "result_ready") == [
        [
            {"text": "Принять", "callback_data": "task:result:accept:V-104"},
            {
                "text": "Перегенерировать",
                "callback_data": "task:result:regenerate:V-104",
            },
        ],
        [
            {"text": "Доработать", "callback_data": "task:result:revise:V-104"},
            {"text": "Отменить", "callback_data": "task:result:cancel:V-104"},
        ],
    ]


def test_callback_is_acknowledged_and_duplicate_or_unknown_user_is_silent(
    tmp_path: Path,
) -> None:
    callback = {
        "update_id": 4,
        "callback_query": {
            "id": "cb-4",
            "from": {"id": 303},
            "message": {"chat": {"id": 303}},
            "data": "noop",
        },
    }
    telegram = FakeTelegram([callback])
    state_path = tmp_path / "state.json"
    offset_path = tmp_path / "offset"

    TelegramPollTick(
        telegram,
        HermesShell(frozenset({101, 202}), state_path),
        offset_path,
    ).run_once()

    assert telegram.answers == ["cb-4"]
    assert telegram.messages == []
    assert not state_path.exists()

    offset_path.write_text("4\n")
    callback_query = callback["callback_query"]
    assert isinstance(callback_query, dict)
    allowed: dict[str, Any] = dict(callback)
    allowed["callback_query"] = {
        **callback_query,
        "from": {"id": 101},
        "message": {"chat": {"id": 101}},
    }
    telegram.pending = [allowed]
    TelegramPollTick(
        telegram,
        HermesShell(frozenset({101, 202}), state_path),
        offset_path,
    ).run_once()
    TelegramPollTick(
        telegram,
        HermesShell(frozenset({101, 202}), state_path),
        tmp_path / "replayed-offset",
    ).run_once()

    assert telegram.answers[-2:] == ["cb-4", "cb-4"]
    assert len(telegram.messages) == 1


def test_gateway_output_never_contains_bot_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "super-secret-bot-token"
    sent_urls: list[str] = []

    def open_url(url: str, timeout: int) -> _Response:
        sent_urls.append(url)
        if url.endswith("/getUpdates?timeout=0&offset=0&allowed_updates=%5B%22message%22%2C+%22callback_query%22%5D"):
            return _Response([_message(1, 101, "/status")])
        return _Response(True)

    monkeypatch.setattr("urllib.request.urlopen", open_url)
    http = TelegramHTTP(secret)
    tick = TelegramPollTick(
        http,
        HermesShell(frozenset({101, 202}), tmp_path / "state.json"),
        tmp_path / "offset",
    )

    assert tick.run_once() == "sent"
    assert secret not in repr(tick)
    assert secret not in json.dumps(tick.last_result, ensure_ascii=False)
