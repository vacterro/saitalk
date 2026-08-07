"""SAITALK eval harness: schema validation, deterministic export, results."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

EVAL_STATES = ("PASS", "FAIL", "SKIP", "NOT_RUN")
REQUIRED_FIELDS = ("setup", "prompt", "must", "must_not")


class EvalError(ValueError):
    pass


def load_cases(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvalError(f"missing eval file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvalError(f"invalid JSON in {path}: {exc}") from exc
    validate_cases(data)
    return data


def validate_cases(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise EvalError("eval file must be a JSON object")
    if data.get("version") != 1:
        raise EvalError(f"eval version must be 1, found {data.get('version')!r}")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise EvalError("eval file must contain a cases array")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise EvalError(f"case {index} is not an object")
        cid = case.get("id")
        if not isinstance(cid, str) or not cid:
            raise EvalError(f"case {index} has no id")
        if cid in seen:
            raise EvalError(f"duplicate case id: {cid}")
        seen.add(cid)
        for field in REQUIRED_FIELDS:
            value = case.get(field)
            if field in ("setup", "prompt") and not isinstance(value, str):
                raise EvalError(f"case {cid}: {field} must be a non-empty string")
            if field in ("must", "must_not"):
                if not isinstance(value, list) or not value or not all(
                    isinstance(item, str) and item for item in value
                ):
                    raise EvalError(
                        f"case {cid}: {field} must be a non-empty list of strings"
                    )


def export_cases(data: dict[str, Any]) -> str:
    """Deterministic JSON serialization for a host/model runner."""
    return json.dumps(data, ensure_ascii=True, sort_keys=True, indent=2) + "\n"


def new_results(cases: dict[str, Any]) -> dict[str, str]:
    return {case["id"]: "NOT_RUN" for case in cases["cases"]}


def write_results(results: dict[str, str], path: Path) -> None:
    for cid, state in results.items():
        if state not in EVAL_STATES:
            raise EvalError(
                f"case {cid}: invalid result state {state!r}; "
                f"allowed: {', '.join(EVAL_STATES)}"
            )
    payload = {
        "version": 1,
        "schema": "saitalk-eval-results",
        "results": dict(sorted(results.items())),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    import argparse

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

    args = parser.parse_args()

    cases_path = args.cases or (Path(__file__).resolve().parents[1] / "evals" / "cases.json")

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
            write_results(results, out)
            print(f"EVALS RESULTS: {out} ({len(results)} cases NOT_RUN)")
            return 0
    except (EvalError, OSError) as exc:
        print(f"EVALS FAIL: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
