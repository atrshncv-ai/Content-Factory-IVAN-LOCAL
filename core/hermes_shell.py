"""Минимальный одношаговый harness Telegram для Hermes workspace."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from core.task_control import TaskQueue
    from core.weekly_plan import ScheduledPlan, WeeklyPlanController

COMMANDS = {
    "/setup": "Настроить профиль бренда",
    "/plan": "Подготовить недельный план",
    "/new_video": "Создать новое видео",
    "/status": "Показать состояние системы",
    "/cancel": "Отменить задачу",
}

PROFILE_FIELDS = (
    ("brand", "бренд", True),
    ("niche", "нишу", True),
    ("audience", "аудиторию", True),
    ("products", "продукты", False),
    ("geography", "географию", False),
    ("tone", "тон", True),
    ("goals", "цели", False),
    ("excluded_topics", "нежелательные темы", False),
    ("timezone", "часовой пояс", True),
)
PROFILE_LABELS = {
    key: label.removesuffix("ю").capitalize()
    for key, label, _required in PROFILE_FIELDS
}
PROFILE_LABELS.update(
    {
        "brand": "Бренд",
        "niche": "Ниша",
        "audience": "Аудитория",
        "geography": "География",
        "tone": "Тон",
        "goals": "Цели",
        "excluded_topics": "Нежелательные темы",
        "timezone": "Часовой пояс",
    }
)


class ShellConfig(BaseSettings):
    """Секреты и runtime-пути оболочки, загружаемые только из окружения."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_admin_id_1: int | None = None
    telegram_admin_id_2: int | None = None
    opencode_zen_api_key_1: SecretStr | None = None
    opencode_zen_api_key_2: SecretStr | None = None
    hermes_state_path: Path | None = None
    hermes_knowledge_path: Path | None = None

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
        knowledge_path: Path | None = None,
        task_queue: TaskQueue | None = None,
        weekly_plan: WeeklyPlanController | None = None,
        scheduled_plan: ScheduledPlan | None = None,
        status_provider: Callable[[], str] | None = None,
    ) -> None:
        self._admin_ids = admin_ids
        self._state_path = state_path
        self._active_key_number = active_key_number
        self._knowledge_path = knowledge_path
        self._task_queue = task_queue
        self._weekly_plan = weekly_plan
        self._scheduled_plan = scheduled_plan
        self._status_provider = status_provider

    def handle(self, update: dict[str, Any]) -> str | None:
        event = self._event(update)
        if event is None:
            return None
        event_id, user_id, value = event
        if user_id not in self._admin_ids:
            return None

        state = self._load_state()
        processed = {str(item) for item in state.get("processed_events", [])}
        if event_id in processed:
            return None
        processed.add(event_id)
        state["processed_events"] = sorted(processed)

        response: str | None
        if event_id.startswith("callback:"):
            response = self._handle_callback(value, user_id, state)
        else:
            response = self._handle_message(value, user_id, state)
        self._save_state(state)
        return response

    def _handle_message(
        self,
        text: str,
        user_id: int,
        state: dict[str, Any],
    ) -> str | None:
        if text.startswith("photo:") and self._scheduled_plan is not None:
            return self._scheduled_plan.receive_photo(
                state, text.removeprefix("photo:"), datetime.now(timezone.utc)
            )
        command = text.strip().split(maxsplit=1)[0].split("@", maxsplit=1)[0]
        if command == "/setup":
            profile = state.get("brand_profile")
            if isinstance(profile, dict):
                return self._profile_summary(profile)
            state["setup"] = {"field": 0, "values": {}, "user_id": user_id}
            return "Настройка профиля. Введите название бренда."
        if command == "/plan":
            if not self._profile_complete(state):
                return "Сначала заполните обязательный профиль бренда через /setup."
            if self._weekly_plan is not None:
                profile = state["brand_profile"]
                assert isinstance(profile, dict)
                try:
                    return self._weekly_plan.start(
                        state, profile, datetime.now(timezone.utc)
                    )
                except RuntimeError as error:
                    return str(error)
        if command == "/status":
            if self._status_provider is not None:
                return self._status_provider()
            response = (
                "Hermes работает.\n"
                f"Активный OpenCode Zen ключ: №{self._active_key_number}.\n"
                "Telegram-оболочка: готова."
            )
            if self._task_queue is not None:
                response += f"\nОчередь:\n{self._task_queue.status(datetime.now(timezone.utc))}"
            return response
        if command == "/new_video":
            return (
                "Пришлите JPEG, PNG или WebP с идеей в подписи. "
                "Минимальная сторона — 720 px."
            )
        if command == "/cancel" and self._task_queue is not None:
            return (
                "Выберите задачу для отмены:\n"
                f"{self._task_queue.status(datetime.now(timezone.utc))}"
            )
        if command.startswith("/") and command in COMMANDS:
            return f"Сценарий {command} пока не настроен."
        setup = state.get("setup")
        if not isinstance(setup, dict):
            if self._weekly_plan is not None:
                return self._weekly_plan.message(text.strip(), state)
            return None
        return self._accept_setup_value(text.strip(), user_id, setup, state)

    def _handle_callback(
        self,
        data: str,
        user_id: int,
        state: dict[str, Any],
    ) -> str:
        parts = data.split(":")
        if (
            self._scheduled_plan is not None
            and parts == ["plan", "photo_confirm"]
        ):
            return self._scheduled_plan.confirm_photo(
                state, datetime.now(timezone.utc)
            )
        if self._weekly_plan is not None and parts[0] == "plan":
            return self._weekly_plan.callback(data, state)
        if (
            self._task_queue is not None
            and len(parts) == 3
            and parts[:2] == ["task", "approve"]
        ):
            self._task_queue.approve(parts[2], datetime.now(timezone.utc))
            return f"{parts[2]}: утверждено и поставлено в очередь."
        if (
            self._task_queue is not None
            and len(parts) == 3
            and parts[:2] == ["task", "cancel"]
        ):
            result = self._task_queue.cancel(
                parts[2], confirmed=False, now=datetime.now(timezone.utc)
            )
            if result == "confirm":
                return f"{parts[2]} уже рендерится. Подтвердите точечную отмену."
            return f"{parts[2]}: отменено."
        if (
            self._task_queue is not None
            and len(parts) == 3
            and parts[:2] == ["task", "confirm_cancel"]
        ):
            self._task_queue.cancel(
                parts[2], confirmed=True, now=datetime.now(timezone.utc)
            )
            return f"{parts[2]}: рендер отменён."
        if len(parts) != 4 or parts[:2] != ["setup", "edit"]:
            return "Этот сценарий пока не настроен."
        field = parts[2]
        profile = state.get("brand_profile")
        try:
            revision = int(parts[3])
        except ValueError:
            revision = -1
        if (
            not isinstance(profile, dict)
            or revision != profile.get("revision")
            or field not in PROFILE_LABELS
        ):
            return "Профиль уже изменён. Откройте /setup заново."
        state["setup"] = {
            "edit": field,
            "revision": revision,
            "user_id": user_id,
        }
        return f"Введите новое значение поля «{PROFILE_LABELS[field].lower()}»."

    def _accept_setup_value(
        self,
        value: str,
        user_id: int,
        setup: dict[str, Any],
        state: dict[str, Any],
    ) -> str:
        edit = setup.get("edit")
        if isinstance(edit, str):
            return self._save_edit(edit, value, user_id, setup, state)

        index = int(setup.get("field", 0))
        key, label, required = PROFILE_FIELDS[index]
        skipped = value.lower() in {"/skip", "-"}
        if required and skipped:
            return f"Поле «{PROFILE_LABELS[key].lower()}» обязательно. Введите значение."
        if key == "timezone" and not skipped and not self._valid_timezone(value):
            return "Неизвестный часовой пояс. Например: Europe/Samara."

        values = setup.setdefault("values", {})
        assert isinstance(values, dict)
        values[key] = "" if skipped else value
        index += 1
        if index < len(PROFILE_FIELDS):
            setup["field"] = index
            next_key, next_label, next_required = PROFILE_FIELDS[index]
            suffix = "" if next_required else " или /skip"
            return f"Введите {next_label}{suffix}."

        profile = dict(values)
        profile.update({"revision": 1, "audit": {"updated_by": user_id}})
        state["brand_profile"] = profile
        state.pop("setup", None)
        if self._knowledge_path is not None:
            self._knowledge_path.mkdir(parents=True, exist_ok=True)
        return "Профиль бренда сохранён (ревизия 1)."

    def _save_edit(
        self,
        field: str,
        value: str,
        user_id: int,
        setup: dict[str, Any],
        state: dict[str, Any],
    ) -> str:
        profile = state.get("brand_profile")
        if (
            not isinstance(profile, dict)
            or profile.get("revision") != setup.get("revision")
        ):
            state.pop("setup", None)
            return "Профиль уже изменён. Откройте /setup заново."
        required = next(item[2] for item in PROFILE_FIELDS if item[0] == field)
        skipped = value.lower() in {"/skip", "-"}
        if required and skipped:
            return f"Поле «{PROFILE_LABELS[field].lower()}» обязательно. Введите значение."
        if field == "timezone" and not self._valid_timezone(value):
            return "Неизвестный часовой пояс. Например: Europe/Samara."
        profile[field] = "" if skipped else value
        profile["revision"] = int(profile["revision"]) + 1
        profile["audit"] = {"updated_by": user_id}
        state.pop("setup", None)
        return f"Профиль бренда сохранён (ревизия {profile['revision']})."

    @staticmethod
    def _valid_timezone(value: str) -> bool:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            return False
        return True

    @staticmethod
    def _profile_complete(state: dict[str, Any]) -> bool:
        profile = state.get("brand_profile")
        return isinstance(profile, dict) and all(
            profile.get(key) for key, _label, required in PROFILE_FIELDS if required
        )

    @staticmethod
    def _profile_summary(profile: dict[str, Any]) -> str:
        lines = ["Актуальный профиль бренда:"]
        lines.extend(
            f"{PROFILE_LABELS[key]}: {profile.get(key) or '—'}"
            for key, _label, _required in PROFILE_FIELDS
        )
        lines.append(f"Ревизия: {profile['revision']}")
        lines.append("Выберите поле для изменения.")
        return "\n".join(lines)

    @staticmethod
    def _event(update: dict[str, Any]) -> tuple[str, int, str] | None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            sender = callback.get("from")
            callback_id = callback.get("id")
            data = callback.get("data")
            if (
                isinstance(sender, dict)
                and callback_id is not None
                and isinstance(data, str)
            ):
                return (
                    f"callback:{callback_id}",
                    int(sender["id"]),
                    data,
                )

        message = update.get("message")
        update_id = update.get("update_id")
        if not isinstance(message, dict) or update_id is None:
            return None
        sender = message.get("from")
        if not isinstance(sender, dict):
            return None
        text = message.get("text")
        photos = message.get("photo")
        if not isinstance(text, str) and isinstance(photos, list):
            largest = max(
                (photo for photo in photos if isinstance(photo, dict)),
                key=lambda photo: int(photo.get("width", 0))
                * int(photo.get("height", 0)),
                default=None,
            )
            if largest is not None and largest.get("file_id"):
                text = f"photo:{largest['file_id']}"
        if not isinstance(text, str):
            return None
        return f"update:{update_id}", int(sender["id"]), text

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        data: dict[str, Any] = json.loads(
            self._state_path.read_text(encoding="utf-8")
        )
        return data

    def _save_state(self, state: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(f"{self._state_path.suffix}.new")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2)
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
        config.hermes_knowledge_path,
    ).handle(update)
    if response is not None:
        print(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
