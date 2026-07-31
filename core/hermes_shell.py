"""Минимальный одношаговый harness Telegram для Hermes workspace."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

COMMANDS = {
    "/setup": "Настроить профиль бренда",
    "/plan": "Подготовить недельный план",
    "/new_video": "Создать новое видео",
    "/status": "Показать состояние системы",
    "/cancel": "Отменить задачу",
}


class ShellConfig(BaseSettings):
    """Секреты и runtime-пути оболочки, загружаемые только из окружения."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_admin_id_1: int | None = None
    telegram_admin_id_2: int | None = None
    opencode_zen_api_key_1: SecretStr | None = None
    opencode_zen_api_key_2: SecretStr | None = None
    hermes_state_path: Path | None = None

    @model_validator(mode="after")
    def require_distinct_admins(self) -> ShellConfig:
        if self.telegram_admin_id_1 is None or self.telegram_admin_id_2 is None:
            raise ValueError("Two Telegram administrator IDs are required")
        if self.telegram_admin_id_1 == self.telegram_admin_id_2:
            raise ValueError("Telegram administrator IDs must be distinct")
        return self

    @property
    def admin_ids(self) -> frozenset[int]:
        assert self.telegram_admin_id_1 is not None
        assert self.telegram_admin_id_2 is not None
        return frozenset((self.telegram_admin_id_1, self.telegram_admin_id_2))

    @property
    def active_key_number(self) -> int:
        if self.opencode_zen_api_key_1 is not None:
            return 1
        if self.opencode_zen_api_key_2 is not None:
            return 2
        return 0


class HermesShell:
    """Обрабатывает один event, сохраняет idempotency-state и завершается."""

    def __init__(
        self,
        admin_ids: frozenset[int],
        state_path: Path,
        active_key_number: int = 0,
    ) -> None:
        self._admin_ids = admin_ids
        self._state_path = state_path
        self._active_key_number = active_key_number

    def handle(self, update: dict[str, Any]) -> str | None:
        event = self._event(update)
        if event is None:
            return None
        event_id, user_id, command = event
        if user_id not in self._admin_ids:
            return None

        processed = self._load_processed()
        if event_id in processed:
            return None
        processed.add(event_id)
        self._save_processed(processed)

        if command is None:
            return "Этот сценарий пока не настроен."
        if command == "/status":
            return (
                "Hermes работает.\n"
                f"Активный OpenCode Zen ключ: №{self._active_key_number}.\n"
                "Telegram-оболочка: готова."
            )
        if command in COMMANDS:
            return f"Сценарий {command} пока не настроен."
        return None

    @staticmethod
    def _event(update: dict[str, Any]) -> tuple[str, int, str | None] | None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            sender = callback.get("from")
            callback_id = callback.get("id")
            if isinstance(sender, dict) and callback_id is not None:
                return (
                    f"callback:{callback_id}",
                    int(sender["id"]),
                    None,
                )

        message = update.get("message")
        update_id = update.get("update_id")
        if not isinstance(message, dict) or update_id is None:
            return None
        sender = message.get("from")
        text = message.get("text")
        if not isinstance(sender, dict) or not isinstance(text, str):
            return None
        command = text.strip().split(maxsplit=1)[0].split("@", maxsplit=1)[0]
        return f"update:{update_id}", int(sender["id"]), command

    def _load_processed(self) -> set[str]:
        if not self._state_path.exists():
            return set()
        data = json.loads(self._state_path.read_text(encoding="utf-8"))
        values = data.get("processed_events", [])
        return {str(value) for value in values}

    def _save_processed(self, processed: set[str]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(f"{self._state_path.suffix}.new")
        temporary.write_text(
            json.dumps(
                {"processed_events": sorted(processed)},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._state_path)


def main() -> int:
    """CLI event-runner: один JSON update из stdin — один переход — exit."""
    config = ShellConfig()
    if config.hermes_state_path is None:
        raise ValueError("HERMES_STATE_PATH is required")
    update = json.load(sys.stdin)
    response = HermesShell(
        config.admin_ids,
        config.hermes_state_path,
        config.active_key_number,
    ).handle(update)
    if response is not None:
        print(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
