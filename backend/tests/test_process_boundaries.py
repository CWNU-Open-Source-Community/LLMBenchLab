"""Fresh-process import boundaries for the API/Worker split."""

from __future__ import annotations

import os
import subprocess
import sys


def test_fresh_api_import_does_not_load_runner_or_adapters() -> None:
    environment = {
        **os.environ,
        "LLMBENCHLAB_REDIS_URL": "",
        "REDIS_URL": "",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.main; "
                "assert 'app.runners.evaluation_runner' not in sys.modules; "
                "assert not any(name == 'app.adapters' or name.startswith('app.adapters.') "
                "for name in sys.modules)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
