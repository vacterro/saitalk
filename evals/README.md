# SAITALK Evals

Behavioral evals for SAITALK. They test whether a host/model actually obeys
the contract; they never define the contract. `SAITALK.md` is the single
normative behavior source.

## Layout

- `cases.json` — the case corpus. Schema version `1`.
- `harness.py` — schema validation, deterministic export, and machine-readable
  results. Zero dependencies.
- `results.json` — recorded results (generated, gitignored).

## Case shape

Every case requires exactly these keys, and no others:

- `id`: unique, lowercase, `[a-z][a-z0-9-]{1,63}`;
- `setup`: configuration or context the runner must load;
- `prompt`: what the model is told;
- `must`: list of behaviors the model MUST exhibit;
- `must_not`: list of behaviors the model MUST NOT exhibit.

`must`/`must_not` entries are compared after collapsing whitespace and
casefolding. Duplicate entries inside one list fail validation, and an
exact overlap between `must` and `must_not` fails validation. `version` must
be `1`.

## Run the harness

Validate the corpus:

```powershell
py -m evals.harness validate
```

Export cases deterministically (stable JSON for a host/model runner):

```powershell
py -m evals.harness export --out evals/export.json
```

Initialize a NOT_RUN result file:

```powershell
py -m evals.harness init-results --out evals/results.json
```

Without `--force` this is non-destructive at the COMMIT, not merely at a
pre-check: the install refuses an existing destination outright, so a results
file created by another writer while `init-results` was running is preserved
rather than replaced. `--force` is the only path that may overwrite recorded
evidence.

Validate an existing results file against the corpus and the active local
contract (`--contract`/`--config`/`--skill` point it at a different package):

```powershell
py -m evals.harness validate-results --results evals/results.json
```

Judge behavioral conformance (a stricter, separate question — see
"Schema validity vs behavioral conformance" below):

```powershell
py -m evals.harness conformance --results evals/results.json
```

## Result format

`results.json` maps each case ID to exactly one record, and every corpus case
ID must appear exactly once:

```json
{
  "state": "PASS",
  "reason": "optional; required for SKIP",
  "execution_provenance": {
    "run_id": "2026-09-04T09-15-00Z-local",
    "host": "opencode 1.18.23",
    "model": "sairoute/SAIFREN",
    "runner": "evals/run_local.py",
    "response_digest": "3f786850e387550fdab836ed7e6dc881de23001b00000000000000000000abcd"
  }
}
```

- `PASS` — a model/host run actually satisfied the case;
- `FAIL` — a model/host run actually violated the case;
- `SKIP` — deliberately not run, with a recorded `reason`;
- `NOT_RUN` — not yet run.

`execution_provenance` is optional for `NOT_RUN` and required by the
`conformance` gate for every other state. When present it must contain
exactly these five keys, each a non-empty string, and nothing else:

| Key | Meaning |
|---|---|
| `run_id` | identifier for the eval run that produced this record |
| `host` | the host/tool that executed the prompt |
| `model` | the model that answered |
| `runner` | the script or harness that drove the run |
| `response_digest` | SHA-256 of the recorded model response, as exactly 64 lowercase hexadecimal characters |

The format is syntax/integrity metadata only: it proves the field has the
documented digest shape, not that any particular runner produced the
response.

The file also records the `contract_id` it was produced under and the eval
corpus `version`, case count, and deterministic `digest`. A results file
bound to a different corpus digest fails `validate-results` outright.
Recorded states (`PASS`, `FAIL`, `SKIP`) require a bound `contract_id` in the
canonical `saitalk-<16 hex>` format; `null`, empty, or malformed values are
rejected, whether the file was produced by `write_results` or hand-edited.

A results file bound to a well-formed but non-current `contract_id` (an old
or foreign package) still passes schema validation — format alone cannot
prove currency — but `validate-results` never reports that as bare `VALID`.
It defaults to recomputing the `contract_id` of the active local package
(`--contract`/`--config`/`--skill` override the default paths) and reports
`EVALS RESULTS CURRENT` only when the bound ID matches; otherwise it reports
`VALID (historical, NOT current)` with both IDs shown, so `PASS` from an old
or foreign contract can never masquerade as current evidence.

A JSON file containing English wishes is not a passing eval. Behavioral
conformance is claimed only when an actual model/host run produced recorded
results. Cases without a run stay `NOT_RUN`, never `PASS`.

## Schema validity vs behavioral conformance

`validate-results` and `conformance` answer different questions, and a file
can pass the first while failing the second.

`validate-results` is about SHAPE and BINDING: the document parses, every
corpus case appears exactly once, states and reasons are well-formed, any
`execution_provenance` present is complete, and the file is bound to this
corpus digest and a canonical `contract_id`. It reports whether that binding
is current, and it is satisfied by an all-`NOT_RUN` file.

`conformance` is about EVIDENCE, and it exits nonzero unless all of these
hold:

- the bound `contract_id` equals the active package's;
- no case is `FAIL` or `NOT_RUN`;
- every `PASS` and `SKIP` record carries complete `execution_provenance`;
- **at least one case actually `PASS`ed.**

That last rule is why an all-`SKIP` corpus is `NOT CONFORMANT`. `SKIP` means
"deliberately not run", so a corpus in which nothing ran proves nothing about
behavior no matter how well-formed its provenance is. Mixed `PASS` + `SKIP` is
valid: `SKIP` is for individual cases that genuinely do not apply to the run,
not for the corpus as a whole.

## Coverage map

Each normative SAITALK area is covered mechanically (unit/static test), by a
behavioral eval, or both. Model-dependent behavior is covered only by
behavioral evals; it cannot be proven by a static validator.

| Normative area | Mechanical/static coverage | Behavioral eval |
|---|---|---|
| §1 language, auto fallback | config parse; §1 wording guard | `fixed-language-*`, `auto-language-selection`, `auto-language-fallback`, `quoted-foreign-text-not-language` |
| §2 voice, no-emoji, budget | config budget tests; §11 membership guard | `required-caveman-ded`, `no-emoji` |
| §3 authority | §3 wording guard | `completion-first`, `evidence-gate` |
| §4 hard bans | — (model-dependent) | `hard-ban-preamble` |
| §5 completion-first | — | `completion-first`, `preference-not-blocking`, `destructive-precheck` |
| §6 evidence gate | — | `evidence-gate`, `zero-finding-review` |
| §7 review discipline | — | `zero-finding-review` |
| §8 exactness, high-stakes | — | `high-stakes-plain-prose`, `high-stakes-plain-prose-preserves-suspended-voice` |
| §9 artifact language | `resolve_artifact_language` unit tests | `artifact-language-independent`, `artifact-explicit-task-language`, `artifact-auto-task-wins` |
| §10 persistence | — | `suspended-voice-surviving-handoff` |
| §11 state machine, voice commands | `validate_state`, `voice_command`, fenced-example tests | `standalone-suspension`, `quoted-normal-mode-not-suspending`, `explicit-resume`, `suspended-voice-keeps-reply-language` |
| §12 config reference | `parse_conf`; structural conformance guards | — |
| Markers/freshness | seal, refresh, structure tests | — |

Explicitly not mechanically testable: whether a model actually obeys hard
bans, no-emoji, persona suppression, evidence classification, or destructive
prechecks. Those are covered by behavioral evals only, and stay `NOT_RUN`
until a real model/host run records a result.
