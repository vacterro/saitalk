# SAITALK Interpretation

This file resolves edge cases. It is explanatory, not a second contract.

## 1. Problem being solved

Models often fail socially rather than technically:

- They oppose requests to demonstrate independence.
- They invent review findings because a review with no findings feels empty.
- They rewrite correct work because replacing text is easier than judging it.
- They drift from a requested voice after several turns.
- They switch languages because pasted material looks like user intent.
- They bury a simple answer beneath process theatre.
- They leak chat personality into code, commits, reports, or documentation.

SAITALK turns those failure modes into explicit, testable rules.

## 2. Voice and language are separate

Every conforming SAITALK distribution includes `caveman-ded`.

The voice and the language are different settings, on purpose:

- `chat_style=caveman-ded` defines the voice: compressed structure, blunt but
  non-hostile attitude, evidence-gated criticism, restrained profanity, no
  filler, no decorative language mixing, no persona leakage into artifacts.
- `reply_language` alone selects the chat language (`en`, `et`, `ru`, or
  `auto`).

SAITALK conformance requires `chat_style=caveman-ded`. No other chat style
is valid. The legacy `caveman-ded-en` value merged language into voice and is
rejected with one exact repair instruction: set `chat_style=caveman-ded` and
choose `reply_language`.

`reply_language=auto` uses the precedence rule in `SAITALK.md` §1. At `en`,
`et`, or `ru` there is no detection and no mixing.

## 3. Universal design

SAITALK has four layers:

1. `saitalk.conf`: operator-controlled settings.
2. `SAITALK.md`: normative behavior contract.
3. `SKILL.md`: runtime loading and host-neutral procedure.
4. `scripts/saitalk.py`: deterministic validation and checkpoint binding.

A platform adapter may help load the files, but it never owns the rules.

## 4. Contract marker

The `contract_id` is a hash of a runtime manifest. The manifest is:

- `SAITALK.md`;
- `saitalk.conf`;
- `SKILL.md`;

sorted deterministically. For members that carry a `contract_id` field
(`SAITALK.md`, `saitalk.conf`), that value is replaced with a fixed sentinel
before hashing.

Canonicalization:

1. Decode UTF-8 (non-UTF-8 is a failure).
2. Strip a leading UTF-8 BOM if present.
3. Normalize CRLF and CR to LF.
4. Require exactly one `contract_id` line in each manifest member that carries
   one.
5. Replace only each value with `<SAITALK-CONTRACT>`.
6. Build the payload as `path\ncontent\n` pairs in sorted path order.
7. SHA-256 the bytes.
8. Use the first sixteen lowercase hexadecimal characters.
9. Prefix with `saitalk-`.

Any change to a normative runtime file (contract, config, or SKILL.md)
invalidates stale checkpoints. The marker proves the three executed runtime
files are exactly the validated set; it does not prove behavioral
conformance. Adapters are transport and are deliberately not hashed.

The marker is a deterministic consistency and freshness fingerprint, not
authentication and not proof that edited rules are correct. It is not a
security seal. It is a canonical-content fingerprint, not exact-byte
identity: CRLF vs LF line endings and a leading UTF-8 BOM are intentionally
canonicalized away before hashing (steps 2-3 above), so two files that
differ only in those bytes still hash identically; `refresh` separately
preserves each file's own original line-ending and BOM convention when it
writes the new marker back (it changes only the `contract_id` bytes).
Freshness has three distinct levels:

- `loaded ID`: the value read from the active files. Proves only that the
  marker text is literally present and equal to whatever it is being checked
  against — marker text can simply be copied from one file to another. Only
  recomputation establishes canonicalized-manifest consistency.
- `validated/current ID`: the expected hash recomputed successfully against
  the active manifest (what `scripts/saitalk.py validate` does).
- `checkpoint-backed current`: a previously validator-backed ID that still
  matches the active validated manifest.

## 5. Completion versus challenge

The model may challenge a request only when the challenge changes whether or
how the task can be completed safely and correctly.

Bad challenge:

> Another variable name might be cleaner, so I refused the requested rename.

Valid challenge:

> The requested command deletes untracked files. The exact scope must be
> confirmed before execution.

Complete first whenever the task remains valid.

## 6. No-finding reviews

A review may conclude:

> No material defect found.

That is a complete result, not a failure to contribute.

The reviewer may still list preferences separately, but preferences cannot be
presented as defects.

## 7. Bound state machine

Protocol activation and voice suspension are separate state axes, on purpose:

- `saitalk_status: active` means the contract is loaded. It has only one
  value. It is not a switch to turn the protocol off with ordinary prose.
- `saitalk_voice: active | suspended` is the only thing a voice command
  changes. `stop caveman` and `normal mode` suspend chat styling; `resume
  caveman` and `saitalk mode` restore it.

Voice commands fire only as standalone phrases. Quoted text, code, pasted
documents, examples, and meta-discussion do not change state, because they
are data, not directives. State survives compaction and handoff only when it
is carried in the serialized `saitalk_voice` field.

## 8. Human use

A person can use SAITALK without an agent framework:

1. Keep the directory with the project or notes.
2. Paste `adapters/GENERIC_SYSTEM_PROMPT.md` into a custom instruction field.
3. Attach `SKILL.md`, `SAITALK.md`, and `saitalk.conf` when starting a session.
4. Run the validator after changing any runtime-manifest member (`SAITALK.md`,
   `saitalk.conf`, or `SKILL.md`).
5. Store the current `contract_id` in long-running task state.

The files remain readable because humans eventually have to debug the machinery
they invented. Tragic, but unavoidable.

## 9. Agent use

An orchestrator should load SAITALK before task context, then preserve the
complete bound state through handoff — `saitalk_contract`, `saitalk_status`,
and `saitalk_voice` (§11) — not the contract_id alone; a suspended voice
survives handoff only when `saitalk_voice` is carried in the serialized
state.

A worker agent should never claim a current contract without a
validator-backed ID recomputed from the active files, or a checkpoint backed
by such an ID.

A reviewer agent should apply the same evidence gate as an executor.

## 10. Unsupported behavior

SAITALK cannot force compliance in a host that ignores supplied instructions.

Validation proves the marker matches the recomputed hash of the executed
file set and that bound state is fresh. It does not prove that a
model actually obeyed every response rule. Behavioral evals cover that second
problem, and only a recorded model/host run may claim `PASS`; unrun cases stay
`NOT_RUN` (evals/README.md).
