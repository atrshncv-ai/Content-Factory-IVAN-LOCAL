"""Детерминированные контракты между Hermes, веб-парсером и недельным планом."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, ClassVar
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from core.task_control import TERMINAL, TaskQueue

SOURCES = ("google_trends", "youtube", "tiktok_creative_center", "instagram")
SOURCE_NOTES = {
    "instagram": "Прямой публичный Instagram — best-effort.",
    "chatplace": "ChatPlace/Viral исследован как необязательный внешний сервис.",
}

Candidate = dict[str, Any]
Card = dict[str, str]
Fetch = Callable[[str], list[Candidate]]
Strategize = Callable[[dict[str, Any], list[Candidate]], list[Card]]
Replace = Callable[[dict[str, Any], list[Candidate], list[Card], int], Card]
Edit = Callable[[dict[str, Any], Card, str], Card]


class InsufficientCandidates(RuntimeError):
    def __init__(self, available: int, unavailable_sources: list[str]) -> None:
        self.available = available
        self.unavailable_sources = unavailable_sources
        sources = ", ".join(unavailable_sources) or "нет"
        super().__init__(
            f"Нужно минимум 5 кандидатов, доступно {available}; "
            f"недоступны: {sources}"
        )


class CandidateStore:
    """JSON cache: свежий 24 часа, аварийный снимок — строго до 7 дней."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def collect(
        self, fetch: Fetch, now: datetime
    ) -> tuple[list[Candidate], str | None]:
        cached_at, cached = self._read()
        age = now - cached_at if cached_at is not None else None
        if age is not None and age <= timedelta(hours=24) and len(cached) >= 5:
            return cached, None

        collected: list[Candidate] = []
        unavailable: list[str] = []
        for source in SOURCES:
            try:
                rows = fetch(source)
            except Exception:
                unavailable.append(source)
                continue
            valid = [row for row in rows if self._valid(row, source)]
            collected.extend(valid)
            if not valid:
                unavailable.append(source)
        collected = self._unique(collected)
        if len(collected) >= 5:
            self._write(now, collected)
            return collected, None
        if age is not None and age <= timedelta(days=7) and len(cached) >= 5:
            return cached, "Использован предупреждённый снимок аналитики до 7 дней."
        raise InsufficientCandidates(len(collected), unavailable)

    def _read(self) -> tuple[datetime | None, list[Candidate]]:
        if not self._path.exists():
            return None, []
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            retrieved_at = datetime.fromisoformat(str(payload["retrieved_at"]))
            candidates = [
                row
                for row in payload["candidates"]
                if isinstance(row, dict)
                and self._valid(row, str(row.get("source", "")))
            ]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None, []
        return retrieved_at, candidates

    def _write(self, now: datetime, candidates: list[Candidate]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.new")
        temporary.write_text(
            json.dumps(
                {"retrieved_at": now.isoformat(), "candidates": candidates},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._path)

    @staticmethod
    def _valid(row: Candidate, expected_source: str) -> bool:
        if row.get("source") != expected_source or expected_source not in SOURCES:
            return False
        url = row.get("url")
        published_at = row.get("published_at")
        metrics = row.get("metrics", {})
        if (
            not isinstance(url, str)
            or urlparse(url).scheme not in {"http", "https"}
            or not urlparse(url).netloc
            or not isinstance(row.get("title"), str)
            or not row["title"].strip()
            or not isinstance(published_at, str)
            or not published_at.strip()
            or not isinstance(metrics, dict)
        ):
            return False
        return all(
            isinstance(key, str) and isinstance(value, (int, float))
            for key, value in metrics.items()
        )

    @staticmethod
    def _unique(candidates: list[Candidate]) -> list[Candidate]:
        return list({str(item["url"]): item for item in candidates}.values())


class WeeklyPlan:
    CARD_FIELDS: ClassVar[tuple[str, ...]] = (
        "publish_at",
        "title",
        "trend_url",
        "audience_fit",
        "hook",
        "idea",
        "photo_recommendation",
    )

    def __init__(
        self,
        profile: dict[str, Any],
        candidates: list[Candidate],
        cards: list[Card],
        *,
        active_index: int = 0,
        revision: int = 1,
        phase: str = "review",
        mode: str | None = None,
    ) -> None:
        self.profile = profile
        self.candidates = candidates
        self.cards = cards
        self.active_index = active_index
        self.revision = revision
        self.phase = phase
        self.mode = mode
        self._validate_cards(cards)

    @classmethod
    def start(
        cls,
        profile: dict[str, Any],
        candidates: list[Candidate],
        strategize: Strategize,
    ) -> WeeklyPlan:
        if len(candidates) < 5:
            raise InsufficientCandidates(len(candidates), [])
        return cls(dict(profile), candidates, strategize(dict(profile), candidates))

    def prompt(self) -> str:
        if self.phase == "review":
            return f"Карточка {self.active_index + 1} из 3"
        if self.phase == "final":
            return "Итоговый план готов"
        return f"План утверждён: {'автоматически' if self.mode == 'auto' else 'с проверкой'}"

    def accept(self, revision: int) -> str:
        self._current(revision, "review")
        self.active_index += 1
        self.revision += 1
        if self.active_index == 3:
            self.phase = "final"
        return self.prompt()

    def replace(self, revision: int, adapter: Replace) -> str:
        self._current(revision, "review")
        replacement = adapter(
            dict(self.profile), self.candidates, self.cards, self.active_index
        )
        self._validate_cards([replacement])
        self.cards[self.active_index] = replacement
        self.revision += 1
        return self.prompt()

    def begin_edit(self, revision: int) -> str:
        self._current(revision, "review")
        self.phase = "editing"
        self.revision += 1
        return "Опишите правку только для активной карточки."

    def edit(self, feedback: str, adapter: Edit) -> str:
        if self.phase != "editing" or not feedback.strip():
            raise ValueError("План не ожидает текстовую правку")
        changed = adapter(
            dict(self.profile), dict(self.cards[self.active_index]), feedback.strip()
        )
        self._validate_cards([changed])
        self.cards[self.active_index] = changed
        self.phase = "review"
        self.revision += 1
        return self.prompt()

    def approve(self, mode: str, revision: int) -> str:
        self._current(revision, "final")
        if mode not in {"auto", "manual"}:
            raise ValueError("Режим должен быть auto или manual")
        self.mode = mode
        self.phase = "approved"
        self.revision += 1
        return self.prompt()

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "candidates": self.candidates,
            "cards": self.cards,
            "active_index": self.active_index,
            "revision": self.revision,
            "phase": self.phase,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WeeklyPlan:
        return cls(
            value["profile"],
            value["candidates"],
            value["cards"],
            active_index=int(value["active_index"]),
            revision=int(value["revision"]),
            phase=str(value["phase"]),
            mode=value.get("mode"),
        )

    def _current(self, revision: int, phase: str) -> None:
        if revision != self.revision or self.phase != phase:
            raise ValueError("Действие устарело; откройте актуальный план")

    def _validate_cards(self, cards: list[Card]) -> None:
        urls = {str(candidate["url"]) for candidate in self.candidates}
        if len(cards) not in {1, 3}:
            raise ValueError("Стратег должен вернуть ровно три карточки")
        for card in cards:
            if any(not isinstance(card.get(field), str) or not card[field].strip()
                   for field in self.CARD_FIELDS):
                raise ValueError("Карточка содержит не все обязательные поля")
            if card["trend_url"] not in urls:
                raise ValueError("Карточка должна ссылаться на реальный кандидат")


class WeeklyPlanController:
    """Один Hermes event превращает в один сохранённый переход плана."""

    def __init__(
        self,
        store: CandidateStore,
        fetch: Fetch,
        strategize: Strategize,
        replace: Replace,
        edit: Edit,
        scheduled: ScheduledPlan | None = None,
    ) -> None:
        self._store = store
        self._fetch = fetch
        self._strategize = strategize
        self._replace = replace
        self._edit = edit
        self._scheduled = scheduled

    def start(
        self,
        state: dict[str, Any],
        profile: dict[str, Any],
        now: datetime,
    ) -> str:
        candidates, warning = self._store.collect(self._fetch, now)
        plan = WeeklyPlan.start(profile, candidates[:10], self._strategize)
        state["weekly_plan"] = plan.to_dict()
        prefix = f"Найдено {len(plan.candidates)} кандидатов (цель 10)."
        if warning is not None:
            prefix += f"\n{warning}"
        return f"{prefix}\n{plan.prompt()}"

    def callback(self, data: str, state: dict[str, Any]) -> str:
        try:
            plan = self._load(state)
            parts = data.split(":")
            if len(parts) == 3:
                action, revision_text = parts[1:]
                revision = int(revision_text)
                if action == "accept":
                    response = plan.accept(revision)
                elif action == "replace":
                    response = plan.replace(revision, self._replace)
                elif action == "edit":
                    response = plan.begin_edit(revision)
                else:
                    raise ValueError("Неизвестное действие плана")
            elif len(parts) == 4 and parts[1] == "approve":
                response = plan.approve(parts[2], int(parts[3]))
            else:
                raise ValueError("Неизвестное действие плана")
        except (KeyError, TypeError, ValueError) as error:
            return str(error)
        state["weekly_plan"] = plan.to_dict()
        if plan.phase == "approved" and self._scheduled is not None:
            return f"{response}\n{self._scheduled.connect_approved(state, datetime.now().astimezone())}"
        return response

    def message(self, text: str, state: dict[str, Any]) -> str | None:
        value = state.get("weekly_plan")
        if not isinstance(value, dict) or value.get("phase") != "editing":
            return None
        plan = WeeklyPlan.from_dict(value)
        try:
            response = plan.edit(text, self._edit)
        except ValueError as error:
            return str(error)
        state["weekly_plan"] = plan.to_dict()
        return response

    def cron(
        self, state: dict[str, Any], profile: dict[str, Any], now: datetime
    ) -> str | None:
        """Один cron-тик: воскресный черновик либо одно напоминание."""
        local = now.astimezone(ZoneInfo(str(profile["timezone"])))
        week = f"{local.isocalendar().year}-{local.isocalendar().week}"
        plan = state.get("weekly_plan")
        if (
            local.weekday() == 6
            and local.hour >= 10
            and not (isinstance(plan, dict) and plan.get("phase") == "approved")
            and state.get("draft_week") != week
        ):
            opening = self.start(state, profile, now)
            state["draft_week"] = week
            return f"Создан черновик недельного плана.\n{opening}"
        plan = state.get("weekly_plan")
        if (
            local.weekday() == 0
            and isinstance(plan, dict)
            and plan.get("phase") != "approved"
            and state.get("reminder_week") != week
        ):
            state["reminder_week"] = week
            return "Напоминание: утвердите недельный план."
        return None

    @staticmethod
    def _load(state: dict[str, Any]) -> WeeklyPlan:
        value = state.get("weekly_plan")
        if not isinstance(value, dict):
            raise ValueError("Сначала откройте /plan")
        return WeeklyPlan.from_dict(value)


class ScheduledPlan:
    """Тонко связывает утверждённый план с общей очередью ComfyUI."""

    def __init__(self, queue: TaskQueue) -> None:
        self._queue = queue

    def connect_approved(self, state: dict[str, Any], now: datetime) -> str:
        plan = self._approved(state)
        timezone = ZoneInfo(str(plan["profile"]["timezone"]))
        fallback = self._fallback(now.astimezone(timezone))
        for index, card in enumerate(plan["cards"], 1):
            try:
                publish_at = datetime.fromisoformat(str(card["publish_at"]))
                if publish_at.tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError):
                publish_at = fallback[index - 1]
                card["publish_at"] = publish_at.isoformat()
            self._queue.create_scheduled(
                self._job_id(plan, index),
                str(plan["mode"]),
                publish_at,
                f"{card['hook']}\n{card['idea']}",
                str(card["idea"]),
            )
        collection = state.setdefault(
            "photo_collection", {"active": 1, "pending": None, "replace": False}
        )
        active = int(collection["active"])
        return f"Пришлите фото для карточки {active} из 3."

    def receive_photo(
        self, state: dict[str, Any], source_path: str, now: datetime
    ) -> str:
        plan = self._approved(state)
        collection = self._collection(state)
        active = int(collection["active"])
        job = self._queue.get(self._job_id(plan, active))
        if job["status"] in {"generating", "result_ready", *TERMINAL}:
            raise ValueError("После старта требуется отмена и новая задача")
        collection["pending"] = source_path
        replacing = job["source_path"] is not None
        collection["replace"] = replacing
        noun = "замену фото" if replacing else "фото"
        return f"Подтвердите {noun} для карточки {active}."

    def confirm_photo(self, state: dict[str, Any], now: datetime) -> str:
        plan = self._approved(state)
        collection = self._collection(state)
        source = collection.get("pending")
        if not isinstance(source, str):
            raise ValueError("Сначала пришлите фото")
        active = int(collection["active"])
        self._queue.set_photo(self._job_id(plan, active), source, now)
        collection["pending"] = None
        collection["replace"] = False
        if active < 3:
            collection["active"] = active + 1
            return f"Пришлите фото для карточки {active + 1} из 3."
        collection["active"] = 1
        return "Все три фото подтверждены."

    def set_mode(
        self,
        state: dict[str, Any],
        card_index: int,
        mode: str,
        now: datetime,
    ) -> None:
        plan = self._approved(state)
        job = self._queue.get(self._job_id(plan, card_index))
        self._before_due_day(job, now)
        self._queue.update_schedule(
            str(job["job_id"]), datetime.fromisoformat(str(job["publish_at"])), mode=mode
        )

    def reschedule(
        self,
        state: dict[str, Any],
        card_index: int,
        publish_at: datetime,
        now: datetime,
    ) -> None:
        plan = self._approved(state)
        job = self._queue.get(self._job_id(plan, card_index))
        self._before_due_day(job, now)
        self._queue.update_schedule(str(job["job_id"]), publish_at)
        plan["cards"][card_index - 1]["publish_at"] = publish_at.isoformat()

    @staticmethod
    def _before_due_day(job: Any, now: datetime) -> None:
        due = datetime.fromisoformat(str(job["due_at"]))
        if now.astimezone(due.tzinfo).date() >= due.date():
            raise ValueError("Изменение разрешено только до дня генерации")

    @staticmethod
    def _fallback(now: datetime) -> list[datetime]:
        result: list[datetime] = []
        cursor = now.date()
        while len(result) < 3:
            cursor += timedelta(days=1)
            if cursor.weekday() in {1, 3, 5}:
                result.append(
                    datetime(cursor.year, cursor.month, cursor.day, 18, tzinfo=now.tzinfo)
                )
        return result

    @staticmethod
    def _approved(state: dict[str, Any]) -> dict[str, Any]:
        plan = state.get("weekly_plan")
        if not isinstance(plan, dict) or plan.get("phase") != "approved":
            raise ValueError("Сначала утвердите недельный план")
        return plan

    @staticmethod
    def _collection(state: dict[str, Any]) -> dict[str, Any]:
        value = state.get("photo_collection")
        if not isinstance(value, dict):
            raise ValueError("Сначала подключите утверждённый план")
        return value

    @staticmethod
    def _job_id(plan: dict[str, Any], index: int) -> str:
        return f"plan-{plan['revision']}-{index}"
