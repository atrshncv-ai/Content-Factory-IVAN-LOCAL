# Инструменты Hermes MVP

- Hermes Agent на Mac Node 2: диалог, планирование и чистые event/cron-сессии.
- Telegram: единственный пользовательский интерфейс, allowlist двух ID.
- SQLite: узкий журнал задач, попыток и аудита.
- Tailscale: приватный транспорт Mac ↔ Windows Node 1.
- ComfyUI + утверждённый API workflow: единственный GPU renderer.
- `job_server.py` + `node1_watchdog.py`: stdlib pull-канал постановки задач.
- `core.poller`: одноразовая проверка history на cron-тике.
- `acceptance_harness.py`: безопасный preflight и измерение ручных A/B циклов.
- Graphify: обновление графа после зелёных тестов.

Ни один инструмент не получает секреты из аргументов или репозитория: значения
существуют только в локальном `.env`/окружении.
