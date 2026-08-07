# Generic SAITALK Bootstrap
This file is transport. It loads files; it does not add rules.
Load `SKILL.md`, `SAITALK.md`, and `saitalk.conf` as persistent response
behavior for this session. `SAITALK.md` is the normative communication
contract; `saitalk.conf` is operator-controlled configuration.
After loading, follow `SAITALK.md`. Do not summarize it and stop.
The `contract_id` inside those files is a loaded marker, not proof of
currency: stale files can carry the same marker. Claim the contract is
current only when `scripts/saitalk.py validate` recomputes the expected ID
from these exact files.
