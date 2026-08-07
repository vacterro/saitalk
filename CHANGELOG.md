# Changelog

## 0.1.8 - 2026-08-07

Correction of a false release claim. The 0.0.4 entry stated that dead config
keys were "removed"; they were not removed, only documented. This entry states
what actually happened to each key:

- `spec_version` — retained, bumped `1` -> `2` for the language/style schema
  migration (0.1.0). Fixed conformance declaration.
- `reply_language` — retained, became the sole chat-language selector
  (0.1.0). Operator-controlled setting.
- `chat_style` — retained, value migrated from `caveman-ded-en` to
  `caveman-ded`; legacy value is rejected with one exact repair instruction
  (0.1.0). Fixed conformance declaration.
- `artifact_language` — retained and given executable semantics via
  `resolve_artifact_language()` (0.1.4). Operator-controlled setting.
- `review_mode` — retained. Fixed conformance declaration.
- `response_budget` — retained and validated (boundaries 1-20, integer-only).
  Operator-controlled setting.
- `contract_id` — retained; runtime seal extended over SAITALK.md, saitalk.conf
  and SKILL.md, digest suffix 8 -> 16 hex (0.1.2). Derived field.

Other migrations in this release wave:

- State schema: `saitalk_status` plus new `saitalk_voice` field, each required
  exactly once; status only `active`, voice `active` or `suspended`;
  standalone-only voice commands (0.1.3).
- Authority: six-level conflict-resolution ladder; completion-first gate with
  a decision table; SAITALK.md is the single normative behavior source, SKILL
  shortened to mechanics, adapters transport-only (0.1.1).
- Refresh is now transactional with byte-exact rollback (0.1.5).
- Tests expanded from 5 to 42 across config, state, seal, refresh, CLI and
  eval classes (0.1.6, 0.1.7).
- Eval status: harness is executable and the corpus is schema-valid, but no
  model run was performed in this wave; results are recorded `NOT_RUN`, not
  `PASS`.

## 0.1.7 - 2026-08-07

- Evals became an honest, executable harness (`evals/harness.py`): schema
  validation (version 1, unique case IDs, required `setup`/`prompt`/`must`/
  `must_not`), deterministic case export for a host/model runner, and a
  machine-readable `results.json` with explicit `PASS`, `FAIL`, `SKIP`, and
  `NOT_RUN` states.
- Rewrote `evals/cases.json` to 17 behavioral cases, including fixed `en`/
  `et`/`ru`, auto language selection, quoted foreign text not changing chat
  language, artifact language independent from chat, zero-finding review,
  preference not blocking completion, destructive precheck, standalone
  suspension, quoted `normal mode` not suspending, explicit resume, suspended
  voice surviving handoff, and no persona leakage into artifacts.
- Added `evals/README.md` and a `NOT_RUN` result policy: behavioral
  conformance is claimed only when a real model/host run recorded results.
- No model run was performed in this wave; the eval corpus is recorded as
  `NOT_RUN`, not `PASS`.

## 0.1.6 - 2026-08-07

- Expanded the regression suite from 5 tests to 34, organized as config,
  state, seal, refresh, CLI-subprocess, and canonical-suite classes.
- Library coverage: every allowed and every invalid language; valid and
  invalid style; `response_budget` boundaries 0/1/20/21/-1 and non-integer;
  missing, extra, duplicate, and empty config keys; duplicate and stale
  `contract_id`; CRLF normalization; invalid UTF-8; missing file; directory
  path; duplicate/missing state fields; invalid status; invalid voice;
  suspension/resume transitions; unrelated host state fields; SKILL mutation
  invalidation; atomic refresh rollback.
- CLI subprocess coverage: `validate`, `refresh`, `print-id`;
  `--contract`, `--config`, `--state`, `--skill` overrides; exact exit codes;
  correct stdout/stderr; no raw traceback on expected failures; lock
  contention.
- Stable package import: `from scripts import saitalk` via `scripts/__init__.py`
  instead of a bare cached module name.
- Canonical suite validates the shipped `examples/STATE.md` and the live
  package.

## 0.1.5 - 2026-08-07

- `refresh` is now transactional: cross-platform process lock, sibling temp
  files with flush+fsync, atomic `os.replace`, and byte-exact restoration of
  both originals if any write or replacement fails. No partial package is
  left behind. Identical contract/config paths are rejected.
- All expected filesystem failures (missing file, path is a directory,
  permission denied, invalid UTF-8, write failure, replacement failure, lock
  contention) now raise a clean `SaitalkError` with no raw traceback.
- Added rollback, concurrent-refresh, and same-path red controls to the test
  suite.

## 0.1.4 - 2026-08-07

- `artifact_language` now has executable semantics:
  `resolve_artifact_language()` implements exact precedence (task-required
  language > existing artifact language > repository-local artifact contract >
  configured `artifact_language` > English fallback; `auto` cascades existing >
  task > repository documentation > English).
- Every config key is now classified in SAITALK.md §12 as operator-controlled
  setting, fixed conformance declaration, or derived field.
- Validator output now reports `artifact_language`.
- All contract markers refreshed.

## 0.1.3 - 2026-08-07

- Real state machine: bound state now requires exactly one occurrence of each
  of `saitalk_contract`, `saitalk_status`, and `saitalk_voice`. Duplicates,
  missing fields, unknown values, and stale contracts fail validation.
  Unrelated host-native state fields remain allowed.
- `saitalk_status` accepts only `active`; `saitalk_voice` accepts `active` or
  `suspended`.
- Voice commands are standalone-only: `stop caveman` / `normal mode` suspend;
  `resume caveman` / `saitalk mode` restore. Quoted, code, pasted, example,
  or discussion text never changes state.
- Persistence across compaction and handoff is guaranteed only through the
  serialized `saitalk_voice` field.
- All contract markers refreshed.

## 0.1.2 - 2026-08-07

- Runtime seal: `contract_id` is now a deterministic path-tagged hash of a
  three-member runtime manifest (SAITALK.md, saitalk.conf, SKILL.md), sorted,
  LF-normalized, UTF-8 with BOM tolerated, sentinel-replaced. Any normative
  file change now invalidates the marker, including SKILL.md edits.
- Digest suffix extended from 8 to 16 hexadecimal characters. Marker format
  migration: `saitalk-<16 hex>`.
- Missing manifest member or mismatched member path is a clean `SaitalkError`.
- Adapters are explicitly nonnormative and are not hashed.

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
