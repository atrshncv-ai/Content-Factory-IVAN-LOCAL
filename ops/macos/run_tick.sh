#!/bin/sh
set -eu

: "${HERMES_PROJECT_DIR:?set HERMES_PROJECT_DIR in launchd environment}"
: "${HERMES_PYTHON:?set HERMES_PYTHON in launchd environment}"
cd "$HERMES_PROJECT_DIR"
exec "$HERMES_PYTHON" -m core.orchestrator
