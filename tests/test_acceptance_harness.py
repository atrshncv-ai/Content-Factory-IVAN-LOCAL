"""Ticket 08: reproducible Mac Node 2 / Windows Node 1 harness contracts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.acceptance import AcceptanceLog, Preflight

NOW = datetime(2026, 7, 31, 10, tzinfo=timezone.utc)


def test_three_level_preflight_checks_tailnet_tcp_and_workflow(tmp_path: Path) -> None:
    workflow = tmp_path / "workflow.json"
    workflow.write_text(
        json.dumps({"1": {"class_type": "LoadImage"}}), encoding="utf-8"
    )
    calls: list[tuple[str, int]] = []

    def tcp(host: str, port: int) -> bool:
        calls.append((host, port))
        return True

    report = Preflight(
        tailnet=lambda: True,
        tcp=tcp,
        comfy=lambda _url: {
            "system_stats": True,
            "node_types": {"LoadImage", "SaveVideo"},
        },
    ).run("node1.tailnet", 8188, "http://node1.tailnet:8188", workflow)

    assert report == {
        "tailnet": True,
        "tcp": True,
        "comfyui": True,
        "workflow": True,
        "ready": True,
    }
    assert calls == [("node1.tailnet", 8188)]


def test_preflight_short_circuits_after_failed_transport(tmp_path: Path) -> None:
    workflow = tmp_path / "workflow.json"
    workflow.write_text("{}", encoding="utf-8")
    called = False

    def comfy(_url: str) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    report = Preflight(
        tailnet=lambda: True,
        tcp=lambda _host, _port: False,
        comfy=comfy,
    ).run("node1", 8188, "http://node1:8188", workflow)

    assert report["tailnet"] is True
    assert report["tcp"] is False
    assert report["ready"] is False
    assert called is False


def test_acceptance_log_measures_cycles_without_secrets(tmp_path: Path) -> None:
    path = tmp_path / "acceptance.json"
    log = AcceptanceLog(path)
    log.start("A", NOW)
    elapsed = log.finish(
        "A",
        NOW + timedelta(seconds=125),
        outcome="manual_pass",
        evidence="Telegram message id 42",
    )

    assert elapsed == 125
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["cycles"]["A"]["elapsed_seconds"] == 125
    assert payload["cycles"]["A"]["outcome"] == "manual_pass"
    serialized = path.read_text(encoding="utf-8").lower()
    assert "token" not in serialized and "api_key" not in serialized


def test_runtime_artifacts_use_environment_paths_and_no_client_secrets() -> None:
    mac = Path("ops/macos/run_tick.sh").read_text(encoding="utf-8")
    node1 = Path("ops/windows/start_node1.cmd").read_text(encoding="utf-8")
    autostart = Path("ops/windows/install_autostart.ps1").read_text(encoding="utf-8")

    assert "HERMES_PROJECT_DIR" in mac
    assert "NODE1_COMFYUI_DIR" in node1 and "NODE1_PYTHON_EXE" in node1
    assert "Register-ScheduledTask" in autostart
    combined = (mac + node1 + autostart).lower()
    assert "kimiadmin" not in combined
    assert "c:\\users\\" not in combined
