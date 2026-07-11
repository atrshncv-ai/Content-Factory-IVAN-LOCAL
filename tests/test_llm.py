"""Тесты менеджера ключей и fallback-цепочки core.llm.

Покрывают регламент надежности из PRD: ротацию облачных ключей при
лимитах и переключение на локальную LM Studio при полном отказе API.
Сеть не используется — openai.OpenAI подменяется фейком.
"""
import pytest
from openai.types.chat import ChatCompletionMessageParam

from core.llm import LLMClient, LLMUnavailableError, build_providers
from tests.conftest import IsolatedSettings


# --- Фейк OpenAI-совместимого клиента -------------------------------------

class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    """Возвращает ответ или падает по плану: {имя_модели: текст | 'FAIL'}."""

    def __init__(self, plan: dict[str, str]) -> None:
        self._plan = plan

    def create(self, model: str, messages: object) -> _FakeResponse:
        outcome = self._plan.get(model, "FAIL")
        if outcome == "FAIL":
            raise RuntimeError(f"provider unavailable: {model}")
        return _FakeResponse(outcome)


class _FakeChat:
    def __init__(self, plan: dict[str, str]) -> None:
        self.completions = _FakeCompletions(plan)


def make_fake_openai(plan: dict[str, str]) -> type:
    """Фабрика подменного класса openai.OpenAI с заданным сценарием ответов."""

    class FakeOpenAI:
        def __init__(self, base_url: str, api_key: str) -> None:
            self.chat = _FakeChat(plan)

    return FakeOpenAI


def make_client(
    monkeypatch: pytest.MonkeyPatch, plan: dict[str, str], **env: str
) -> LLMClient:
    """LLMClient с подменённым openai.OpenAI и заданными env-переменными."""
    monkeypatch.setattr("core.llm.OpenAI", make_fake_openai(plan))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return LLMClient(IsolatedSettings())


MESSAGES: list[ChatCompletionMessageParam] = [{"role": "user", "content": "ping"}]


# --- Тесты сборки цепочки ---------------------------------------------------

def test_build_providers_skips_missing_keys() -> None:
    """Провайдеры без ключа не попадают в цепочку; LM Studio — всегда последняя."""
    settings = IsolatedSettings(openai_api_key="oa-1")
    chain = build_providers(settings)
    assert [p.name for p in chain] == ["openai", "lmstudio"]


def test_build_providers_full_pool() -> None:
    """Полный пул ключей выстраивается в порядке приоритета fallback."""
    settings = IsolatedSettings(
        openrouter_api_key="or",
        openai_api_key="oa",
        anthropic_api_key="an",
    )
    assert [p.name for p in build_providers(settings)] == [
        "openrouter", "openai", "anthropic", "lmstudio",
    ]


# --- Тесты ротации и fallback ------------------------------------------------

def test_chat_uses_first_healthy_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Первый живой провайдер отвечает без перебора остальных."""
    client = make_client(
        monkeypatch, {"or-model": "hello-or"}, OPENROUTER_API_KEY="or",
        OPENROUTER_MODEL="or-model",
    )
    assert client.chat(MESSAGES) == "hello-or"


def test_chat_rotates_to_next_key_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """При лимите первого ключа клиент молча переключается на следующий."""
    client = make_client(
        monkeypatch,
        {"or-model": "FAIL", "oa-model": "ok-oa", "local-model": "ok-local"},
        OPENROUTER_API_KEY="or", OPENROUTER_MODEL="or-model",
        OPENAI_API_KEY="oa", OPENAI_MODEL="oa-model",
        LMSTUDIO_MODEL="local-model",
    )
    # LM Studio тоже «жива», но облачный ключ приоритетнее.
    assert client.chat(MESSAGES) == "ok-oa"


def test_chat_falls_back_to_lmstudio(monkeypatch: pytest.MonkeyPatch) -> None:
    """При полном отказе облака управление уходит локальной LM Studio."""
    client = make_client(
        monkeypatch,
        {"or-model": "FAIL", "oa-model": "FAIL", "local-model": "ok-local"},
        OPENROUTER_API_KEY="or", OPENROUTER_MODEL="or-model",
        OPENAI_API_KEY="oa", OPENAI_MODEL="oa-model",
        LMSTUDIO_MODEL="local-model",
    )
    assert client.chat(MESSAGES) == "ok-local"


def test_chat_raises_when_everything_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если пали все провайдеры включая LM Studio — честное исключение."""
    client = make_client(
        monkeypatch, {"or-model": "FAIL", "local-model": "FAIL"},
        OPENROUTER_API_KEY="or", OPENROUTER_MODEL="or-model",
        LMSTUDIO_MODEL="local-model",
    )
    with pytest.raises(LLMUnavailableError):
        client.chat(MESSAGES)
