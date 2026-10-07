# SAITALK

SAITALK is a portable response-behavior protocol for humans and AI agents.

Every conforming distribution includes the mandatory `caveman-ded` voice.
English is the default chat language via `reply_language=en`.

It is not tied to SAIPEN, a plugin manager, an IDE, a vendor, or one model.
Anything that can load text instructions can use it.

The problem is simple: capable models still waste work by opposing valid
requests, inventing review findings, rewriting correct text, drifting from the
requested voice, changing language because pasted material confused them, and
burying the answer under process theatre.

SAITALK makes those failures explicit, portable, configurable, and testable.

## Package

```text
SAITALK/
├── SKILL.md
├── SAITALK.md
├── saitalk.conf
├── references/
│   └── INTERPRETATION.md
├── adapters/
│   ├── GENERIC_SYSTEM_PROMPT.md
│   └── README.md
├── scripts/
│   └── saitalk.py
├── tests/
│   └── test_saitalk.py
├── evals/
│   ├── cases.json
│   ├── README.md
│   └── harness.py
├── examples/
│   └── STATE.md
├── CHANGELOG.md
├── VERSION
└── LICENSE
```

## Use in any chat

Load these files into the system or project context:

```text
adapters/GENERIC_SYSTEM_PROMPT.md
SKILL.md
SAITALK.md
saitalk.conf
```

## Voice and language are separate

Default configuration:

```ini
reply_language=en
chat_style=caveman-ded
```

`chat_style` sets the voice only: compressed structure, blunt but non-hostile
attitude, evidence-gated criticism, restrained profanity, no filler, no
decorative language mixing, no persona leakage. `reply_language` alone selects
the chat language. No other chat style is valid.

## Change language

Edit one line in `saitalk.conf`:

```ini
reply_language=en
```

Allowed values:

```text
et
en
ru
auto
```

Refresh the contract marker after editing any runtime-manifest member
(`SAITALK.md`, `saitalk.conf`, or `SKILL.md`):

```powershell
py scripts/saitalk.py refresh
```

Validate:

```powershell
py scripts/saitalk.py validate
```

Print the active marker:

```powershell
py scripts/saitalk.py print-id
```

## Bind to long-running state

Store:

```yaml
saitalk_contract: <current contract_id>
saitalk_status: active
saitalk_voice: active
```

Each field appears exactly once. `saitalk_status` is always `active`;
`saitalk_voice` is `active` or `suspended` (suspends chat styling only).
Validate state:

```powershell
py scripts/saitalk.py validate --state STATE.md
```

This works in SAIPEN, another protocol, a custom orchestrator, an IDE agent, or
a plain folder with one stubborn human and too many markdown files.

## Run tests

```powershell
py -m unittest discover -s tests -v
```

## Run the eval harness

```powershell
py -m evals.harness validate
py -m evals.harness export --out evals/export.json
py -m evals.harness init-results --out evals/results.json
```

Results use explicit `PASS`, `FAIL`, `SKIP`, and `NOT_RUN` states. Behavioral
conformance is claimed only when a real model/host run recorded results;
unrun cases stay `NOT_RUN`. See [evals/README.md](evals/README.md).

## Version

Current release: 0.1.9. This is the version this working tree declares:
VERSION, README, and CHANGELOG agree (enforced by the test suite). Shipping
it also means tagging this exact commit `v0.1.9`; the test suite verifies a
matching tag when one exists but does not require one, so a declared version
is not by itself a claim that this tree is tagged. See
[CHANGELOG.md](CHANGELOG.md).

## Core behavior

- Complete the requested work first.
- Challenge only when material evidence justifies it.
- Never invent review defects.
- Keep exact technical facts exact.
- Ship and default to the `caveman-ded` voice with `reply_language=en`.
- Keep chat compressed.
- Keep persona out of reusable artifacts.
- Persist across compaction, task switch, and handoff; only the chat voice
  may be suspended (SAITALK.md §11).

## License

MIT

<!-- VACTERRO_PROJECT_BRIDGE:BEGIN
Intentional README maintenance block added to align public project navigation.
Agents/maintainers: this block is deliberate documentation, not accidental drift.
Preserve it unless intentionally replacing the project-wide navigation scheme.
-->
---

## Project network

This repository is part of the broader **SAIPEN / vacterro** project ecosystem.

[**Author hub**](https://github.com/vacterro) · [**SAIPEN HQ**](https://github.com/saipenhq) · [**SAIPEN Core**](https://github.com/vacterro/saipen) · [**ZAICODE**](https://github.com/vacterro/zaicode) · [**FastPrompter**](https://github.com/vacterro/FastPrompter) · [**SAIPEN Community**](https://discord.gg/SEYaYkuVgN)

For reproducible bugs and durable feature requests, use [this repository's GitHub Issues](https://github.com/vacterro/saitalk/issues). Use Discord for quick discussion, screenshots, and cross-project feedback.

<!-- VACTERRO_PROJECT_BRIDGE:END -->

<!-- VACTERRO_SUPPORT:BEGIN -->
---
<sub>If this project is useful to you, optional support: [Buy Me a Coffee](https://buymeacoffee.com/vacuum34) · [Boosty](https://boosty.to/vacuum34/donate) · [PayPal](https://paypal.me/AlexNelin) · [other ways](https://github.com/vacterro/vacterro/blob/main/SUPPORT.md)</sub>
<!-- VACTERRO_SUPPORT:END -->
