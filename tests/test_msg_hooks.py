"""Tests for msg-hook CLI commands."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from claude_code_tools.amux.model import Agent as AmuxAgent
from claude_code_tools.msg import hooks as hooks_module
from claude_code_tools.msg.activation import (
    load_host_session_attestation,
)
from claude_code_tools.msg.hooks import _find_self_agent, cli
from claude_code_tools.msg.models import (
    Agent,
    AgentKind,
    ContinuationState,
    DeliveryMode,
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
    result = CliRunner().invoke(
        cli, ["session-start"], input=json.dumps({"session_id": "019d-host"}),
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {}
    attestation = load_host_session_attestation(
        db_path, "main", "/tmp/tmux", "%2",
    )
    assert attestation["host_session_id"] == "019d-host"
    assert "host_session_title" not in attestation
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


def test_codex_push_stop_without_messages_emits_empty_json(store):
    receiver = store.register_agent(
        "codex", "%98", "test", AgentKind.CODEX,
    )
    with patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ), patch(
        "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
    ):
        result = CliRunner().invoke(
            cli, ["stop"], input=json.dumps({"model": "gpt-test"}),
        )

    assert json.loads(result.output) == {}


@pytest.mark.parametrize("stop_hook_active", (False, True))
def test_codex_push_stop_uses_official_block_shape(
    store, stop_hook_active,
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
                {"model": "gpt-test", "stop_hook_active": stop_hook_active},
            ),
        )

    output = json.loads(result.output)
    assert output["decision"] == "block"
    assert "Run msg inbox" in output["reason"]
    assert "hookSpecificOutput" not in output
    assert store.get_inbox(receiver.session_id)[0]["state"] == "notified"


def test_push_output_failure_releases_claim(store):
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


def test_push_hook_claims_only_its_recipient(store):
    sender = store.register_agent("sender", "%1", "test", AgentKind.CLAUDE)
    first = store.register_agent("first", "%2", "test", AgentKind.CLAUDE)
    second = store.register_agent("second", "%3", "test", AgentKind.CODEX)
    thread = store.create_thread(
        "Test",
        sender.session_id,
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


def test_push_hook_does_not_repeat_notified_delivery(setup_agents):
    _sender, receiver, store = setup_agents
    claimed = store.claim_pending_deliveries(
        "watcher", recipient_id=receiver.session_id,
    )
    store.mark_notified(claimed[0]["id"], "watcher")

    with patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ), patch(
        "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
    ):
        result = CliRunner().invoke(cli, ["stop"], input="{}")

    assert result.output == ""


def test_push_prompt_submit_adds_inbox_notice(setup_agents):
    _sender, receiver, store = setup_agents
    with patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ), patch(
        "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
    ):
        result = CliRunner().invoke(cli, ["prompt-submit"], input="{}")

    output = json.loads(result.output)
    context = output["hookSpecificOutput"]
    assert context["hookEventName"] == "UserPromptSubmit"
    assert "msg inbox" in context["additionalContext"]



def register_first_mate(
    store, *, kind=AgentKind.CODEX, pane="%2", tmux_socket=None,
):
    return store.register_agent(
        "first-mate", pane, "test", kind, tmux_socket,
        pid=202, cwd="/repo",
        consumer_protocol="first-mate.v1",
        delivery_mode=DeliveryMode.PULL,
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
    assert output["reason"] == (
        "[MSG pull protocol=first-mate.v1] pending=1; lease=idle."
    )
    assert "msg inbox" not in output["reason"]
    assert store.get_inbox(receiver.session_id)[0]["state"] == "pending"


def test_pull_stop_echoes_opaque_protocol_without_interpreting_it(store):
    receiver = store.register_agent(
        "opaque-pull",
        "%2",
        "test",
        AgentKind.CODEX,
        pid=202,
        cwd="/repo",
        consumer_protocol="probe.other.v1",
        delivery_mode=DeliveryMode.PULL,
        process_start_identity="linux:202:2",
    )
    store.set_continuation(
        RegistrationIdentity.from_agent(receiver),
        "opaque-generation",
        ttl_secs=90,
    )

    with patch(
        "claude_code_tools.msg.hooks.MsgStore", return_value=store,
    ), patch(
        "claude_code_tools.msg.hooks._find_self_agent", return_value=receiver,
    ):
        result = CliRunner().invoke(cli, ["stop"], input="{}")

    assert json.loads(result.output)["reason"] == (
        "[MSG pull protocol=probe.other.v1] "
        "pending=0; lease=active_fresh."
    )


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
    assert json.loads(fresh.output)["reason"].endswith(
        "lease=active_fresh."
    )

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
    assert json.loads(stale.output)["reason"].endswith(
        "lease=active_stale."
    )
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
    assert context == (
        "[MSG pull protocol=first-mate.v1] pending=1; lease=idle."
    )
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
        lambda *_args: {
            "session_id": receiver.session_id,
            "consumer_protocol": "first-mate.v1",
            "delivery_mode": "pull",
        },
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
    assert output["reason"].endswith("lease=recovery_required.")


def test_first_mate_db_unavailable_stop_uses_activation_marker(monkeypatch):
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks._current_tmux_scope",
        lambda: ("main", "/tmp/tmux", "%2"),
    )
    monkeypatch.setattr(
        "claude_code_tools.msg.hooks.load_activation",
        lambda *_args: {
            "session_id": "stable",
            "consumer_protocol": "first-mate.v1",
            "delivery_mode": "pull",
        },
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
    assert output["reason"].endswith("lease=recovery_required.")


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
        return {
            "session_id": "stable",
            "consumer_protocol": "first-mate.v1",
            "delivery_mode": "pull",
        }

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
    assert output["reason"].endswith("lease=recovery_required.")
