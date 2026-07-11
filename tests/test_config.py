"""Тесты модуля конфигурации core.config.

Проверяют, что пул API-ключей и эндпоинт LM Studio читаются
исключительно из переменных окружения / `.env` (правило AGENTS.md).
"""
import pytest

from tests.conftest import IsolatedSettings


def make_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> IsolatedSettings:
    """Собирает Settings из заданных env-переменных без чтения реального .env."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return IsolatedSettings()


def test_settings_load_key_pool_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Все ключи пула подхватываются из окружения."""
    settings = make_settings(
        monkeypatch,
        OPENROUTER_API_KEY="or-123",
        OPENAI_API_KEY="oa-456",
        ANTHROPIC_API_KEY="an-789",
    )
    assert settings.openrouter_api_key == "or-123"
    assert settings.openai_api_key == "oa-456"
    assert settings.anthropic_api_key == "an-789"


def test_settings_keys_are_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отсутствующие ключи не роняют конфиг — слот просто пропускается."""
    settings = make_settings(monkeypatch)
    assert settings.openrouter_api_key is None
    assert settings.openai_api_key is None
    assert settings.anthropic_api_key is None


def test_settings_lmstudio_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """У локального fallback есть рабочие дефолты (LM Studio на Node 2)."""
    settings = make_settings(monkeypatch)
    assert settings.lmstudio_base_url == "http://localhost:1234/v1"
    assert settings.lmstudio_model


def test_settings_lmstudio_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Эндпоинт LM Studio переопределяется через окружение (другой хост/порт)."""
    settings = make_settings(monkeypatch, LMSTUDIO_BASE_URL="http://gpu-node:1234/v1")
    assert settings.lmstudio_base_url == "http://gpu-node:1234/v1"
