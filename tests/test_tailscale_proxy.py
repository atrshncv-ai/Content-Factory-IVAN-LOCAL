"""Контракт локального TCP-моста в Tailscale, обходящего системный VPN-маршрут."""

from __future__ import annotations

import socket
from typing import Any

from core.config import Settings
from scripts.tailscale_proxy import ProxyConfig, relay


def test_config_reads_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("TAILSCALE_PROXY_TARGET_HOST", "100.64.0.10")
    monkeypatch.setenv("TAILSCALE_PROXY_TARGET_PORT", "9000")
    monkeypatch.setenv("TAILSCALE_PROXY_LISTEN_HOST", "127.0.0.2")
    monkeypatch.setenv("TAILSCALE_PROXY_LISTEN_PORT", "19000")

    config = ProxyConfig.from_env()

    assert config.target_host == "100.64.0.10"
    assert config.target_port == 9000
    assert config.listen_host == "127.0.0.2"
    assert config.listen_port == 19000
    assert config.command == ["tailscale", "nc", "100.64.0.10", "9000"]


def test_config_reuses_project_settings() -> None:
    settings = Settings(
        _env_file=None,
        tailscale_proxy_target_host="100.64.0.20",
        tailscale_proxy_target_port=8189,
        tailscale_proxy_listen_host="127.0.0.3",
        tailscale_proxy_listen_port=18189,
    )

    assert ProxyConfig.from_settings(settings) == ProxyConfig(
        target_host="100.64.0.20",
        target_port=8189,
        listen_host="127.0.0.3",
        listen_port=18189,
    )


def test_relay_copies_bytes_until_eof() -> None:
    source, source_peer = socket.socketpair()
    target, target_peer = socket.socketpair()
    try:
        source_peer.sendall(b"prompt")
        source_peer.shutdown(socket.SHUT_WR)
        relay(source, target)
        assert target_peer.recv(6) == b"prompt"
    finally:
        source.close()
        source_peer.close()
        target.close()
        target_peer.close()
