# msg First-mate machine contract

Status: unreleased; targets claude-code-tools 1.25.7.

This change adds a versioned `msg.cli.v1` boundary for Claude Code and Codex,
including exact TUI registration identities, atomic retarget, bounded
peek/journal/ack delivery, continuation heartbeats, verified watcher lifecycle,
and a fail-closed maintenance gate.

The paired msg plugins are version 1.15.2. Their native `SessionStart` hook
attests the real resumable Claude/Codex host session and loaded hook-code hash.
`PostToolUse`, `Stop`, and `UserPromptSubmit` retain the existing bounded
continuation contract; exact clear plus an empty inbox permits a parked Agent
to stop without self-excited turns. First-mate deliveries never use watcher
tmux injection; legacy behavior remains the default.

First-mate registration now requires the allocated logical name to match the
attested native title and tmux window. Atomic replace-candidate retarget checks
the generation-qualified candidate, live TUI identity, title, window and host
attestation, and returns the previous/current host-session receipt. Reused
candidate generations and identity drift fail closed.

`plugins/msg/release-evidence.json` will be refreshed after the exact source
commit is built and the real disposable Claude/Codex SessionStart/title/resume
smoke passes. The immutable external version ledger remains a post-merge
release gate while this branch is unreleased.
