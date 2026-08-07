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

Validate an existing results file against the corpus and the active local
contract (`--contract`/`--config`/`--skill` point it at a different package):

```powershell
py -m evals.harness validate-results --results evals/results.json
```

## Result format

`results.json` maps each case ID to exactly one record, and every corpus case
ID must appear exactly once:

```json
{
  "state": "PASS",
  "reason": "optional; required for SKIP"
}
```

- `PASS` — a model/host run actually satisfied the case;
- `FAIL` — a model/host run actually violated the case;
- `SKIP` — deliberately not run, with a recorded `reason`;
- `NOT_RUN` — not yet run.

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
