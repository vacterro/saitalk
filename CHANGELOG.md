# Changelog

## 0.1.1 - 2026-08-07

- Added SAITALK.md §3 Authority: six-level conflict-resolution ladder.
  Ordinary user prose cannot silently disable SAITALK; exact facts always beat
  persona.
- Rewrote SAITALK.md §5 as an explicit completion gate: resolve conflicts,
  check safety/destructive scope/possibility before side effects, stop with
  the exact blocker, then complete. Added a six-row request decision table.
- Made SAITALK.md the single normative behavior source. SKILL.md now owns only
  loading, activation, validation, and handoff mechanics.
- Adapters reduced to transport: they load files, they do not restate rules.
- Added `validate_drift`: rejects independent-rule tokens in `adapters/*.md`
  that would contradict or extend the contract.
- All contract markers refreshed.

## 0.1.0 - 2026-08-07

- Separated chat language from chat voice. `chat_style=caveman-ded` now names
  the voice only (compression, attitude, evidence gate, restrained profanity,
  no filler, no decorative mixing, no persona leakage). `reply_language` alone
  selects the chat language.
- Legacy `chat_style=caveman-ded-en` is rejected with one exact repair
  instruction (`chat_style=caveman-ded` + a `reply_language` choice) instead
  of validating while demanding contradictory output.
- `spec_version` bumped from `1` to `2`.
- All contract markers refreshed for the migrated contract.

## 0.0.4 - 2026-08-07

- Removed dead config keys (spec_version, artifact_language, review_mode, response_budget had no runtime effect and no documentation).
- Added §11 Configuration reference to SAITALK.md documenting every config key.
- Fixed tri-source suspension-list contradiction (SAITALK.md §10, SKILL.md, evals/cases.json now agree on 7 items).
- Removed "host may expose additional profiles" language — chat_style=caveman-ded-en is the only valid value.
- Fixed response_budget now references saitalk.conf instead of hardcoding "five"/"eight".
- Fixed Windows backslash paths to forward slashes for cross-platform compatibility.
- Fixed fragile importlib绕道 in tests — saitalk.py now uses normal `sys.exit(main())` guard.
- Simplified CONF_PATTERN regex (redundant alternation removed).
- INTERPRETATION.md §7 human-use path now includes SKILL.md in attach list.

## 0.0.3 - 2026-08-07

- Validator now enforces `saitalk_status: active` in bound state files when the field is present.
- Added tests for inactive-status failure and active-status pass.

## 0.0.2 - 2026-08-07

- Added SAIPEN Core maintenance state (`.saipen/`, gitignored) and gitignore entries.
- Added README version line.

## 0.0.1 - 2026-08-07

- Initial release of SAITALK as a standalone repository.
- Portable, host-neutral response-behavior protocol for humans and AI agents.
- Mandatory built-in English `caveman-ded-en` profile with English as default language.
- Root `SKILL.md`, normative `SAITALK.md` behavior contract, and operator-controlled `saitalk.conf`.
- Completion-first and evidence-gated criticism; no-fake-findings review discipline.
- Deterministic contract marker and optional long-running state binding.
- Generic host adapter, regression evals, and zero-dependency tests.
