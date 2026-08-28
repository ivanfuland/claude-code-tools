"""Hook commands for msg inter-agent communication.

Provides Stop and UserPromptSubmit hooks that check
for unread messages and inject notifications into the
agent's context. Used by both Claude Code and Codex CLI.

Both hooks use the same claim protocol as the watcher
to prevent double-notification.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import click

from claude_code_tools.amux.scan import resolve_pane_agent
from claude_code_tools.process_identity import process_start_identity

from .activation import (
    is_first_mate_session_title,
    load_activation,
    load_host_session_attestation,
    write_host_session_attestation,
)
from .models import (
    AgentKind,
    ConsumerProtocol,
    ContinuationState,
    RegistrationIdentity,
    _new_uuid,
)
from .store import DEFAULT_DB_PATH, MsgStore

_LOADED_HOOK_MODULE_SHA256 = hashlib.sha256(
    Path(__file__).read_bytes(),
).hexdigest()


def hook_module_sha256() -> str:
    return _LOADED_HOOK_MODULE_SHA256


def _current_tmux_scope() -> tuple[str, str | None, str] | None:
    """Resolve the current pane scope without opening the msg database."""
    pane_id = os.environ.get("TMUX_PANE")
    if not pane_id:
        return None
    tmux_socket = os.environ.get("TMUX", "").split(",", 1)[0] or None

    try:
        cmd = ["tmux"]
        if tmux_socket:
            cmd += ["-S", tmux_socket]
        cmd += ["display-message", "-t", pane_id, "-p", "#{session_name}"]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=5,
        )
        tmux_session = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if not tmux_session:
        return None
    return tmux_session, tmux_socket, pane_id


def _current_tmux_locator() -> tuple[str | None, str] | None:
    pane_id = os.environ.get("TMUX_PANE")
    if not pane_id:
        return None
    tmux_socket = os.environ.get("TMUX", "").split(",", 1)[0] or None
    return tmux_socket, pane_id


def _current_window_name(
    tmux_socket: str | None, pane_id: str,
) -> str | None:
    cmd = ["tmux"]
    if tmux_socket:
        cmd += ["-S", tmux_socket]
    cmd += ["display-message", "-t", pane_id, "-p", "#{window_name}"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.rstrip("\n")
    return value or None


def _attest_host_session(hook_input: object) -> None:
    if not isinstance(hook_input, dict):
        raise TypeError("hook input is invalid")
    host_session_id = hook_input.get("session_id")
    if (
        not isinstance(host_session_id, str)
        or not host_session_id
        or len(host_session_id.encode("utf-8")) > 256
        or any(ord(character) < 32 or ord(character) == 127
               for character in host_session_id)
    ):
        raise ValueError("host session id is invalid")
    scope = _current_tmux_scope()
    if scope is None:
        raise ValueError("tmux scope is unavailable")
    tmux_session, tmux_socket, pane_id = scope
    target = resolve_pane_agent(pane_id, tmux_socket)
    if target is None:
        raise ValueError("TUI identity is unavailable")
    start_identity = process_start_identity(target.pid)
    title = _current_window_name(tmux_socket, pane_id)
    if start_identity is None or title is None:
        raise ValueError("host identity is incomplete")
    write_host_session_attestation(
        DEFAULT_DB_PATH,
        tmux_session=tmux_session,
        tmux_socket=tmux_socket,
        pane_id=pane_id,
        agent_kind=AgentKind(target.kind),
        pid=target.pid,
        process_start_identity=start_identity,
        cwd=target.cwd,
        host_session_id=host_session_id,
        host_session_title=title,
        code_sha256=hook_module_sha256(),
    )


def _set_codex_session_title(host_session_id: str, title: str) -> bool:
    requests = "\n".join((
        json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "thread/name/set",
            "params": {"threadId": host_session_id, "name": title},
        }),
        json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "thread/read",
            "params": {"threadId": host_session_id, "includeTurns": False},
        }),
    )) + "\n"
    try:
        result = subprocess.run(
            ["codex", "app-server", "proxy"],
            input=requests,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0 or result.stderr:
        return False
    responses = []
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("id") in {1, 2}:
            responses.append(value)
    if len(responses) != 2 or any("error" in value for value in responses):
        return False
    readback = next(value for value in responses if value.get("id") == 2)
    return (
        ((readback.get("result") or {}).get("thread") or {}).get("name")
        == title
    )


def _bootstrap_title_response(
    hook_event: str, scope: tuple[str, str | None, str] | None,
) -> dict | None:
    if hook_event != "UserPromptSubmit" or scope is None:
        return None
    tmux_session, tmux_socket, pane_id = scope
    attestation = load_host_session_attestation(
        DEFAULT_DB_PATH, tmux_session, tmux_socket, pane_id,
    )
    if not attestation or attestation.get("invalid"):
        return None
    title = attestation.get("host_session_title")
    host_session_id = attestation.get("host_session_id")
    if (
        not isinstance(title, str)
        or not is_first_mate_session_title(title)
        or not isinstance(host_session_id, str)
        or not host_session_id
    ):
        return None
    kind = attestation.get("agent_kind")
    if kind == AgentKind.CLAUDE.value:
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "sessionTitle": title,
            }
        }
    if kind == AgentKind.CODEX.value:
        if not _set_codex_session_title(host_session_id, title):
            raise ValueError("Codex session title readback failed")
        return {}
    return None


def _find_self_agent(store: MsgStore) -> object | None:
    """Find the exact agent registered for this pane."""
    scope = _current_tmux_scope()
    if scope is None:
        return None
    tmux_session, tmux_socket, pane_id = scope

    matches = [
        agent for agent in store.list_agents(tmux_session, tmux_socket)
        if agent.pane_id == pane_id and agent.tmux_socket == tmux_socket
    ]
    if len(matches) != 1:
        return None
    agent = matches[0]
    if agent.consumer_protocol is ConsumerProtocol.LEGACY:
        return agent
    target = resolve_pane_agent(agent.pane_id, agent.tmux_socket)
    if target is None:
        return None
    actual = (
        target.session,
        target.extra.get("pane_id"),
        target.kind,
        target.pid,
        process_start_identity(target.pid),
        target.cwd,
    )
    expected = (
        agent.tmux_session,
        agent.pane_id,
        agent.agent_kind.value,
        agent.pid,
        agent.process_start_identity,
        agent.cwd,
    )
    return agent if actual == expected else None


def _check_and_notify(
    hook_event: str,
) -> None:
    """Common logic for both Stop and UserPromptSubmit.

    Reads JSON from stdin, checks DB for unread messages,
    claims deliveries, outputs JSON response.
    """
    # Read hook input
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    if hook_event == "SessionStart":
        try:
            _attest_host_session(hook_input)
        except (OSError, TypeError, ValueError):
            _emit_first_mate_recovery(hook_event)
            return
        _approve(json_object=True)
        return

    scope = _current_tmux_scope()
    try:
        title_response = _bootstrap_title_response(hook_event, scope)
    except ValueError:
        _emit_first_mate_recovery(hook_event)
        return
    marker = None
    if scope is not None:
        marker = load_activation(DEFAULT_DB_PATH, *scope)
    else:
        locator = _current_tmux_locator()
        if locator is not None:
            tmux_socket, pane_id = locator
            marker = load_activation(
                DEFAULT_DB_PATH, None, tmux_socket, pane_id,
            )

    try:
        store = MsgStore()
    except Exception:
        if marker:
            _emit_first_mate_recovery(hook_event)
            return
        _approve(
            json_object=(
                hook_event == "Stop"
                and isinstance(hook_input, dict)
                and "model" in hook_input
            )
        )
        return

    try:
        me = _find_self_agent(store)
        candidates = []
        if scope is not None:
            tmux_session, tmux_socket, pane_id = scope
            candidates = [
                agent for agent in store.list_agents(tmux_session, tmux_socket)
                if agent.pane_id == pane_id and agent.tmux_socket == tmux_socket
            ]
        known_first_mate = any(
            agent.consumer_protocol is ConsumerProtocol.FIRST_MATE_V1
            for agent in candidates
        )
    except Exception:
        if marker:
            _emit_first_mate_recovery(hook_event)
            return
        raise
    if not me:
        if title_response is not None:
            print(json.dumps(title_response))
            return
        if marker or known_first_mate:
            _emit_first_mate_recovery(hook_event)
            return
        _approve(
            json_object=(
                hook_event == "Stop"
                and isinstance(hook_input, dict)
                and "model" in hook_input
            )
        )
        return

    if marker and me.consumer_protocol is not ConsumerProtocol.FIRST_MATE_V1:
        _emit_first_mate_recovery(hook_event)
        return
    if me.consumer_protocol is ConsumerProtocol.FIRST_MATE_V1:
        try:
            _first_mate_hook(store, me, hook_event, hook_input)
        except Exception:
            _emit_first_mate_recovery(hook_event)
        return

    if hook_event == "PostToolUse":
        _approve()
        return

    # Claim deliveries (same protocol as watcher)
    claimer_id = f"hook-{hook_event}-{_new_uuid()[:8]}"
    claimed = store.claim_pending_deliveries(
        claimer_id, recipient_id=me.session_id,
    )
    if not claimed:
        _approve(
            json_object=(
                hook_event == "Stop" and me.agent_kind is AgentKind.CODEX
            )
        )
        return

    # Build notification
    count = len(claimed)
    senders = list(dict.fromkeys(
        delivery.get("from_name", "unknown") for delivery in claimed
    ))
    sender_str = ", ".join(senders)
    notification = (
        f"[MSG] {count} unread message(s) "
        f"from {sender_str}. "
        f"Run msg inbox when ready."
    )

    if hook_event == "Stop" and me.agent_kind is AgentKind.CODEX:
        response = {"decision": "block", "reason": notification}
    else:
        response = {
            "hookSpecificOutput": {
                "hookEventName": hook_event,
                "additionalContext": notification,
            }
        }
    try:
        print(json.dumps(response), flush=True)
    except Exception:
        for delivery in claimed:
            store.release_delivery(delivery["id"], claimer_id)
        raise

    # A valid host response is now durable on stdout; finalize notification.
    for delivery in claimed:
        store.mark_notified(delivery["id"], claimer_id)


def _first_mate_hook(
    store: MsgStore,
    me: object,
    hook_event: str,
    _hook_input: object,
) -> None:
    """Emit bounded native-hook state without consuming First-mate delivery."""
    identity = RegistrationIdentity.from_agent(me)
    if hook_event == "PostToolUse":
        status = store.get_continuation_status(me.session_id)
        if status.generation is not None:
            try:
                store.touch_continuation(
                    identity, status.generation, ttl_secs=90,
                )
            except ValueError:
                pass
        _approve()
        return

    pending = store.count_pending_deliveries(me.session_id)
    continuation = store.get_continuation_status(me.session_id)
    if hook_event == "Stop":
        if pending == 0 and continuation.state is ContinuationState.IDLE:
            _approve(json_object=True)
            return
        if continuation.state is ContinuationState.ACTIVE_STALE:
            action = "Run $first-mate recovery before resuming bounded wait."
        else:
            action = "Run $first-mate wait and continue the armed responsibility."
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"{action} pending_deliveries={pending}; "
                f"continuation={continuation.state.value}."
            ),
        }))
        return

    if hook_event == "UserPromptSubmit":
        if pending == 0 and continuation.state is ContinuationState.IDLE:
            _approve(json_object=True)
            return
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    "[FIRST-MATE] Invoke $first-mate before ordinary work; "
                    f"pending_deliveries={pending}; "
                    f"continuation={continuation.state.value}."
                ),
            }
        }))
        return

    _approve(json_object=True)


def _emit_first_mate_recovery(hook_event: str) -> None:
    message = (
        "[FIRST-MATE] Run $first-mate recovery; "
        "msg state is unavailable or identity is stale."
    )
    if hook_event == "Stop":
        print(json.dumps({"decision": "block", "reason": message}))
    elif hook_event in {"PostToolUse", "UserPromptSubmit"}:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": hook_event,
                "additionalContext": message,
            }
        }))
    else:
        print("{}")


def _approve(*, json_object: bool = False) -> None:
    """Complete the hook without emitting a client-specific response."""
    if json_object:
        print("{}")


@click.group()
def cli() -> None:
    """msg-hook: Hook commands for msg notifications."""
    pass


@cli.command()
def stop() -> None:
    """Stop hook — check inbox when agent stops."""
    _check_and_notify("Stop")


@cli.command("prompt-submit")
def prompt_submit() -> None:
    """UserPromptSubmit hook — check inbox on user input."""
    _check_and_notify("UserPromptSubmit")


@cli.command("post-tool-use")
def post_tool_use() -> None:
    """PostToolUse hook — refresh an existing continuation heartbeat."""
    _check_and_notify("PostToolUse")


@cli.command("session-start")
def session_start() -> None:
    """SessionStart hook — attest the resumable host identity."""
    _check_and_notify("SessionStart")


def main() -> None:
    """Entry point for msg-hook CLI."""
    cli()


if __name__ == "__main__":
    main()
