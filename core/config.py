"""Конфигурация контент-завода.

Единый источник настроек для всех субагентов. Все секреты и эндпоинты
читаются только из `.env` / переменных окружения (правило AGENTS.md:
никакого хардкода ключей и путей).
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки системы, загружаемые из `.env`.

    Attributes:
        openrouter_api_key: Ключ OpenRouter (primary-провайдер интеллекта).
        openai_api_key: Ключ OpenAI (резервный провайдер).
        anthropic_api_key: Ключ Anthropic (резервный провайдер).
        openrouter_model: Модель OpenRouter по умолчанию.
        openai_model: Модель OpenAI по умолчанию.
        anthropic_model: Модель Anthropic по умолчанию.
        lmstudio_base_url: OpenAI-совместимый эндпоинт локальной LM Studio —
            последний fallback при полном отказе облачных API.
        lmstudio_model: Имя локальной модели в LM Studio.
        db_path: Путь к файлу операционной БД SQLite.
        scrape_creators_api_key: Ключ Scrape Creators API (primary-парсер).
        scrape_creators_base_url: Базовый URL Scrape Creators API.
        markudown_command: Команда запуска MarkUDown MCP (fallback-парсер).
        shared_folder: Локальный путь к Shared Folder (шаринг с Node 1).
        webhook_host: Интерфейс Webhook-приёмника.
        webhook_port: Порт Webhook-приёмника.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    openrouter_model: str = "anthropic/claude-3.5-sonnet"
    openai_model: str = "gpt-4o-mini"
    anthropic_model: str = "claude-3-5-sonnet-latest"

    lmstudio_base_url: str = "http://localhost:1234/v1"
    lmstudio_model: str = "local-model"

    db_path: str = "data/factory.db"

    scrape_creators_api_key: str | None = None
    scrape_creators_base_url: str = "https://api.scrapecreators.com/v1"
    markudown_command: str = "markudown"

    shared_folder: str = "shared"
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8099
