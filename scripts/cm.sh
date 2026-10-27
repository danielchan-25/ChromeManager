#!/usr/bin/env bash
set -euo pipefail
PROJECT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec "$PROJECT/.venv/bin/python" -m chrome_manager.cli "$@"
