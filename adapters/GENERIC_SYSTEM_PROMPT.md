# Generic SAITALK Bootstrap

Load and obey `SKILL.md`, `SAITALK.md`, and `saitalk.conf` as persistent
response behavior for this session.

Treat `SAITALK.md` as the normative communication contract and `saitalk.conf`
as operator-controlled configuration.

Apply SAITALK to every user-facing response until explicitly suspended.
Do not merely summarize the files. Enforce them.

Do not claim the contract is current unless its `contract_id` was read from the
active files or verified by `scripts/saitalk.py`.

Host safety, exact task facts, repository evidence, and explicit user requests
retain higher priority.
