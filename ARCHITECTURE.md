# Hermes Content Factory MVP: фактическая архитектура

На этапе приёмки Node 2 — Mac разработчика. Hermes и Telegram-оболочка
принимают только `/setup`, `/plan`, `/new_video`, `/status`, `/cancel`.
Каждое событие и cron-тик запускает чистый процесс, делает один переход
сохранённого Kanban-state и завершается. Установка Hermes на Windows-ПК
клиента в этот этап не входит.

Node 1 — клиентский Windows 11 GPU-узел: Tailscale, ComfyUI и
`scripts/node1_watchdog.py`. Watchdog забирает `job_*.json` у
`scripts/job_server.py`, ставит один утверждённый API workflow в локальный
ComfyUI и удаляет job-файл только после успешного submit. Результаты
проверяются активным `core.poller` на следующем cron-тике; webhook и SMB в
MVP-пути не используются.

Перед запуском `scripts.acceptance_harness preflight` проверяет:

1. авторизацию Tailscale CLI;
2. TCP-доступность Node 1;
3. `/system_stats`, `/object_info` и наличие всех типов нод утверждённого
   workflow.

Пути, адреса, ID и секреты читаются только из `.env`. SQLite содержит
технический журнал; Hermes workspace — профиль/план; постоянные оригиналы
остаются на Node 1, временная копия MP4 на Mac удаляется после доставки.
