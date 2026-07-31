"""Одноразовый Telegram transport для HermesShell — без вечного poll-loop."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Protocol

from core.hermes_shell import COMMANDS, HermesShell

Keyboard = list[list[dict[str, str]]]


class TelegramTransport(Protocol):
    def updates(self, offset: int = 0) -> list[dict[str, Any]]: ...

    def send_message(
        self, chat_id: int, text: str, keyboard: Keyboard | None = None
    ) -> None: ...

    def answer_callback(self, callback_id: str) -> None: ...

    def set_commands(self, commands: list[dict[str, str]]) -> None: ...


def register_menu(telegram: TelegramTransport) -> None:
    """Регистрирует ровно пять команд Telegram; вызывается вручную один раз."""
    telegram.set_commands(
        [
            {
                "command": command.removeprefix("/"),
                "description": description,
            }
            for command, description in COMMANDS.items()
        ]
    )


class TelegramPollTick:
    """Получает и завершает ровно один Telegram event за cron-запуск."""

    def __init__(
        self,
        telegram: TelegramTransport,
        shell: HermesShell,
        offset_path: Path,
    ) -> None:
        self._telegram = telegram
        self._shell = shell
        self._offset_path = offset_path
        self.last_result: dict[str, object] = {"stage": "idle"}

    def run_once(self) -> str:
        updates = self._telegram.updates(self._offset())
        if not updates:
            self.last_result = {"stage": "idle"}
            return "idle"
        update = min(updates, key=lambda item: int(item["update_id"]))
        callback = update.get("callback_query")
        if isinstance(callback, dict) and callback.get("id") is not None:
            self._telegram.answer_callback(str(callback["id"]))
        response = self._shell.handle(update)
        self._save_offset(int(update["update_id"]) + 1)
        if response is None:
            self.last_result = {"stage": "ignored"}
            return "ignored"
        chat_id = self._chat_id(update)
        if chat_id is None:
            self.last_result = {"stage": "ignored"}
            return "ignored"
        keyboard = self._shell.keyboard(response)
        self._telegram.send_message(chat_id, response, keyboard or None)
        self.last_result = {"stage": "sent", "chat_id": chat_id}
        return "sent"

    def bootstrap_offset(self) -> int:
        """Пропускает накопленную историю, не передавая её в shell."""
        updates = self._telegram.updates(-1)
        offset = max(
            (int(update["update_id"]) + 1 for update in updates),
            default=0,
        )
        self._save_offset(offset)
        self.last_result = {"stage": "bootstrapped", "offset": offset}
        return offset

    def _offset(self) -> int:
        try:
            return int(self._offset_path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return 0

    def _save_offset(self, offset: int) -> None:
        self._offset_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._offset_path.with_suffix(f"{self._offset_path.suffix}.new")
        temporary.write_text(f"{offset}\n", encoding="utf-8")
        temporary.replace(self._offset_path)

    @staticmethod
    def _chat_id(update: dict[str, Any]) -> int | None:
        message = update.get("message")
        if not isinstance(message, dict):
            callback = update.get("callback_query")
            if isinstance(callback, dict):
                message = callback.get("message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        return int(chat["id"]) if isinstance(chat, dict) and "id" in chat else None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(offset_path={self._offset_path!s})"


def main(argv: list[str] | None = None) -> int:
    """One-shot CLI: register-menu либо один poll tick."""
    from core.hermes_shell import ShellConfig
    from core.telegram_mvp import TelegramHTTP

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("register-menu", "bootstrap-offset", "tick"),
    )
    args = parser.parse_args(argv)
    config = ShellConfig()
    if config.telegram_bot_token is None:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    telegram = TelegramHTTP(config.telegram_bot_token.get_secret_value())
    if args.action == "register-menu":
        register_menu(telegram)
        return 0
    if config.hermes_state_path is None:
        raise ValueError("HERMES_STATE_PATH is required")
    offset_path = config.hermes_state_path.with_name(
        f"{config.hermes_state_path.name}.telegram-offset"
    )
    poll_tick = TelegramPollTick(
        telegram,
        HermesShell(
            config.admin_ids,
            config.hermes_state_path,
            config.active_key_number,
            config.hermes_knowledge_path,
        ),
        offset_path,
    )
    if args.action == "bootstrap-offset":
        print(poll_tick.bootstrap_offset())
        return 0
    result = poll_tick.run_once()
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
