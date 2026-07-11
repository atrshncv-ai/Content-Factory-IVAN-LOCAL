"""Субагент «Парсер» (Trend Analyst) — сбор трендов с fallback-цепочкой.

Primary:  Scrape Creators API (REST, ключ из .env).
Fallback: MarkUDown MCP — вызывается как внешняя CLI-утилита (syscall),
          согласно AGENTS.md падежная логика вынесена за пределы агента.

Оба источника нормализуются в единую схему тренда
{title, url, engagement, source}, готовую к записи в Graphify/Vector DB.
Весь I/O спрятан за двумя швами (_http_get_json, _run_command) —
это граница мокирования в тестах и единая точка отказоустойчивости.
"""
import json
import subprocess
import urllib.parse
import urllib.request

from core.config import Settings

DEFAULT_LIMIT = 10


class ParserUnavailableError(RuntimeError):
    """Недоступны оба источника трендов: Scrape Creators API и MarkUDown."""


def _http_get_json(url: str, headers: dict[str, str]) -> dict:
    """GET-запрос, ожидающий JSON (stdlib urllib — ноль сторонних зависимостей)."""
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


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

    Порядок попыток: Scrape Creators API → MarkUDown MCP.
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
            ParserUnavailableError: оба источника недоступны.
        """
        errors: list[str] = []
        if self._settings.scrape_creators_api_key:
            try:
                return self._from_scrape_creators(query, limit)
            except Exception as exc:
                errors.append(f"scrape_creators: {exc}")
        try:
            return self._from_markudown(query, limit)
        except Exception as exc:
            errors.append(f"markudown: {exc}")
        raise ParserUnavailableError(" | ".join(errors) or "нет доступных источников")

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

    def _from_markudown(self, query: str, limit: int) -> list[dict[str, object]]:
        """Fallback-путь: локальный MarkUDown MCP как CLI-syscall."""
        stdout = _run_command([
            self._settings.markudown_command, "trends", query,
            "--json", "--limit", str(limit),
        ])
        raw = json.loads(stdout)
        raw_items = raw.get("trends", []) if isinstance(raw, dict) else raw
        return [
            {
                "title": str(item.get("title", "")),
                "url": str(item.get("url", "")),
                "engagement": int(item.get("engagement", 0)),
                "source": "markudown",
            }
            for item in raw_items[:limit]
        ]
