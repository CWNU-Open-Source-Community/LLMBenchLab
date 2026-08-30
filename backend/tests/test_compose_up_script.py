"""Offline checks for the bounded multi-Worker Compose launcher."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_UP_SCRIPT = REPOSITORY_ROOT / "scripts" / "compose_up.sh"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _prepare_project(tmp_path: Path, *, dotenv: str | None = None) -> tuple[Path, Path]:
    project = tmp_path / "project"
    scripts = project / "scripts"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    (scripts / "compose_up.sh").write_text(
        COMPOSE_UP_SCRIPT.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    if dotenv is not None:
        (project / ".env").write_text(dotenv, encoding="utf-8")

    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" != "compose" ]]; then
  exit 90
fi
shift
case "${1:-}" in
  ps)
    if [[ "${2:-}" == "--all" && "${3:-}" == "-q" && "${4:-}" == "worker" ]]; then
      printf 'ps:all\n' >>"$DOCKER_CALLS_FILE"
      current_workers="${FAKE_ALL_WORKERS:-0}"
    elif [[ "${2:-}" == "--status" && "${3:-}" == "running" \
      && "${4:-}" == "-q" && "${5:-}" == "worker" ]]; then
      printf 'ps:running\n' >>"$DOCKER_CALLS_FILE"
      current_workers="${FAKE_RUNNING_WORKERS:-0}"
    else
      exit 95
    fi
    for ((worker_index = 1; worker_index <= current_workers; worker_index += 1)); do
      printf 'fake-worker-%s\n' "$worker_index"
    done
    ;;
  build)
    printf 'build\n' >>"$DOCKER_CALLS_FILE"
    ;;
  up)
    if [[ -n "${FAKE_EXPECTED_DATABASE_URL:-}" \
      && "${LLMBENCHLAB_COMPOSE_DATABASE_URL:-}" != "$FAKE_EXPECTED_DATABASE_URL" ]]; then
      exit 94
    fi
    printf 'up:%s\n' "$*" >>"$DOCKER_CALLS_FILE"
    for argument in "$@"; do
      if [[ "$argument" == "api" ]]; then
        printf '%s\n' "${LLMBENCHLAB_COMPOSE_WORKER_EXPECTED_PROCESSES-unset}" \
          >"$EXPECTED_PROCESSES_FILE"
      fi
    done
    exit "${FAKE_UP_EXIT_CODE:-0}"
    ;;
  run)
    if [[ " $* " != *" --rm --no-deps -T worker python -c "* \
      || "${8:-}" != *"database_utc_now"* ]]; then
      exit 96
    fi
    printf 'watermark\n' >>"$DOCKER_CALLS_FILE"
    printf 'LLMBENCHLAB_SCAN_WATERMARK=2026-08-30T08:00:00+00:00\n'
    ;;
  exec)
    if [[ "${2:-}" != "-T" || "${4:-}" != "python" || "${5:-}" != "-c" \
      || "${7:-}" != "${LLMBENCHLAB_COMPOSE_WORKER_EXPECTED_PROCESSES}" ]]; then
      exit 93
    fi
    expected="${LLMBENCHLAB_COMPOSE_WORKER_EXPECTED_PROCESSES}"
    if [[ "${3:-}" == "worker" && "${6:-}" == *"WorkerProcess"* \
      && "${6:-}" == *"last_scan_at"* && "${6:-}" == *"last_seen_at >= cutoff"* \
      && "${6:-}" == *"last_scan_at >= watermark"* \
      && "${9:-}" =~ ^[0-9]+$ ]]; then
      printf 'scan\n' >>"$DOCKER_CALLS_FILE"
      new_required="${9:-0}"
      scan_attempt=0
      if [[ -f "$SCAN_ATTEMPT_FILE" ]]; then
        scan_attempt="$(<"$SCAN_ATTEMPT_FILE")"
      fi
      scan_attempt=$((scan_attempt + 1))
      printf '%s\n' "$scan_attempt" >"$SCAN_ATTEMPT_FILE"
      case "${FAKE_SCAN_MODE:-ready}" in
        ready)
          printf '%s/%s\n' "$expected" "$new_required"
          exit 0
          ;;
        transition)
          if (( scan_attempt == 1 )); then
            printf '0/0\n'
            exit 1
          fi
          printf '%s/%s\n' "$expected" "$new_required"
          exit 0
          ;;
        never)
          printf '0/0\n'
          exit 1
          ;;
        stale)
          printf '%s/0\n' "$expected"
          exit 1
          ;;
        *)
          exit 91
          ;;
      esac
    fi
    if [[ "${3:-}" == "api" && "${6:-}" == *"/api/v1/tasks/metrics"* \
      && "${6:-}" == *"worker_expected_processes"* \
      && "${6:-}" == *"worker_registered_processes"* \
      && "${6:-}" == *"worker_live_processes"* \
      && "${6:-}" == *"worker_stalled_processes"* \
      && "${6:-}" == *"worker_shortfall_processes"* ]]; then
      printf 'metrics\n' >>"$DOCKER_CALLS_FILE"
      attempt=0
      if [[ -f "$METRICS_ATTEMPT_FILE" ]]; then
        attempt="$(<"$METRICS_ATTEMPT_FILE")"
      fi
      attempt=$((attempt + 1))
      printf '%s\n' "$attempt" >"$METRICS_ATTEMPT_FILE"
      case "${FAKE_METRICS_MODE:-ready}" in
        ready)
          printf '%s/%s/%s/0/0\n' "$expected" "$expected" "$expected"
          exit 0
          ;;
        transition)
          if (( attempt == 1 )); then
            printf '%s/%s/0/0/%s\n' "$expected" "$expected" "$expected"
            exit 1
          fi
          printf '%s/%s/%s/0/0\n' "$expected" "$expected" "$expected"
          exit 0
          ;;
        never)
          printf '%s/%s/0/0/%s\n' "$expected" "$expected" "$expected"
          exit 1
          ;;
        stale)
          printf '%s/%s/%s/1/0\n' "$expected" "$((expected + 1))" "$expected"
          exit 1
          ;;
        *)
          exit 91
          ;;
      esac
    fi
    exit 93
    ;;
  *)
    printf 'unexpected:%s\n' "$*" >>"$DOCKER_CALLS_FILE"
    exit 92
    ;;
esac
""",
    )
    _write_executable(
        fake_bin / "sleep",
        """#!/usr/bin/env bash
exit 0
""",
    )
    return project, fake_bin


def _run_compose_up(
    project: Path,
    fake_bin: Path,
    *,
    worker_processes: str | None = None,
    metrics_mode: str = "ready",
    scan_mode: str = "ready",
    current_workers: int = 0,
    all_workers: int | None = None,
    extra_environment: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    calls_file = project / "docker-calls.log"
    expected_file = project / "expected-processes.log"
    attempt_file = project / "metrics-attempts.log"
    scan_attempt_file = project / "scan-attempts.log"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "DOCKER_CALLS_FILE": str(calls_file),
            "EXPECTED_PROCESSES_FILE": str(expected_file),
            "METRICS_ATTEMPT_FILE": str(attempt_file),
            "SCAN_ATTEMPT_FILE": str(scan_attempt_file),
            "FAKE_METRICS_MODE": metrics_mode,
            "FAKE_SCAN_MODE": scan_mode,
            "FAKE_RUNNING_WORKERS": str(current_workers),
            "FAKE_ALL_WORKERS": str(current_workers if all_workers is None else all_workers),
        }
    )
    environment.pop("LLMBENCHLAB_COMPOSE_WORKER_PROCESSES", None)
    environment.pop("LLMBENCHLAB_COMPOSE_WORKER_EXPECTED_PROCESSES", None)
    if worker_processes is not None:
        environment["LLMBENCHLAB_COMPOSE_WORKER_PROCESSES"] = worker_processes
    if extra_environment is not None:
        environment.update(extra_environment)
    result = subprocess.run(
        ["bash", str(project / "scripts" / "compose_up.sh")],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result, calls_file, expected_file, attempt_file


def test_compose_launcher_defaults_to_two_workers(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)

    result, calls_file, expected_file, attempt_file = _run_compose_up(project, fake_bin)

    assert result.returncode == 0
    assert calls_file.read_text(encoding="utf-8").splitlines() == [
        "ps:all",
        "ps:running",
        "build",
        "up:up --wait --wait-timeout 180 --remove-orphans postgres redis migrate",
        "watermark",
        "up:up --wait --wait-timeout 180 --remove-orphans --no-deps --scale worker=2 worker",
        "scan",
        "up:up --wait --wait-timeout 180 --remove-orphans --no-deps --force-recreate api",
        "up:up --wait --wait-timeout 180 --remove-orphans --no-deps frontend",
        "metrics",
    ]
    assert expected_file.read_text(encoding="utf-8") == "2\n"
    assert attempt_file.read_text(encoding="utf-8") == "1\n"
    assert "Worker expected/registered/live/stalled/shortfall=2/2/2/0/0" in result.stdout


def test_compose_launcher_uses_explicit_worker_count(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)

    result, calls_file, expected_file, _ = _run_compose_up(
        project,
        fake_bin,
        worker_processes="4",
    )

    assert result.returncode == 0
    assert "--scale worker=4" in calls_file.read_text(encoding="utf-8")
    assert expected_file.read_text(encoding="utf-8") == "4\n"
    assert "Worker expected/registered/live/stalled/shortfall=4/4/4/0/0" in result.stdout


def test_compose_launcher_reads_worker_count_from_dotenv(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(
        tmp_path,
        dotenv='export LLMBENCHLAB_COMPOSE_WORKER_PROCESSES="6" # local scale\n',
    )

    result, calls_file, expected_file, _ = _run_compose_up(project, fake_bin)

    assert result.returncode == 0
    assert "--scale worker=6" in calls_file.read_text(encoding="utf-8")
    assert expected_file.read_text(encoding="utf-8") == "6\n"


def test_explicit_worker_count_overrides_dotenv(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(
        tmp_path,
        dotenv="LLMBENCHLAB_COMPOSE_WORKER_PROCESSES=7\n",
    )

    result, calls_file, expected_file, _ = _run_compose_up(
        project,
        fake_bin,
        worker_processes="3",
    )

    assert result.returncode == 0
    assert "--scale worker=3" in calls_file.read_text(encoding="utf-8")
    assert expected_file.read_text(encoding="utf-8") == "3\n"


def test_dotenv_does_not_override_unrelated_explicit_compose_environment(
    tmp_path: Path,
) -> None:
    project, fake_bin = _prepare_project(
        tmp_path,
        dotenv=(
            "LLMBENCHLAB_COMPOSE_WORKER_PROCESSES=2\n"
            "LLMBENCHLAB_COMPOSE_DATABASE_URL=postgresql://dotenv-secret@postgres/db\n"
        ),
    )

    result, _, _, _ = _run_compose_up(
        project,
        fake_bin,
        extra_environment={
            "LLMBENCHLAB_COMPOSE_DATABASE_URL": "postgresql://explicit-secret@postgres/db",
            "FAKE_EXPECTED_DATABASE_URL": "postgresql://explicit-secret@postgres/db",
        },
    )

    assert result.returncode == 0
    assert "dotenv-secret" not in result.stdout + result.stderr
    assert "explicit-secret" not in result.stdout + result.stderr


def test_dotenv_is_parsed_without_executing_shell_content(tmp_path: Path) -> None:
    marker = tmp_path / "dotenv-command-ran"
    project, fake_bin = _prepare_project(
        tmp_path,
        dotenv=(f"UNRELATED=$(touch {marker})\nLLMBENCHLAB_COMPOSE_WORKER_PROCESSES=2\n"),
    )

    result, _, _, _ = _run_compose_up(project, fake_bin)

    assert result.returncode == 0
    assert not marker.exists()


@pytest.mark.parametrize(
    "worker_processes",
    ["", "0", "02", "33", "-1", "2.5", "two", " 2", "18446744073709551618"],
)
def test_invalid_worker_count_fails_before_calling_docker(
    tmp_path: Path,
    worker_processes: str,
) -> None:
    project, fake_bin = _prepare_project(tmp_path)

    result, calls_file, expected_file, attempt_file = _run_compose_up(
        project,
        fake_bin,
        worker_processes=worker_processes,
    )

    assert result.returncode == 2
    assert "must be an integer from 1 through 32" in result.stderr
    assert not calls_file.exists()
    assert not expected_file.exists()
    assert not attempt_file.exists()


def test_metrics_poll_retries_until_all_workers_are_live(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)

    result, calls_file, _, attempt_file = _run_compose_up(
        project,
        fake_bin,
        metrics_mode="transition",
    )

    assert result.returncode == 0
    assert calls_file.read_text(encoding="utf-8").splitlines() == [
        "ps:all",
        "ps:running",
        "build",
        "up:up --wait --wait-timeout 180 --remove-orphans postgres redis migrate",
        "watermark",
        "up:up --wait --wait-timeout 180 --remove-orphans --no-deps --scale worker=2 worker",
        "scan",
        "up:up --wait --wait-timeout 180 --remove-orphans --no-deps --force-recreate api",
        "up:up --wait --wait-timeout 180 --remove-orphans --no-deps frontend",
        "metrics",
        "metrics",
    ]
    assert attempt_file.read_text(encoding="utf-8") == "2\n"
    assert "Worker expected/registered/live/stalled/shortfall=2/2/2/0/0" in result.stdout


def test_scale_up_waits_for_worker_scans_before_recreating_api(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)

    result, calls_file, _, _ = _run_compose_up(
        project,
        fake_bin,
        worker_processes="2",
        current_workers=1,
        scan_mode="transition",
    )

    assert result.returncode == 0
    calls = calls_file.read_text(encoding="utf-8").splitlines()
    worker_up = next(index for index, call in enumerate(calls) if "--scale worker=2" in call)
    api_up = next(index for index, call in enumerate(calls) if call.endswith(" api"))
    assert calls[worker_up + 1 : api_up] == ["scan", "scan"]
    assert worker_up < api_up


def test_scale_down_recreates_api_before_graceful_worker_scale(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)

    result, calls_file, _, _ = _run_compose_up(
        project,
        fake_bin,
        worker_processes="1",
        current_workers=2,
    )

    assert result.returncode == 0
    calls = calls_file.read_text(encoding="utf-8").splitlines()
    api_up = next(index for index, call in enumerate(calls) if call.endswith(" api"))
    worker_up = next(index for index, call in enumerate(calls) if "--scale worker=1" in call)
    assert api_up < worker_up
    assert calls[worker_up + 1] == "scan"


def test_exited_replica_is_counted_when_selecting_scale_down_order(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)

    result, calls_file, _, _ = _run_compose_up(
        project,
        fake_bin,
        worker_processes="1",
        current_workers=1,
        all_workers=2,
    )

    assert result.returncode == 0
    calls = calls_file.read_text(encoding="utf-8").splitlines()
    assert calls[:2] == ["ps:all", "ps:running"]
    api_up = next(index for index, call in enumerate(calls) if call.endswith(" api"))
    worker_up = next(index for index, call in enumerate(calls) if "--scale worker=1" in call)
    assert api_up < worker_up


def test_stale_generation_cannot_satisfy_post_scale_scan_gate(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)

    result, calls_file, expected_file, attempt_file = _run_compose_up(
        project,
        fake_bin,
        worker_processes="2",
        current_workers=1,
        all_workers=2,
        scan_mode="stale",
    )

    assert result.returncode == 1
    calls = calls_file.read_text(encoding="utf-8").splitlines()
    assert "watermark" in calls
    assert calls[-30:] == ["scan"] * 30
    assert not any(call.endswith(" api") for call in calls)
    assert not expected_file.exists()
    assert not attempt_file.exists()
    assert "last observed live/new=2/0" in result.stderr


def test_worker_scan_timeout_stops_before_api_recreate_and_leaves_stack(
    tmp_path: Path,
) -> None:
    project, fake_bin = _prepare_project(tmp_path)

    result, calls_file, expected_file, attempt_file = _run_compose_up(
        project,
        fake_bin,
        scan_mode="never",
    )

    assert result.returncode == 1
    calls = calls_file.read_text(encoding="utf-8").splitlines()
    assert calls[-30:] == ["scan"] * 30
    assert not expected_file.exists()
    assert not attempt_file.exists()
    assert "last observed live/new=0/0" in result.stderr
    assert "left running for inspection" in result.stderr
    assert all("down" not in call for call in calls)


def test_stalled_or_extra_registered_worker_never_passes_ready_gate(
    tmp_path: Path,
) -> None:
    project, fake_bin = _prepare_project(tmp_path)

    result, calls_file, _, attempt_file = _run_compose_up(
        project,
        fake_bin,
        metrics_mode="stale",
    )

    assert result.returncode == 1
    calls = calls_file.read_text(encoding="utf-8").splitlines()
    assert calls[-30:] == ["metrics"] * 30
    assert attempt_file.read_text(encoding="utf-8") == "30\n"
    assert "last observed=2/3/2/1/0" in result.stderr


def test_compose_up_failure_does_not_enter_worker_or_metrics_polling(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)

    result, calls_file, expected_file, attempt_file = _run_compose_up(
        project,
        fake_bin,
        extra_environment={"FAKE_UP_EXIT_CODE": "17"},
    )

    assert result.returncode == 17
    assert calls_file.read_text(encoding="utf-8").splitlines() == [
        "ps:all",
        "ps:running",
        "build",
        "up:up --wait --wait-timeout 180 --remove-orphans postgres redis migrate",
    ]
    assert not expected_file.exists()
    assert not attempt_file.exists()


def test_metrics_timeout_is_nonzero_and_leaves_stack_running(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)

    result, calls_file, _, attempt_file = _run_compose_up(
        project,
        fake_bin,
        metrics_mode="never",
    )

    assert result.returncode == 1
    calls = calls_file.read_text(encoding="utf-8").splitlines()
    assert calls[:7] == [
        "ps:all",
        "ps:running",
        "build",
        "up:up --wait --wait-timeout 180 --remove-orphans postgres redis migrate",
        "watermark",
        "up:up --wait --wait-timeout 180 --remove-orphans --no-deps --scale worker=2 worker",
        "scan",
    ]
    assert calls[7:9] == [
        "up:up --wait --wait-timeout 180 --remove-orphans --no-deps --force-recreate api",
        "up:up --wait --wait-timeout 180 --remove-orphans --no-deps frontend",
    ]
    assert calls[9:] == ["metrics"] * 30
    assert attempt_file.read_text(encoding="utf-8") == "30\n"
    assert "last observed=2/2/0/0/2" in result.stderr
    assert "left running for inspection" in result.stderr
    assert all("down" not in call for call in calls)


@pytest.mark.parametrize(
    ("target", "make_variable", "launcher_variable"),
    [
        ("dev", "DEV_WORKERS", "LLMBENCHLAB_DEV_WORKER_PROCESSES"),
        ("docker-up", "WORKERS", "LLMBENCHLAB_COMPOSE_WORKER_PROCESSES"),
    ],
)
def test_make_forwards_explicit_empty_worker_count_for_launcher_rejection(
    target: str,
    make_variable: str,
    launcher_variable: str,
) -> None:
    result = subprocess.run(
        ["make", "--no-print-directory", "-n", target, f"{make_variable}="],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert f'{launcher_variable}=""' in result.stdout
