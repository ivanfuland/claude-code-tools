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
    load_activation,
    write_host_session_attestation,
)
from .models import (
    AgentKind,
    ContinuationState,
    DeliveryMode,
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
            capture_output=True, text=True, timeout=5, check=False,
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
    if start_identity is None:
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
        code_sha256=hook_module_sha256(),
    )


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
    if agent.delivery_mode is DeliveryMode.PUSH:
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
            _emit_pull_recovery(hook_event, "unknown")
            return
        _approve(json_object=True)
        return

    scope = _current_tmux_scope()
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
            _emit_pull_recovery(
                hook_event, str(marker.get("consumer_protocol", "unknown")),
            )
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
        pane_agents = []
        if scope is not None:
            tmux_session, tmux_socket, pane_id = scope
            pane_agents = [
                agent for agent in store.list_agents(tmux_session, tmux_socket)
                if agent.pane_id == pane_id and agent.tmux_socket == tmux_socket
            ]
        known_pull = any(
            agent.delivery_mode is DeliveryMode.PULL
            for agent in pane_agents
        )
    except Exception:
        if marker:
            _emit_pull_recovery(
                hook_event, str(marker.get("consumer_protocol", "unknown")),
            )
            return
        raise
    if not me:
        if marker or known_pull:
            protocol = (
                str(marker.get("consumer_protocol", "unknown"))
                if marker else "unknown"
            )
            _emit_pull_recovery(hook_event, protocol)
            return
        _approve(
            json_object=(
                hook_event == "Stop"
                and isinstance(hook_input, dict)
                and "model" in hook_input
            )
        )
        return

    if marker and me.delivery_mode is not DeliveryMode.PULL:
        _emit_pull_recovery(hook_event, me.consumer_protocol)
        return
    if me.delivery_mode is DeliveryMode.PULL:
        try:
            _pull_hook(store, me, hook_event, hook_input)
        except Exception:
            _emit_pull_recovery(hook_event, me.consumer_protocol)
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


def _pull_hook(
    store: MsgStore,
    me: object,
    hook_event: str,
    _hook_input: object,
) -> None:
    """Emit bounded native-hook state without consuming pull delivery."""
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
    marker = (
        f"[MSG pull protocol={me.consumer_protocol}] "
        f"pending={pending}; lease={continuation.state.value}."
    )
    if hook_event == "Stop":
        if pending == 0 and continuation.state is ContinuationState.IDLE:
            _approve(json_object=True)
            return
        print(json.dumps({
            "decision": "block",
            "reason": marker,
        }))
        return

    if hook_event == "UserPromptSubmit":
        if pending == 0 and continuation.state is ContinuationState.IDLE:
            _approve(json_object=True)
            return
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": marker,
            }
        }))
        return

    _approve(json_object=True)


def _emit_pull_recovery(hook_event: str, protocol: str) -> None:
    message = (
        f"[MSG pull protocol={protocol}] "
        "pending=unknown; lease=recovery_required."
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
