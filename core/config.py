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
        shared_folder: Локальный путь к Shared Folder (шаринг с Node 1).
        evaluator_max_retries: Safety Valve микро-Ralph Loop Контролёра Качества.
        autopost_command: Команда CLI-утилиты автопостинга (SMM-Паблишер).
        comfyui_command: Команда cli-anything-comfyui (отправка на Node 1).
        comfyui_server_url: Базовый URL ComfyUI на Node 1 (по Tailscale).
        comfyui_workflow_template: Шаблон workflow ComfyUI (API-граф).
        comfyui_workflow_dir: Каталог для сгенерированных workflow-файлов.
        content_niche: Тематика контента для Парсера/Оркестратора.
        headroom_enabled: Включить сжатие контекста через Headroom перед LLM.
        headroom_api_key: Ключ Headroom (если требуется для облачного сжатия).
        headroom_model: Модель сжатия Headroom; если None — используется модель
            текущего провайдера.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    opencode_api_key: str | None = None
    openrouter_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    opencode_model: str = "deepseek-v4-flash-free"
    openrouter_model: str = "anthropic/claude-3.5-sonnet"
    openai_model: str = "gpt-4o-mini"
    anthropic_model: str = "claude-3-5-sonnet-latest"

    lmstudio_base_url: str = "http://localhost:1234/v1"
    lmstudio_model: str = "local-model"

    db_path: str = "data/factory.db"

    scrape_creators_api_key: str | None = None
    scrape_creators_base_url: str = "https://api.scrapecreators.com/v1"

    shared_folder: str = "shared"

    evaluator_max_retries: int = 3

    autopost_command: str = "autopost"
    comfyui_command: str = "cli-anything-comfyui"
    comfyui_server_url: str = "http://localhost:8188"
    comfyui_workflow_template: str = "assets/comfyui_workflow_template.json"
    comfyui_workflow_dir: str = "/tmp"
    content_niche: str = "ai"

    headroom_enabled: bool = False
    headroom_api_key: str | None = None
    headroom_model: str | None = None
