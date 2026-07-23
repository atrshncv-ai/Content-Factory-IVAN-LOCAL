"""Локальный TCP-мост через `tailscale nc`, обходящий маршруты стороннего VPN."""

from __future__ import annotations

import os
import socket
import socketserver
import subprocess
from dataclasses import dataclass

from core.config import Settings


@dataclass(frozen=True)
class ProxyConfig:
    target_host: str
    target_port: int
    listen_host: str
    listen_port: int

    @classmethod
    def from_env(cls) -> "ProxyConfig":
        return cls(
            target_host=os.environ["TAILSCALE_PROXY_TARGET_HOST"],
            target_port=int(os.environ["TAILSCALE_PROXY_TARGET_PORT"]),
            listen_host=os.environ["TAILSCALE_PROXY_LISTEN_HOST"],
            listen_port=int(os.environ["TAILSCALE_PROXY_LISTEN_PORT"]),
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProxyConfig":
        values = (
            settings.tailscale_proxy_target_host,
            settings.tailscale_proxy_target_port,
            settings.tailscale_proxy_listen_host,
            settings.tailscale_proxy_listen_port,
        )
        if any(value is None for value in values):
            raise ValueError("TAILSCALE_PROXY_* must be configured in .env")
        return cls(
            target_host=str(values[0]),
            target_port=int(values[1]),  # type: ignore[arg-type]
            listen_host=str(values[2]),
            listen_port=int(values[3]),  # type: ignore[arg-type]
        )

    @property
    def command(self) -> list[str]:
        return ["tailscale", "nc", self.target_host, str(self.target_port)]


def relay(source: socket.socket, target: socket.socket) -> None:
    """Копирует поток байтов до EOF."""
    while chunk := source.recv(65536):
        target.sendall(chunk)


def serve(config: ProxyConfig) -> None:
    """Каждое локальное соединение передаёт Tailscale CLI без системного route."""

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            subprocess.run(
                config.command,
                stdin=self.request,
                stdout=self.request,
                stderr=subprocess.PIPE,
                check=False,
            )

    with socketserver.ThreadingTCPServer(
        (config.listen_host, config.listen_port), Handler
    ) as server:
        server.daemon_threads = True
        server.allow_reuse_address = True
        print(
            f"Tailscale proxy: {config.listen_host}:{config.listen_port}"
            f" -> {config.target_host}:{config.target_port}"
        )
        server.serve_forever()


if __name__ == "__main__":
    serve(ProxyConfig.from_settings(Settings()))
