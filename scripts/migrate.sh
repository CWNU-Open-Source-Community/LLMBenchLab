#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "Error: 'uv' is required. Install it from https://docs.astral.sh/uv/." >&2
  exit 1
fi

cd "$project_root"
set -a
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source ./.env
fi
set +a

cd backend
uv run python -m app.db.prepare_migrations
uv run alembic upgrade head
