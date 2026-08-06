# Changelog

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
