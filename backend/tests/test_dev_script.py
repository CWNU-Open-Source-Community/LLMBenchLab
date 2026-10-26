"""The combined development launcher keeps service chatter out of the console."""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEV_SCRIPT = REPOSITORY_ROOT / "scripts" / "dev.sh"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _prepare_project(tmp_path: Path, *, bootstrap_exit_code: int = 0) -> tuple[Path, Path]:
    project = tmp_path / "project"
    scripts = project / "scripts"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    (project / "backend" / ".venv").mkdir(parents=True)
    (project / "frontend" / "node_modules").mkdir(parents=True)
    (scripts / "dev.sh").write_text(DEV_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    _write_executable(
        scripts / "bootstrap_credential_keyring.sh",
        f"""#!/usr/bin/env bash
echo 'keyring success status'
if (( {bootstrap_exit_code} != 0 )); then
  echo 'keyring bootstrap failed safely' >&2
fi
exit {bootstrap_exit_code}
""",
    )
    _write_executable(
        fake_bin / "fake-service",
        """#!/usr/bin/env bash
set -euo pipefail
service="$1"
printf '%s child stdout\\n' "$service"
printf '%s child stderr\\n' "$service" >&2
printf '%s expected=%s\\n' "$service" "${LLMBENCHLAB_WORKER_EXPECTED_PROCESSES:-unset}"
if [[ "${EXIT_SERVICE:-}" == "$service" ]]; then
  sleep "${EXIT_DELAY_SECONDS:-0.2}"
  exit "${EXIT_CODE:-0}"
fi
trap '' INT
trap 'printf "%s:terminated\\n" "$service" >>"$EVENT_FILE"; exit 0' TERM
while true; do
  sleep 0.1
done
""",
    )
    _write_executable(
        fake_bin / "uv",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" uvicorn "* ]]; then
  exec "$FAKE_SERVICE_SCRIPT" api
fi
if [[ " $* " == *" app.worker "* ]]; then
  if [[ -n "${LLMBENCHLAB_DEV_WORKER_INDEX:-}" ]]; then
    exec "$FAKE_SERVICE_SCRIPT" "worker-${LLMBENCHLAB_DEV_WORKER_INDEX}"
  fi
  exec "$FAKE_SERVICE_SCRIPT" worker
fi
echo 'unexpected fake uv invocation' >&2
exit 91
""",
    )
    _write_executable(
        fake_bin / "npm",
        """#!/usr/bin/env bash
set -euo pipefail
exec "$FAKE_SERVICE_SCRIPT" frontend
""",
    )
    return project, fake_bin


def _run_dev(
    project: Path,
    fake_bin: Path,
    log_dir: Path,
    *,
    exit_service: str,
    exit_code: int,
    worker_processes: str = "1",
    database_url: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = _dev_environment(
        project,
        fake_bin,
        log_dir,
        exit_service=exit_service,
        exit_code=exit_code,
        worker_processes=worker_processes,
        database_url=database_url,
    )
    return subprocess.run(
        ["bash", str(project / "scripts" / "dev.sh")],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _dev_environment(
    project: Path,
    fake_bin: Path,
    log_dir: Path,
    *,
    exit_service: str = "",
    exit_code: int = 0,
    worker_processes: str = "1",
    database_url: str | None = None,
) -> dict[str, str]:
    environment = os.environ.copy()
    for variable_name in (
        "DATABASE_URL",
        "LLMBENCHLAB_DATABASE_URL",
        "LLMBENCHLAB_DEV_WORKER_PROCESSES",
        "LLMBENCHLAB_WORKER_EXPECTED_PROCESSES",
    ):
        environment.pop(variable_name, None)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_SERVICE_SCRIPT": str(fake_bin / "fake-service"),
            "EVENT_FILE": str(project / "service-events.log"),
            "EXIT_SERVICE": exit_service,
            "EXIT_CODE": str(exit_code),
            "LLMBENCHLAB_DEV_LOG_DIR": str(log_dir),
            "LLMBENCHLAB_DEV_WORKER_PROCESSES": worker_processes,
        }
    )
    if database_url is not None:
        environment["LLMBENCHLAB_DATABASE_URL"] = database_url
    return environment


def test_dev_launcher_redirects_all_service_output_to_append_only_logs(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)
    log_dir = tmp_path / "custom-dev-logs"
    log_dir.mkdir()
    for service in ("api", "worker", "frontend"):
        (log_dir / f"{service}.log").write_text("existing log entry\n", encoding="utf-8")

    result = _run_dev(project, fake_bin, log_dir, exit_service="api", exit_code=0)

    assert result.returncode == 0
    console = result.stdout + result.stderr
    assert "Starting LLMBenchLab: Web http://127.0.0.1:5173 | API http://127.0.0.1:8000" in console
    assert f"Logs: API {log_dir / 'api.log'}" in console
    assert "API stopped with status 0" in console
    assert "keyring success status" not in console
    assert "child stdout" not in console
    assert "child stderr" not in console

    for service in ("api", "worker", "frontend"):
        contents = (log_dir / f"{service}.log").read_text(encoding="utf-8")
        assert contents.startswith("existing log entry\n")
        assert contents.count("=== LLMBenchLab dev session ") == 1
        assert re.search(
            r"=== LLMBenchLab dev session \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ===",
            contents,
        )
        assert f"{service} child stdout" in contents
        assert f"{service} child stderr" in contents
        assert f"{service} expected=1" in contents
        assert stat.S_IMODE((log_dir / f"{service}.log").stat().st_mode) == 0o600
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700


def test_dev_launcher_propagates_failure_and_terminates_other_services(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)
    log_dir = tmp_path / "failure-logs"

    result = _run_dev(project, fake_bin, log_dir, exit_service="worker", exit_code=17)

    assert result.returncode == 17
    assert result.stdout.count("Starting LLMBenchLab:") == 1
    assert "child stdout" not in result.stdout + result.stderr
    assert "child stderr" not in result.stdout + result.stderr
    assert f"Error: Worker exited with status 17. Log: {log_dir / 'worker.log'}" in result.stderr
    events = (project / "service-events.log").read_text(encoding="utf-8").splitlines()
    assert set(events) == {"api:terminated", "frontend:terminated"}
    assert all((log_dir / f"{service}.log").is_file() for service in ("api", "worker", "frontend"))


def test_dev_launcher_suppresses_keyring_success_but_preserves_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, fake_bin = _prepare_project(tmp_path, bootstrap_exit_code=23)
    log_dir = tmp_path / "unused-logs"
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("LLMBENCHLAB_DEV_LOG_DIR", str(log_dir))

    result = subprocess.run(
        ["bash", str(project / "scripts" / "dev.sh")],
        cwd=project,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 23
    assert result.stdout == ""
    assert "keyring success status" not in result.stderr
    assert result.stderr.strip() == "keyring bootstrap failed safely"
    assert not log_dir.exists()


def test_dev_launcher_runs_multiple_workers_with_private_logs_and_shared_expected_count(
    tmp_path: Path,
) -> None:
    project, fake_bin = _prepare_project(tmp_path)
    log_dir = tmp_path / "multi-worker-logs"

    result = _run_dev(
        project,
        fake_bin,
        log_dir,
        exit_service="worker-2",
        exit_code=19,
        worker_processes="3",
        database_url="postgresql+psycopg://user:secret@db.example/bench",
    )

    assert result.returncode == 19
    console = result.stdout + result.stderr
    assert "Error: Worker 2 exited with status 19." in result.stderr
    assert "secret" not in console
    assert "child stdout" not in console
    assert "child stderr" not in console
    assert "Workers" in result.stdout

    expected_logs = {
        "api": log_dir / "api.log",
        "worker-1": log_dir / "worker-1.log",
        "worker-2": log_dir / "worker-2.log",
        "worker-3": log_dir / "worker-3.log",
        "frontend": log_dir / "frontend.log",
    }
    for service, log_path in expected_logs.items():
        contents = log_path.read_text(encoding="utf-8")
        assert f"{service} child stdout" in contents
        assert f"{service} child stderr" in contents
        assert f"{service} expected=3" in contents
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600

    events = (project / "service-events.log").read_text(encoding="utf-8").splitlines()
    assert set(events) == {
        "api:terminated",
        "worker-1:terminated",
        "worker-3:terminated",
        "frontend:terminated",
    }


def test_explicit_worker_count_overrides_dotenv_launcher_default(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)
    log_dir = tmp_path / "dotenv-worker-override-logs"
    (project / ".env").write_text(
        "LLMBENCHLAB_DEV_WORKER_PROCESSES=1\n"
        "LLMBENCHLAB_DATABASE_URL=postgresql://dotenv-user:dotenv-secret@db/bench\n",
        encoding="utf-8",
    )

    result = _run_dev(
        project,
        fake_bin,
        log_dir,
        exit_service="worker-2",
        exit_code=21,
        worker_processes="2",
    )

    assert result.returncode == 21
    assert "Error: Worker 2 exited with status 21." in result.stderr
    assert (log_dir / "worker-1.log").is_file()
    assert (log_dir / "worker-2.log").is_file()
    assert not (log_dir / "worker.log").exists()
    assert "dotenv-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize("worker_processes", ["0", "33", "two", "01", "2.5"])
def test_dev_launcher_rejects_invalid_worker_process_count_before_logs_or_services(
    tmp_path: Path,
    worker_processes: str,
) -> None:
    project, fake_bin = _prepare_project(tmp_path)
    log_dir = tmp_path / "invalid-count-logs"

    result = _run_dev(
        project,
        fake_bin,
        log_dir,
        exit_service="",
        exit_code=0,
        worker_processes=worker_processes,
        database_url="postgresql://user:secret@db.example/bench",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "Error: LLMBENCHLAB_DEV_WORKER_PROCESSES must be an integer from 1 through 32."
    )
    assert "secret" not in result.stderr
    assert not log_dir.exists()
    assert not (project / "service-events.log").exists()


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite:///./data/local.db",
        "mysql+pymysql://user:do-not-print@db.example/bench",
        "not-a-database-url-with-do-not-print",
    ],
)
def test_dev_launcher_rejects_non_postgresql_multi_worker_before_logs_or_services(
    tmp_path: Path,
    database_url: str,
) -> None:
    project, fake_bin = _prepare_project(tmp_path)
    log_dir = tmp_path / "wrong-database-logs"

    result = _run_dev(
        project,
        fake_bin,
        log_dir,
        exit_service="",
        exit_code=0,
        worker_processes="2",
        database_url=database_url,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "Error: multiple development Workers require a PostgreSQL database URL."
    )
    assert database_url not in result.stderr
    assert "do-not-print" not in result.stderr
    assert not log_dir.exists()
    assert not (project / "service-events.log").exists()


def test_dev_launcher_uses_final_database_url_precedence_for_multi_worker_guard(
    tmp_path: Path,
) -> None:
    project, fake_bin = _prepare_project(tmp_path)
    log_dir = tmp_path / "precedence-logs"
    (project / ".env").write_text(
        "DATABASE_URL=postgresql://user:env-secret@db.example/bench\n"
        "LLMBENCHLAB_DATABASE_URL=sqlite:///./data/winner.db\n",
        encoding="utf-8",
    )

    result = _run_dev(
        project,
        fake_bin,
        log_dir,
        exit_service="",
        exit_code=0,
        worker_processes="2",
        database_url="postgresql://user:process-secret@db.example/bench",
    )

    assert result.returncode == 1
    assert "PostgreSQL database URL" in result.stderr
    assert "env-secret" not in result.stderr
    assert "process-secret" not in result.stderr
    assert not log_dir.exists()
    assert not (project / "service-events.log").exists()


def test_dev_launcher_forwards_termination_and_cleans_up_every_child(tmp_path: Path) -> None:
    project, fake_bin = _prepare_project(tmp_path)
    log_dir = tmp_path / "signal-logs"
    environment = _dev_environment(
        project,
        fake_bin,
        log_dir,
        worker_processes="2",
        database_url="postgresql://user:secret@db.example/bench",
    )
    process = subprocess.Popen(
        ["bash", str(project / "scripts" / "dev.sh")],
        cwd=project,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    expected_logs = [
        log_dir / "api.log",
        log_dir / "worker-1.log",
        log_dir / "worker-2.log",
        log_dir / "frontend.log",
    ]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if all(path.is_file() and "child stdout" in path.read_text() for path in expected_logs):
            break
        time.sleep(0.05)
    else:
        process.kill()
        process.communicate(timeout=5)
        pytest.fail("development services did not all start before the signal test deadline")

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 143
    assert "child stdout" not in stdout + stderr
    events = (project / "service-events.log").read_text(encoding="utf-8").splitlines()
    assert set(events) == {
        "api:terminated",
        "worker-1:terminated",
        "worker-2:terminated",
        "frontend:terminated",
    }


def test_dev_launcher_converts_interrupt_to_term_cleanup_for_ignoring_children(
    tmp_path: Path,
) -> None:
    project, fake_bin = _prepare_project(tmp_path)
    log_dir = tmp_path / "interrupt-logs"
    environment = _dev_environment(
        project,
        fake_bin,
        log_dir,
        worker_processes="2",
        database_url="postgresql://user:secret@db.example/bench",
    )
    process = subprocess.Popen(
        ["bash", str(project / "scripts" / "dev.sh")],
        cwd=project,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    expected_logs = [
        log_dir / "api.log",
        log_dir / "worker-1.log",
        log_dir / "worker-2.log",
        log_dir / "frontend.log",
    ]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if all(path.is_file() and "child stdout" in path.read_text() for path in expected_logs):
            break
        time.sleep(0.05)
    else:
        process.kill()
        process.communicate(timeout=5)
        pytest.fail("development services did not all start before the interrupt test deadline")

    process.send_signal(signal.SIGINT)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 130
    assert "child stdout" not in stdout + stderr
    events = (project / "service-events.log").read_text(encoding="utf-8").splitlines()
    assert set(events) == {
        "api:terminated",
        "worker-1:terminated",
        "worker-2:terminated",
        "frontend:terminated",
    }
