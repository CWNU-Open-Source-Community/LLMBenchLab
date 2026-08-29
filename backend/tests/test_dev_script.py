"""The combined development launcher keeps service chatter out of the console."""

from __future__ import annotations

import os
import re
import stat
import subprocess
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
if [[ "${EXIT_SERVICE:-}" == "$service" ]]; then
  sleep "${EXIT_DELAY_SECONDS:-0.2}"
  exit "${EXIT_CODE:-0}"
fi
trap 'printf "%s:terminated\\n" "$service" >>"$EVENT_FILE"; exit 0' TERM INT
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
) -> subprocess.CompletedProcess[str]:
    event_file = project / "service-events.log"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_SERVICE_SCRIPT": str(fake_bin / "fake-service"),
            "EVENT_FILE": str(event_file),
            "EXIT_SERVICE": exit_service,
            "EXIT_CODE": str(exit_code),
            "LLMBENCHLAB_DEV_LOG_DIR": str(log_dir),
        }
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
