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

./scripts/bootstrap_credential_keyring.sh >/dev/null

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
fi

api_host="${API_HOST:-127.0.0.1}"
api_port="${API_PORT:-8000}"
frontend_host="${FRONTEND_HOST:-127.0.0.1}"
dev_log_dir="${LLMBENCHLAB_DEV_LOG_DIR:-$project_root/artifacts/dev-logs}"
mkdir -p "$dev_log_dir"
chmod 700 "$dev_log_dir"

api_log="$dev_log_dir/api.log"
worker_log="$dev_log_dir/worker.log"
frontend_log="$dev_log_dir/frontend.log"
session_started_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
for service_log in "$api_log" "$worker_log" "$frontend_log"; do
  touch "$service_log"
  chmod 600 "$service_log"
  printf '\n=== LLMBenchLab dev session %s ===\n' "$session_started_at" >>"$service_log"
done

backend_pid=""
worker_pid=""
frontend_pid=""

# Invoked indirectly by the EXIT trap below.
# shellcheck disable=SC2329
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  for process_id in "$backend_pid" "$worker_pid" "$frontend_pid"; do
    if [[ -n "$process_id" ]] && kill -0 "$process_id" 2>/dev/null; then
      kill "$process_id" 2>/dev/null || true
    fi
  done
  for process_id in "$backend_pid" "$worker_pid" "$frontend_pid"; do
    if [[ -n "$process_id" ]]; then
      wait "$process_id" 2>/dev/null || true
    fi
  done
  exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT TERM

printf 'Starting LLMBenchLab: Web http://%s:5173 | API http://%s:%s\n' \
  "$frontend_host" "$api_host" "$api_port"
printf 'Logs: API %s | Worker %s | frontend %s\n' \
  "$api_log" "$worker_log" "$frontend_log"
(
  cd backend
  exec uv run uvicorn app.main:app --host "$api_host" --port "$api_port" --reload
) >>"$api_log" 2>&1 &
backend_pid=$!

(
  cd backend
  exec uv run python -m app.worker
) >>"$worker_log" 2>&1 &
worker_pid=$!

(
  cd frontend
  exec npm run dev -- --host "$frontend_host"
) >>"$frontend_log" 2>&1 &
frontend_pid=$!

stopped_service=""
stopped_pid=""
stopped_log=""
while true; do
  if ! kill -0 "$backend_pid" 2>/dev/null; then
    stopped_service="API"
    stopped_pid="$backend_pid"
    stopped_log="$api_log"
    break
  fi
  if ! kill -0 "$worker_pid" 2>/dev/null; then
    stopped_service="Worker"
    stopped_pid="$worker_pid"
    stopped_log="$worker_log"
    break
  fi
  if ! kill -0 "$frontend_pid" 2>/dev/null; then
    stopped_service="frontend"
    stopped_pid="$frontend_pid"
    stopped_log="$frontend_log"
    break
  fi
  sleep 1
done

if wait "$stopped_pid"; then
  status=0
else
  status=$?
fi

if (( status == 0 )); then
  printf '%s stopped with status 0; shutting down the other services. Log: %s\n' \
    "$stopped_service" "$stopped_log"
else
  printf 'Error: %s exited with status %s. Log: %s\n' \
    "$stopped_service" "$status" "$stopped_log" >&2
fi
exit "$status"
