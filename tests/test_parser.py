"""Тесты субагента «Парсер» (core.parser).

Покрывают fallback-цепочку сбора трендов:
Scrape Creators API (primary) → crawl4ai (fallback) → Jina Reader (last resort).
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


class _FakeHTTPText:
    """Подмена _http_get_text: отдаёт текст или падает."""

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str] | None = None) -> str:
        self.calls.append(url)
        if self._error:
            raise self._error
        return self._text


class _FakeCrawl4AI:
    """Подмена _crawl4ai_fetch: возвращает список трендов."""

    def __init__(self, trends: list[dict] | None = None, error: Exception | None = None) -> None:
        self._trends = trends or []
        self._error = error
        self.calls: list[str] = []

    async def __call__(self, url: str, limit: int) -> list[dict]:
        self.calls.append(url)
        if self._error:
            raise self._error
        return self._trends


def make_parser(
    monkeypatch: pytest.MonkeyPatch,
    http: _FakeHTTP | None = None,
    http_text: _FakeHTTPText | None = None,
    crawl4ai: _FakeCrawl4AI | None = None,
    **env: str,
) -> TrendParser:
    """TrendParser с подменёнными швами I/O и заданными env-переменными."""
    if http is not None:
        monkeypatch.setattr("core.parser._http_get_json", http)
    if http_text is not None:
        monkeypatch.setattr("core.parser._http_get_text", http_text)
    if crawl4ai is not None:
        monkeypatch.setattr("core.parser.TrendParser._crawl4ai_fetch", crawl4ai)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return TrendParser(IsolatedSettings())


SC_PAYLOAD = {
    "trends": [
        {"title": "AI reels hook", "url": "https://ig.example/p/1", "likes": 1200},
        {"title": "POV trend", "url": "https://ig.example/p/2", "likes": 800},
    ]
}

CRAWL4AI_TRENDS = [
    {"title": "Crawled trend 1", "url": "https://ig.example/p/3", "engagement": 55, "source": "crawl4ai"},
    {"title": "Crawled trend 2", "url": "https://ig.example/p/4", "engagement": 33, "source": "crawl4ai"},
]

JINA_TEXT = """# Instagram Explore: fitness
## Popular Posts
### #fitnessmotivation
Check out this trending reel about workout routines.
### #homeworkout
Quick 15-minute exercises going viral.
"""


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
    """Без ключа primary не дёргается — сразу fallback (crawl4ai)."""
    http = _FakeHTTP(SC_PAYLOAD)
    crawl = _FakeCrawl4AI(CRAWL4AI_TRENDS)
    parser = make_parser(monkeypatch, http=http, crawl4ai=crawl)
    trends = parser.fetch_trends("fitness")
    assert http.calls == []
    assert {t["source"] for t in trends} == {"crawl4ai"}


# --- Fallback 1: crawl4ai ----------------------------------------------------

def test_fallback_to_crawl4ai_when_primary_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """При падении API парсер переключается на crawl4ai."""
    http = _FakeHTTP(error=ConnectionError("api down"))
    crawl = _FakeCrawl4AI(CRAWL4AI_TRENDS)
    parser = make_parser(monkeypatch, http=http, crawl4ai=crawl, SCRAPE_CREATORS_API_KEY="sc-key")
    trends = parser.fetch_trends("fitness")
    assert [t["source"] for t in trends] == ["crawl4ai", "crawl4ai"]
    assert trends[0]["title"] == "Crawled trend 1"
    assert trends[0]["engagement"] == 55
    assert len(crawl.calls) == 1


def test_crawl4ai_skipped_without_key_and_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ключа API и при падении crawl4ai — переход на Jina."""
    http_text = _FakeHTTPText(JINA_TEXT)
    crawl = _FakeCrawl4AI(error=OSError("browser not available"))
    parser = make_parser(monkeypatch, http_text=http_text, crawl4ai=crawl)
    trends = parser.fetch_trends("fitness")
    assert len(trends) > 0
    assert all(t["source"] == "jina" for t in trends)


# --- Fallback 2: Jina Reader -------------------------------------------------

def test_fallback_to_jina_when_all_others_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """При падении API и crawl4ai — парсер использует Jina Reader."""
    http = _FakeHTTP(error=ConnectionError("api down"))
    http_text = _FakeHTTPText(JINA_TEXT)
    crawl = _FakeCrawl4AI(error=OSError("browser not available"))
    parser = make_parser(monkeypatch, http=http, http_text=http_text, crawl4ai=crawl, SCRAPE_CREATORS_API_KEY="sc-key")
    trends = parser.fetch_trends("fitness")
    assert len(trends) > 0
    assert all(t["source"] == "jina" for t in trends)
    assert http_text.calls  # Jina был вызван


def test_raises_when_all_paths_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если пали все три источника — честное исключение."""
    parser = make_parser(
        monkeypatch,
        http=_FakeHTTP(error=ConnectionError("api down")),
        http_text=_FakeHTTPText(error=OSError("network down")),
        crawl4ai=_FakeCrawl4AI(error=OSError("browser down")),
        SCRAPE_CREATORS_API_KEY="sc-key",
    )
    with pytest.raises(ParserUnavailableError):
        parser.fetch_trends("fitness")
