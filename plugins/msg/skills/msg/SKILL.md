---
name: msg
description: >-
  Inter-agent communication via the msg CLI.
  Use this when you need to send messages to other
  agent sessions, read incoming messages, or
  coordinate with other agents in tmux panes.
---

# msg: Inter-Agent Communication

You can communicate with other coding agent sessions
(Claude Code or Codex CLI) using the `msg` CLI tool.

## Registration

Before sending or receiving messages, register yourself:

```bash
msg register <your-name>
```

This auto-detects your tmux pane. You only need to do
this once per session.

Durable pull consumers register an opaque protocol label plus delivery mode:

```bash
msg register --consumer-protocol <protocol-id> --delivery-mode pull --json <your-name>
```

The default remains protocol `legacy` with delivery mode `push`. The native
`SessionStart` hook records an optional opaque Claude/Codex host session fact;
registration attaches it only to the exact physical endpoint. Never pass or
invent a host session ID on the command line.

## Sending Messages

Send a message directly to another agent:

```bash
msg send <agent-name> "Your message here"
```

Send to multiple agents:

```bash
msg send agent1,agent2 "Message for both of you"
```

## Replying

```bash
msg reply <agent-name> "Your reply here"
```

## Receiving Messages

Check your inbox:

```bash
msg inbox
```

This shows all unread messages grouped by thread and
marks them as read.

For a pull registration, never use that legacy read-and-mark path. Invoke the
handler identified by the registration's opaque protocol label. It repeatedly
peeks a bounded page, durably journals it, then explicitly acknowledges exact
delivery IDs. Hooks may touch an existing wake lease but never create or
replace its generation.

Plugin-native `SessionStart`, `PostToolUse`, `Stop`, and `UserPromptSubmit`
hooks attest the resumable host endpoint and keep an armed pull consumer in
the agent loop. A stale heartbeat emits a generic recovery-required marker;
it never clears the lease. Exact clear with no pending delivery allows Stop.
Pull delivery never uses tmux prompt injection.

Codex treats plugin hooks as non-managed code and skips changed definitions
until the user reviews and trusts the current hash in `/hooks`. Never bypass
that trust decision. A plugin upgrade requires a fresh review when hook bytes
change. That trust covers the hook definition, not imported adapter bytes; the
release evidence and the consuming protocol's verifier must separately check
the complete plugin payload and reject same-version tree drift.

## Other Commands

```bash
msg list          # List registered agents
msg threads       # List active threads
msg status        # Check system health
```

## Guidelines

- Keep messages concise -- they consume context in the
  receiving agent's session.
- When replying, include enough context that the
  recipient understands without re-reading the full
  thread.
- If you need to share code or file paths, reference
  them in the message text rather than pasting large
  blocks.
