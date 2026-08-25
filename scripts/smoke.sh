#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "Error: 'uv' is required. Run 'make setup' after installing uv." >&2
  exit 1
fi

smoke_tmp="$(mktemp -d "${TMPDIR:-/tmp}/llmbenchlab-smoke.XXXXXX")"
cleanup() {
  if [[ -n "$smoke_tmp" && -d "$smoke_tmp" ]]; then
    rm -rf -- "$smoke_tmp"
  fi
}
trap cleanup EXIT INT TERM

export LLMBENCHLAB_DATABASE_URL="sqlite:///${smoke_tmp}/smoke.db"
export LLMBENCHLAB_LOG_LEVEL=WARNING
export LLMBENCHLAB_REDIS_URL=

echo "Running offline Mock smoke test with an isolated temporary SQLite database..."
cd "$project_root/backend"
uv run pytest -m smoke tests/test_smoke.py
