---
name: saitalk
description: Portable response-behavior contract for humans and AI agents. Enforces a configured reply language, a suspendable caveman-ded voice layer over persistent non-voice invariants (evidence-gated criticism, exact technical facts, artifact boundaries, completion-first answers), anti-oppositional review, and anti-drift checks across chats, IDE agents, CLI agents, orchestration systems, and project-local workflows.
---

# SAITALK

SAITALK is a portable conversation-control skill.

It does not depend on SAIPEN, Claude Code, Codex, Gemini, an IDE, a plugin
system, or a specific model. Any host that can load text instructions can use
it. Hosts with files and hooks can additionally validate state and contract
freshness.

`SAITALK.md` owns all normative behavior: language, voice, authority, hard
bans, completion-first, evidence gate, review discipline, exactness,
surfaces, persistence, suspension, and configuration. This file owns only
loading, activation, validation, and handoff mechanics.

## Load order

1. Read `saitalk.conf`.
2. Read `SAITALK.md`.
3. Apply the configured `chat_style` and `reply_language` to every
   user-facing response. `chat_style` may be suspended by a standalone voice
   command (SAITALK.md §11); `reply_language` and every non-voice rule stay
   active.
4. Read `references/INTERPRETATION.md` only for conflicts, edge cases, review
   disputes, or contract maintenance.
5. Never guess missing or invalid configuration values.

## Authority

Authority and precedence are defined in `SAITALK.md` §3. This file adds no
authority rules.

## Portable activation

A host may activate SAITALK in any of these ways:

- Add `SKILL.md`, `SAITALK.md`, and `saitalk.conf` to its system context.
- Place this directory in a supported skills folder.
- Reference `adapters/GENERIC_SYSTEM_PROMPT.md` from project instructions.
- Prepend the generated compact bootstrap to a session.
- Load the contract through an orchestrator before dispatching work.

Adapters are transport. They are not normative.

## Optional state checkpoint

For long-running or multi-agent work, store the bound state fields defined in
`SAITALK.md` §11:

```yaml
saitalk_contract: <contract_id>
saitalk_status: active
saitalk_voice: active
```

Field names and values must be exact; each field appears exactly once.
Unrelated host-native state fields may coexist.

Validate with:

```powershell
py scripts/saitalk.py validate --state STATE.md
```

After editing any normative file (`SAITALK.md`, `saitalk.conf`, `SKILL.md`),
refresh the marker:

```powershell
py scripts/saitalk.py refresh
```

## Suspension

Suspension rules, voice commands, and what remains active while voice is
suspended are defined in `SAITALK.md` §11.
