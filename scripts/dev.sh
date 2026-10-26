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

worker_processes_was_set=false
worker_processes_override=""
if [[ ${LLMBENCHLAB_DEV_WORKER_PROCESSES+x} == x ]]; then
  worker_processes_was_set=true
  worker_processes_override="$LLMBENCHLAB_DEV_WORKER_PROCESSES"
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
fi

if [[ "$worker_processes_was_set" == true ]]; then
  LLMBENCHLAB_DEV_WORKER_PROCESSES="$worker_processes_override"
fi

worker_processes="${LLMBENCHLAB_DEV_WORKER_PROCESSES:-1}"
case "$worker_processes" in
  [1-9]|[12][0-9]|3[0-2]) ;;
  *)
    echo "Error: LLMBENCHLAB_DEV_WORKER_PROCESSES must be an integer from 1 through 32." >&2
    exit 1
    ;;
esac

if (( worker_processes > 1 )); then
  if [[ ${LLMBENCHLAB_DATABASE_URL+x} == x ]]; then
    effective_database_url="$LLMBENCHLAB_DATABASE_URL"
  elif [[ ${DATABASE_URL+x} == x ]]; then
    effective_database_url="$DATABASE_URL"
  else
    effective_database_url="sqlite:///./data/llmbenchlab.db"
  fi
  case "$effective_database_url" in
    postgresql://*|postgresql+?*://*) ;;
    *)
      echo "Error: multiple development Workers require a PostgreSQL database URL." >&2
      exit 1
      ;;
  esac
  unset effective_database_url
fi

export LLMBENCHLAB_WORKER_EXPECTED_PROCESSES="$worker_processes"

./scripts/bootstrap_credential_keyring.sh >/dev/null

api_host="${API_HOST:-127.0.0.1}"
api_port="${API_PORT:-8000}"
frontend_host="${FRONTEND_HOST:-127.0.0.1}"
dev_log_dir="${LLMBENCHLAB_DEV_LOG_DIR:-$project_root/artifacts/dev-logs}"
mkdir -p "$dev_log_dir"
chmod 700 "$dev_log_dir"

api_log="$dev_log_dir/api.log"
frontend_log="$dev_log_dir/frontend.log"
worker_logs=()
if (( worker_processes == 1 )); then
  worker_logs[0]="$dev_log_dir/worker.log"
else
  worker_index=1
  while (( worker_index <= worker_processes )); do
    worker_logs[worker_index - 1]="$dev_log_dir/worker-${worker_index}.log"
    worker_index=$((worker_index + 1))
  done
fi

session_started_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
service_logs=("$api_log")
for worker_log in "${worker_logs[@]}"; do
  service_logs[${#service_logs[@]}]="$worker_log"
done
service_logs[${#service_logs[@]}]="$frontend_log"
for service_log in "${service_logs[@]}"; do
  touch "$service_log"
  chmod 600 "$service_log"
  printf '\n=== LLMBenchLab dev session %s ===\n' "$session_started_at" >>"$service_log"
done

service_names=()
service_pids=()
service_log_paths=()
service_count=0

register_service() {
  service_names[service_count]="$1"
  service_pids[service_count]="$2"
  service_log_paths[service_count]="$3"
  service_count=$((service_count + 1))
}

# Invoked indirectly by the EXIT trap through cleanup.
# shellcheck disable=SC2329
terminate_children() {
  local signal_name="$1"
  local service_index=0
  local process_id
  while (( service_index < service_count )); do
    process_id="${service_pids[$service_index]}"
    if kill -0 "$process_id" 2>/dev/null; then
      kill "-$signal_name" "$process_id" 2>/dev/null || true
    fi
    service_index=$((service_index + 1))
  done
}

# Invoked indirectly by the EXIT trap through cleanup.
# shellcheck disable=SC2329
wait_for_children() {
  local service_index=0
  while (( service_index < service_count )); do
    wait "${service_pids[$service_index]}" 2>/dev/null || true
    service_index=$((service_index + 1))
  done
}

# Invoked indirectly by the EXIT trap below.
# shellcheck disable=SC2329
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  terminate_children TERM
  wait_for_children
  exit "$status"
}

# Invoked indirectly by the signal traps below.
# shellcheck disable=SC2329
forward_signal() {
  if [[ "$1" == "INT" ]]; then
    exit 130
  fi
  exit 143
}

trap cleanup EXIT
trap 'forward_signal INT' INT
trap 'forward_signal TERM' TERM

printf 'Starting LLMBenchLab: Web http://%s:5173 | API http://%s:%s\n' \
  "$frontend_host" "$api_host" "$api_port"
if (( worker_processes == 1 )); then
  printf 'Logs: API %s | Worker %s | frontend %s\n' \
    "$api_log" "${worker_logs[0]}" "$frontend_log"
else
  printf 'Logs: API %s | Workers' "$api_log"
  for worker_log in "${worker_logs[@]}"; do
    printf ' %s' "$worker_log"
  done
  printf ' | frontend %s\n' "$frontend_log"
fi
(
  cd backend
  exec uv run uvicorn app.main:app --host "$api_host" --port "$api_port" --reload
) >>"$api_log" 2>&1 &
backend_pid=$!
register_service "API" "$backend_pid" "$api_log"

worker_index=1
while (( worker_index <= worker_processes )); do
  worker_log="${worker_logs[$((worker_index - 1))]}"
  (
    cd backend
    if (( worker_processes > 1 )); then
      export LLMBENCHLAB_DEV_WORKER_INDEX="$worker_index"
    fi
    exec uv run python -m app.worker
  ) >>"$worker_log" 2>&1 &
  worker_pid=$!
  if (( worker_processes == 1 )); then
    worker_name="Worker"
  else
    worker_name="Worker $worker_index"
  fi
  register_service "$worker_name" "$worker_pid" "$worker_log"
  worker_index=$((worker_index + 1))
done

(
  cd frontend
  exec npm run dev -- --host "$frontend_host"
) >>"$frontend_log" 2>&1 &
frontend_pid=$!
register_service "frontend" "$frontend_pid" "$frontend_log"

stopped_service=""
stopped_pid=""
stopped_log=""
while true; do
  service_index=0
  while (( service_index < service_count )); do
    if ! kill -0 "${service_pids[$service_index]}" 2>/dev/null; then
      stopped_service="${service_names[$service_index]}"
      stopped_pid="${service_pids[$service_index]}"
      stopped_log="${service_log_paths[$service_index]}"
      break 2
    fi
    service_index=$((service_index + 1))
  done
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
