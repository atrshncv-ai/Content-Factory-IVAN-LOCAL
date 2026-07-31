# Ticket 08 — TDD evidence

Source: `.scratch/hermes-content-factory-mvp/issues/08-mac-node1-acceptance.md`.

## Journeys and RED → GREEN

- Оператор воспроизводимо запускает одноразовый Mac tick и Windows Node 1 из
  env-driven артефактов.
- Оператор до GPU-submit проверяет tailnet, TCP и ComfyUI/workflow.
- Человек измеряет A/B циклы без записи секретов.

RED: `uv run pytest -q tests/test_acceptance_harness.py` завершился
`ModuleNotFoundError: core.acceptance`; commit `90b34a0`.

GREEN: `uv run pytest -q tests/test_acceptance_harness.py` — `4 passed`.
Full suite: `uv run pytest -q` — `159 passed`; `uv run mypy core tests
scripts/acceptance_harness.py` — clean (38 files). Graphify: 1069 nodes,
2213 edges. Локальный read-only preflight: tailnet `true`, TCP/ComfyUI/workflow
`false`; клиентский Node 1 в момент проверки недоступен.

| Гарантия | Тест | Тип |
|---|---|---|
| Три уровня preflight и workflow node contract | `test_three_level_preflight_checks_tailnet_tcp_and_workflow` | integration |
| Недоступный транспорт не вызывает ComfyUI | `test_preflight_short_circuits_after_failed_transport` | unit |
| A/B журнал хранит elapsed/outcome без секретов | `test_acceptance_log_measures_cycles_without_secrets` | unit |
| Mac/Windows launchers используют env-пути | `test_runtime_artifacts_use_environment_paths_and_no_client_secrets` | contract |

Реальные A/B, Telegram, GPU и reboot намеренно не запускались и не считаются
PASS до ручного evidence на клиентском Node 1.
