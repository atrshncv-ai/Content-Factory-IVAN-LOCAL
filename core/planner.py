"""Субагент «Сценарист» (Content Planner).

Берёт задачу в статусе 'new' (сырой тренд от Парсера) и генерирует
виральную концепцию и сценарий для Instagram-рилса/поста.

Через LLMClient обращается к модели для генерации идей.
Результат — текст концепции, сохраняемый в поле concept таблицы media_jobs.
Статус задачи после обработки: new → planned.

Паттерн: Clean Context — один вызов = одна задача, затем выход.
"""
import json
import re

from typing import Protocol

from openai.types.chat import ChatCompletionMessageParam

from core.db import Database


class LLMChat(Protocol):
    """Минимальный интерфейс LLM-клиента для Сценариста."""
    def chat(self, messages: list[ChatCompletionMessageParam]) -> str: ...

PLANNER_PROMPT = """Ты — сценарист контента для Instagram. На основе тренда придумай виральную концепцию для статичного поста.

Тренд: {trend_title}
URL: {trend_url}

ЖЁСТКОЕ ОГРАНИЧЕНИЕ: МЫ ГЕНЕРИРУЕМ ТОЛЬКО СТАТИЧНЫЕ ФОТО. Запрещено использовать
термины анимации, движения, камеры или видео (например: «моргает», «машет рукой»,
«камера наезжает», «видео», «анимация»). Концепция должна описывать только
композицию, свет и стиль статичного кадра.

Создай концепцию из 3-5 предложений:
1. Что делает этот тренд вирусным
2. Как адаптировать под нишу {niche}
3. Эмоциональный триггер для аудитории
4. Описание статичного кадра: композиция, свет, стиль

Ответь СТРОГО валидным JSON без лишних слов:
{{"concept": "текст концепции на русском языке"}}"""


class ContentPlanner:
    """Сценарист: new → planned.

    Использует LLMClient для генерации вирусных концепций на основе трендов.

    Args:
        llm: LLM-клиент с fallback-цепочкой.
        db: Операционная БД.
        niche: Тематика контента для генерации концепций.

    Example:
        >>> planner = ContentPlanner(llm, db, "ai")
        >>> result = planner.plan_job("trend-abc123")
    """

    def __init__(self, llm: LLMChat, db: Database, niche: str = "ai") -> None:
        self._llm = llm
        self._db = db
        self._niche = niche

    def plan_job(self, job_id: str) -> dict[str, str]:
        """Генерирует концепцию для задания и сохраняет в БД.

        Args:
            job_id: Задание со статусом 'new' в media_jobs.

        Returns:
            {"job_id": str, "concept": str, "status": "planned"}

        Raises:
            ValueError: задание не найдено или не в статусе 'new'.
        """
        job = self._db.fetch_job(job_id)
        if job is None:
            raise ValueError(f"задание не найдено: {job_id}")
        if job["status"] != "new":
            raise ValueError(f"задание {job_id} не в статусе new: {job['status']}")

        trend_title = job["job_id"]
        trend_url = job["job_id"]

        messages: list[ChatCompletionMessageParam] = [
            {
                "role": "user",
                "content": PLANNER_PROMPT.format(
                    trend_title=trend_title,
                    trend_url=trend_url,
                    niche=self._niche,
                ),
            },
        ]

        raw = self._llm.chat(messages)
        concept = self._parse_concept(raw)

        self._db.upsert_job(job_id, "planned", concept=concept)
        self._db.log("INFO", "planner", f"job {job_id}: concept created ({len(concept)} chars)")

        return {"job_id": job_id, "concept": concept, "status": "planned"}

    @staticmethod
    def _parse_concept(raw: str) -> str:
        """Парсит JSON-ответ модели и извлекает concept."""
        text = raw.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        try:
            data = json.loads(text)
            return str(data.get("concept", text))
        except (json.JSONDecodeError, AttributeError):
            return text
