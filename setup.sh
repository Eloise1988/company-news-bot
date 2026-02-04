#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${1:-}" ]]; then
  echo "Usage: ./setup.sh /apps/AI/CompaniesToFollow"
  exit 1
fi

ROOT="$1"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Please install Python 3."
  exit 1
fi

python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"

mkdir -p "$ROOT/logs"
mkdir -p "$ROOT/data"

echo "Setup complete."
