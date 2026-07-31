#!/bin/sh
set -eu

: "${HERMES_PROJECT_DIR:?set HERMES_PROJECT_DIR in launchd environment}"
: "${HERMES_PYTHON:?set HERMES_PYTHON in launchd environment}"
cd "$HERMES_PROJECT_DIR"
if ! "$HERMES_PYTHON" -m core.telegram_gateway tick; then
  echo "Telegram tick failed" >&2
  exit 1
fi
if ! "$HERMES_PYTHON" -m core.orchestrator; then
  echo "Orchestrator tick failed" >&2
  exit 1
fi
