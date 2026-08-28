"""Process-wide isolation for msg tests before test modules import."""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


_ISOLATION_ROOT = tempfile.mkdtemp(prefix="cc-msg-tests-")
os.chmod(_ISOLATION_ROOT, 0o700)
os.environ["MSG_TEST_ISOLATION_ROOT"] = _ISOLATION_ROOT
os.environ["HOME"] = _ISOLATION_ROOT
os.environ.pop("TMUX", None)
os.environ.pop("TMUX_PANE", None)


def _cleanup_msg_test_home() -> None:
    db_path = Path(_ISOLATION_ROOT) / ".msg/msg.db"
    if db_path.exists():
        subprocess.run(
            [
                sys.executable,
                "-m",
                "claude_code_tools.msg.cli",
                "--db",
                str(db_path),
                "watch",
                "stop",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    shutil.rmtree(_ISOLATION_ROOT, ignore_errors=True)


atexit.register(_cleanup_msg_test_home)
