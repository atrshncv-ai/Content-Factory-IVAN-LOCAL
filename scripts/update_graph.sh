#!/usr/bin/env bash
# Обновляет граф знаний кодовой базы (Graphify) в graphify-out/.
# Идемпотентен — безопасно вешать на cron / post-commit hook.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Предпочитаем graphify из .venv проекта, иначе системный.
GRAPHIFY="${GRAPHIFY_BIN:-graphify}"
if [[ -x "$ROOT/.venv/bin/graphify" ]]; then
  GRAPHIFY="$ROOT/.venv/bin/graphify"
fi

# update = ре-экстракция AST и перестройка графа, LLM не требуется.
"$GRAPHIFY" update "$ROOT"
echo "Graphify: граф обновлён -> $ROOT/graphify-out/"
