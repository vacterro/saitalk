# Changelog

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
