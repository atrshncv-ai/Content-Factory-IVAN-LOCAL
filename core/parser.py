"""Субагент «Парсер» (Trend Analyst) — сбор трендов с fallback-цепочкой.

Цепочка источников (по приоритету):
  1. Scrape Creators API  — REST, ключ из .env (primary).
  2. crawl4ai             — web-crawling трендовых страниц.
  3. Agent-Reach / Jina   — чтение страниц через Jina Reader (last resort).

Оба источника нормализуются в единую схему тренда
{title, url, engagement, source}, готовую к записи в Graphify/Vector DB.
Весь I/O спрятан за швами (_http_get_json, _run_command) —
это граница мокирования в тестах и единая точка отказоустойчивости.
"""
import asyncio
import concurrent.futures
import json
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Coroutine
from typing import Any, TypeVar

from core.config import Settings

DEFAULT_LIMIT = 10

_T = TypeVar("_T")


def _run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Безопасный запуск корутины из синхронного кода.

    asyncio.run() падает с RuntimeError, если в вызывающем потоке уже
    работает event loop (вызов из async-окружения). В этом случае корутина
    уезжает в отдельный поток со своим свежим loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # активного loop нет — обычный путь
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class ParserUnavailableError(RuntimeError):
    """Недоступны все источники трендов в fallback-цепочке."""


def _http_get_json(url: str, headers: dict[str, str]) -> dict:
    """GET-запрос, ожидающий JSON (stdlib urllib — ноль сторонних зависимостей)."""
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_get_text(url: str, headers: dict[str, str] | None = None) -> str:
    """GET-запрос, возвращающий текст (HTML/Markdown)."""
    req_headers = headers or {"User-Agent": "Mozilla/5.0"}
    request = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _run_command(command: list[str]) -> str:
    """Запуск внешней CLI-утилиты как syscall. Returns: stdout.

    Raises:
        subprocess.CalledProcessError: утилита завершилась с ошибкой.
        subprocess.TimeoutExpired: утилита не ответила вовремя.
    """
    result = subprocess.run(
        command, capture_output=True, text=True, check=True, timeout=120,
    )
    return result.stdout


class TrendParser:
    """Субагент «Trend Analyst»: собирает тренды по нише.

    Цепочка попыток: Scrape Creators API → crawl4ai → Jina Reader.
    При отсутствии API-ключа primary пропускается сразу.

    Example:
        >>> parser = TrendParser(Settings())
        >>> trends = parser.fetch_trends("fitness", limit=10)
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def fetch_trends(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict[str, object]]:
        """Возвращает нормализованные тренды первого живого источника.

        Args:
            query: Ниша/тема поиска трендов.
            limit: Максимум трендов в ответе.

        Raises:
            ParserUnavailableError: все источники недоступны.
        """
        errors: list[str] = []

        # 1. Primary: Scrape Creators API
        if self._settings.scrape_creators_api_key:
            try:
                return self._from_scrape_creators(query, limit)
            except Exception as exc:
                errors.append(f"scrape_creators: {exc}")

        # 2. Fallback: crawl4ai
        try:
            return self._from_crawl4ai(query, limit)
        except Exception as exc:
            errors.append(f"crawl4ai: {exc}")

        # 3. Last resort: Jina Reader (бесплатный, без ключа)
        try:
            return self._from_jina(query, limit)
        except Exception as exc:
            errors.append(f"jina: {exc}")

        raise ParserUnavailableError(" | ".join(errors) or "нет доступных источников")

    # --- Источники ---------------------------------------------------------------

    def _from_scrape_creators(self, query: str, limit: int) -> list[dict[str, object]]:
        """Primary-путь: REST API Scrape Creators."""
        params = urllib.parse.urlencode({"q": query, "limit": limit})
        url = f"{self._settings.scrape_creators_base_url}/trends?{params}"
        headers = {
            "x-api-key": self._settings.scrape_creators_api_key or "",
            "Accept": "application/json",
        }
        payload = _http_get_json(url, headers)
        raw_items = payload.get("trends", []) if isinstance(payload, dict) else []
        return [
            {
                "title": str(item.get("title", "")),
                "url": str(item.get("url", "")),
                "engagement": int(item.get("likes", 0)),
                "source": "scrape_creators",
            }
            for item in raw_items[:limit]
        ]

    def _from_crawl4ai(self, query: str, limit: int) -> list[dict[str, object]]:
        """Fallback: web-crawling через crawl4ai (async, без API-ключа)."""
        url = f"https://www.google.com/search?q=instagram+trending+reels+{query}"
        # crawl4ai требует async — безопасный мост sync→async (см. _run_async)
        return _run_async(self._crawl4ai_fetch(url, limit))

    async def _crawl4ai_fetch(self, url: str, limit: int) -> list[dict[str, object]]:
        """Асинхронный краулинг одной страницы."""
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig  # lazy import

        config = CrawlerRunConfig()
        async with AsyncWebCrawler() as crawler:
            page = await crawler.arun(url, config=config)
            if not page.success:
                raise RuntimeError(f"crawl4ai: fetch failed for {url}")
            md = page.markdown
            raw = md.raw_markdown if hasattr(md, "raw_markdown") else str(md)
            return self._parse_html_trends(raw, limit, "crawl4ai")

    def _from_jina(self, query: str, limit: int) -> list[dict[str, object]]:
        """Last resort: чтение страницы через Jina Reader (бесплатный proxy)."""
        # Используем Google Search вместо Instagram (Instagram блокирует прямой доступ)
        target = f"https://www.google.com/search?q=instagram+trending+reels+{query}"
        jina_url = f"https://r.jina.ai/{target}"
        text = _http_get_text(jina_url, {"Accept": "text/plain"})
        return self._parse_html_trends(text, limit, "jina")

    # --- Парсинг ----------------------------------------------------------------

    @staticmethod
    def _parse_html_trends(text: str, limit: int, source: str) -> list[dict[str, object]]:
        """Извлекает тренды из текста/HTML через простой regex-hack."""
        import re

        trends: list[dict[str, object]] = []
        # Ищем заголовки h2/h3 или строки с #hashtag
        headings = re.findall(r"#{1,3}\s+(.+?)(?:\n|$)", text)
        hashtags = re.findall(r"#(\w+)", text)
        links = re.findall(r"\[([^\]]+)\]\((https?://[^\)]+)\)", text)

        for title in headings[:limit]:
            trends.append({"title": title.strip(), "url": "", "engagement": 0, "source": source})
        if not trends:
            for tag in hashtags[:limit]:
                trends.append({"title": f"#{tag}", "url": "", "engagement": 0, "source": source})
        if not trends:
            for title, url in links[:limit]:
                trends.append({"title": title.strip(), "url": url, "engagement": 0, "source": source})
        return trends
