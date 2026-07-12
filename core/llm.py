"""Единый LLM-клиент с менеджером ключей и fallback-цепочкой.

Реализует регламент надежности из PRD: все субагенты обращаются к модели
только через этот модуль. Порядок попыток:
OpenRouter → OpenAI → Anthropic → локальная LM Studio.

При лимите/ошибке провайдера клиент переключается на следующий ключ;
при полном отказе облака управление уходит локальной LM Studio
(режим «вечнозеленого» контента). Все провайдеры вызываются через
единый openai-SDK по OpenAI-совместимым эндпоинтам (Ponytail-минимализм:
ноль собственных HTTP-клиентов и бойлерплейта).
"""
from dataclasses import dataclass

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from core.config import Settings


class LLMUnavailableError(RuntimeError):
    """Все провайдеры, включая локальную LM Studio, недоступны."""


@dataclass(frozen=True)
class Provider:
    """Один слот fallback-цепочки: эндпоинт + ключ + модель."""

    name: str
    base_url: str
    api_key: str
    model: str


def build_providers(settings: Settings) -> list[Provider]:
    """Собирает fallback-цепочку из настроек.

    Облачные провайдеры попадают в цепочку только если задан их ключ;
    LM Studio добавляется всегда и всегда последней.

    Args:
        settings: Конфигурация с пулом ключей и моделями.

    Returns:
        Упорядоченный список провайдеров от primary к локальному fallback.
    """
    chain: list[Provider] = []
    # OpenCode Zen — Primary (DeepSeek V4 flash free)
    if settings.opencode_api_key:
        chain.append(Provider(
            "opencode", "https://opencode.ai/zen/v1",
            settings.opencode_api_key, settings.opencode_model,
        ))
    if settings.openrouter_api_key:
        chain.append(Provider(
            "openrouter", "https://openrouter.ai/api/v1",
            settings.openrouter_api_key, settings.openrouter_model,
        ))
    if settings.openai_api_key:
        chain.append(Provider(
            "openai", "https://api.openai.com/v1",
            settings.openai_api_key, settings.openai_model,
        ))
    if settings.anthropic_api_key:
        chain.append(Provider(
            "anthropic", "https://api.anthropic.com/v1",
            settings.anthropic_api_key, settings.anthropic_model,
        ))
    chain.append(Provider(
        "lmstudio", settings.lmstudio_base_url, "lm-studio", settings.lmstudio_model,
    ))
    return chain


class LLMClient:
    """Клиент с ротацией ключей: перебирает цепочку до первого успеха.

    Интеллект — в модели, отказоустойчивость — здесь, в инфраструктуре
    (принцип Harness Engineering: Agent = Model + Harness).

    Example:
        >>> client = LLMClient(Settings())
        >>> reply = client.chat([{"role": "user", "content": "Привет"}])
    """

    def __init__(self, settings: Settings) -> None:
        """Строит fallback-цепочку один раз при инициализации."""
        self._chain = build_providers(settings)

    def chat(self, messages: list[ChatCompletionMessageParam]) -> str:
        """Отправляет сообщения первому живому провайдеру из цепочки.

        Args:
            messages: Диалог в OpenAI-формате chat completions.

        Returns:
            Текст ответа модели.

        Raises:
            LLMUnavailableError: недоступны все провайдеры, включая LM Studio.
        """
        errors: list[str] = []
        for provider in self._chain:
            try:
                client = OpenAI(base_url=provider.base_url, api_key=provider.api_key)
                response = client.chat.completions.create(
                    model=provider.model, messages=messages,
                )
                content = response.choices[0].message.content
                if content:
                    return content
                raise ValueError("пустой ответ модели")
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
        raise LLMUnavailableError(" | ".join(errors))
