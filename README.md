# SAITALK

SAITALK is a portable response-behavior protocol for humans and AI agents.

Every conforming distribution includes the mandatory English
`caveman-ded-en` profile. English is the default chat language.

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
│   └── cases.json
├── examples/
│   └── STATE.md
├── CHANGELOG.md
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

## Required English mode

Default configuration:

```ini
reply_language=en
chat_style=caveman-ded-en
```

This profile is part of SAITALK itself, not an optional adapter. Additional
language profiles may exist, but the English profile must remain available.

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

Refresh the contract marker after any config or contract edit:

```powershell
py scripts\saitalk.py refresh
```

Validate:

```powershell
py scripts\saitalk.py validate
```

Print the active marker:

```powershell
py scripts\saitalk.py print-id
```

## Bind to long-running state

Store:

```yaml
saitalk_contract: <current contract_id>
saitalk_status: active
```

Validate state:

```powershell
py scripts\saitalk.py validate --state STATE.md
```

This works in SAIPEN, another protocol, a custom orchestrator, an IDE agent, or
a plain folder with one stubborn human and too many markdown files.

## Run tests

```powershell
py -m unittest discover -s tests -v
```

## Core behavior

- Complete the requested work first.
- Challenge only when material evidence justifies it.
- Never invent review defects.
- Keep exact technical facts exact.
- Ship and default to the English `caveman-ded-en` profile.
- Keep chat compressed.
- Keep persona out of reusable artifacts.
- Persist until explicit suspension.

## License

MIT
