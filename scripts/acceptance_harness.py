"""One-shot operator CLI; never starts Telegram or queues GPU work."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from core.acceptance import (
    AcceptanceLog,
    Preflight,
    comfy_contract,
    tailnet_available,
    tcp_available,
)
from core.config import Settings


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    start = sub.add_parser("start")
    start.add_argument("cycle", choices=("A", "B"))
    finish = sub.add_parser("finish")
    finish.add_argument("cycle", choices=("A", "B"))
    finish.add_argument("outcome", choices=("manual_pass", "manual_fail"))
    finish.add_argument("--evidence", required=True)
    args = parser.parse_args()
    settings = Settings()
    now = datetime.now().astimezone()
    if args.command == "start":
        log = AcceptanceLog(Path(os.environ["ACCEPTANCE_LOG_PATH"]))
        log.start(args.cycle, now)
        print(f"Acceptance {args.cycle}: timer started")
        return 0
    if args.command == "finish":
        log = AcceptanceLog(Path(os.environ["ACCEPTANCE_LOG_PATH"]))
        elapsed = log.finish(
            args.cycle, now, outcome=args.outcome, evidence=args.evidence
        )
        print(f"Acceptance {args.cycle}: {args.outcome}, {elapsed}s")
        return 0
    if (
        settings.tailscale_proxy_target_host is None
        or settings.tailscale_proxy_target_port is None
    ):
        parser.error("TAILSCALE_PROXY_TARGET_HOST/PORT are required")
    report = Preflight(
        tailnet=tailnet_available,
        tcp=tcp_available,
        comfy=comfy_contract,
    ).run(
        settings.tailscale_proxy_target_host,
        settings.tailscale_proxy_target_port,
        settings.comfyui_server_url,
        Path(settings.comfyui_workflow_template),
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
