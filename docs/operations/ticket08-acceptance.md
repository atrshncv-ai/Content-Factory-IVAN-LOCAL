# Mac Node 2 + Windows Node 1: operator runbook

## Mac Node 2

Заполнить локальный `.env` по `.env.example`, затем:

```sh
uv run python -m scripts.acceptance_harness preflight
uv run python -m scripts.job_server
```

Для one-shot cron указать `HERMES_PROJECT_DIR` и `HERMES_PYTHON`, заменить
маркеры `__PROJECT_DIR__`, `__PYTHON__`, `__LOG_DIR__` в
`ops/macos/com.hermes.content-factory.plist.example`, скопировать результат в
`~/Library/LaunchAgents/com.hermes.content-factory.plist` и выполнить:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hermes.content-factory.plist
launchctl kickstart -k gui/$(id -u)/com.hermes.content-factory
```

Шаблон запускает `python -m core.orchestrator` раз в 60 секунд; процесс делает
один тик и завершается. Hermes Gateway/Telegram запускаются штатным Hermes
launcher согласно локальной установке; их executable не подменяется этим
репозиторием.

## Windows Node 1

В пользовательском окружении задать `NODE1_COMFYUI_DIR`, `NODE1_PYTHON_EXE`,
`NODE1_WATCHDOG_PY`, `NODE1_LAUNCHER`, `NODE2_URL`, `COMFYUI_URL`. Проверить
ручной запуск `ops\windows\start_node1.cmd`, затем в PowerShell администратора
(`NODE1_LAUNCHER` указывает на этот cmd-файл):

```powershell
powershell -ExecutionPolicy Bypass -File (Join-Path (Split-Path $env:NODE1_LAUNCHER) "install_autostart.ps1")
```

Tailscale должен быть установлен и настроен на автозапуск отдельно. Эти
артефакты ничего не устанавливают и не меняют firewall.

## Acceptance (только при участии человека)

```sh
uv run python -m scripts.acceptance_harness start A
# пройти /new_video ... Принять
uv run python -m scripts.acceptance_harness finish A manual_pass --evidence "Telegram message id ..."
uv run python -m scripts.acceptance_harness start B
# пройти /plan ... получить MP4
uv run python -m scripts.acceptance_harness finish B manual_pass --evidence "Telegram message id ..."
```

После этого оператор перезапускает процессы/Node 1 и вручную подтверждает
сохранение state и уведомление «система онлайн». До этой проверки ticket
остаётся `in-progress`.
