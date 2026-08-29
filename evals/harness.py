"""SAITALK eval harness: schema validation, deterministic export, results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl

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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvalError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise EvalError(f"missing JSON file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise EvalError(f"not UTF-8: {path}") from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except EvalError as exc:
        raise EvalError(f"invalid JSON in {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvalError(f"invalid JSON in {path}: {exc}") from exc


@contextmanager
def _destination_lock(path: Path):
    """CORE-002 / PERF-002 / W2-003: canonicalized lock identity,
    non-blocking acquisition, and safe teardown.
    """
    # CORE-002: resolve() gives the canonical filesystem identity so two
    # symlinks pointing at the same file share one lock.
    canonical = path.resolve(strict=False)
    lock_path = Path(tempfile.gettempdir()) / (
        "saitalk-evals-" + hashlib.sha256(str(canonical).encode()).hexdigest()[:16] + ".lock"
    )
    fh = None
    try:
        if os.name == "nt" and (not lock_path.exists() or lock_path.stat().st_size < 1):
            lock_path.write_bytes(b"\0")
        fh = lock_path.open("r+b" if os.name == "nt" else "a+b")
        # PERF-002: non-blocking acquisition so contention is surfaced as
        # an EvalError instead of an indefinite stall.
        if os.name == "nt":
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if fh is not None:
            fh.close()
        raise EvalError(f"cannot lock results destination {path}: {exc}") from exc
    try:
        yield
    finally:
        # W2-003: suppress cleanup errors so they never mask a primary
        # exception from the guarded block.
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass


def validate_contract_id(value: Any, *, context: str) -> str:
    """The one canonical contract_id format validator: saitalk-<16 lowercase hex>."""
    if not isinstance(value, str) or not CONTRACT_ID_RE.fullmatch(value):
        raise EvalError(
            f"{context}: contract_id must match saitalk-[0-9a-f]{{16}}, found {value!r}"
        )
    return value


def load_cases(path: Path) -> dict[str, Any]:
    data = _load_json(path)
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
            if field in ("setup", "prompt") and (
                not isinstance(value, str) or not value.strip()
            ):
                raise EvalError(f"case {cid}: {field} must be a non-empty string")
        # PERF-002: normalize each must / must_not item exactly once into a
        # retained set; detect duplicates while building and compute the
        # overlap directly from the two sets (no throwaway second pass).
        must_set: set[str] = set()
        must_not_set: set[str] = set()
        for field, target in (("must", must_set), ("must_not", must_not_set)):
            value = case.get(field)
            if (
                not isinstance(value, list)
                or not value
                or not all(isinstance(item, str) and item.strip() for item in value)
            ):
                raise EvalError(
                    f"case {cid}: {field} must be a non-empty list of non-empty strings"
                )
            for item in value:
                norm = _norm(item)
                if norm in target:
                    raise EvalError(f"case {cid}: {field} contains duplicate entries")
                target.add(norm)
        overlap = must_set & must_not_set
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


def _write_results_locked(
    results: dict[str, Any],
    path: Path,
    cases: dict[str, Any],
    contract_id: str | None = None,
) -> None:
    payload = {
        "version": 1,
        "schema": "saitalk-eval-results",
        "contract_id": contract_id,
        "corpus": {
            "version": cases["version"],
            "cases": len(cases["cases"]),
            "digest": corpus_digest(cases),
        },
        "results": results,
    }
    if path.is_symlink():
        raise EvalError(f"write_results refuses to replace symlink destination: {path}")
    existing_mode = None
    if os.name != "nt" and path.is_file():
        try:
            existing_mode = os.stat(path).st_mode & 0o7777
        except OSError as exc:
            raise EvalError(f"cannot read destination mode: {path}: {exc}") from exc
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    tmp = Path(tmp_path)
    fd_owned = True
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8")
        fd_owned = False
        with fh:
            json.dump(payload, fh, ensure_ascii=True, sort_keys=True, indent=2)
            fh.write("\n")
            fh.flush()
            if existing_mode is not None and os.name != "nt":
                os.fchmod(fh.fileno(), existing_mode)
        tmp.replace(path)
    except BaseException:
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
    with _destination_lock(path):
        _write_results_locked(results, path, cases, contract_id)


def load_results(path: Path, cases: dict[str, Any]) -> dict[str, Any]:
    """Load and fully validate a results file and its provenance."""
    try:
        data = _load_json(path)
    except EvalError as exc:
        if str(exc).startswith("missing JSON file:"):
            raise EvalError(str(exc).replace("missing JSON file:", "missing results file:", 1)) from exc
        raise
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


def current_contract_id(args: argparse.Namespace) -> str:
    """Return the active local SAITALK package's contract_id.

    W2-004: any failure to establish or validate the active package is
    raised, not swallowed, so callers (validate-results) can report a nonzero
    status instead of a misleading VALID.  Expected validation failures
    (``SaitalkError``) and unexpected errors both propagate.
    """
    from scripts import saitalk as _saitalk

    contract_path, conf_path, skill_path = _resolve_saitalk_paths(args)
    contract_id, _ = _saitalk.validate_package(contract_path, conf_path, skill_path)
    return contract_id


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
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing results file even if it holds recorded evidence.",
    )

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
            with _destination_lock(out):
                if out.exists() and not getattr(args, "force", False):
                    try:
                        existing = load_results(out, data)
                    except (EvalError, OSError, json.JSONDecodeError):
                        existing = None
                    if (
                        existing is not None
                        and existing.get("version") == 1
                        and existing.get("schema") == "saitalk-eval-results"
                    ):
                        recorded = any(
                            r["state"] != "NOT_RUN" for r in existing["results"].values()
                        )
                        if (
                            not recorded
                            and existing["results"] == results
                            and existing.get("corpus", {}).get("digest") == corpus_digest(data)
                        ):
                            print(f"EVALS RESULTS: {out} (already initialized, no-op)")
                            return 0
                    raise EvalError(
                        f"init-results: refusing to overwrite existing results file "
                        f"{out}; use --force to reset recorded evidence"
                    )
                _write_results_locked(results, out, data)
            print(f"EVALS RESULTS: {out} ({len(results)} cases NOT_RUN)")
            return 0
        if args.command == "validate-results":
            data = load_cases(cases_path)
            results_path = args.results or cases_path.with_name("results.json")
            loaded = load_results(results_path, data)
            bound_id = loaded.get("contract_id")
            try:
                active_id = current_contract_id(args)
            except Exception as exc:  # noqa: BLE001 - reported, never VALID
                print(
                    f"EVALS FAIL: active package could not be validated: {exc}",
                    file=sys.stderr,
                )
                return 1
            if bound_id is None:
                print(
                    f"EVALS RESULTS VALID (no contract_id bound; only NOT_RUN entries): "
                    f"{results_path}"
                )
                return 0
            if bound_id == active_id:
                print(
                    f"EVALS RESULTS CURRENT: {results_path} bound to the active "
                    f"validated contract_id={bound_id}"
                )
            else:
                print(
                    f"EVALS RESULTS VALID (historical, NOT current): {results_path} "
                    f"bound to contract_id={bound_id!r}, active package is {active_id!r}"
                )
            return 0
    except (EvalError, OSError) as exc:
        print(f"EVALS FAIL: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
