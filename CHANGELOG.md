# Changelog

## 0.1.9 - 2026-08-07

Surgical fix/hunt wave over the v0.1.8 baseline. Every item below has a red
control in the test suite. Normative contract text and all markers were
refreshed last, after every other change settled.

### Artifact and chat language

- Artifact resolver precedence fixed completely: the explicit task language,
  the existing-artifact language, and the repository-local language all
  accept any well-formed language tag now, not only `en`/`et`/`ru`
  (`existing=es`, `repo=ja`, and `existing=en-US` previously fell through to
  English or were mis-normalized instead of being honored as `es`, `ja`, and
  `en-us`). Precedence is identical for a fixed and `auto` configuration; the
  task language always wins when more than one explicit level is present. A
  malformed or empty value at any of the three explicit levels fails loudly,
  and so does an invalid configured `artifact_language`.
- Removed the phantom "configured documented fallback" from the `auto`
  reply-language rule (§1) and the matching eval case — there was no such
  config key. The fallback is hardcoded English, stated as such.
- The `auto` chat-language rule no longer special-cases Russian.
  Deterministic precedence: substantive current user prose, else clearly
  established repository documentation language, else English.

### Refresh integrity

- `refresh` validates full contract structure (`check_structure`) against
  the same snapshot it hashes, before any byte is written, so a structurally
  invalid package can never be blessed with a fresh marker. It previously
  skipped structural validation entirely, so a broken required section could
  be refreshed successfully and only fail on the very next `validate`.
- `refresh` reads every manifest member — including `SKILL.md` — exactly
  once, as bytes, under a lock held on all three files; the hash and the
  byte-level `contract_id` replacement are derived from that one snapshot,
  closing a window where a hashed read and a later patched read could
  observe different bytes. `SKILL.md` is now locked too, so a concurrent
  cooperating refresh contends instead of racing.
- Rollback restores only the targets a `refresh` call actually replaced. It
  previously restored every mutable target unconditionally on any failure,
  including ones that were never written — capable of silently overwriting
  an unrelated concurrent edit to an untouched file. The transaction
  guarantee is stated truthfully: caught write/replace failures are
  rollback-attempted for replaced targets only; an interrupted refresh may
  leave a stale split marker recoverable by rerunning `refresh`.
- `refresh` still is byte-preserving (it changes only `contract_id` values
  and keeps each file's own LF/CRLF and BOM convention) and still locks
  every mutable target in deterministic canonical order, rejecting
  contract/config/skill aliasing including same-file hardlinks.
- `replace_id_bytes` is BOM-aware: a UTF-8 BOM immediately before a
  first-line `contract_id` no longer breaks the `^`-anchored byte match
  (previously: 0 matches, `refresh` FAIL, on otherwise-valid content).
- `read_text` mirrors `read_bytes`'s catch-all `OSError` handling, and
  `_require_coherent_root` wraps `Path.resolve()`/`os.path.samefile()` the
  same way, so an unexpected filesystem failure raises a clean
  `SaitalkError` instead of an uncaught exception.
- `RUNTIME_MANIFEST` is now the single executable source of manifest
  membership and order; `read_manifest`, `refresh`, and `root_paths` all
  derive from it through one shared helper instead of separately hardcoding
  the same three filenames.
- Windows lock files no longer grow: acquiring a lock previously wrote a NUL
  byte to a handle opened in append mode, which ignores `seek()` for writes
  and appended a new byte on every acquisition. The lock file is now sized
  once and opened for in-place random access.

### Structural guard

- `check_structure` parses top-level `##` headings outside fenced code
  blocks and HTML comments, and requires the exact 12 canonical section
  identities (number and title) in order — not just that the numbers 1-12
  appear somewhere in the document. Previously, a heading inside a fenced
  example counted toward the check, a renamed section title was invisible to
  it, and an appended unnumbered section (or a fenced decoy standing in for
  a removed real section) could pass. §11 still must reference every bound
  state field and voice command, §12 still document every config key, and
  `SKILL.md` still name `SAITALK.md` as the sole normative behavior source.
  Guards check shape and invariants, never wording.

### Eval harness

- Eval results carry one canonical `contract_id` provenance check
  (`saitalk-[0-9a-f]{16}`), enforced identically on the way out
  (`write_results`) and the way in (`load_results`, which previously did not
  check the field at all): `null`, empty, and malformed values are rejected
  whenever any state besides `NOT_RUN` is recorded.
- `validate-results` distinguishes a schema-valid-but-non-current result
  (`EVALS RESULTS VALID (historical, NOT current)`) from one actually bound
  to the active local package (`EVALS RESULTS CURRENT`), by recomputing the
  active package's `contract_id` (`--contract`/`--config`/`--skill` override
  the default paths). A well-formed but stale or foreign `contract_id`
  previously validated as generic, unqualified `VALID`.
- `load_results`/`validate_results` validate container shapes before any
  `.get`/`.items`/`set()` call: a top-level JSON array, or a `results` field
  that is a list instead of an object, previously raised a raw
  `AttributeError` traceback instead of a clean `EVALS FAIL`.
- Schema strictness closed: whitespace-only `setup`/`prompt`/`must`/
  `must_not` entries and whitespace-only `SKIP` reasons are now rejected
  (previously accepted by a truthiness check that whitespace satisfies);
  unknown keys are rejected at every object level the schema defines — the
  corpus root, the results-file root, and the results file's embedded corpus
  sub-object.
- Results integrity otherwise as before: `results.json` must contain exactly
  the current corpus IDs, one structured record per ID; `SKIP` requires a
  recorded `reason`; a results file bound to a different corpus digest,
  version, or case count fails `validate-results` outright.

### State, drift, and handoff docs

- State parsing also ignores `~~~` fences (previously only backtick fences)
  and HTML comments, with fence-delimiter-type pairing (a ` ``` ` fence
  cannot be closed by `~~~`), so no supported Markdown example or comment
  form can become live state. Bound-state wording says "exactly three
  SAITALK fields" because unrelated host-native fields are allowed.
- Voice commands: trim outer whitespace, collapse internal whitespace,
  casefold, then exact standalone-phrase comparison. `NORMAL MODE` suspends;
  `normal mode.` and `"normal mode"` do not.
- `validate_drift` normalizes and searches each adapter file as one document
  instead of line by line, so a banned token split across a markdown
  line-wrap (`completion-` / `first`) can no longer evade the guard; it
  still scans the adapter tree recursively and states clearly that it is a
  structural drift guard, not semantic proof.
- High-stakes plain prose (§8) is now explicit that it is an ephemeral
  rendering override only: it never mutates the serialized `saitalk_voice`
  state, and the next ordinary response returns to whichever voice state —
  active or suspended — was already in effect. A new eval case
  (`high-stakes-plain-prose-preserves-suspended-voice`) covers the suspended
  side.
- Handoff docs (`adapters/README.md` Orchestrator section,
  `references/INTERPRETATION.md` §9) require persisting the complete bound
  state — `saitalk_contract`, `saitalk_status`, `saitalk_voice` — not
  `contract_id` alone, matching §11, where suspension only survives handoff
  when `saitalk_voice` is serialized.
- `references/INTERPRETATION.md` §4: the "loaded ID" freshness level no
  longer claims to prove the files "were packaged together" — marker text
  can simply be copied; only recomputation establishes consistency.
  Canonicalization now documents the UTF-8 BOM strip it already performed in
  code but never listed, and states plainly that the marker is a
  canonical-content fingerprint (CRLF and BOM intentionally canonicalized
  away before hashing), not exact-byte identity.
- `README.md`'s refresh recipe says "any runtime-manifest member" instead of
  "config or contract edit"; human-use wording says the same instead of
  "either file". `SKILL.md` frontmatter no longer claims a "fixed reply
  language" or a "persistent caveman-ded voice" — both wrong in the general
  case (`reply_language` may be `auto`; the voice may be suspended).
- Authority section: verified repository evidence constrains factual
  correctness; repository prose has only the authority explicitly assigned
  by host, user, or protocol.
- `validate` derives the drift root from the active manifest paths instead
  of the default repo root; manifest members must form one coherent package
  root, and mixed overrides are rejected. Stateless chat activation loads
  `SKILL.md` alongside `SAITALK.md` and `saitalk.conf`; a static regression
  checks every documented activation recipe names all mandatory members.

### Release truth

- The release-tag regression peels annotated tags to their commit
  (`git rev-parse vX.Y.Z^{}`) instead of comparing the tag object's own sha
  to HEAD, which would have falsely failed against a real annotated release
  tag; a synthetic-repo regression covers both annotated and lightweight
  tags. VERSION must still equal the README current release and the
  first/latest CHANGELOG release heading; a matching tag, when present, must
  still point at HEAD.
- `README.md`'s Version section states explicitly that the declared version
  (VERSION/README/CHANGELOG agreement, enforced by the test suite) is not
  itself a claim that this exact tree is tagged and shipped; the tag check
  stays a soft invariant — verified when present, never silently required.
- Coverage sweep: tests expanded from 43 to 172 (+1 skip); eval corpus
  expanded from 17 to 25 behavioral cases. Newly hunted coverage includes
  artifact resolver precedence for non-`en`/`et`/`ru` tags, refresh's
  structural gate and single-snapshot read, rollback's replaced-only
  restoration, eval `contract_id` provenance and current-vs-historical
  reporting, malformed eval container types, eval schema whitespace/root-key
  strictness, tilde-fence and HTML-comment state ownership, the annotated
  release tag, BOM-at-first-line refresh, generic filesystem `OSError`
  discipline, the multiline drift bypass, and the canonical structural
  guard.

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
- Refresh is now transactional with byte-exact rollback (0.1.5); 0.1.8
  documents the narrowed crash-interruption guarantee.
- Tests expanded from 5 to 43 across config, state, seal, refresh, CLI and
  eval classes (0.1.6, 0.1.7, 0.1.8).
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
  both originals if any write or replacement fails. Caught write/replace
  failures are rollback-attempted; an interrupted refresh (process or power
  crash between replacements) may leave a stale split marker recoverable by
  rerunning `refresh`. Identical contract/config paths are rejected.
- All expected filesystem failures (missing file, path is a directory,
  permission denied, invalid UTF-8, write failure, replacement failure, lock
  contention) now raise a clean `SaitalkError` with no raw traceback.
- Added rollback, concurrent-refresh, and same-path red controls to the test
  suite.

## 0.1.4 - 2026-08-07

- `artifact_language` now has executable semantics:
  `resolve_artifact_language()` implements exact precedence (task-required
  language > existing artifact language > repository-local artifact contract >
  configured `artifact_language` > English fallback). The explicit
  task-required language always wins, in `auto` too; see 0.1.9 for the
  correction of the earlier `auto` cascade wording.
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
