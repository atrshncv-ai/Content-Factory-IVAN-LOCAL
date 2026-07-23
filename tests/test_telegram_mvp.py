"""Контракт Telegram photo → LTX workflow для MVP."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.orchestrator import render_workflow
from core.telegram_mvp import TelegramRequest, parse_request


def _update(chat_id: int = 941296693) -> dict[str, object]:
    return {
        "update_id": 42,
        "message": {
            "chat": {"id": chat_id},
            "caption": "Создай атмосферное видео о Самаре ночью",
            "photo": [
                {"file_id": "small", "width": 90, "height": 90},
                {"file_id": "large", "width": 1280, "height": 1280},
            ],
        },
    }


def test_parse_request_accepts_only_allowed_chat() -> None:
    assert parse_request(_update(chat_id=1), allowed_chat_id=941296693) is None
    assert parse_request(_update(), allowed_chat_id=941296693) == TelegramRequest(
        update_id=42,
        chat_id=941296693,
        file_id="large",
        topic="Создай атмосферное видео о Самаре ночью",
    )


@pytest.mark.parametrize(
    "field",
    ["photo", "caption"],
)
def test_parse_request_requires_photo_and_caption(field: str) -> None:
    update = _update()
    message = update["message"]
    assert isinstance(message, dict)
    message.pop(field)
    assert parse_request(update, allowed_chat_id=941296693) is None


def test_devmode_uses_uploaded_telegram_photo(tmp_path: Path) -> None:
    output = render_workflow(
        Path("КОД COMFY/LTX-2.3_Triple_Sampler_DevMode.json"),
        "tg-42",
        "",
        str(tmp_path),
        video_prompt="Slow cinematic camera movement through neon reflections",
        first_frame_image="telegram_tg-42.jpg",
    )
    workflow = json.loads(output.read_text(encoding="utf-8"))

    assert workflow["5029"]["inputs"]["image"] == "telegram_tg-42.jpg"
    assert workflow["5314"]["inputs"]["text"].startswith("Slow cinematic")
    assert workflow["5294"]["inputs"]["save_output"] is True
    assert workflow["5294"]["inputs"]["filename_prefix"] == "LTX_Output_tg-42_"
