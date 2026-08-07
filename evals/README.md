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

Every case requires exactly:

- `setup`: configuration or context the runner must load;
- `prompt`: what the model is told;
- `must`: list of behaviors the model MUST exhibit;
- `must_not`: list of behaviors the model MUST NOT exhibit.

Case IDs are unique. `version` must be `1`.

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

## Result format

`results.json` maps each case ID to exactly one state:

- `PASS` — a model/host run actually satisfied the case;
- `FAIL` — a model/host run actually violated the case;
- `SKIP` — deliberately not run, with a recorded reason;
- `NOT_RUN` — not yet run.

A JSON file containing English wishes is not a passing eval. Behavioral
conformance is claimed only when an actual model/host run produced recorded
results. Cases without a run stay `NOT_RUN`, never `PASS`.
