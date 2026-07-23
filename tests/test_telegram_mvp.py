"""Контракт Telegram photo → LTX workflow для MVP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.db import Database
from core.orchestrator import render_workflow
from core.telegram_mvp import (
    MVPWorker,
    TelegramRequest,
    validate_queue_result,
    parse_request,
    parse_video_plan,
)


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


def test_parse_video_plan_accepts_fenced_json_and_rejects_empty() -> None:
    assert parse_video_plan(
        '```json\n{"scenario":"Night city","video_prompt":"Slow dolly shot"}\n```'
    ) == ("Night city", "Slow dolly shot")
    with pytest.raises(ValueError):
        parse_video_plan('{"scenario":"","video_prompt":"x"}')


def test_queue_result_rejects_node_validation_errors() -> None:
    validate_queue_result('{"prompt_id":"ok","node_errors":{}}')
    with pytest.raises(RuntimeError, match="5330"):
        validate_queue_result(
            '{"prompt_id":"bad","node_errors":{"5330":{"errors":[]}}}'
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
    assert workflow["5330"]["inputs"]["aspect_ratio"] == "Custom"
    assert workflow["5294"]["inputs"]["save_output"] is True
    assert workflow["5294"]["inputs"]["filename_prefix"] == "LTX_Output_tg-42_"
    assert sum(
        node.get("class_type") == "VHS_VideoCombine"
        for node in workflow.values()
    ) == 10


class _Telegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, Path, str]] = []

    def updates(self) -> list[dict[str, Any]]:
        return [_update()]

    def download_photo(self, file_id: str, destination: Path) -> None:
        assert file_id == "large"
        destination.write_bytes(b"\xff\xd8\xff\xe0photo")

    def send_video(self, chat_id: int, video: Path, caption: str) -> None:
        self.sent.append((chat_id, video, caption))


def test_worker_is_idempotent_and_advances_one_stage_per_tick(
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "mvp.db"))
    db.init_schema()
    telegram = _Telegram()

    def plan(topic: str) -> tuple[str, str]:
        return f"Сценарий: {topic}", "Cinematic slow camera movement"

    def submit(job_id: str, image: Path, prompt: str) -> None:
        assert job_id == "tg-42"
        assert image.exists()
        assert prompt.startswith("Cinematic")

    def poll() -> None:
        db.upsert_job(
            "tg-42", "done", file_path="LTX_Output_tg-42_00001_.mp4"
        )

    def download_video(filename: str, destination: Path) -> None:
        assert filename.startswith("LTX_Output_tg-42_")
        destination.write_bytes(b"\x00\x00\x00\x18ftypisomvideo")

    worker = MVPWorker(
        db=db,
        telegram=telegram,
        allowed_chat_id=941296693,
        work_dir=tmp_path / "work",
        plan=plan,
        submit=submit,
        poll=poll,
        download_video=download_video,
    )

    assert worker.run_once()["stage"] == "received"
    assert worker.run_once()["stage"] == "planned"
    assert worker.run_once()["stage"] == "submitted"
    assert worker.run_once()["stage"] == "polled"
    assert worker.run_once()["stage"] == "delivered"
    job = db.fetch_job("tg-42")
    assert job is not None and job["status"] == "delivered"
    assert len(telegram.sent) == 1
    assert "Полный цикл:" in telegram.sent[0][2]
    assert not list((tmp_path / "work").glob("*"))

    assert worker.run_once()["stage"] == "idle"
    assert len(telegram.sent) == 1


def test_worker_polls_telegram_job_behind_older_generating_job(
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "mvp.db"))
    db.init_schema()
    db.upsert_job("trend-old", "generating")
    db.upsert_job("tg-42", "generating")
    calls: list[str] = []
    worker = MVPWorker(
        db=db,
        telegram=_Telegram(),
        allowed_chat_id=941296693,
        work_dir=tmp_path / "work",
        plan=lambda _: ("scenario", "prompt"),
        submit=lambda *_: None,
        poll=lambda: calls.append("poll"),
        download_video=lambda *_: None,
    )

    assert worker.run_once() == {"stage": "polled", "job_id": "tg-42"}
    assert calls == ["poll"]
