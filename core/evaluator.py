"""Субагент «Контролёр Качества» (Vision Evaluator).

Строгий судья медиафайлов от Node 1: отправляет изображение в Vision-модель
через единый LLMClient и требует строгий JSON-вердикт
{"approved": bool, "reason": str}.

Здесь — единственное место системы, где AGENTS.md разрешает `while True`
(микро-Ralph Loop перегенерации). Обязательный Safety Valve: цикл ограничен
MAX_RETRIES; при исчерпании лимита задание помечается 'failed', а процесс
завершается — бюджет токенов под контролем.

Видео-файлы (LTX производит .mp4) Vision-модель не поддерживает: для них —
Gate auto-approve без микро-цикла (см. VIDEO_EXTENSIONS).

Невалидный/мусорный ответ модели считается браком: судья никогда не падает
(отказоустойчивость — в обвязке, не в модели).
"""
import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from openai.types.chat import ChatCompletionMessageParam

from core.db import Database

# Расширения видео-файлов: Vision-модель их не поддерживает — auto-approve.
VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mov"})

VISION_PROMPT = """Ты — строгий контролёр качества AI-изображений для Instagram. Проверь картинку:
1. АНАТОМИЯ: лишние/недостающие пальцы и конечности, деформированные кисти,
   асимметрия лица и глаз.
2. РЕНДЕР: блюр, «мыльные» текстуры, пластиковая кожа, шумы, ореолы.
3. ФИЗИКА: невозможные тени/отражения, обрезанные объекты, плавающие предметы.
4. ПОСТОРОННЕЕ: водяные знаки, текст-абракадабра, чужие логотипы.

Ответь СТРОГО валидным JSON без единого лишнего слова:
{"approved": true|false, "reason": "кратко на русском"}
approved=true ТОЛЬКО при полном отсутствии артефактов. Сомневаешься — false."""


@dataclass(frozen=True)
class Verdict:
    """Вердикт судьи: аппрув + причина (для аудит-лога)."""

    approved: bool
    reason: str


class VisionClient(Protocol):
    """Минимальный интерфейс Vision-модели. LLMClient удовлетворяет ему структурно."""

    def chat(self, messages: list[ChatCompletionMessageParam]) -> str: ...


def parse_verdict(raw: str) -> Verdict:
    """Парсит JSON-вердикт модели (в т.ч. в ```json-фенсе).

    Любой мусор/невалидный JSON → брак (approved=False) — цикл не падает.
    """
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
        return Verdict(
            approved=bool(data.get("approved", False)),
            reason=str(data.get("reason", "")),
        )
    except (json.JSONDecodeError, AttributeError):
        return Verdict(approved=False, reason="невалидный ответ судьи")


class QualityEvaluator:
    """Контролёр качества с микро-Ralph Loop перегенерации.

    Машина статусов в media_jobs: done → (approved | regenerating → … | failed).
    Каждый вердикт пишется в аудит-лог (agent='evaluator').

    Args:
        llm: Vision-модель (наш LLMClient с fallback-цепочкой).
        db: Операционная БД.
        regenerate: Коллбэк перегенерации на Node 1 (cli-anything-comfyui).
        max_retries: Safety Valve — максимум перегенераций до 'failed'.

    Example:
        >>> evaluator = QualityEvaluator(llm, db, regenerate, max_retries=3)
        >>> verdict = evaluator.evaluate_job("job-42")
    """

    def __init__(
        self,
        llm: VisionClient,
        db: Database,
        regenerate: Callable[[str], None],
        max_retries: int = 3,
    ) -> None:
        self._llm = llm
        self._db = db
        self._regenerate = regenerate
        self._max_retries = max_retries

    def evaluate_image(self, image_path: Path) -> Verdict:
        """Отправляет изображение в Vision-модель. Returns: вердикт судьи."""
        b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        messages: list[ChatCompletionMessageParam] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            },
        ]
        return parse_verdict(self._llm.chat(messages))

    def evaluate_job(self, job_id: str) -> dict[str, Any]:
        """Оценивает задание: видео — auto-approve, изображение — микро-цикл.

        Args:
            job_id: Задание со статусом 'done' и file_path в media_jobs.

        Returns:
            {"approved": bool, "job_id": str, "attempts": int, "reason": str}.

        Raises:
            ValueError: задание не найдено или без файла.
        """
        job = self._db.fetch_job(job_id)
        if job is None or not job["file_path"]:
            raise ValueError(f"задание без файла: {job_id}")
        media_path = Path(job["file_path"])

        # Gate для видео (LTX): Vision-модель видео не поддерживает —
        # пропускаем без оценки и без микро-цикла перегенерации.
        if media_path.suffix.lower() in VIDEO_EXTENSIONS:
            self._db.log(
                "INFO", "evaluator",
                f"job {job_id}: видео ({media_path.suffix}) — auto-approve "
                "без Vision-оценки",
            )
            self._db.upsert_job(job_id, "approved")
            return {
                "approved": True, "job_id": job_id,
                "attempts": 0, "reason": "видео: Vision-оценка не поддерживается",
            }

        attempts = 0
        while True:  # микро-Ralph Loop — разрешён AGENTS.md только здесь
            verdict = self.evaluate_image(media_path)
            self._db.log(
                "INFO", "evaluator",
                f"job {job_id}: approved={verdict.approved} — {verdict.reason}",
            )
            if verdict.approved:
                self._db.upsert_job(job_id, "approved")
                return {
                    "approved": True, "job_id": job_id,
                    "attempts": attempts, "reason": verdict.reason,
                }
            if attempts >= self._max_retries:  # Safety Valve
                self._db.upsert_job(job_id, "failed")
                return {
                    "approved": False, "job_id": job_id,
                    "attempts": attempts, "reason": verdict.reason,
                }
            attempts += 1
            self._db.upsert_job(job_id, "regenerating")
            self._regenerate(job_id)
