"""Контракты веб-аналитики и последовательного согласования недельного плана."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.weekly_plan import (
    SOURCES,
    CandidateStore,
    InsufficientCandidates,
    WeeklyPlan,
)

NOW = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
PROFILE = {
    "brand": "Иван",
    "niche": "маркетинг",
    "audience": "предприниматели",
    "tone": "экспертный",
    "timezone": "Europe/Samara",
}


def _candidate(index: int, source: str = "youtube") -> dict[str, Any]:
    return {
        "source": source,
        "title": f"Тренд {index}",
        "url": f"https://example.com/trend/{index}",
        "published_at": f"2026-07-{20 + index:02d}",
        "metrics": {"views": index * 100} if index % 2 else {},
    }


def _card(candidate: dict[str, Any], suffix: str = "") -> dict[str, str]:
    return {
        "publish_at": "2026-08-04T18:00:00+04:00",
        "title": f"Карточка {candidate['title']}{suffix}",
        "trend_url": str(candidate["url"]),
        "audience_fit": "Полезно предпринимателям",
        "hook": "Не делайте эту ошибку",
        "idea": "Показать решение за три шага",
        "photo_recommendation": "Портрет по пояс",
    }


def test_candidate_contract_uses_all_sources_and_only_available_metrics(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def fetch(source: str) -> list[dict[str, Any]]:
        calls.append(source)
        return [_candidate(len(calls), source)]

    candidates, warning = CandidateStore(tmp_path / "analytics.json").collect(
        fetch, NOW
    )

    assert calls == list(SOURCES)
    assert {item["source"] for item in candidates} == set(SOURCES)
    assert warning is None
    assert candidates[0]["url"].startswith("https://")
    assert "published_at" in candidates[0]
    assert "likes" not in candidates[0]["metrics"]
    saved = json.loads((tmp_path / "analytics.json").read_text(encoding="utf-8"))
    assert saved["retrieved_at"] == NOW.isoformat()


def test_cache_is_fresh_for_24_hours_and_warned_fallback_expires_at_7_days(
    tmp_path: Path,
) -> None:
    path = tmp_path / "analytics.json"
    store = CandidateStore(path)
    ten = [_candidate(index) for index in range(1, 11)]
    path.write_text(
        json.dumps({"retrieved_at": NOW.isoformat(), "candidates": ten}),
        encoding="utf-8",
    )

    def broken(_source: str) -> list[dict[str, Any]]:
        raise OSError("offline")

    cached, warning = store.collect(broken, NOW + timedelta(hours=23, minutes=59))
    assert cached == ten
    assert warning is None

    stale, warning = store.collect(broken, NOW + timedelta(days=6))
    assert stale == ten
    assert warning is not None and "снимок" in warning

    with pytest.raises(InsufficientCandidates, match="недоступны"):
        store.collect(broken, NOW + timedelta(days=7, seconds=1))


def test_invalid_or_fewer_than_five_candidates_stop_without_invention(
    tmp_path: Path,
) -> None:
    def fetch(source: str) -> list[dict[str, Any]]:
        if source == "youtube":
            return [_candidate(index) for index in range(1, 5)]
        if source == "instagram":
            raise OSError("best effort unavailable")
        return [{"source": source, "title": "bad", "url": "not-real"}]

    with pytest.raises(InsufficientCandidates) as error:
        CandidateStore(tmp_path / "analytics.json").collect(fetch, NOW)

    assert error.value.available == 4
    assert "instagram" in error.value.unavailable_sources


def test_strategist_gets_profile_and_ten_candidates_then_reviews_sequentially(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}
    candidates = [_candidate(index) for index in range(1, 11)]

    def strategize(
        profile: dict[str, Any], pool: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        seen.update(profile)
        assert len(pool) == 10
        return [_card(item) for item in pool[:3]]

    plan = WeeklyPlan.start(PROFILE, candidates, strategize)
    assert seen["audience"] == "предприниматели"
    assert plan.active_index == 0
    assert plan.revision == 1
    assert "Карточка 1 из 3" in plan.prompt()

    assert plan.accept(1) == "Карточка 2 из 3"
    assert plan.active_index == 1
    assert plan.accept(2) == "Карточка 3 из 3"
    assert plan.accept(3) == "Итоговый план готов"
    assert plan.phase == "final"


def test_replace_and_text_edit_change_only_active_card_and_stale_actions_lose(
    tmp_path: Path,
) -> None:
    candidates = [_candidate(index) for index in range(1, 11)]
    plan = WeeklyPlan.start(
        PROFILE, candidates, lambda _profile, pool: [_card(item) for item in pool[:3]]
    )
    original = [dict(card) for card in plan.cards]

    def replace(
        profile: dict[str, Any],
        pool: list[dict[str, Any]],
        cards: list[dict[str, str]],
        active: int,
    ) -> dict[str, str]:
        assert profile["brand"] == "Иван"
        assert active == 0
        return _card(pool[5], " замена")

    assert "Карточка 1 из 3" in plan.replace(1, replace)
    replace_revision = plan.revision
    assert plan.cards[0] != original[0]
    assert plan.cards[1:] == original[1:]
    assert plan.begin_edit(replace_revision) == "Опишите правку"

    def edit(
        profile: dict[str, Any], card: dict[str, str], feedback: str
    ) -> dict[str, str]:
        assert profile["tone"] == "экспертный"
        changed = dict(card)
        changed["hook"] = feedback
        return changed

    before_edit = [dict(card) for card in plan.cards]
    assert "Карточка 1 из 3" in plan.edit("Новый хук", edit)
    assert plan.cards[0]["hook"] == "Новый хук"
    assert plan.cards[1:] == before_edit[1:]
    with pytest.raises(ValueError, match="устарело"):
        plan.accept(replace_revision)


def test_final_approval_sets_one_mode_without_scheduling_or_stale_overwrite() -> None:
    candidates = [_candidate(index) for index in range(1, 11)]
    plan = WeeklyPlan.start(
        PROFILE, candidates, lambda _profile, pool: [_card(item) for item in pool[:3]]
    )
    for revision in (1, 2, 3):
        plan.accept(revision)
    final_revision = plan.revision

    assert plan.approve("manual", final_revision) == "План утверждён: с проверкой"
    assert plan.mode == "manual"
    assert plan.phase == "approved"
    assert "render" not in plan.to_dict()
    with pytest.raises(ValueError, match="устарело"):
        plan.approve("auto", final_revision)
