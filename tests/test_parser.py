"""Тесты субагента «Парсер» (core.parser).

Покрывают fallback-цепочку сбора трендов:
Scrape Creators API (primary) → MarkUDown MCP (fallback).
Сеть и subprocess не используются — моки на швах _http_get_json / _run_command.
"""
import json

import pytest

from core.parser import ParserUnavailableError, TrendParser
from tests.conftest import IsolatedSettings


class _FakeHTTP:
    """Подмена _http_get_json: отдаёт payload или падает, записывает вызовы."""

    def __init__(self, payload: dict | None = None, error: Exception | None = None) -> None:
        self._payload = payload or {}
        self._error = error
        self.calls: list[dict] = []

    def __call__(self, url: str, headers: dict[str, str]) -> dict:
        self.calls.append({"url": url, "headers": headers})
        if self._error:
            raise self._error
        return self._payload


class _FakeRunner:
    """Подмена _run_command: возвращает stdout или падает, записывает команды."""

    def __init__(self, stdout: str = "", error: Exception | None = None) -> None:
        self._stdout = stdout
        self._error = error
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> str:
        self.calls.append(command)
        if self._error:
            raise self._error
        return self._stdout


def make_parser(
    monkeypatch: pytest.MonkeyPatch,
    http: _FakeHTTP | None = None,
    runner: _FakeRunner | None = None,
    **env: str,
) -> TrendParser:
    """TrendParser с подменёнными швами I/O и заданными env-переменными."""
    if http is not None:
        monkeypatch.setattr("core.parser._http_get_json", http)
    if runner is not None:
        monkeypatch.setattr("core.parser._run_command", runner)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return TrendParser(IsolatedSettings())


SC_PAYLOAD = {
    "trends": [
        {"title": "AI reels hook", "url": "https://ig.example/p/1", "likes": 1200},
        {"title": "POV trend", "url": "https://ig.example/p/2", "likes": 800},
    ]
}
MD_PAYLOAD = json.dumps([
    {"title": "Local trend", "url": "https://ig.example/p/9", "engagement": 42},
])


# --- Primary: Scrape Creators API --------------------------------------------

def test_primary_success_returns_normalized_trends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Успешный ответ API нормализуется в единую схему тренда."""
    parser = make_parser(
        monkeypatch, http=_FakeHTTP(SC_PAYLOAD), SCRAPE_CREATORS_API_KEY="sc-key",
    )
    trends = parser.fetch_trends("fitness", limit=5)
    assert len(trends) == 2
    assert trends[0] == {
        "title": "AI reels hook",
        "url": "https://ig.example/p/1",
        "engagement": 1200,
        "source": "scrape_creators",
    }


def test_primary_sends_api_key_and_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключ из .env уходит в заголовки, запрос — в URL."""
    http = _FakeHTTP(SC_PAYLOAD)
    parser = make_parser(monkeypatch, http=http, SCRAPE_CREATORS_API_KEY="sc-key")
    parser.fetch_trends("fitness", limit=5)
    assert len(http.calls) == 1
    headers = http.calls[0]["headers"]
    assert any("sc-key" in str(value) for value in headers.values())
    assert "fitness" in http.calls[0]["url"]


def test_primary_limit_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """limit режет число возвращаемых трендов."""
    parser = make_parser(
        monkeypatch, http=_FakeHTTP(SC_PAYLOAD), SCRAPE_CREATORS_API_KEY="sc-key",
    )
    assert len(parser.fetch_trends("fitness", limit=1)) == 1


def test_primary_skipped_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ключа primary не дёргается — сразу fallback (как в build_providers)."""
    http = _FakeHTTP(SC_PAYLOAD)
    runner = _FakeRunner(MD_PAYLOAD)
    parser = make_parser(monkeypatch, http=http, runner=runner)
    trends = parser.fetch_trends("fitness")
    assert http.calls == []
    assert {t["source"] for t in trends} == {"markudown"}


# --- Fallback: MarkUDown MCP ---------------------------------------------------

def test_fallback_to_markudown_when_primary_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """При падении API парсер переключается на MarkUDown (CLI-syscall)."""
    http = _FakeHTTP(error=ConnectionError("api down"))
    runner = _FakeRunner(MD_PAYLOAD)
    parser = make_parser(
        monkeypatch, http=http, runner=runner, SCRAPE_CREATORS_API_KEY="sc-key",
    )
    trends = parser.fetch_trends("fitness")
    assert [t["source"] for t in trends] == ["markudown"]
    assert trends[0]["title"] == "Local trend"
    assert trends[0]["engagement"] == 42
    assert runner.calls and "markudown" in runner.calls[0][0]


def test_raises_when_both_paths_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если пали и API, и MarkUDown — честное исключение."""
    parser = make_parser(
        monkeypatch,
        http=_FakeHTTP(error=ConnectionError("api down")),
        runner=_FakeRunner(error=OSError("mcp down")),
        SCRAPE_CREATORS_API_KEY="sc-key",
    )
    with pytest.raises(ParserUnavailableError):
        parser.fetch_trends("fitness")
