"""Общие тестовые утилиты."""
from pydantic_settings import SettingsConfigDict

from core.config import Settings


class IsolatedSettings(Settings):
    """Settings без чтения реального `.env` — только monkeypatched env и kwargs.

    Гарантирует изоляцию тестов от локального окружения разработчика.
    """

    model_config = SettingsConfigDict(env_file=None, extra="ignore")
