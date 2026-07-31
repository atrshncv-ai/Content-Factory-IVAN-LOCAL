"""Одношаговый Telegram photo-to-video MVP."""

from __future__ import annotations

import json
import hashlib
import mimetypes
import re
import subprocess
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from openai.types.chat import ChatCompletionMessageParam

from core.config import Settings
from core.db import Database
from core.llm import LLMClient
from core.orchestrator import render_workflow
from core.poller import poll_once


@dataclass(frozen=True)
class TelegramRequest:
    update_id: int
    chat_id: int
    file_id: str
    topic: str


class TelegramGateway(Protocol):
    def updates(self) -> list[dict[str, Any]]: ...

    def download_photo(self, file_id: str, destination: Path) -> None: ...

    def send_video(self, chat_id: int, video: Path, caption: str) -> None: ...


def assert_workflow_checksum(path: Path, approved: str | None) -> str:
    """Сверяет API workflow с одобренным SHA-256, если он настроен."""
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if approved is not None and actual.lower() != approved.strip().lower():
        raise ValueError("Workflow отклонён: контрольная сумма не совпала")
    return actual


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in range(0xC0, 0xC4):
            return int.from_bytes(data[offset + 7 : offset + 9], "big"), int.from_bytes(
                data[offset + 5 : offset + 7], "big"
            )
        length = int.from_bytes(data[offset + 2 : offset + 4], "big")
        if length < 2:
            break
        offset += length + 2
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if data[12:16] == b"VP8X" and len(data) >= 30:
        return (
            1 + int.from_bytes(data[24:27], "little"),
            1 + int.from_bytes(data[27:30], "little"),
        )
    if data[12:16] == b"VP8 " and len(data) >= 30:
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    if data[12:16] == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def validate_photo(path: Path) -> tuple[str, int, int]:
    """Проверяет читаемый JPEG/PNG/WebP и минимальную сторону 720 px."""
    data = path.read_bytes()
    kind = ""
    size: tuple[int, int] | None = None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        kind = "png"
        size = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    elif data.startswith(b"\xff\xd8"):
        kind, size = "jpeg", _jpeg_size(data)
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        kind, size = "webp", _webp_size(data)
    if size is None or not all(size):
        raise ValueError("Нужно читаемое фото JPEG, PNG или WebP")
    if min(size) < 720:
        raise ValueError("Минимальная сторона фото — 720 px")
    return kind, size[0], size[1]


def parse_request(
    update: dict[str, Any], allowed_chat_id: int
) -> TelegramRequest | None:
    """Принимает только подписанное фото от разрешённого Telegram-чата."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    photos = message.get("photo")
    topic = message.get("caption")
    if (
        not isinstance(chat, dict)
        or chat.get("id") != allowed_chat_id
        or not isinstance(photos, list)
        or not photos
        or not isinstance(topic, str)
        or not topic.strip()
    ):
        return None
    largest = max(
        (photo for photo in photos if isinstance(photo, dict)),
        key=lambda photo: int(photo.get("width", 0)) * int(photo.get("height", 0)),
        default=None,
    )
    if largest is None or not largest.get("file_id"):
        return None
    return TelegramRequest(
        update_id=int(update["update_id"]),
        chat_id=allowed_chat_id,
        file_id=str(largest["file_id"]),
        topic=topic.strip(),
    )


def parse_video_plan(raw: str) -> tuple[str, str]:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    data = json.loads(text)
    scenario = str(data.get("scenario", "")).strip()
    prompt = str(data.get("video_prompt", "")).strip()
    if not scenario or not prompt:
        raise ValueError("LLM returned an empty scenario or video_prompt")
    return scenario, prompt


def validate_queue_result(raw: str) -> None:
    result = json.loads(raw)
    errors = result.get("node_errors")
    if errors:
        raise RuntimeError(f"ComfyUI rejected workflow nodes: {errors}")


def _multipart(
    fields: dict[str, str], field_name: str, path: Path
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f"\r\n\r\n{value}\r\n".encode()
        )
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; '
        f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
        + path.read_bytes()
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class TelegramHTTP:
    def __init__(self, token: str) -> None:
        self._api = f"https://api.telegram.org/bot{token}"
        self._file_api = f"https://api.telegram.org/file/bot{token}"

    def _call(self, method: str, query: dict[str, str] | None = None) -> Any:
        url = f"{self._api}/{method}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.loads(response.read().decode())
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} failed")
        return payload["result"]

    def updates(self, offset: int = 0) -> list[dict[str, Any]]:
        result = self._call(
            "getUpdates",
            {
                "timeout": "0",
                "offset": str(offset),
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
        )
        return [item for item in result if isinstance(item, dict)]

    def set_commands(self, commands: list[dict[str, str]]) -> None:
        self._call("setMyCommands", {"commands": json.dumps(commands, ensure_ascii=False)})

    def send_message(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        query = {"chat_id": str(chat_id), "text": text}
        if keyboard:
            query["reply_markup"] = json.dumps(
                {"inline_keyboard": keyboard}, ensure_ascii=False
            )
        self._call("sendMessage", query)

    def answer_callback(self, callback_id: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id})

    def download_photo(self, file_id: str, destination: Path) -> None:
        info = self._call("getFile", {"file_id": file_id})
        file_path = str(info["file_path"])
        with urllib.request.urlopen(
            f"{self._file_api}/{urllib.parse.quote(file_path)}", timeout=60
        ) as response:
            destination.write_bytes(response.read())

    def send_video(self, chat_id: int, video: Path, caption: str) -> None:
        body, content_type = _multipart(
            {"chat_id": str(chat_id), "caption": caption}, "video", video
        )
        request = urllib.request.Request(
            f"{self._api}/sendVideo",
            data=body,
            headers={"Content-Type": content_type},
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode())
        if not payload.get("ok"):
            raise RuntimeError("Telegram sendVideo failed")


class MVPWorker:
    """Продвигает одно Telegram-задание ровно на одну Kanban-стадию."""

    def __init__(
        self,
        db: Database,
        telegram: TelegramGateway,
        allowed_chat_id: int,
        work_dir: Path,
        plan: Callable[[str], tuple[str, str]],
        submit: Callable[[str, Path, str], None],
        poll: Callable[[], object],
        download_video: Callable[[str, Path], None],
        health_check: Callable[[], object] = lambda: None,
        store_on_node1: Callable[[str, Path, Path], None] = lambda *_: None,
        delivery_chat_ids: frozenset[int] | None = None,
    ) -> None:
        self._db = db
        self._telegram = telegram
        self._allowed_chat_id = allowed_chat_id
        self._work_dir = work_dir
        self._plan = plan
        self._submit = submit
        self._poll = poll
        self._download_video = download_video
        self._health_check = health_check
        self._store_on_node1 = store_on_node1
        self._delivery_chat_ids = delivery_chat_ids or frozenset({allowed_chat_id})

    def run_once(self) -> dict[str, str]:
        self._work_dir.mkdir(parents=True, exist_ok=True)
        done = next(
            (
                job
                for job in self._db.fetch_jobs_by_status("done")
                if str(job["job_id"]).startswith("tg-")
            ),
            None,
        )
        if done is not None:
            try:
                self._deliver(done)
            except Exception as error:
                self._db.upsert_job(str(done["job_id"]), "delivery_failed")
                send_message = getattr(self._telegram, "send_message", None)
                if callable(send_message):
                    reason = type(error).__name__
                    node1_path = str(done["file_path"])
                    for chat_id in sorted(self._delivery_chat_ids):
                        send_message(
                            chat_id,
                            f"{done['job_id']}: Telegram-файл не доставлен "
                            f"({reason}). Результат сохранён на Node 1: {node1_path}",
                        )
                return {"stage": "delivery_failed", "job_id": str(done["job_id"])}
            return {"stage": "delivered", "job_id": str(done["job_id"])}

        generating = next(
            (
                job
                for job in self._db.fetch_jobs_by_status("generating")
                if str(job["job_id"]).startswith("tg-")
            ),
            None,
        )
        if generating is not None:
            self._poll()
            return {"stage": "polled", "job_id": str(generating["job_id"])}

        ready = self._db.fetch_jobs_by_status("tg_ready")
        if ready:
            job = ready[0]
            self._health_check()
            self._submit(
                str(job["job_id"]),
                Path(str(job["source_path"] or job["file_path"])),
                str(job["video_prompt"]),
            )
            self._db.upsert_job(str(job["job_id"]), "generating")
            return {"stage": "submitted", "job_id": str(job["job_id"])}

        received = self._db.fetch_jobs_by_status("tg_received")
        if received:
            job = received[0]
            scenario, prompt = self._plan(str(job["trend_title"]))
            self._db.upsert_job(
                str(job["job_id"]),
                "tg_ready",
                concept=scenario,
                video_prompt=prompt,
            )
            return {"stage": "planned", "job_id": str(job["job_id"])}

        for update in self._telegram.updates():
            request = parse_request(update, self._allowed_chat_id)
            if request is None:
                continue
            job_id = f"tg-{request.update_id}"
            if self._db.fetch_job(job_id) is not None:
                continue
            image = self._work_dir / f"{job_id}.jpg"
            self._telegram.download_photo(request.file_id, image)
            try:
                validate_photo(image)
            except ValueError:
                image.unlink(missing_ok=True)
                return {"stage": "invalid_photo", "job_id": job_id}
            self._db.upsert_job(
                job_id,
                "tg_received",
                file_path=str(image),
                source_path=str(image),
                trend_title=request.topic,
                trend_url=f"telegram:{request.update_id}",
            )
            return {"stage": "received", "job_id": job_id}
        return {"stage": "idle"}

    def _deliver(self, job: Any) -> None:
        job_id = str(job["job_id"])
        filename = Path(str(job["file_path"])).name
        video = self._work_dir / filename
        self._download_video(filename, video)
        header = video.read_bytes()[:12]
        if b"ftyp" not in header:
            raise ValueError("ComfyUI output is not an MP4 file")
        created = datetime.fromisoformat(str(job["created_at"])).replace(
            tzinfo=timezone.utc
        )
        elapsed = int((datetime.now(timezone.utc) - created).total_seconds())
        source = Path(str(job["source_path"]))
        self._store_on_node1(job_id, source, video)
        for chat_id in sorted(self._delivery_chat_ids):
            self._telegram.send_video(
                chat_id,
                video,
                f"{job_id}: видео готово. Полный цикл: "
                f"{elapsed // 60} мин {elapsed % 60} сек.",
            )
        for temporary in self._work_dir.glob(f"{job_id}*"):
            temporary.unlink(missing_ok=True)
        video.unlink(missing_ok=True)
        self._db.upsert_job(job_id, "delivered")


class ComfyHTTP:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def upload_image(self, source: Path, name: str, subfolder: str = "") -> None:
        upload = source.with_name(Path(name).name)
        upload.write_bytes(source.read_bytes())
        try:
            body, content_type = _multipart(
                {
                    "type": "input",
                    "overwrite": "true",
                    "subfolder": subfolder,
                },
                "image",
                upload,
            )
            request = urllib.request.Request(
                f"{self._base_url}/upload/image",
                data=body,
                headers={"Content-Type": content_type},
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode())
            if result.get("name") != name:
                raise RuntimeError("ComfyUI returned an unexpected input filename")
        finally:
            upload.unlink(missing_ok=True)

    def health(self) -> None:
        with urllib.request.urlopen(
            f"{self._base_url}/system_stats", timeout=30
        ) as response:
            if response.status != 200:
                raise RuntimeError("ComfyUI health-check failed")

    def archive(self, job_id: str, source: Path, result: Path) -> None:
        now = datetime.now(timezone.utc)
        subfolder = f"{now.year}/{now.isocalendar().week:02d}/{job_id}"
        self.upload_image(source, f"source{source.suffix.lower()}", subfolder)
        self.upload_image(result, "result.mp4", subfolder)

    def download_output(self, filename: str, destination: Path) -> None:
        query = urllib.parse.urlencode({"filename": filename, "type": "output"})
        with urllib.request.urlopen(
            f"{self._base_url}/view?{query}", timeout=300
        ) as response:
            destination.write_bytes(response.read())


def _make_plan(llm: LLMClient, topic: str) -> tuple[str, str]:
    messages: list[ChatCompletionMessageParam] = [
        {
            "role": "user",
            "content": (
                "Create exactly one short vertical social-video scenario in Russian "
                "with sections Хук (0–2 с), Развитие (2–8 с), Завершение (8–10+ с) "
                "and a Russian spoken line of at most 20 words. Also create exactly "
                "one technical English image-to-video prompt for LTX-2.3. Topic: "
                f"{topic!r}. Describe camera movement and subject motion while "
                "preserving the person/object from the supplied first frame. "
                'Return only JSON: {"scenario":"...","video_prompt":"..."}'
            ),
        }
    ]
    return parse_video_plan(llm.chat(messages))


def build_worker(settings: Settings, db: Database) -> MVPWorker:
    if settings.telegram_bot_token is None or settings.telegram_chat_id is None:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be in .env")
    if settings.comfyui_workflow_sha256 is None:
        raise ValueError("COMFYUI_WORKFLOW_SHA256 must be in .env")
    telegram = TelegramHTTP(settings.telegram_bot_token.get_secret_value())
    comfy = ComfyHTTP(settings.comfyui_server_url)
    llm = LLMClient(settings)
    work_dir = Path(settings.telegram_mvp_dir)

    def submit(job_id: str, image: Path, prompt: str) -> None:
        assert_workflow_checksum(
            Path(settings.comfyui_workflow_template),
            settings.comfyui_workflow_sha256,
        )
        input_name = f"telegram_{job_id}{image.suffix.lower() or '.jpg'}"
        comfy.upload_image(image, input_name)
        workflow = render_workflow(
            Path(settings.comfyui_workflow_template),
            job_id,
            "",
            str(work_dir),
            video_prompt=prompt,
            first_frame_image=input_name,
        )
        result = subprocess.run(
            [
                settings.comfyui_command,
                "--url",
                settings.comfyui_server_url,
                "--json",
                "queue",
                "prompt",
                "--workflow",
                str(workflow),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        validate_queue_result(result.stdout)

    return MVPWorker(
        db=db,
        telegram=telegram,
        allowed_chat_id=settings.telegram_chat_id,
        work_dir=work_dir,
        plan=lambda topic: _make_plan(llm, topic),
        submit=submit,
        poll=lambda: poll_once(db, settings),
        download_video=comfy.download_output,
        health_check=comfy.health,
        store_on_node1=comfy.archive,
        delivery_chat_ids=frozenset(
            admin_id
            for admin_id in (
                settings.telegram_admin_id_1,
                settings.telegram_admin_id_2,
                settings.telegram_chat_id,
            )
            if admin_id is not None
        ),
    )


def main() -> None:
    settings = Settings()
    db = Database(settings.db_path)
    db.init_schema()
    try:
        print(json.dumps(build_worker(settings, db).run_once(), ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
