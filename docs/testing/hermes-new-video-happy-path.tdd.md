# TDD evidence: `/new_video` happy path

## Source

Specification: `.scratch/hermes-content-factory-mvp/spec.md`.
Ticket: `.scratch/hermes-content-factory-mvp/issues/03-new-video-happy-path.md`.

## User journey

An allowed administrator sends an idea as a photo caption. One clean worker
tick advances the persisted job by one stage: receive, Hermes planning,
health-checked ComfyUI submission, polling, MP4 download, Node 1 archive and
Telegram delivery.

## RED → GREEN

- RED: `uv run pytest -q tests/test_telegram_mvp.py tests/test_hermes_shell.py`
  failed during collection because `assert_workflow_checksum` and
  `validate_photo` did not exist. Checkpoint: `e47c0cc`.
- GREEN: `uv run pytest -q` — `125 passed`.
- Types: `uv run mypy core tests` — `Success: no issues found in 28 source files`.
  Checkpoint: `4d53010`.

## Guarantees

| Guarantee | Evidence | Type |
|---|---|---|
| `/new_video` requests a supported image with a 720 px minimum side | `test_plan_requires_profile_but_new_video_remains_available` | contract |
| PNG/JPEG/WebP headers and dimensions are validated before a job exists | `test_photo_validation_accepts_supported_large_image_and_rejects_bad_input`, `test_invalid_photo_does_not_create_job_or_spend_attempt` | unit/integration |
| Workflow SHA-256 mismatch blocks submission | `test_workflow_checksum_must_match_approved_value` | unit |
| Each tick advances one persisted stage and calls Hermes once | `test_worker_is_idempotent_and_advances_one_stage_per_tick` | integration |
| Health precedes submit; one MP4 is archived, delivered to configured admins, and removed locally | `test_worker_is_idempotent_and_advances_one_stage_per_tick` | integration |
| Existing LTX patcher enforces the approved first frame, prompt, 9:16 and one saver | `test_devmode_uses_uploaded_telegram_photo` | contract |
| Existing active poller records ComfyUI output without webhook | `tests/test_poller.py` | integration |

## Intentional boundary

No live Hermes, Telegram, Tailscale or ComfyUI call was run. Duration, audio,
codec and artistic quality are client acceptance checks; automation verifies
only that the downloaded result has a basic MP4 container header. Queue limits,
retry and cancel belong to ticket 04.
