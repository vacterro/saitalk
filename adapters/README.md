# Host Adapters

SAITALK is host-neutral. Use the smallest adapter the host supports.

## System prompt or custom instructions

Load:

```text
adapters/GENERIC_SYSTEM_PROMPT.md
SKILL.md
SAITALK.md
saitalk.conf
```

## Project instruction file

Reference the same four files from the host's project-level instruction file.

## Skills directory

Place the complete SAITALK directory in a supported skills location. The root
`SKILL.md` is intentionally self-contained and portable.

## Orchestrator

Load SAITALK before worker dispatch. Persist the complete bound SAITALK
state in handoff state — `saitalk_contract`, `saitalk_status`, and
`saitalk_voice` (SAITALK.md §11), not `contract_id` alone: voice suspension
survives handoff only when `saitalk_voice` is carried too. Reject stale
workers that claim a different `saitalk_contract`.

## Stateless chat

Paste `adapters/GENERIC_SYSTEM_PROMPT.md`, then attach or paste `SKILL.md`,
`SAITALK.md`, and `saitalk.conf`.

No adapter is normative. If an adapter conflicts with `SAITALK.md`, fix the
adapter.
