#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

for command_name in uv npm; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Error: '$command_name' is required. Run scripts/setup.sh after installing prerequisites." >&2
    exit 1
  fi
done

if [[ ! -d backend/.venv || ! -d frontend/node_modules ]]; then
  echo "Error: dependencies are missing. Run 'make setup' first." >&2
  exit 1
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
else
  echo "Notice: .env is absent; safe local defaults will be used. Run 'make setup' to create it."
fi

api_host="${API_HOST:-127.0.0.1}"
api_port="${API_PORT:-8000}"
frontend_host="${FRONTEND_HOST:-127.0.0.1}"

backend_pid=""
frontend_pid=""

# Invoked indirectly by the EXIT trap below.
# shellcheck disable=SC2329
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  for process_id in "$backend_pid" "$frontend_pid"; do
    if [[ -n "$process_id" ]] && kill -0 "$process_id" 2>/dev/null; then
      kill "$process_id" 2>/dev/null || true
    fi
  done
  for process_id in "$backend_pid" "$frontend_pid"; do
    if [[ -n "$process_id" ]]; then
      wait "$process_id" 2>/dev/null || true
    fi
  done
  exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT TERM

echo "Starting API at http://${api_host}:${api_port}"
(
  cd backend
  exec uv run uvicorn app.main:app --host "$api_host" --port "$api_port" --reload
) &
backend_pid=$!

echo "Starting web app at http://${frontend_host}:5173"
(
  cd frontend
  exec npm run dev -- --host "$frontend_host"
) &
frontend_pid=$!

status=0
while true; do
  if ! kill -0 "$backend_pid" 2>/dev/null; then
    wait "$backend_pid" || status=$?
    break
  fi
  if ! kill -0 "$frontend_pid" 2>/dev/null; then
    wait "$frontend_pid" || status=$?
    break
  fi
  sleep 1
done

if (( status == 0 )); then
  echo "A development service stopped; shutting down the other service."
else
  echo "Error: a development service exited with status ${status}." >&2
fi
exit "$status"
