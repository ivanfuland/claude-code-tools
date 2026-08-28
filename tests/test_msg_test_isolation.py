"""The msg test process must never see the operator's live state roots."""

from __future__ import annotations

import os
from pathlib import Path

from claude_code_tools.msg.store import DEFAULT_DB_PATH


def test_msg_suite_isolates_db_without_replacing_process_home():
    isolation_root = os.environ.get("MSG_TEST_ISOLATION_ROOT")

    assert isolation_root
    assert Path(DEFAULT_DB_PATH).is_relative_to(Path(isolation_root))
    assert Path.home() != Path(isolation_root)
    assert "TMUX" not in os.environ
    assert "TMUX_PANE" not in os.environ
