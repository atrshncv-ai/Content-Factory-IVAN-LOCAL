"""Субагент «Промпт-инженер» (Prompt Engineer).

Берёт задачу в статусе 'planned' (концепция от Сценариста) и генерирует
два технических промпта на английском языке для ComfyUI. ЖЁСТКОЕ
бизнес-ограничение: генератор рендерит ТОЛЬКО статичные фото — термины
анимации/движения/камеры/видео запрещены в промптах.

Gate Pattern: пустые поля от LLM не проходят — задача остаётся planned
(повтор на следующем тике), ошибка пишется в лог.

Результат — два промпта, сохраняемые в полях video_prompt и photo_prompt.
Статус задачи после обработки: planned → ready_for_render.

Паттерн: Clean Context — один вызов = одна задача, затем выход.
"""
import json
import re
from typing import Protocol

from openai.types.chat import ChatCompletionMessageParam

from core.db import Database


class LLMChat(Protocol):
    """Минимальный интерфейс LLM-клиента для Промпт-инженера."""
    def chat(self, messages: list[ChatCompletionMessageParam]) -> str: ...


PROMPTER_PROMPT = """You are a professional prompt engineer for AI STATIC IMAGE generation (ComfyUI).
Based on the content concept below, create TWO specialized prompts for static images.

Concept: {concept}
Niche: {niche}

HARD CONSTRAINT: WE GENERATE ONLY STATIC PHOTOS. It is FORBIDDEN to use terms of
animation, motion, camera movement or video (e.g. "blinks", "waves hand",
"camera pans", "video", "animation"). Prompts must describe ONLY composition,
lighting and style of a static frame.

Create:
1. IMAGE_PROMPT (JSON key "video_prompt"): A detailed prompt for the main static
   Instagram image.
   - Subject, composition, lighting, style, color palette
   - Square (1:1) format, English, specific and technical

2. COVER_PROMPT (JSON key "photo_prompt"): A detailed prompt for an alternate
   static cover image.
   - Subject, composition, lighting, mood
   - Square (1:1) format, English, specific and technical

Return STRICTLY valid JSON with no extra words:
{{"video_prompt": "static image prompt in English", "photo_prompt": "static image prompt in English"}}"""


class PromptEngineer:
    """Промпт-инженер: planned → ready_for_render.

    Генерирует технические промпты для ComfyUI на основе концепции.

    Args:
        llm: LLM-клиент с fallback-цепочкой.
        db: Операционная БД.
        niche: Тематика контента.

    Example:
        >>> engineer = PromptEngineer(llm, db, "ai")
        >>> result = engineer.create_prompts("trend-abc123")
    """

    def __init__(self, llm: LLMChat, db: Database, niche: str = "ai") -> None:
        self._llm = llm
        self._db = db
        self._niche = niche

    def create_prompts(self, job_id: str) -> dict[str, str]:
        """Генерирует промпты для задания и сохраняет в БД.

        Args:
            job_id: Задание со статусом 'planned' в media_jobs.

        Returns:
            {"job_id": str, "video_prompt": str, "photo_prompt": str, "status": "ready_for_render"}

        Raises:
            ValueError: задание не найдено, не в статусе 'planned', нет концепции
                или модель вернула пустые промпты (Gate).
        """
        job = self._db.fetch_job(job_id)
        if job is None:
            raise ValueError(f"задание не найдено: {job_id}")
        if job["status"] != "planned":
            raise ValueError(f"задание {job_id} не в статусе planned: {job['status']}")
        concept = job["concept"]
        if not concept:
            raise ValueError(f"задание {job_id} не имеет концепции")

        messages: list[ChatCompletionMessageParam] = [
            {
                "role": "user",
                "content": PROMPTER_PROMPT.format(concept=concept, niche=self._niche),
            },
        ]

        raw = self._llm.chat(messages)
        video_prompt, photo_prompt = self._parse_prompts(raw)

        # Gate Pattern: пустые промпты не проходят дальше — задача остаётся
        # planned и будет повторена на следующем тике Оркестратора.
        if not video_prompt.strip() or not photo_prompt.strip():
            self._db.log(
                "ERROR", "prompter",
                f"job {job_id}: модель вернула пустые промпты — статус остаётся planned",
            )
            raise ValueError(f"job {job_id}: модель вернула пустые промпты")

        self._db.upsert_job(
            job_id, "ready_for_render",
            video_prompt=video_prompt, photo_prompt=photo_prompt,
        )
        self._db.log(
            "INFO", "prompter",
            f"job {job_id}: prompts created (video={len(video_prompt)}c, photo={len(photo_prompt)}c)",
        )

        return {
            "job_id": job_id,
            "video_prompt": video_prompt,
            "photo_prompt": photo_prompt,
            "status": "ready_for_render",
        }

    @staticmethod
    def _parse_prompts(raw: str) -> tuple[str, str]:
        """Парсит JSON-ответ модели и извлекает video_prompt и photo_prompt."""
        text = raw.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        try:
            data = json.loads(text)
            return str(data.get("video_prompt", "")), str(data.get("photo_prompt", ""))
        except (json.JSONDecodeError, AttributeError):
            return "", ""  # мусор = брак: Gate отклонит, задача уйдёт на повтор
