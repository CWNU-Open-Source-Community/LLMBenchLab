#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

# Docker Compose reads .env itself while preserving values exported by the
# invoking shell. Read only this numeric launcher setting without sourcing the
# file, so dotenv content is never executed and unrelated precedence is intact.
if [[ ${LLMBENCHLAB_COMPOSE_WORKER_PROCESSES+x} != x && -f .env ]]; then
  dotenv_worker_processes=""
  while IFS= read -r dotenv_line || [[ -n "$dotenv_line" ]]; do
    dotenv_assignment="${dotenv_line#"${dotenv_line%%[![:space:]]*}"}"
    case "$dotenv_assignment" in
      export[[:space:]]*)
        dotenv_assignment="${dotenv_assignment#export}"
        dotenv_assignment="${dotenv_assignment#"${dotenv_assignment%%[![:space:]]*}"}"
        ;;
    esac
    case "$dotenv_assignment" in
      LLMBENCHLAB_COMPOSE_WORKER_PROCESSES=*)
        dotenv_candidate="${dotenv_assignment#*=}"
        dotenv_candidate="${dotenv_candidate%%#*}"
        dotenv_candidate="${dotenv_candidate#"${dotenv_candidate%%[![:space:]]*}"}"
        dotenv_candidate="${dotenv_candidate%"${dotenv_candidate##*[![:space:]]}"}"
        case "$dotenv_candidate" in
          \"*\") dotenv_candidate="${dotenv_candidate#\"}"; dotenv_candidate="${dotenv_candidate%\"}" ;;
          \'*\') dotenv_candidate="${dotenv_candidate#\'}"; dotenv_candidate="${dotenv_candidate%\'}" ;;
        esac
        dotenv_worker_processes="$dotenv_candidate"
        ;;
    esac
  done <.env
  if [[ -n "$dotenv_worker_processes" ]]; then
    LLMBENCHLAB_COMPOSE_WORKER_PROCESSES="$dotenv_worker_processes"
  fi
  unset dotenv_assignment dotenv_candidate dotenv_line dotenv_worker_processes
fi

worker_processes="${LLMBENCHLAB_COMPOSE_WORKER_PROCESSES-2}"
case "$worker_processes" in
  [1-9]|[12][0-9]|3[0-2]) ;;
  *)
    echo "Error: LLMBENCHLAB_COMPOSE_WORKER_PROCESSES must be an integer from 1 through 32." >&2
    exit 2
    ;;
esac

export LLMBENCHLAB_COMPOSE_WORKER_PROCESSES="$worker_processes"
export LLMBENCHLAB_COMPOSE_WORKER_EXPECTED_PROCESSES="$worker_processes"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: 'docker' is required to start the Compose stack." >&2
  exit 1
fi

if ! current_worker_ids="$(docker compose ps --all -q worker 2>/dev/null)"; then
  echo "Error: unable to inspect all current Compose Worker replicas." >&2
  exit 1
fi
current_worker_processes="$(
  printf '%s\n' "$current_worker_ids" | awk 'NF { count += 1 } END { print count + 0 }'
)"
unset current_worker_ids

if ! running_worker_ids="$(docker compose ps --status running -q worker 2>/dev/null)"; then
  echo "Error: unable to inspect the running Compose Worker replicas." >&2
  exit 1
fi
running_worker_processes="$(
  printf '%s\n' "$running_worker_ids" | awk 'NF { count += 1 } END { print count + 0 }'
)"
unset running_worker_ids

new_worker_processes=$((worker_processes - running_worker_processes))
if (( new_worker_processes < 0 )); then
  new_worker_processes=0
fi

docker compose build
docker compose up \
  --wait \
  --wait-timeout 180 \
  --remove-orphans \
  postgres redis migrate

scan_watermark=""
if (( new_worker_processes > 0 )); then
  watermark_probe='from app.db.clock import database_utc_now
from app.db.session import SessionLocal
with SessionLocal() as session:
    print("LLMBENCHLAB_SCAN_WATERMARK=" + database_utc_now(session).isoformat())'
  watermark_output=""
  if ! watermark_output="$(
    docker compose run --rm --no-deps -T worker python -c "$watermark_probe" 2>/dev/null
  )"; then
    echo "Error: unable to read the application database clock before starting Workers." >&2
    exit 1
  fi
  scan_watermark="$(
    printf '%s\n' "$watermark_output" |
      awk '/^LLMBENCHLAB_SCAN_WATERMARK=/ {
        sub(/^LLMBENCHLAB_SCAN_WATERMARK=/, "")
        value = $0
      } END { print value }'
  )"
  unset watermark_output watermark_probe
  case "$scan_watermark" in
    ????-??-??T??:??:??*) ;;
    *)
      echo "Error: application database clock returned an invalid scan watermark." >&2
      exit 1
      ;;
  esac
fi

start_workers() {
  docker compose up \
    --wait \
    --wait-timeout 180 \
    --remove-orphans \
    --no-deps \
    --scale "worker=$worker_processes" \
    worker
}

start_api() {
  docker compose up \
    --wait \
    --wait-timeout 180 \
    --remove-orphans \
    --no-deps \
    --force-recreate \
    api
}

scan_probe='import sys
from datetime import datetime, timedelta
from sqlalchemy import func, select
from app.core.config import get_settings
from app.db.clock import database_utc_now
from app.db.session import SessionLocal
from app.models import WorkerProcess
expected = int(sys.argv[1])
new_required = int(sys.argv[3])
watermark = datetime.fromisoformat(sys.argv[2]) if new_required else None
try:
    with SessionLocal() as session:
        now = database_utc_now(session)
        cutoff = now - timedelta(seconds=get_settings().worker_progress_stale_seconds)
        live_scanned = int(session.scalar(
            select(func.count()).select_from(WorkerProcess).where(
                WorkerProcess.stopped_at.is_(None),
                WorkerProcess.last_seen_at >= cutoff,
                WorkerProcess.last_scan_at.is_not(None),
            )
        ) or 0)
        new_scanned = 0
        if watermark is not None:
            new_scanned = int(session.scalar(
                select(func.count()).select_from(WorkerProcess).where(
                    WorkerProcess.stopped_at.is_(None),
                    WorkerProcess.last_seen_at >= cutoff,
                    WorkerProcess.started_at >= watermark,
                    WorkerProcess.last_scan_at >= watermark,
                )
            ) or 0)
except Exception:
    raise SystemExit(1)
print(f"{live_scanned}/{new_scanned}")
raise SystemExit(0 if live_scanned == expected and new_scanned >= new_required else 1)'

wait_for_worker_scans() {
  local scan_attempts=30
  local scan_attempt
  local scan_output=""
  local last_scanned="unavailable"
  for (( scan_attempt = 1; scan_attempt <= scan_attempts; scan_attempt += 1 )); do
    scan_output=""
    if scan_output="$(
      docker compose exec -T worker python -c "$scan_probe" \
        "$worker_processes" "$scan_watermark" "$new_worker_processes" 2>/dev/null
    )" && [[ "$scan_output" == "$worker_processes/"* ]]; then
      return 0
    fi
    if [[ "$scan_output" =~ ^[0-9]+/[0-9]+$ ]]; then
      last_scanned="$scan_output"
    else
      last_scanned="unavailable"
    fi
    if (( scan_attempt < scan_attempts )); then
      sleep 1
    fi
  done
  printf 'Error: timed out waiting for %s fresh Worker generations to have a database scan and %s newly started generations to scan after the scale watermark; last observed live/new=%s. The Compose stack was left running for inspection.\n' \
    "$worker_processes" "$new_worker_processes" "$last_scanned" >&2
  return 1
}

# ADR-0016 requires opposite ordering for the two directions: lower API expected
# before shrinking, but prove the new Workers scan before raising API expected.
if (( current_worker_processes > worker_processes )); then
  start_api
  start_workers
  wait_for_worker_scans
else
  start_workers
  wait_for_worker_scans
  start_api
fi

docker compose up \
  --wait \
  --wait-timeout 180 \
  --remove-orphans \
  --no-deps \
  frontend

metrics_probe='import json, sys, urllib.request
expected = int(sys.argv[1])
try:
    with urllib.request.urlopen(
        "http://127.0.0.1:8000/api/v1/tasks/metrics", timeout=2
    ) as response:
        payload = json.load(response)
    gauges = (
        int(payload["worker_expected_processes"]),
        int(payload["worker_registered_processes"]),
        int(payload["worker_live_processes"]),
        int(payload["worker_stalled_processes"]),
        int(payload["worker_shortfall_processes"]),
    )
except (KeyError, TypeError, ValueError, OSError):
    raise SystemExit(1)
print("/".join(str(value) for value in gauges))
raise SystemExit(0 if gauges == (expected, expected, expected, 0, 0) else 1)'

metrics_attempts=30
last_metrics="unavailable"
for (( attempt = 1; attempt <= metrics_attempts; attempt += 1 )); do
  metrics_output=""
  if metrics_output="$(
    docker compose exec -T api python -c "$metrics_probe" "$worker_processes" 2>/dev/null
  )" && [[ "$metrics_output" == "$worker_processes/$worker_processes/$worker_processes/0/0" ]]; then
    printf 'LLMBenchLab Compose is ready; Worker expected/registered/live/stalled/shortfall=%s.\n' \
      "$metrics_output"
    exit 0
  fi

  if [[ "$metrics_output" =~ ^[0-9]+/[0-9]+/[0-9]+/[0-9]+/[0-9]+$ ]]; then
    last_metrics="$metrics_output"
  else
    last_metrics="unavailable"
  fi
  if (( attempt < metrics_attempts )); then
    sleep 1
  fi
done

printf 'Error: timed out waiting for Worker expected/registered/live/stalled/shortfall=%s/%s/%s/0/0; last observed=%s. The Compose stack was left running for inspection.\n' \
  "$worker_processes" "$worker_processes" "$worker_processes" "$last_metrics" >&2
exit 1
