# msg generic push/pull machine contract

Status: unreleased; targets claude-code-tools 1.25.7.

This change keeps the versioned `msg.cli.v1` boundary for Claude Code and
Codex while separating communication behavior from upper-layer workflow
semantics. Registrations now carry an opaque `consumer_protocol` label, an
independent `delivery_mode` (`push` or `pull`), and an optional opaque
`host_session_id`.

The paired msg plugins are version 1.15.2. Their native `SessionStart` hook
attests the real resumable Claude/Codex host endpoint and loaded hook-code
hash. The attestation contains physical endpoint facts only; `msg` no longer
owns native session titles, logical naming rules, resume policy, or business
workflow state.

Push registrations retain watcher notification and legacy inbox behavior.
Pull registrations never enter the watcher's tmux injection path. Native
hooks emit only a generic marker containing the opaque protocol label,
pending count, and continuation-lease state. `msg` does not interpret the
label or invoke a protocol handler.

`msg retarget --replace-registration ID` atomically retires one exact active
temporary registration and moves the stable registration to its verified
physical endpoint. It preserves the stable name, protocol, delivery mode,
threads, deliveries, and continuation lease while returning the replacement
registration ID and previous/current opaque host IDs. Upper layers own any
naming, resume, attempt, or handoff rules around that operation.

Schema v5 adds `delivery_mode` and `host_session_id` and removes all title
semantics from the public contract. The v4 migration maps historical
`consumer_protocol=first-mate.v1` rows to `delivery_mode=pull`; this is the
only compatibility-specific protocol value in runtime source.

`plugins/msg/release-evidence.json` will be refreshed after the exact source
commit is built and the real disposable Claude/Codex endpoint, push/pull, and
registration-replacement smoke passes. The immutable external version ledger
remains a post-merge release gate while this branch is unreleased.
