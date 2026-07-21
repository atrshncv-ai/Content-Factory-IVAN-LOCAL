"""Job-server Node 2 (pull-модель): раздаёт job-файлы по HTTP для Node 1.

Node 1 (ПК с ComfyUI) сам забирает задачи исходящим соединением, поэтому
брандмауэр Windows ему не мешает. API:
  GET    /            → JSON-список имён job_*.json
  GET    /job_X.json  → содержимое job-файла
  DELETE /job_X.json  → удаление после успешной обработки

Слушает интерфейс Tailscale. Ponytail: только stdlib.
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = os.environ.get("JOB_SERVER_HOST", "0.0.0.0")
PORT = int(os.environ.get("JOB_SERVER_PORT", "8899"))
JOBS_DIR = Path(os.environ.get("JOBS_DIR", "data/jobs"))

# Только job-файлы без слешей — защита от path traversal.
JOB_RE = re.compile(r"^job_[\w.-]+\.json$")


class Handler(BaseHTTPRequestHandler):
    """Раздаёт список job-файлов, их содержимое и удаляет по DELETE."""

    def _jobs(self) -> list[str]:
        return sorted(p.name for p in JOBS_DIR.glob("job_*.json") if JOB_RE.match(p.name))

    def _send(self, code: int, body: bytes = b"") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, json.dumps(self._jobs()).encode("utf-8"))
            return
        name = self.path.lstrip("/")
        path = JOBS_DIR / name
        if JOB_RE.match(name) and path.is_file():
            self._send(200, path.read_bytes())
        else:
            self._send(404, b'{"error": "not found"}')

    def do_DELETE(self) -> None:
        name = self.path.lstrip("/")
        path = JOBS_DIR / name
        if JOB_RE.match(name) and path.is_file():
            path.unlink()
            self._send(200, b'{"deleted": true}')
        else:
            self._send(404, b'{"error": "not found"}')

    def log_message(self, format: str, *args: Any) -> None:
        pass


def main() -> None:
    """Запускает HTTP-сервер раздачи задач."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Job-server: http://{HOST}:{PORT}/ -> {JOBS_DIR.resolve()}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
