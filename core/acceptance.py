"""Безопасный preflight и журнал ручной приёмки Mac Node 2 + Node 1."""

from __future__ import annotations

import json
import socket
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, build_opener

TailnetCheck = Callable[[], bool]
TcpCheck = Callable[[str, int], bool]
ComfyCheck = Callable[[str], dict[str, Any]]


class Preflight:
    """Три уровня: tailnet, TCP, затем ComfyUI и типы нод workflow."""

    def __init__(
        self,
        *,
        tailnet: TailnetCheck,
        tcp: TcpCheck,
        comfy: ComfyCheck,
    ) -> None:
        self._tailnet = tailnet
        self._tcp = tcp
        self._comfy = comfy

    def run(
        self, host: str, port: int, url: str, workflow_path: Path
    ) -> dict[str, bool]:
        result = {
            "tailnet": self._safe(self._tailnet),
            "tcp": False,
            "comfyui": False,
            "workflow": False,
            "ready": False,
        }
        if not result["tailnet"]:
            return result
        result["tcp"] = self._safe(lambda: self._tcp(host, port))
        if not result["tcp"]:
            return result
        try:
            remote = self._comfy(url)
            result["comfyui"] = bool(remote.get("system_stats"))
            workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
            required = {
                str(node["class_type"])
                for node in workflow.values()
                if isinstance(node, dict) and isinstance(node.get("class_type"), str)
            }
            available = set(remote.get("node_types", set()))
            result["workflow"] = bool(required) and required <= available
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        result["ready"] = all(
            result[name] for name in ("tailnet", "tcp", "comfyui", "workflow")
        )
        return result

    @staticmethod
    def _safe(check: Callable[[], bool]) -> bool:
        try:
            return bool(check())
        except (OSError, subprocess.SubprocessError, ValueError):
            return False


class AcceptanceLog:
    """Хранит только время, исход и короткую несекретную ссылку на evidence."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def start(self, cycle: str, now: datetime) -> None:
        payload = self._load()
        payload.setdefault("cycles", {})[cycle] = {"started_at": now.isoformat()}
        self._save(payload)

    def finish(
        self,
        cycle: str,
        now: datetime,
        *,
        outcome: str,
        evidence: str,
    ) -> int:
        if outcome not in {"manual_pass", "manual_fail"}:
            raise ValueError("outcome must be manual_pass or manual_fail")
        if any(word in evidence.lower() for word in ("token", "api_key", "secret")):
            raise ValueError("evidence must not contain secret material")
        payload = self._load()
        row = payload.get("cycles", {}).get(cycle)
        if not isinstance(row, dict) or not isinstance(row.get("started_at"), str):
            raise ValueError(f"cycle {cycle} was not started")
        elapsed = int(
            (now - datetime.fromisoformat(str(row["started_at"]))).total_seconds()
        )
        row.update(
            {
                "finished_at": now.isoformat(),
                "elapsed_seconds": elapsed,
                "outcome": outcome,
                "evidence": evidence,
            }
        )
        self._save(payload)
        return elapsed

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"cycles": {}}
        value = json.loads(self._path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"cycles": {}}

    def _save(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.new")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._path)


def tailnet_available() -> bool:
    return subprocess.run(
        ["tailscale", "status", "--json"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    ).returncode == 0


def tcp_available(host: str, port: int) -> bool:
    with socket.create_connection((host, port), timeout=10):
        return True


def comfy_contract(url: str) -> dict[str, Any]:
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"{url.rstrip('/')}/system_stats", timeout=10) as response:
        system_stats = json.load(response)
    with opener.open(f"{url.rstrip('/')}/object_info", timeout=30) as response:
        object_info = json.load(response)
    return {
        "system_stats": bool(system_stats),
        "node_types": set(object_info) if isinstance(object_info, dict) else set(),
    }
