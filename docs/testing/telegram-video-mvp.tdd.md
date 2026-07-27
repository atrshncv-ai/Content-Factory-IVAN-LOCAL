# Telegram photo-to-video MVP — TDD plan

## User journey

The allowed Telegram user sends a photo with a topic. One-shot cron ticks turn
it into an LTX video on Node 1 and return the MP4 with total elapsed time.

## Guarantees

| # | Guarantee | Type |
|---|---|---|
| 1 | Updates from any other chat are ignored | Security/unit |
| 2 | The largest Telegram photo and its caption become one request | Unit |
| 3 | Missing photo or caption is rejected before GPU work | Unit |
| 4 | The uploaded input filename is written exactly to DevMode node `5029` | Unit |
| 5 | DevMode prompt is written to node `5314`; one primary MP4 is enabled | Unit |
| 6 | Duplicate Telegram `update_id` cannot create a second render | DB/integration |
| 7 | A completed MP4 is downloaded, sent, timed, then removed from Node 2 | E2E |

## Real E2E

Send a real Telegram photo with a caption, run one-shot ticks until completion,
validate the MP4 container, receive it in Telegram, and report wall-clock time.

## 2026-07-27 evidence

- RED: both workflow tests found 10 `VHS_VideoCombine` output targets.
- GREEN: only primary GGUF branch `5294` remains in the submitted API graph.
- Validation: `110 passed`; `mypy` reports no issues in 15 source files.
- Real E2E: 1-second/512 px diagnostic completed in 110 seconds and its MP4
  was delivered to the configured Telegram chat.
- Coverage was not collected because `pytest-cov` is not installed.
