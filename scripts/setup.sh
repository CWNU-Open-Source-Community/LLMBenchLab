#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

require_command() {
  local command_name="$1"
  local install_hint="$2"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Error: '$command_name' is required. $install_hint" >&2
    exit 1
  fi
}

require_command python3 "Install Python 3.11 or newer."
require_command uv "Install uv from https://docs.astral.sh/uv/."
require_command node "Install Node.js 22 or newer."
require_command npm "Install npm with Node.js."

if [[ ! -f .env.example ]]; then
  echo "Error: .env.example is missing from $project_root." >&2
  exit 1
fi

if [[ -e .env ]]; then
  echo "Keeping existing .env unchanged."
else
  cp .env.example .env
  echo "Created .env from .env.example. Review it before configuring a real provider."
fi

mkdir -p backend/data

echo "Installing locked backend dependencies..."
(
  cd backend
  uv sync --frozen --extra dev
)

echo "Installing frontend dependencies..."
if [[ -f frontend/package-lock.json ]]; then
  npm --prefix frontend ci
else
  npm --prefix frontend install
fi

echo "Applying database migrations..."
./scripts/migrate.sh

echo "Setup complete. Start both services with: make dev"
