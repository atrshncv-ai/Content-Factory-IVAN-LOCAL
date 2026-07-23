"""Тесты Главного Оркестратора (core.orchestrator).

Проверяют ИСКЛЮЧИТЕЛЬНО логику переходов машины состояний Kanban:
все субагенты (Parser, Planner, Prompter, Evaluator, Publisher) и отправка
на Node 1 замоканы. Оркестратор не содержит while-циклов: один запуск =
один шаг по доске + чистый возврат summary (Clean Context, exit(0)).

Интеграция с Node 1: Оркестратор рендерит workflow-JSON из шаблона
(подстановка photo_prompt в CLIPTextEncode) и вызывает
`cli-anything-comfyui queue prompt --workflow PATH`.
"""
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from core.db import Database
from core.orchestrator import (
    PROMPT_PLACEHOLDER,
    Orchestrator,
    _build_node1_command,
    _first_frame_image_name,
    _job_id_from_trend,
)
from core.publisher import PublishError
from tests.conftest import IsolatedSettings


class _FakeParser:
    """Парсер-заглушка: отдаёт заданные тренды, считает вызовы."""

    def __init__(self, trends: list[dict[str, object]]) -> None:
        self._trends = trends
        self.calls = 0

    def fetch_trends(self, query: str, limit: int = 10) -> list[dict[str, object]]:
        self.calls += 1
        return self._trends


class _FakePlanner:
    """Сценарист-заглушка: записывает концепцию в БД, считает вызовы."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self.calls: list[str] = []

    def plan_job(self, job_id: str) -> dict[str, str]:
        self.calls.append(job_id)
        concept = f"Виральная концепция для {job_id}"
        self._db.upsert_job(job_id, "planned", concept=concept)
        return {"job_id": job_id, "concept": concept, "status": "planned"}


class _FakePrompter:
    """Промпт-инженер-заглушка: записывает промпты в БД, считает вызовы."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self.calls: list[str] = []

    def create_prompts(self, job_id: str) -> dict[str, str]:
        self.calls.append(job_id)
        video = f"Video prompt for {job_id}"
        photo = f"Photo prompt for {job_id}"
        self._db.upsert_job(job_id, "ready_for_render", video_prompt=video, photo_prompt=photo)
        return {"job_id": job_id, "video_prompt": video, "photo_prompt": photo, "status": "ready_for_render"}


class _FakeEvaluator:
    """Evaluator-заглушка: записывает вызовы и двигает задание в approved."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self.calls: list[str] = []

    def evaluate_job(self, job_id: str) -> dict[str, Any]:
        self.calls.append(job_id)
        self._db.upsert_job(job_id, "approved")
        return {"approved": True}


class _FakePublisher:
    """Publisher-заглушка: отдаёт post_id; падает на файлах с меткой fail_on.

    fail_metrics=True — collect_metrics падает (публикация должна устоять).
    """

    def __init__(self, fail_on: str | None = None, fail_metrics: bool = False) -> None:
        self.calls: list[str] = []
        self.metrics_calls: list[tuple[str, str]] = []
        self._fail_on = fail_on
        self._fail_metrics = fail_metrics

    def publish(self, media_path: str, caption: str) -> str:
        self.calls.append(media_path)
        if self._fail_on and self._fail_on in media_path:
            raise PublishError("CLI down")
        return "post-1"

    def collect_metrics(self, post_id: str, period: str) -> dict[str, Any]:
        self.metrics_calls.append((post_id, period))
        if self._fail_metrics:
            raise PublishError("metrics CLI down")
        return {
            "post_id": post_id, "period": period,
            "likes": 0, "comments": 0, "reach": 0,
        }


class _FakeSubmit:
    """Устаревшая заглушка — оставлена для обратной совместимости тестов.

    В гибридной модели A+B workflow-файлы пишутся в jobs_dir (pull-модель),
    а не отправляются через push. Эта заглушка больше не используется
    в новых тестах.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.paths: list[str] = []

    def __call__(self, job_id: str, workflow_path: str) -> None:
        self.calls.append(job_id)
        self.paths.append(workflow_path)


TEMPLATE_STUB = {
    "1": {"class_type": "CheckpointLoaderSimple",
          "inputs": {"ckpt_name": "model.safetensors"}},
    "6": {"class_type": "CLIPTextEncode",
          "inputs": {"text": PROMPT_PLACEHOLDER, "clip": ["1", 1]}},
    "3": {"class_type": "KSampler", "inputs": {"positive": ["6", 0]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "factory"}},
}


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(str(tmp_path / "ops.db"))
    database.init_schema()
    yield database
    database.close()


@pytest.fixture
def media(tmp_path: Path) -> Path:
    file = tmp_path / "final.png"
    file.write_bytes(b"\x89PNG fake")
    return file


def make_orchestrator(
    db: Database,
    tmp_path: Path,
    parser: _FakeParser | None = None,
    planner: _FakePlanner | None = None,
    prompter: _FakePrompter | None = None,
    evaluator: _FakeEvaluator | None = None,
    publisher: _FakePublisher | None = None,
    workflow_template: str | None = None,
    jobs_dir: str | None = None,
    poller: Callable[[], dict[str, int]] | None = None,
) -> Orchestrator:
    """Оркестратор со всеми замоканными субагентами и tmp-шаблоном workflow."""
    template_path = tmp_path / "workflow_template.json"
    if workflow_template is None:
        template_path.write_text(json.dumps(TEMPLATE_STUB), encoding="utf-8")
        workflow_template = str(template_path)
    if jobs_dir is None:
        jobs_dir = str(tmp_path / "jobs")
    return Orchestrator(
        db=db,
        parser=parser or _FakeParser([]),
        planner=planner or _FakePlanner(db),
        prompter=prompter or _FakePrompter(db),
        evaluator=evaluator or _FakeEvaluator(db),
        publisher=publisher or _FakePublisher(),
        jobs_dir=jobs_dir,
        workflow_template=workflow_template,
        workflow_dir=str(tmp_path),
        poller=poller,
    )


TRENDS = [
    {"title": "T1", "url": "https://ig.example/p/1", "engagement": 10,
     "source": "scrape_creators"},
    {"title": "T2", "url": "https://ig.example/p/2", "engagement": 20,
     "source": "scrape_creators"},
]


# --- Переходы состояний (cron-тик) --------------------------------------------

def test_tick_new_goes_to_planner(db: Database, tmp_path: Path) -> None:
    """new → Planner → planned."""
    db.upsert_job("job-c", "new")
    planner = _FakePlanner(db)
    summary = make_orchestrator(db, tmp_path, planner=planner).run_once()
    assert planner.calls == ["job-c"]
    assert summary["planned"] == 1
    job = db.fetch_job("job-c")
    assert job is not None and job["status"] == "planned"
    assert job["concept"] is not None


def test_tick_planned_goes_to_prompter(db: Database, tmp_path: Path) -> None:
    """planned → Prompter → ready_for_render."""
    db.upsert_job("job-d", "planned", concept="Test concept")
    prompter = _FakePrompter(db)
    summary = make_orchestrator(db, tmp_path, prompter=prompter).run_once()
    assert prompter.calls == ["job-d"]
    assert summary["prompts"] == 1
    job = db.fetch_job("job-d")
    assert job is not None and job["status"] == "ready_for_render"
    assert job["video_prompt"] is not None
    assert job["photo_prompt"] is not None


def test_tick_ready_goes_to_generating(db: Database, tmp_path: Path) -> None:
    """ready_for_render → workflow-JSON записан в jobs_dir, статус generating."""
    db.upsert_job("job-e", "ready_for_render")
    jobs_dir = str(tmp_path / "jobs")
    summary = make_orchestrator(db, tmp_path, jobs_dir=jobs_dir).run_once()
    assert summary["submitted"] == 1
    job = db.fetch_job("job-e")
    assert job is not None and job["status"] == "generating"
    # Файл workflow создан в jobs_dir для node1_watchdog.py
    job_file = Path(jobs_dir) / "job_job-e_workflow.json"
    assert job_file.exists()


def test_submit_renders_workflow_with_photo_prompt(db: Database, tmp_path: Path) -> None:
    """photo_prompt подставляется в CLIPTextEncode-ноду workflow-файла в jobs_dir."""
    db.upsert_job(
        "job-p", "ready_for_render",
        video_prompt="VP", photo_prompt="flat lay, warm tones",
    )
    jobs_dir = str(tmp_path / "jobs")
    summary = make_orchestrator(db, tmp_path, jobs_dir=jobs_dir).run_once()
    assert summary["submitted"] == 1
    job_file = Path(jobs_dir) / "job_job-p_workflow.json"
    assert job_file.exists()
    workflow = json.loads(job_file.read_text(encoding="utf-8"))
    texts = [
        node["inputs"]["text"] for node in workflow.values()
        if node["class_type"] == "CLIPTextEncode"
    ]
    assert "flat lay, warm tones" in texts
    assert PROMPT_PLACEHOLDER not in texts


def test_video_prompt_ignored_and_logged(db: Database, tmp_path: Path) -> None:
    """video_prompt не попадает в workflow, факт игнорирования пишется в лог."""
    db.upsert_job(
        "job-v", "ready_for_render",
        video_prompt="waves hand slowly", photo_prompt="static portrait",
    )
    jobs_dir = str(tmp_path / "jobs")
    make_orchestrator(db, tmp_path, jobs_dir=jobs_dir).run_once()
    job_file = Path(jobs_dir) / "job_job-v_workflow.json"
    workflow_text = job_file.read_text(encoding="utf-8")
    assert "waves hand slowly" not in workflow_text
    assert any("video_prompt" in log["message"] for log in db.fetch_logs())


def test_tick_evaluates_done_jobs(db: Database, tmp_path: Path, media: Path) -> None:
    """done → Evaluator → approved."""
    db.upsert_job("job-a", "done", str(media))
    evaluator = _FakeEvaluator(db)
    summary = make_orchestrator(db, tmp_path, evaluator=evaluator).run_once()
    assert evaluator.calls == ["job-a"]
    assert summary["evaluated"] == 1
    job = db.fetch_job("job-a")
    assert job is not None and job["status"] == "approved"


def test_tick_publishes_approved_jobs(db: Database, tmp_path: Path, media: Path) -> None:
    """approved → Publisher → published."""
    db.upsert_job("job-b", "approved", str(media))
    publisher = _FakePublisher()
    summary = make_orchestrator(db, tmp_path, publisher=publisher).run_once()
    assert publisher.calls == [str(media)]
    assert publisher.metrics_calls == [("post-1", "1d")]
    assert summary["published"] == 1
    job = db.fetch_job("job-b")
    assert job is not None and job["status"] == "published"


def test_tick_creates_jobs_from_trends_when_queue_empty(db: Database, tmp_path: Path) -> None:
    """Пустая доска → Парсер ищет тренды → создаются задания new с метаданными."""
    parser = _FakeParser(TRENDS)
    summary = make_orchestrator(db, tmp_path, parser=parser).run_once()
    assert parser.calls == 1
    assert summary["created"] == 2
    jobs = db.fetch_jobs_by_status("new")
    assert len(jobs) == 2
    assert {j["trend_title"] for j in jobs} == {"T1", "T2"}
    assert {j["trend_url"] for j in jobs} == {
        "https://ig.example/p/1", "https://ig.example/p/2",
    }


def test_tick_full_pipeline_one_pass(db: Database, tmp_path: Path, media: Path) -> None:
    """Один тик двигает ВСЕ стадии разом; Парсер не дёргается."""
    db.upsert_job("job-a", "done", str(media))
    db.upsert_job("job-b", "approved", str(media))
    db.upsert_job("job-c", "new")
    db.upsert_job("job-d", "planned", concept="Concept for D")
    db.upsert_job("job-e", "ready_for_render", photo_prompt="static")
    parser = _FakeParser(TRENDS)
    summary = make_orchestrator(db, tmp_path, parser=parser).run_once()
    assert parser.calls == 0
    assert summary["created"] == 0
    job_a = db.fetch_job("job-a")
    job_b = db.fetch_job("job-b")
    job_c = db.fetch_job("job-c")
    job_d = db.fetch_job("job-d")
    job_e = db.fetch_job("job-e")
    assert job_a is not None and job_a["status"] == "approved"
    assert job_b is not None and job_b["status"] == "published"
    assert job_c is not None and job_c["status"] == "planned"
    assert job_d is not None and job_d["status"] == "ready_for_render"
    assert job_e is not None and job_e["status"] == "generating"


# --- CLI-контракт Node 1 ---------------------------------------------------------

def test_node1_command_is_queue_prompt_with_workflow() -> None:
    """Реальный контракт: --url из settings + queue prompt --workflow PATH."""
    settings = IsolatedSettings(
        comfyui_command="cli-anything-comfyui",
        comfyui_server_url="http://100.64.1.1:8188",
    )
    command = _build_node1_command(settings, "/tmp/job_job-1_workflow.json")
    assert command == [
        "cli-anything-comfyui",
        "--url", "http://100.64.1.1:8188",
        "queue", "prompt",
        "--workflow", "/tmp/job_job-1_workflow.json",
    ]


def test_bundled_workflow_template_is_valid() -> None:
    """Боевой шаблон assets/ — валидный API-граф с placeholder-нодой."""
    template = Path("assets/comfyui_workflow_template.json")
    workflow = json.loads(template.read_text(encoding="utf-8"))
    types = {node["class_type"] for node in workflow.values()}
    assert {"CheckpointLoaderSimple", "CLIPTextEncode", "KSampler", "SaveImage"} <= types
    assert any(
        node["inputs"].get("text") == PROMPT_PLACEHOLDER
        for node in workflow.values()
        if node["class_type"] == "CLIPTextEncode"
    )


# --- Маршрутизация по trigger -----------------------------------------------------

def test_webhook_trigger_evaluates_single_job(db: Database, tmp_path: Path, media: Path) -> None:
    """Триггер от Node 1: оценивается ТОЛЬКО указанный job, остальное не трогается."""
    db.upsert_job("job-x", "done", str(media))
    db.upsert_job("job-b", "approved", str(media))
    evaluator = _FakeEvaluator(db)
    publisher = _FakePublisher()
    parser = _FakeParser(TRENDS)
    summary = make_orchestrator(
        db, tmp_path, parser=parser, evaluator=evaluator, publisher=publisher,
    ).run_once(trigger_job_id="job-x")
    assert evaluator.calls == ["job-x"]
    assert publisher.calls == []
    assert parser.calls == 0
    assert summary["evaluated"] == 1


# --- Poller результатов ComfyUI (generating → done) --------------------------------

def test_tick_polls_generating_jobs(db: Database, tmp_path: Path) -> None:
    """Есть generating → поллер вызывается; свежий done в Evaluator НЕ идёт (1 шаг/тик)."""
    db.upsert_job("job-p", "generating")
    evaluator = _FakeEvaluator(db)

    def poller() -> dict[str, int]:
        db.upsert_job("job-p", "done", file_path="LTX_Output_job-p_.mp4")
        return {"checked": 1, "done": 1, "errors": 0}

    summary = make_orchestrator(db, tmp_path, evaluator=evaluator, poller=poller).run_once()
    assert summary["polled"] == 1
    job = db.fetch_job("job-p")
    assert job is not None and job["status"] == "done"
    assert evaluator.calls == []  # done после снимка — оценка на следующем тике


def test_tick_skips_poller_without_generating(db: Database, tmp_path: Path) -> None:
    """Нет generating → поллер не дёргается, summary['polled'] == 0."""
    calls: list[str] = []

    def poller() -> dict[str, int]:
        calls.append("called")
        return {"checked": 0, "done": 0, "errors": 0}

    summary = make_orchestrator(db, tmp_path, poller=poller).run_once()
    assert summary["polled"] == 0
    assert calls == []


def test_tick_survives_poller_failure(db: Database, tmp_path: Path) -> None:
    """Падение поллера пишется в errors и не роняет остальные стадии тика."""
    db.upsert_job("job-g", "generating")
    db.upsert_job("job-c", "new")
    planner = _FakePlanner(db)

    def broken() -> dict[str, int]:
        raise RuntimeError("comfyui unreachable")

    summary = make_orchestrator(db, tmp_path, planner=planner, poller=broken).run_once()
    assert summary["errors"] == 1
    assert planner.calls == ["job-c"]  # остальные стадии отработали


# --- Отказоустойчивость ----------------------------------------------------------

def test_tick_continues_on_agent_error(db: Database, tmp_path: Path, media: Path) -> None:
    """Падение одного субагента не роняет тик: остальные задания обрабатываются."""
    db.upsert_job("job-bad", "approved", "/nonexistent/bad.png")
    db.upsert_job("job-good", "approved", str(media))
    publisher = _FakePublisher(fail_on="bad")
    summary = make_orchestrator(db, tmp_path, publisher=publisher).run_once()
    assert summary["errors"] == 1
    assert summary["published"] == 1
    good = db.fetch_job("job-good")
    bad = db.fetch_job("job-bad")
    assert good is not None and good["status"] == "published"
    assert bad is not None and bad["status"] == "approved"  # ждёт следующего тика


# --- P0: уникальность job_id для трендов без URL -------------------------------


def test_job_id_from_url_is_stable() -> None:
    """Тренд с URL → детерминированный id (одинаковый между процессами)."""
    trend: dict[str, object] = {"title": "A", "url": "https://ig.example/p/1"}
    assert _job_id_from_trend(trend) == _job_id_from_trend(dict(trend))
    assert _job_id_from_trend(trend).startswith("trend-")


def test_job_id_no_collision_for_url_less_trends() -> None:
    """P0: тренды без URL (crawl4ai/Jina) НЕ должны схлопываться в один id."""
    trends: list[dict[str, object]] = [
        {"title": "Morning routines", "url": ""},
        {"title": "AI tools for creators", "url": ""},
        {"title": "Minimalist desk setup", "url": ""},
    ]
    ids = [_job_id_from_trend(t) for t in trends]
    assert len(set(ids)) == 3, f"collision: {ids}"


def test_job_id_fallback_to_title_is_stable() -> None:
    """Одинаковый title без URL → одинаковый id (дедупликация повторов)."""
    trend2: dict[str, object] = {"title": "Same trend", "url": ""}
    assert _job_id_from_trend(trend2) == _job_id_from_trend(dict(trend2))


def test_job_id_url_takes_priority_over_title() -> None:
    """URL — первичный ключ: разные title с одним URL → один id (это один пост)."""
    a: dict[str, object] = {"title": "Title A", "url": "https://ig.example/p/9"}
    b: dict[str, object] = {"title": "Title B", "url": "https://ig.example/p/9"}
    assert _job_id_from_trend(a) == _job_id_from_trend(b)


def test_url_less_trends_create_distinct_jobs(db: Database, tmp_path: Path) -> None:
    """E2E регресс: N трендов без URL → N отдельных заданий на доске."""
    parser = _FakeParser([
        {"title": "Trend one", "url": "", "engagement": 10, "source": "jina"},
        {"title": "Trend two", "url": "", "engagement": 20, "source": "jina"},
    ])
    summary = make_orchestrator(db, tmp_path, parser=parser).run_once()
    assert summary["created"] == 2
    jobs = db.fetch_jobs_by_status("new")
    assert len(jobs) == 2
    assert len({j["job_id"] for j in jobs}) == 2


LTX_WORKFLOW_PATH = Path("КОД COMFY/LTX-2.3_Triple_Sampler_DevMode.json")


def test_ltx_video_workflow_is_patched(
    db: Database, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """LTX-шаблон программно патчится: video_prompt, first frame, 9:16, saver."""
    job_id = "job-ltx"
    video = "A young man in a white balaclava waves at the camera"
    photo = "portrait of a young man in a white balaclava, soft studio light"
    db.upsert_job(job_id, "ready_for_render", video_prompt=video, photo_prompt=photo)
    jobs_dir = str(tmp_path / "jobs")
    with caplog.at_level("WARNING"):
        summary = make_orchestrator(
            db, tmp_path, jobs_dir=jobs_dir, workflow_template=str(LTX_WORKFLOW_PATH)
        ).run_once()
    assert summary["submitted"] == 1
    job_file = Path(jobs_dir) / f"job_{job_id}_workflow.json"
    assert job_file.exists()
    workflow = json.loads(job_file.read_text(encoding="utf-8"))
    expected_first_frame = _first_frame_image_name(job_id, photo)
    assert workflow["5314"]["inputs"]["text"] == video
    assert workflow["5029"]["inputs"]["image"] == expected_first_frame
    assert workflow["5330"]["inputs"]["aspect_ratio"] == "Custom"
    assert workflow["5330"]["inputs"]["custom_ratio_width"] == 9
    assert workflow["5330"]["inputs"]["custom_ratio_height"] == 16
    assert workflow["5294"]["inputs"]["save_output"] is True
    assert workflow["5294"]["inputs"]["filename_prefix"] == f"LTX_Output_{job_id}_"
    savers = {
        node_id: node
        for node_id, node in workflow.items()
        if node.get("class_type") == "VHS_VideoCombine"
    }
    assert len(savers) == 10
    assert all(node["inputs"]["save_output"] is True for node in savers.values())
    assert "Тест конфи.mp3" in caplog.text


def test_metrics_failure_keeps_job_published(
    db: Database, tmp_path: Path, media: Path
) -> None:
    """Сбой collect_metrics НЕ откатывает публикацию: published + ошибка в summary."""
    db.upsert_job("job-m", "approved", str(media))
    publisher = _FakePublisher(fail_metrics=True)
    summary = make_orchestrator(db, tmp_path, publisher=publisher).run_once()
    assert publisher.calls == [str(media)]
    assert publisher.metrics_calls == [("post-1", "1d")]
    assert summary["published"] == 0   # guard засчитал ошибку, а не публикацию
    assert summary["errors"] == 1
    job = db.fetch_job("job-m")
    assert job is not None and job["status"] == "published"  # пост не ре-постится


# --- Pull-модель (A+B гибрид) -------------------------------------------------

def test_pull_model_writes_workflow_to_jobs_dir(db: Database, tmp_path: Path) -> None:
    """ready_for_render → workflow-JSON в jobs_dir, а не через push."""
    db.upsert_job("job-pull", "ready_for_render", photo_prompt="sunset beach")
    jobs_dir = str(tmp_path / "pull-jobs")
    summary = make_orchestrator(db, tmp_path, jobs_dir=jobs_dir).run_once()
    assert summary["submitted"] == 1
    job_file = Path(jobs_dir) / "job_job-pull_workflow.json"
    assert job_file.exists()
    workflow = json.loads(job_file.read_text(encoding="utf-8"))
    # Проверяем что photo_prompt подставлен в ноду
    assert any(
        node["inputs"].get("text") == "sunset beach"
        for node in workflow.values()
        if node.get("class_type") == "CLIPTextEncode"
    )


def test_pull_model_creates_jobs_dir_if_missing(db: Database, tmp_path: Path) -> None:
    """jobs_dir создаётся автоматически, если не существует."""
    db.upsert_job("job-newdir", "ready_for_render")
    jobs_dir = str(tmp_path / "nonexistent" / "deep" / "jobs")
    make_orchestrator(db, tmp_path, jobs_dir=jobs_dir).run_once()
    assert Path(jobs_dir).is_dir()
    assert (Path(jobs_dir) / "job_job-newdir_workflow.json").exists()


def test_pull_model_file_contains_valid_json(db: Database, tmp_path: Path) -> None:
    """Записанный workflow — валидный JSON, который может распарсить node1_watchdog."""
    db.upsert_job("job-json", "ready_for_render", photo_prompt="minimalist desk")
    jobs_dir = str(tmp_path / "jobs")
    make_orchestrator(db, tmp_path, jobs_dir=jobs_dir).run_once()
    job_file = Path(jobs_dir) / "job_job-json_workflow.json"
    data = json.loads(job_file.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert any(node.get("class_type") == "CLIPTextEncode" for node in data.values())
