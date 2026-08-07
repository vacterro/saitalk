"""SAITALK eval harness: schema validation, deterministic export, results."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

EVAL_STATES = ("PASS", "FAIL", "SKIP", "NOT_RUN")
REQUIRED_FIELDS = ("setup", "prompt", "must", "must_not")
CASE_KEYS = frozenset({"id", "setup", "prompt", "must", "must_not"})
CASE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
RESULT_KEYS = frozenset({"state", "reason"})
CORPUS_ROOT_KEYS = frozenset({"version", "cases"})
RESULTS_ROOT_KEYS = frozenset({"version", "schema", "contract_id", "corpus", "results"})
RESULTS_CORPUS_KEYS = frozenset({"version", "cases", "digest"})
CONTRACT_ID_RE = re.compile(r"^saitalk-[0-9a-f]{16}$")


class EvalError(ValueError):
    pass


def validate_contract_id(value: Any, *, context: str) -> str:
    """The one canonical contract_id format validator: saitalk-<16 lowercase hex>."""
    if not isinstance(value, str) or not CONTRACT_ID_RE.fullmatch(value):
        raise EvalError(
            f"{context}: contract_id must match saitalk-[0-9a-f]{{16}}, found {value!r}"
        )
    return value


def load_cases(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvalError(f"missing eval file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise EvalError(f"not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvalError(f"invalid JSON in {path}: {exc}") from exc
    validate_cases(data)
    return data


def _norm(item: str) -> str:
    return " ".join(item.casefold().split())


def validate_cases(data: Any) -> None:
    if not isinstance(data, dict):
        raise EvalError("eval file must be a JSON object")
    unknown_root = set(data) - CORPUS_ROOT_KEYS
    if unknown_root:
        raise EvalError(f"eval file: unknown keys: {', '.join(sorted(unknown_root))}")
    if data.get("version") != 1:
        raise EvalError(f"eval version must be 1, found {data.get('version')!r}")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise EvalError("eval file must contain a cases array")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise EvalError(f"case {index} is not an object")
        unknown = set(case) - CASE_KEYS
        if unknown:
            raise EvalError(
                f"case {index}: unknown keys: {', '.join(sorted(unknown))}"
            )
        cid = case.get("id")
        if not isinstance(cid, str) or not CASE_ID_RE.fullmatch(cid):
            raise EvalError(
                f"case {index}: id must match [a-z][a-z0-9-]{{1,63}}"
            )
        if cid in seen:
            raise EvalError(f"duplicate case id: {cid}")
        seen.add(cid)
        for field in REQUIRED_FIELDS:
            value = case.get(field)
            if field in ("setup", "prompt"):
                if not isinstance(value, str) or not value.strip():
                    raise EvalError(f"case {cid}: {field} must be a non-empty string")
            else:
                if (
                    not isinstance(value, list)
                    or not value
                    or not all(isinstance(item, str) and item.strip() for item in value)
                ):
                    raise EvalError(
                        f"case {cid}: {field} must be a non-empty list of non-empty strings"
                    )
                normalized = [_norm(item) for item in value]
                if len(set(normalized)) != len(normalized):
                    raise EvalError(
                        f"case {cid}: {field} contains duplicate entries"
                    )
        overlap = {_norm(item) for item in case["must"]} & {
            _norm(item) for item in case["must_not"]
        }
        if overlap:
            raise EvalError(
                f"case {cid}: must and must_not overlap on: "
                + ", ".join(sorted(overlap))
            )


def export_cases(data: dict[str, Any]) -> str:
    """Deterministic JSON serialization for a host/model runner."""
    return json.dumps(data, ensure_ascii=True, sort_keys=True, indent=2) + "\n"


def corpus_digest(data: dict[str, Any]) -> str:
    return hashlib.sha256(export_cases(data).encode("utf-8")).hexdigest()


def new_results(cases: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {case["id"]: {"state": "NOT_RUN"} for case in cases["cases"]}


def validate_results(results: Any, cases: dict[str, Any]) -> None:
    validate_cases(cases)
    if not isinstance(results, dict):
        raise EvalError("results must be a JSON object")
    expected = {case["id"] for case in cases["cases"]}
    actual = set(results)
    unknown = actual - expected
    missing = expected - actual
    if unknown:
        raise EvalError(
            "unknown case ids in results: " + ", ".join(sorted(unknown))
        )
    if missing:
        raise EvalError(
            "missing case ids in results: " + ", ".join(sorted(missing))
        )
    for cid, record in results.items():
        if not isinstance(record, dict):
            raise EvalError(f"case {cid}: result must be an object")
        unknown_keys = set(record) - RESULT_KEYS
        if unknown_keys:
            raise EvalError(
                f"case {cid}: unknown result keys: {', '.join(sorted(unknown_keys))}"
            )
        state = record.get("state")
        if state not in EVAL_STATES:
            raise EvalError(
                f"case {cid}: invalid result state {state!r}; "
                f"allowed: {', '.join(EVAL_STATES)}"
            )
        reason = record.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise EvalError(f"case {cid}: reason must be a string")
        if state == "SKIP" and not (isinstance(reason, str) and reason.strip()):
            raise EvalError(f"case {cid}: SKIP requires a recorded reason")


def write_results(
    results: dict[str, Any],
    path: Path,
    cases: dict[str, Any],
    contract_id: str | None = None,
) -> None:
    validate_results(results, cases)
    has_recorded = any(record["state"] != "NOT_RUN" for record in results.values())
    if contract_id is not None:
        validate_contract_id(contract_id, context="write_results")
    elif has_recorded:
        raise EvalError(
            "recorded results (states other than NOT_RUN) must bind a contract_id"
        )
    payload = {
        "version": 1,
        "schema": "saitalk-eval-results",
        "contract_id": contract_id,
        "corpus": {
            "version": cases["version"],
            "cases": len(cases["cases"]),
            "digest": corpus_digest(cases),
        },
        "results": dict(sorted(results.items())),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def load_results(path: Path, cases: dict[str, Any]) -> dict[str, Any]:
    """Load and fully validate a results file: schema, corpus binding, and
    contract_id provenance (format, and presence whenever any state is
    recorded) -- the same invariant write_results enforces on the way out.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvalError(f"missing results file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise EvalError(f"not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvalError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EvalError(f"{path}: results document must be a JSON object")
    unknown_root = set(data) - RESULTS_ROOT_KEYS
    if unknown_root:
        raise EvalError(f"{path}: unknown keys: {', '.join(sorted(unknown_root))}")
    if data.get("version") != 1 or data.get("schema") != "saitalk-eval-results":
        raise EvalError(f"{path}: not a saitalk eval results file")
    corpus = data.get("corpus")
    if not isinstance(corpus, dict):
        raise EvalError(f"{path}: corpus must be a JSON object")
    unknown_corpus = set(corpus) - RESULTS_CORPUS_KEYS
    if unknown_corpus:
        raise EvalError(f"{path}: corpus: unknown keys: {', '.join(sorted(unknown_corpus))}")
    if corpus.get("digest") != corpus_digest(cases):
        raise EvalError(
            f"{path}: results bound to a different eval corpus digest; "
            "re-run the eval or refresh the results"
        )
    if corpus.get("version") != cases.get("version") or corpus.get("cases") != len(cases.get("cases", [])):
        raise EvalError(f"{path}: results bound to a different eval corpus version/count")
    results = data.get("results")
    if not isinstance(results, dict):
        raise EvalError(f"{path}: results field must be a JSON object")
    validate_results(results, cases)
    contract_id = data.get("contract_id")
    has_recorded = any(record["state"] != "NOT_RUN" for record in results.values())
    if contract_id is not None:
        validate_contract_id(contract_id, context=str(path))
    elif has_recorded:
        raise EvalError(f"{path}: recorded results must bind a contract_id")
    return data


def _resolve_saitalk_paths(args: argparse.Namespace):
    """Default to the active local SAITALK package; --contract/--config/--skill override."""
    from scripts import saitalk as _saitalk

    default_contract, default_conf, default_skill = _saitalk.root_paths()
    return (
        getattr(args, "contract", None) or default_contract,
        getattr(args, "config", None) or default_conf,
        getattr(args, "skill", None) or default_skill,
    )


def current_contract_id(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Return (contract_id, error) for the active local SAITALK package.

    Never raises: a package that fails to validate just means "current"
    cannot be established, which the caller reports as such rather than
    crashing the results check.
    """
    try:
        from scripts import saitalk as _saitalk

        contract_path, conf_path, skill_path = _resolve_saitalk_paths(args)
        contract_id, _ = _saitalk.validate(contract_path, conf_path, skill_path)
        return contract_id, None
    except Exception as exc:  # noqa: BLE001 - reported, never crashes the CLI
        return None, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="SAITALK eval harness.")
    sub = parser.add_subparsers(dest="command", required=True)

    validate_parser = sub.add_parser("validate", help="Validate eval schema.")
    validate_parser.add_argument("--cases", type=Path, default=None)

    export_parser = sub.add_parser("export", help="Export cases deterministically.")
    export_parser.add_argument("--cases", type=Path, default=None)
    export_parser.add_argument("--out", type=Path, default=None)

    init_parser = sub.add_parser("init-results", help="Write a NOT_RUN result file.")
    init_parser.add_argument("--cases", type=Path, default=None)
    init_parser.add_argument("--out", type=Path, default=None)

    results_parser = sub.add_parser(
        "validate-results",
        help="Validate a results file against the corpus and the active contract.",
    )
    results_parser.add_argument("--results", type=Path, default=None)
    results_parser.add_argument("--cases", type=Path, default=None)
    results_parser.add_argument("--contract", type=Path, default=None)
    results_parser.add_argument("--config", type=Path, default=None)
    results_parser.add_argument("--skill", type=Path, default=None)

    args = parser.parse_args()

    cases_path = args.cases or (
        Path(__file__).resolve().parents[1] / "evals" / "cases.json"
    )

    try:
        if args.command == "validate":
            data = load_cases(cases_path)
            print(f"EVALS VALID: {len(data['cases'])} cases, ids unique, schema OK")
            return 0
        if args.command == "export":
            data = load_cases(cases_path)
            text = export_cases(data)
            if args.out:
                args.out.write_text(text, encoding="utf-8")
                print(f"EVALS EXPORT: {args.out}")
            else:
                sys.stdout.write(text)
            return 0
        if args.command == "init-results":
            data = load_cases(cases_path)
            results = new_results(data)
            out = args.out or cases_path.with_name("results.json")
            write_results(results, out, data)
            print(f"EVALS RESULTS: {out} ({len(results)} cases NOT_RUN)")
            return 0
        if args.command == "validate-results":
            data = load_cases(cases_path)
            results_path = args.results or cases_path.with_name("results.json")
            loaded = load_results(results_path, data)
            bound_id = loaded.get("contract_id")
            if bound_id is None:
                print(
                    f"EVALS RESULTS VALID (no contract_id bound; only NOT_RUN entries): "
                    f"{results_path}"
                )
                return 0
            active_id, active_error = current_contract_id(args)
            if active_id is not None and bound_id == active_id:
                print(
                    f"EVALS RESULTS CURRENT: {results_path} bound to the active "
                    f"validated contract_id={bound_id}"
                )
            elif active_id is not None:
                print(
                    f"EVALS RESULTS VALID (historical, NOT current): {results_path} "
                    f"bound to contract_id={bound_id!r}, active package is {active_id!r}"
                )
            else:
                print(
                    f"EVALS RESULTS VALID (schema-valid only; active package could not "
                    f"be validated: {active_error}): {results_path} bound to "
                    f"contract_id={bound_id!r}"
                )
            return 0
    except (EvalError, OSError) as exc:
        print(f"EVALS FAIL: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
