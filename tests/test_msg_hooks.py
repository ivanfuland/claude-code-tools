"""Tests for msg-hook CLI commands."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from claude_code_tools.amux.model import Agent as AmuxAgent
from claude_code_tools.msg import hooks as hooks_module
from claude_code_tools.msg.activation import (
    load_host_session_attestation,
    write_host_session_attestation,
)
from claude_code_tools.msg.hooks import _find_self_agent, cli
from claude_code_tools.msg.models import (
    Agent,
    AgentKind,
    ConsumerProtocol,
    ContinuationState,
    RegistrationIdentity,
)
from claude_code_tools.msg.store import MsgStore


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_msg.db")
    return MsgStore(db_path=db_path)


@pytest.fixture
def setup_agents(store):
    """Register two agents and create a thread with
    a pending message."""
    a = store.register_agent(
        name="sender",
        pane_id="%1",
        tmux_session="test",
        agent_kind=AgentKind.CLAUDE,
    )
    b = store.register_agent(
        name="receiver",
        pane_id="%2",
        tmux_session="test",
        agent_kind=AgentKind.CLAUDE,
    )
    thread = store.create_thread(
        title="Test",
        created_by=a.session_id,
        participant_ids=[a.session_id, b.session_id],
    )
    store.send_message(
        thread_id=thread.id,
        from_agent=a.session_id,
        body="Hello receiver!",
    )
    return a, b, store


def test_find_self_agent_includes_tmux_socket(store):
    store.register_agent("server-a", "%2", "shared", AgentKind.CODEX, "/tmp/a")
    expected = store.register_agent(
        "server-b", "%2", "shared", AgentKind.CODEX, "/tmp/b"
    )

    with patch.dict(
        os.environ,
        {"TMUX_PANE": "%2", "TMUX": "/tmp/b,123,0"},
    ), patch("claude_code_tools.msg.hooks.subprocess.run") as run:
        run.return_value.stdout = "shared\n"
        found = _find_self_agent(store)

    assert found.session_id == expected.session_id
    assert run.call_args.args[0][:3] == ["tmux", "-S", "/tmp/b"]


def test_find_self_agent_fails_closed_for_duplicate_pane(monkeypatch, store):
    matches = [
        Agent(name=name, pane_id="%2", tmux_session="shared", tmux_socket="/tmp/b")
        for name in ("first", "duplicate")
    ]
    monkeypatch.setattr(
        store, "list_agents", lambda tmux_session=None, tmux_socket=None: matches,
    )

    with patch.dict(
        os.environ,
        {"TMUX_PANE": "%2", "TMUX": "/tmp/b,123,0"},
    ), patch("claude_code_tools.msg.hooks.subprocess.run") as run:
        run.return_value.stdout = "shared\n"
        found = _find_self_agent(store)

    assert found is None


@pytest.mark.parametrize(
    ("observed_start", "expected_name"),
    (("linux:202:2", "first-mate"), ("linux:202:reused", None)),
)
def test_find_self_first_mate_requires_exact_tui_identity(
    monkeypatch, store, observed_start, expected_name,
):
    register_first_mate(store, tmux_socket="/tmp/tmux")
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks.resolve_pane_agent",
        lambda _pane, _socket: AmuxAgent(
            pane="test:1.2", session="test", kind="codex",
            pid=202, cwd="/repo", extra={"pane_id": "%2"},
        ),
    )
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks.process_start_identity",
        lambda _pid: observed_start,
    )

    with patch.dict(
        os.environ,
        {"TMUX_PANE": "%2", "TMUX": "/tmp/tmux,123,0"},
    ), patch("claude_code_tools.msg.hooks.subprocess.run") as run:
        run.return_value.stdout = "test\n"
        found = _find_self_agent(store)

    assert (found.name if found else None) == expected_name


class TestStopHook:

    def test_no_messages_approves(self, store):
        """When no messages, hook outputs approve."""
        store.register_agent(
            name="lonely",
            pane_id="%99",
            tmux_session="test",
            agent_kind=AgentKind.CLAUDE,
        )
        runner = CliRunner()
        with patch.dict(os.environ, {"TMUX_PANE": "%99"}), \
             patch(
                 "claude_code_tools.msg.hooks.MsgStore",
                 return_value=store,
             ), \
             patch(
                 "claude_code_tools.msg.hooks"
                 "._find_self_agent",
             ) as mock_find:
            agents = store.list_agents(
                tmux_session="test",
            )
            mock_find.return_value = agents[0]
            result = runner.invoke(
                cli, ["stop"],
                input=json.dumps({}),
            )
        assert result.output == ""


def test_session_start_attests_resumable_host_identity(monkeypatch, tmp_path):
    db_path = tmp_path / "msg.db"
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks.DEFAULT_DB_PATH", str(db_path),
    )
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks._current_tmux_scope",
        lambda: ("main", "/tmp/tmux", "%2"),
    )
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks.resolve_pane_agent",
        lambda _pane, _socket: AmuxAgent(
            pane="main:1.0", session="main", kind="codex",
            pid=202, cwd="/repo", extra={"pane_id": "%2"},
        ),
    )
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks.process_start_identity",
        lambda _pid: "linux:202:2",
    )
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks._current_window_name",
        lambda _socket, _pane: "example-exec-docs_01",
        raising=False,
    )

    result = CliRunner().invoke(
        cli, ["session-start"], input=json.dumps({"session_id": "019d-host"}),
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {}
    attestation = load_host_session_attestation(
        db_path, "main", "/tmp/tmux", "%2",
    )
    assert attestation["host_session_id"] == "019d-host"
    assert attestation["host_session_title"] == "example-exec-docs_01"
    assert attestation["process_start_identity"] == "linux:202:2"
    assert attestation["code_sha256"] == hashlib.sha256(
        Path(hooks_module.__file__).read_bytes(),
    ).hexdigest()


@pytest.mark.parametrize("host_session_id", ("", "bad\nvalue", "x" * 257))
def test_session_start_rejects_invalid_host_session_id_without_marker(
    monkeypatch, tmp_path, host_session_id,
):
    db_path = tmp_path / "msg.db"
    monkeypatch.setattr(hooks_module, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(
        hooks_module, "_current_tmux_scope",
        lambda: ("main", "/tmp/tmux", "%2"),
    )

    result = CliRunner().invoke(
        cli, ["session-start"], input=json.dumps({"session_id": host_session_id}),
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {}
    assert load_host_session_attestation(
        db_path, "main", "/tmp/tmux", "%2",
    ) is None


def test_claude_bootstrap_prompt_sets_native_session_title(monkeypatch, tmp_path):
    db_path = tmp_path / "msg.db"
    store = MsgStore(db_path)
    monkeypatch.setattr(hooks_module, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(
        hooks_module, "_current_tmux_scope",
        lambda: ("main", "/tmp/tmux", "%2"),
    )
    monkeypatch.setattr(hooks_module, "MsgStore", lambda: store)
    write_host_session_attestation(
        db_path,
        tmux_session="main",
        tmux_socket="/tmp/tmux",
        pane_id="%2",
        agent_kind=AgentKind.CLAUDE,
        pid=202,
        process_start_identity="linux:202:2",
        cwd="/repo",
        host_session_id="claude-host",
        host_session_title="example-exec-docs_01",
        code_sha256=hooks_module.hook_module_sha256(),
    )

    result = CliRunner().invoke(
        cli, ["prompt-submit"], input=json.dumps({"session_id": "claude-host"}),
    )

    output = json.loads(result.output)
    assert output["hookSpecificOutput"] == {
        "hookEventName": "UserPromptSubmit",
        "sessionTitle": "example-exec-docs_01",
    }


def test_codex_title_adapter_sets_and_reads_back_exact_name(
    monkeypatch, tmp_path,
):
    server = tmp_path / "fake_app_server.py"
    request_log = tmp_path / "requests.json"
    server.write_text(
        """\
import json
import os
import sys

requests = []
def read_request():
    value = json.loads(sys.stdin.readline())
    requests.append(value)
    return value

initialize = read_request()
print(json.dumps({"id": initialize["id"], "result": {}}), flush=True)
initialized = read_request()
rename = read_request()
print(json.dumps({"id": rename["id"], "result": {}}), flush=True)
readback = read_request()
with open(os.environ["FAKE_CODEX_REQUEST_LOG"], "w", encoding="utf-8") as handle:
    json.dump(requests, handle)
print(json.dumps({
    "id": readback["id"],
    "result": {"thread": {"name": os.environ.get(
        "FAKE_CODEX_READBACK", rename["params"]["name"],
    )}},
}), flush=True)
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CODEX_REQUEST_LOG", str(request_log))
    monkeypatch.setattr(
        hooks_module,
        "_CODEX_APP_SERVER_COMMAND",
        (sys.executable, str(server)),
        raising=False,
    )

    assert hooks_module._set_codex_session_title(
        "019d-thread", "example-exec-docs_01",
    )
    requests = json.loads(request_log.read_text(encoding="utf-8"))
    assert [request["method"] for request in requests] == [
        "initialize", "initialized", "thread/name/set", "thread/read",
    ]
    assert requests[2]["params"] == {
        "threadId": "019d-thread", "name": "example-exec-docs_01",
    }
    monkeypatch.setenv("FAKE_CODEX_READBACK", "mismatched-title")
    assert not hooks_module._set_codex_session_title(
        "019d-thread", "example-exec-docs_01",
    )


def test_codex_title_adapter_drains_buffered_notification_before_response(
    monkeypatch, tmp_path,
):
    """A notification sharing one write with a response must not hide it."""
    server = tmp_path / "fake_app_server_with_notification.py"
    server.write_text(
        """\
import json
import os
import sys

initialize = json.loads(sys.stdin.readline())
payload = (
    json.dumps({"method": "thread/started", "params": {}}) + "\\n"
    + json.dumps({"id": initialize["id"], "result": {}}) + "\\n"
).encode("utf-8")
os.write(sys.stdout.fileno(), payload)
sys.stdin.readline()
rename = json.loads(sys.stdin.readline())
print(json.dumps({"id": rename["id"], "result": {}}), flush=True)
readback = json.loads(sys.stdin.readline())
print(json.dumps({
    "id": readback["id"],
    "result": {"thread": {"name": rename["params"]["name"]}},
}), flush=True)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        hooks_module,
        "_CODEX_APP_SERVER_COMMAND",
        (sys.executable, str(server)),
        raising=False,
    )

    assert hooks_module._set_codex_session_title(
        "019d-thread", "example-exec-docs_01",
    )


def test_codex_bootstrap_stop_sets_native_session_title(monkeypatch, tmp_path):
    db_path = tmp_path / "msg.db"
    store = MsgStore(db_path)
    monkeypatch.setattr(hooks_module, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(
        hooks_module, "_current_tmux_scope",
        lambda: ("main", "/tmp/tmux", "%2"),
    )
    monkeypatch.setattr(hooks_module, "MsgStore", lambda: store)
    calls = []
    monkeypatch.setattr(
        hooks_module, "_set_codex_session_title",
        lambda session_id, title: calls.append((session_id, title)) or True,
        raising=False,
    )
    write_host_session_attestation(
        db_path,
        tmux_session="main",
        tmux_socket="/tmp/tmux",
        pane_id="%2",
        agent_kind=AgentKind.CODEX,
        pid=202,
        process_start_identity="linux:202:2",
        cwd="/repo",
        host_session_id="codex-host",
        host_session_title="example-exec-docs_01",
        code_sha256=hooks_module.hook_module_sha256(),
    )

    prompt = CliRunner().invoke(
        cli, ["prompt-submit"], input=json.dumps({"session_id": "codex-host"}),
    )
    result = CliRunner().invoke(
        cli, ["stop"], input=json.dumps({"session_id": "codex-host"}),
    )

    assert prompt.output == ""
    assert json.loads(result.output) == {}
    assert calls == [("codex-host", "example-exec-docs_01")]

    def test_codex_no_messages_emits_valid_empty_stop_json(self, store):
        codex = store.register_agent(
            name="codex",
            pane_id="%98",
            tmux_session="test",
            agent_kind=AgentKind.CODEX,
        )
        with patch(
            "claude_code_tools.msg.hooks.MsgStore", return_value=store,
        ), patch(
            "claude_code_tools.msg.hooks._find_self_agent", return_value=codex,
        ):
            result = CliRunner().invoke(
                cli, ["stop"], input=json.dumps({"model": "gpt-test"}),
            )

        assert json.loads(result.output) == {}

    def test_with_messages_notifies(self, setup_agents):
        """When messages exist, hook injects context."""
        a, b, store = setup_agents
        runner = CliRunner()
        with patch.dict(os.environ, {"TMUX_PANE": "%2"}), \
             patch(
                 "claude_code_tools.msg.hooks.MsgStore",
                 return_value=store,
             ), \
             patch(
                 "claude_code_tools.msg.hooks"
                 "._find_self_agent",
                 return_value=b,
             ):
            result = runner.invoke(
                cli, ["stop"],
                input=json.dumps({}),
            )
        output = json.loads(result.output)
        assert "hookSpecificOutput" in output
        ctx = output["hookSpecificOutput"]
        assert ctx["hookEventName"] == "Stop"
        assert "unread" in ctx["additionalContext"]
        assert "sender" in ctx["additionalContext"]

    @pytest.mark.parametrize("stop_hook_active", (False, True))
    def test_codex_pending_uses_official_stop_shape(
        self, store, stop_hook_active,
    ):
        sender = store.register_agent("sender", "%1", "test", AgentKind.CLAUDE)
        receiver = store.register_agent("codex", "%2", "test", AgentKind.CODEX)
        thread = store.create_thread(
            "codex", sender.session_id, [sender.session_id, receiver.session_id],
        )
        store.send_message(thread.id, sender.session_id, "pending")

        with patch(
            "claude_code_tools.msg.hooks.MsgStore", return_value=store,
        ), patch(
            "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
        ):
            result = CliRunner().invoke(
                cli,
                ["stop"],
                input=json.dumps(
                    {"model": "gpt-test", "stop_hook_active": stop_hook_active}
                ),
            )

        output = json.loads(result.output)
        assert output["decision"] == "block"
        assert "Run msg inbox" in output["reason"]
        assert "hookSpecificOutput" not in output
        assert store.get_inbox(receiver.session_id)[0]["state"] == "notified"

    def test_output_failure_releases_claim_instead_of_swallowing_delivery(self, store):
        sender = store.register_agent("sender", "%1", "test", AgentKind.CLAUDE)
        receiver = store.register_agent("codex", "%2", "test", AgentKind.CODEX)
        thread = store.create_thread(
            "codex", sender.session_id, [sender.session_id, receiver.session_id],
        )
        store.send_message(thread.id, sender.session_id, "pending")

        with patch(
            "claude_code_tools.msg.hooks.MsgStore", return_value=store,
        ), patch(
            "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
        ), patch("builtins.print", side_effect=OSError("closed stdout")):
            result = CliRunner().invoke(
                cli, ["stop"], input=json.dumps({"model": "gpt-test"}),
            )

        assert result.exit_code != 0
        assert store.get_inbox(receiver.session_id)[0]["state"] == "pending"

    def test_claims_only_this_hooks_recipient(self, store):
        sender = store.register_agent(
            "sender", "%1", "test", AgentKind.CLAUDE,
        )
        first = store.register_agent(
            "first", "%2", "test", AgentKind.CLAUDE,
        )
        second = store.register_agent(
            "second", "%3", "test", AgentKind.CODEX,
        )
        thread = store.create_thread(
            "Test", sender.session_id,
            [sender.session_id, first.session_id, second.session_id],
        )
        store.send_message(thread.id, sender.session_id, "hello both")

        with patch(
            "claude_code_tools.msg.hooks.MsgStore", return_value=store,
        ), patch(
            "claude_code_tools.msg.hooks._find_self_agent", return_value=first,
        ):
            result = CliRunner().invoke(cli, ["stop"], input="{}")

        assert "hookSpecificOutput" in json.loads(result.output)
        claimed = store.claim_pending_deliveries("watcher")
        assert [item["recipient_id"] for item in claimed] == [second.session_id]

    def test_already_notified_message_does_not_notify_again(self, setup_agents):
        """Unread deliveries produce only one notification."""
        _sender, receiver, store = setup_agents
        claimed = store.claim_pending_deliveries(
            "watcher", recipient_id=receiver.session_id,
        )
        store.mark_notified(claimed[0]["id"], "watcher")

        with patch(
            "claude_code_tools.msg.hooks.MsgStore", return_value=store,
        ), patch(
            "claude_code_tools.msg.hooks._find_self_agent",
            return_value=receiver,
        ):
            result = CliRunner().invoke(cli, ["stop"], input="{}")

        assert result.output == ""


class TestPromptSubmitHook:

    def test_with_messages_notifies(self, setup_agents):
        """UserPromptSubmit hook injects context."""
        a, b, store = setup_agents
        runner = CliRunner()
        with patch.dict(os.environ, {"TMUX_PANE": "%2"}), \
             patch(
                 "claude_code_tools.msg.hooks.MsgStore",
                 return_value=store,
             ), \
             patch(
                 "claude_code_tools.msg.hooks"
                 "._find_self_agent",
                 return_value=b,
             ):
            result = runner.invoke(
                cli, ["prompt-submit"],
                input=json.dumps({}),
            )
        output = json.loads(result.output)
        ctx = output["hookSpecificOutput"]
        assert ctx["hookEventName"] == "UserPromptSubmit"
        assert "msg inbox" in ctx["additionalContext"]


def register_first_mate(
    store, *, kind=AgentKind.CODEX, pane="%2", tmux_socket=None,
):
    return store.register_agent(
        "first-mate", pane, "test", kind, tmux_socket,
        pid=202, cwd="/repo",
        consumer_protocol=ConsumerProtocol.FIRST_MATE_V1,
        process_start_identity="linux:202:2",
    )


@pytest.mark.parametrize("kind", (AgentKind.CLAUDE, AgentKind.CODEX))
@pytest.mark.parametrize("stop_hook_active", (False, True))
def test_first_mate_stop_pending_uses_native_continuation_without_consuming(
    store, kind, stop_hook_active,
):
    sender = store.register_agent("sender", "%1", "test", AgentKind.CLAUDE)
    receiver = register_first_mate(store, kind=kind)
    thread = store.create_thread(
        "pending", sender.session_id, [sender.session_id, receiver.session_id],
    )
    store.send_message(thread.id, sender.session_id, "work")
    hook_input = {"stop_hook_active": stop_hook_active}

    with patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ), patch(
        "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
    ):
        result = CliRunner().invoke(cli, ["stop"], input=json.dumps(hook_input))

    output = json.loads(result.output)
    assert output["decision"] == "block"
    assert "$first-mate" in output["reason"]
    assert "msg inbox" not in output["reason"]
    assert store.get_inbox(receiver.session_id)[0]["state"] == "pending"


def test_first_mate_stop_routes_fresh_to_wait_and_stale_to_recovery(store):
    receiver = register_first_mate(store)
    identity = RegistrationIdentity.from_agent(receiver)
    now = datetime.now(timezone.utc)
    store.set_continuation(identity, "generation-1", ttl_secs=90, now=now)

    with patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ), patch(
        "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
    ):
        fresh = CliRunner().invoke(cli, ["stop"], input="{}")
    assert "wait" in json.loads(fresh.output)["reason"].lower()

    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE continuation_leases SET expires_at = ? WHERE agent_id = ?",
            ((now - timedelta(seconds=1)).isoformat(), receiver.session_id),
        )
    with patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ), patch(
        "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
    ):
        stale = CliRunner().invoke(cli, ["stop"], input="{}")
    assert "recovery" in json.loads(stale.output)["reason"].lower()
    assert store.get_continuation_status(receiver.session_id).state is (
        ContinuationState.ACTIVE_STALE
    )


def test_first_mate_post_tool_touch_refreshes_existing_but_never_creates(store):
    receiver = register_first_mate(store)
    identity = RegistrationIdentity.from_agent(receiver)
    stale_now = datetime.now(timezone.utc) - timedelta(minutes=5)
    store.set_continuation(
        identity, "generation-1", ttl_secs=1, now=stale_now,
    )

    with patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ), patch(
        "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
    ):
        result = CliRunner().invoke(cli, ["post-tool-use"], input="{}")

    assert result.output == ""
    refreshed = store.get_continuation_status(receiver.session_id)
    assert refreshed.state is ContinuationState.ACTIVE_FRESH
    assert refreshed.generation == "generation-1"
    assert store.clear_continuation(identity, "generation-1")

    with patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ), patch(
        "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
    ):
        CliRunner().invoke(cli, ["post-tool-use"], input="{}")
    assert store.get_continuation_status(receiver.session_id).state is (
        ContinuationState.IDLE
    )


def test_first_mate_prompt_submit_adds_bounded_state_without_prompt_bytes(store):
    sender = store.register_agent("sender", "%1", "test", AgentKind.CLAUDE)
    receiver = register_first_mate(store)
    thread = store.create_thread(
        "pending", sender.session_id, [sender.session_id, receiver.session_id],
    )
    store.send_message(thread.id, sender.session_id, "work")
    prompt = "USER-PROMPT-MUST-STAY-OPAQUE"

    with patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ), patch(
        "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
    ):
        result = CliRunner().invoke(
            cli, ["prompt-submit"], input=json.dumps({"prompt": prompt}),
        )

    output = json.loads(result.output)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "$first-mate" in context
    assert "pending_deliveries=1" in context
    assert prompt not in result.output
    assert len(result.output.encode("utf-8")) < 4096
    assert store.get_inbox(receiver.session_id)[0]["state"] == "pending"


def test_first_mate_stop_allows_exit_only_when_idle_and_empty(store):
    receiver = register_first_mate(store)
    with patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ), patch(
        "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
    ):
        result = CliRunner().invoke(cli, ["stop"], input="{}")

    assert json.loads(result.output) == {}


def test_first_mate_identity_mismatch_stop_fails_closed_to_recovery(
    monkeypatch, store,
):
    receiver = register_first_mate(store, tmux_socket="/tmp/tmux")
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks.resolve_pane_agent", lambda *_args: None,
    )
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks.load_activation",
        lambda *_args: {"session_id": receiver.session_id},
    )
    with patch.dict(
        os.environ,
        {"TMUX_PANE": "%2", "TMUX": "/tmp/tmux,123,0"},
    ), patch("claude_code_tools.msg.hooks.subprocess.run") as run, patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ):
        run.return_value.stdout = "test\n"
        result = CliRunner().invoke(
            cli, ["stop"], input=json.dumps({"model": "gpt-test"}),
        )

    output = json.loads(result.output)
    assert output["decision"] == "block"
    assert "recovery" in output["reason"].lower()


def test_first_mate_db_unavailable_stop_uses_activation_marker(monkeypatch):
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks._current_tmux_scope",
        lambda: ("main", "/tmp/tmux", "%2"),
    )
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks.load_activation",
        lambda *_args: {"session_id": "stable"},
    )
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks.MsgStore",
        lambda: (_ for _ in ()).throw(OSError("db unavailable")),
    )

    result = CliRunner().invoke(
        cli, ["stop"], input=json.dumps({"model": "gpt-test"}),
    )

    output = json.loads(result.output)
    assert output["decision"] == "block"
    assert "recovery" in output["reason"].lower()


def test_tmux_session_probe_failure_still_finds_marker_by_socket_and_pane(
    monkeypatch,
):
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    seen = []

    def load(_db, session, socket, pane):
        seen.append((session, socket, pane))
        return {"session_id": "stable"}

    monkeypatch.setattr("claude_code_tools.msg.hooks.load_activation", load)
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks.MsgStore",
        lambda: (_ for _ in ()).throw(OSError("db unavailable")),
    )
    with patch.dict(
        os.environ,
        {"TMUX_PANE": "%2", "TMUX": "/tmp/tmux,123,0"},
    ):
        result = CliRunner().invoke(
            cli, ["stop"], input=json.dumps({"model": "gpt-test"}),
        )

    assert seen == [(None, "/tmp/tmux", "%2")]
    assert json.loads(result.output)["decision"] == "block"


def test_first_mate_store_read_error_stop_fails_closed(monkeypatch, store):
    receiver = register_first_mate(store)
    monkeypatch.setattr(
        store,
        "count_pending_deliveries",
        lambda _agent_id: (_ for _ in ()).throw(sqlite3.DatabaseError("broken")),
    )
    with patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ), patch(
        "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
    ):
        result = CliRunner().invoke(
            cli, ["stop"], input=json.dumps({"model": "gpt-test"}),
        )

    output = json.loads(result.output)
    assert output["decision"] == "block"
    assert "recovery" in output["reason"].lower()
