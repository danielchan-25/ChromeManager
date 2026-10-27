#!/usr/bin/env bash
set -euo pipefail
PROJECT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$PROJECT"
export CHROME_MANAGER_DATA_ROOT=${CHROME_MANAGER_DATA_ROOT:-"$PROJECT/.data"}
export PYTHONUTF8=1 PYTHONUNBUFFERED=1
exec "$PROJECT/.venv/bin/python" -m chrome_manager.cli web "$@"
