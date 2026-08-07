# Generic SAITALK Bootstrap
This file is transport. It loads files; it does not add rules.
Load `SKILL.md`, `SAITALK.md`, and `saitalk.conf` as persistent response
behavior for this session. `SAITALK.md` is the normative communication
contract; `saitalk.conf` is operator-controlled configuration.
After loading, follow `SAITALK.md`. Do not summarize it and stop.
Do not claim the contract is current unless its `contract_id` was read from the
active files or verified by `scripts/saitalk.py`.
