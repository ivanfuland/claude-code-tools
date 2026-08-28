---
description: Register this session as a named agent for inter-agent communication
allowed-tools: Bash
arguments:
  - name: agent_name
    description: A human-friendly name for this agent session (e.g., architect, tester, reviewer)
    required: true
---

Run ONLY this ONE command. Do NOT run anything before
or after it. No tmux commands, no queries, no status
checks. The command auto-detects everything it needs.

```bash
msg register $ARGUMENTS
```

This command keeps the default push notification route. A durable pull
consumer registers an opaque upper-layer protocol label explicitly:

```bash
msg register --consumer-protocol <protocol-id> --delivery-mode pull --json $ARGUMENTS
```

The plugin SessionStart hook records an optional opaque host-session endpoint
fact. `msg register` attaches it only when pane/Harness/PID/start/cwd match;
the CLI never accepts a caller-supplied host session ID or interprets names.

Do not switch an existing registration from pull to push while it owns an
active wake lease.

Show the output to the user. You are done.
