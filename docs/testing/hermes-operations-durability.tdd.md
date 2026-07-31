# Ticket 07 — TDD evidence

Source: `.scratch/hermes-content-factory-mvp/issues/07-operations-and-durability.md`.

## User journeys

- Администратор безопасно видит здоровье обоих узлов и сохранённый прогресс.
- Система переживает лимиты ключей, рестарт, нехватку диска и сбой доставки.
- Оператор запускает backup, cleanup и log retention отдельными cron-тиками.

## RED → GREEN

- RED: `uv run pytest -q tests/test_operations.py tests/test_telegram_mvp.py`
  завершился на `ModuleNotFoundError: core.operations`; commit `1275078`.
- GREEN: `uv run pytest -q` — `155 passed`.
- Types: `uv run mypy core tests` — `Success: no issues found in 35 source files`.

| Гарантия | Тест | Тип | Результат |
|---|---|---|---|
| Два ключевых слота ротируются на 60 минут без записи значений | `test_key_pool_rotates_deterministically_and_pauses_after_two_limits` | unit | PASS |
| `/status` объединяет health и не раскрывает prompt/secret | `test_status_is_composite_safe_and_never_contains_secret_or_prompt` | integration | PASS |
| Persisted задачи/план восстанавливаются, active job сверяется с history | `test_recovery_checks_active_history_and_notifies_both_admins` | integration | PASS |
| Логи живут 30 дней, аудит хранит Telegram ID | `test_audit_has_telegram_id_logs_are_redacted_and_retained_30_days` | integration | PASS |
| Backup хранит семь ZIP и исключает `.env` | `test_backup_keeps_seven_versions_and_excludes_env` | unit | PASS |
| Cleanup сохраняет accepted и удаляет просроченное | `test_cleanup_removes_old_failed_cancelled_and_temp_but_keeps_accepted` | integration | PASS |
| Диск ниже 20 ГБ блокирует submit и уведомляет двоих | `test_disk_guard_blocks_new_generation_and_notifies_both_admins` | integration | PASS |
| Сбой доставки оставляет оригинальный Node 1 path | `test_delivery_failure_preserves_node1_path_and_reports_reason` | integration | PASS |

Новых зависимостей и daemon-loop нет. Операции являются явными одноразовыми
функциями для внешнего cron. Windows install/autostart относится к ticket08.
Coverage-плагин в окружении отсутствует (`pytest: unrecognized arguments:
--cov`), поэтому процент не измерялся; все 155 существующих и новых тестов
пройдены без установки зависимости и без сети.
