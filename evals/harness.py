"""SAITALK eval harness: schema validation, deterministic export, results."""

from __future__ import annotations

import argparse
import copy
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
RESULT_KEYS = frozenset({"state", "reason", "execution_provenance"})
EXECUTION_PROVENANCE_KEYS = frozenset(
    {"run_id", "host", "model", "runner", "response_digest"}
)
CORPUS_ROOT_KEYS = frozenset({"version", "cases"})
RESULTS_ROOT_KEYS = frozenset({"version", "schema", "contract_id", "corpus", "results"})
# PERF-003 (SRC-002:R010): detailed conformance-failure diagnostics are
# bounded to this many case-specific reasons; everything past it is one
# deterministic summary line plus exact counts. The shipped 25-case corpus
# sits far below the cap, so its diagnostics are unchanged.
REASON_DETAIL_CAP = 75
RESULTS_CORPUS_KEYS = frozenset({"version", "cases", "digest"})
CONTRACT_ID_RE = re.compile(r"^saitalk-[0-9a-f]{16}$")
# SRC-003:R006: response_digest is documented as a digest of the recorded
# model response; the documented example is 64 hex characters. This is the ONE
# canonical syntax validator -- write_results, load_results and
# evaluate_conformance all reach it through validate_results.
RESPONSE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class EvalError(ValueError):
    pass


class PostCommitError(EvalError):
    """The destination was already installed before the failure arrived.

    SRC-003:R005: a KeyboardInterrupt/SystemExit (or any non-EvalError)
    raised after ``os.link``/``os.replace`` committed the destination must
    not masquerade as a pre-commit cancellation -- a caller could otherwise
    retry and either collide with the existing file (no-clobber) or blindly
    overwrite the committed snapshot. This class says plainly: installed,
    already visible, do not retry as though nothing was committed. The
    original exception is chained as ``__cause__`` so cancellation identity
    is preserved.
    """



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
            # R016/W2-007: the primary failure is the LOCK failure. A
            # close() that also fails during acquisition teardown must not
            # replace it -- contention then gets diagnosed as whatever the
            # cleanup happened to raise.
            try:
                fh.close()
            except OSError:
                pass
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
    """R018/PERF-003: stream the canonical JSON straight into SHA-256.

    The digest must remain byte-for-byte compatible with the canonical
    representation produced by ``export_cases``: ``ensure_ascii=True``,
    ``sort_keys=True``, ``indent=2``, one final newline.

    ``json.dump`` into a ``StringIO`` and then ``getvalue().encode()``
    was the previous implementation, and it materialized the whole
    serialization twice -- once as ``str``, once as ``bytes`` -- despite
    the docstring claiming it did not. ``iterencode`` yields the same
    canonical byte stream in chunks, so peak memory is a chunk rather
    than a multiple of the corpus.
    """
    sink = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=True, sort_keys=True, indent=2)
    for chunk in encoder.iterencode(data):
        sink.update(chunk.encode("utf-8"))
    sink.update(b"\n")
    return sink.hexdigest()


def new_results(cases: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {case["id"]: {"state": "NOT_RUN"} for case in cases["cases"]}


def validate_results(
    results: Any, cases: dict[str, Any], *, cases_validated: bool = False
) -> None:
    """Validate a results mapping against *cases*.

    R019/PERF-004: ``cases_validated=True`` means the caller already ran
    :func:`validate_cases` on this exact corpus object, so the corpus walk
    is skipped here. Without the flag the corpus is still fully validated,
    because a caller that does not hold a validated snapshot must not get
    a weaker check. The flag is keyword-only and defaults to False, so the
    strict behaviour is what every existing caller keeps.
    """
    if not cases_validated:
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
        prov = record.get("execution_provenance")
        if prov is not None:
            if not isinstance(prov, dict):
                raise EvalError(
                    f"case {cid}: execution_provenance must be an object"
                )
            unknown_prov = set(prov) - EXECUTION_PROVENANCE_KEYS
            if unknown_prov:
                raise EvalError(
                    f"case {cid}: execution_provenance: unknown keys: "
                    f"{', '.join(sorted(unknown_prov))}"
                )
            for key in EXECUTION_PROVENANCE_KEYS:
                value = prov.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise EvalError(
                        f"case {cid}: execution_provenance.{key} must be a "
                        f"non-empty string"
                    )
            # SRC-003:R006 (W2-002): the field is named and documented as a
            # digest, so the validator must enforce its own field-level
            # invariant. SHA-256 hex is the documented algorithm; 64 lowercase
            # hexadecimal characters. Syntax/integrity metadata only -- this
            # never claims runner authenticity.
            digest = prov.get("response_digest")
            if not RESPONSE_DIGEST_RE.fullmatch(digest):
                raise EvalError(
                    f"case {cid}: execution_provenance.response_digest must be "
                    f"64 lowercase hexadecimal characters (sha256 hex), found "
                    f"{digest!r}"
                )


def _capture_destination_mode(path: Path) -> int | None:
    """Return *path*'s POSIX permission bits, or None when there are none.

    None means "no mode to preserve": the destination does not exist yet, or
    this platform has no POSIX mode bits to carry over.

    T-905: the platform check lives HERE rather than inline at the call site,
    so the whole preserve-mode decision is one named unit a test can drive on
    any host. Previously both halves sat behind `os.name != "nt"` inside
    `_write_results_locked`, which meant the only host that runs this suite
    had ZERO coverage of them and could not have noticed them drifting.
    """
    if os.name == "nt" or not path.is_file():
        return None
    try:
        return os.stat(path).st_mode & 0o7777
    except OSError as exc:
        raise EvalError(f"cannot read destination mode: {path}: {exc}") from exc


def _apply_destination_mode(fileno: int, mode: int | None) -> None:
    """Apply a captured destination *mode* to the staged file descriptor.

    Called after the content write and fsync but BEFORE the install, so the
    replacement carries the original file's permissions instead of mkstemp's
    private 0o600. A None mode is a no-op, which is also the Windows case.
    """
    if mode is None or os.name == "nt":
        return
    os.fchmod(fileno, mode)


def _fsync_parent_directory(path: Path) -> None:
    """Durably commit *path*'s directory entry after an atomic install.

    R015/W2-006: a FAILED directory fsync is not swallowed. This step is what
    makes the rename crash-durable, so reporting plain success after it failed
    reported a guarantee that was not delivered. The new bytes ARE already
    visible when this runs -- the install happened -- so the message says so
    explicitly and the caller must not retry assuming nothing was committed.

    CORE-003 (SRC-002:R003): the same holds when the parent directory cannot
    be OPENED on POSIX. The previous contract called that a capability limit,
    but the caller cannot tell "this filesystem never supports directory
    fsync" from "this one does and the attempt failed" -- both reached the
    caller as silent success, so a crash could invalidate the installed name
    after a reported success. An unopenable directory is now the same
    durability-uncertain error with the same already-visible semantics.

    Windows has no directory-fsync equivalent and never claimed one, so this
    is a no-op there. The platform check lives HERE, not at the call site, so
    the whole step is one testable unit on every host.
    """
    if os.name == "nt":
        return
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError as exc:
        raise EvalError(
            f"commit durability uncertain for {path}: the results were "
            f"installed and are already visible, but the directory fsync that "
            f"makes the rename crash-durable could not be attempted: {exc}"
        ) from exc
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        raise EvalError(
            f"commit durability uncertain for {path}: the results were "
            f"installed and are already visible, but the directory fsync that "
            f"makes the rename crash-durable failed: {exc}"
        ) from exc
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


def _commit_no_clobber(tmp: Path, path: Path) -> None:
    """Install *tmp* at *path*, FAILING if *path* already exists.

    R013/W2-004: ``os.replace`` overwrites unconditionally, so
    ``init-results`` -- documented as refusing to overwrite recorded
    evidence without ``--force`` -- could destroy a results file created
    after its own ``exists()`` check. The destination lock only serializes
    cooperating harness writers; it never makes the "did not exist"
    observation atomic with the commit.

    ``os.link`` (POSIX) and ``os.rename`` (Windows) both refuse an existing
    destination, so the filesystem itself performs the check-and-commit as
    one operation. The staged temp file is removed by the caller's cleanup
    on failure.

    W2-003 (SRC-002:R006): a successful ``os.link`` is the point the
    destination becomes installed and visible, even though the staging name
    still exists. A staging ``unlink`` failure after that point is a
    POST-INSTALL CLEANUP failure -- reported as installed-and-visible with
    no-retry guidance -- never as an installation failure, and the caller
    still runs the parent-directory durability step for the namespace state
    that actually exists.
    """
    try:
        if os.name == "nt":
            os.rename(tmp, path)
        else:
            os.link(tmp, path)
    except FileExistsError as exc:
        raise EvalError(
            f"refusing to overwrite existing results at {path}: it appeared "
            f"during this run; pass --force to replace recorded evidence"
        ) from exc
    except OSError as exc:
        raise EvalError(f"cannot install results at {path}: {exc}") from exc
    if os.name == "nt":
        return
    try:
        tmp.unlink()
    except OSError as exc:
        raise EvalError(
            f"results were installed at {path} and are already visible, but "
            f"removing the staging file afterwards failed: {exc}; do not "
            f"retry as though nothing was committed"
        ) from exc


def _write_results_locked(
    results: dict[str, Any],
    path: Path,
    cases: dict[str, Any],
    contract_id: str | None = None,
    *,
    must_not_exist: bool = False,
    corpus: dict[str, Any] | None = None,
) -> None:
    """Stage, durably commit and atomically install one results document.

    *must_not_exist* is the R013/W2-004 no-clobber commit: the rename is
    performed with ``os.link`` + ``unlink`` (POSIX) or ``os.rename``
    (Windows), both of which FAIL when the destination already exists,
    instead of ``os.replace``, which overwrites unconditionally. That
    closes the check-then-replace window in which a file created between
    ``exists()`` and the commit was destroyed by the command documented as
    non-destructive.

    *corpus* (SRC-003:R007) is the pre-computed corpus binding
    ``{"version", "cases", "digest"}``. When supplied it is used verbatim
    instead of hashing *cases* again, so the expensive digest never runs
    while the destination lock is held. The caller must derive it from its
    already-validated snapshot; the ``corpus_digest`` bytes are unchanged.
    """
    payload = {
        "version": 1,
        "schema": "saitalk-eval-results",
        "contract_id": contract_id,
        "corpus": (
            corpus
            if corpus is not None
            else {
                "version": cases["version"],
                "cases": len(cases["cases"]),
                "digest": corpus_digest(cases),
            }
        ),
        "results": results,
    }
    if path.is_symlink():
        raise EvalError(f"write_results refuses to replace symlink destination: {path}")
    existing_mode = _capture_destination_mode(path)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    tmp = Path(tmp_path)
    fd_owned = True
    installed_cleanup_error: EvalError | None = None
    # SRC-003:R005: False until the destination is proven to exist. Declared
    # before the try so the post-commit classification can read it on any
    # exit path.
    installed = False
    candidate_identity = None
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8")
        fd_owned = False
        with fh:
            json.dump(payload, fh, ensure_ascii=True, sort_keys=True, indent=2)
            fh.write("\n")
            fh.flush()
            # CORE-003 (SRC-002:R003): apply the captured destination mode
            # BEFORE the staged fsync, so the one fsync that precedes the
            # install synchronizes the content AND the final inode metadata
            # together. Applying it after the fsync left the permission
            # change outside the durability guarantee.
            _apply_destination_mode(fh.fileno(), existing_mode)
            # CORE-006: durably commit the staged payload before replace.
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                raise EvalError(
                    f"fsync failed for staged results {tmp}: {exc}"
                ) from exc
            candidate_identity = os.fstat(fh.fileno())
        if must_not_exist:
            try:
                _commit_no_clobber(tmp, path)
            except EvalError as exc:
                if "installed at" not in str(exc) or "already visible" not in str(exc):
                    raise
                # W2-003 (SRC-002:R006): the destination IS installed; the
                # failure was staging cleanup. The durability step below
                # still runs for the namespace state that exists, and the
                # post-install error surfaces only after it.
                installed_cleanup_error = exc
        else:
            os.replace(tmp, path)
        # Successful installation returned; exception paths below also check
        # the staged file identity because cancellation can arrive immediately
        # after the filesystem commit, before this assignment executes.
        installed = path.exists() or installed_cleanup_error is not None
        # CORE-006 / R015: durably commit the rename so the directory entry
        # survives a crash between install and the next read. A failure there
        # is reported, never swallowed -- see _fsync_parent_directory, which
        # also owns the platform check. W2-003: this runs even when the
        # post-install cleanup above failed, because the destination exists.
        try:
            _fsync_parent_directory(path)
        except BaseException as exc:
            if installed and not isinstance(exc, EvalError):
                # A cancellation here is NOT a pre-commit cancellation: the
                # results are installed and already visible, only their crash
                # durability is unknown.
                raise PostCommitError(
                    f"results were installed at {path} and are already "
                    f"visible, but the run was interrupted before the "
                    f"commit could be made crash-durable: {exc!r}; do not "
                    f"retry as though nothing was committed"
                ) from exc
            raise
        if installed_cleanup_error is not None:
            raise installed_cleanup_error
    except BaseException as exc:
        if not installed and candidate_identity is not None:
            try:
                installed = os.path.samestat(candidate_identity, path.lstat())
            except OSError:
                pass
        # SRC-003:R005: any non-EvalError escaping after the install point is
        # a POST-COMMIT failure, never an indistinguishable pre-commit
        # cancellation. EvalError diagnostics keep their existing wording.
        if (
            installed
            and not isinstance(exc, EvalError)
            and not isinstance(exc, PostCommitError)
        ):
            if fd_owned:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise PostCommitError(
                f"results were installed at {path} and are already visible, "
                f"but the run was interrupted afterwards: {exc!r}; do not "
                f"retry as though nothing was committed"
            ) from exc
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
    *,
    must_not_exist: bool = False,
) -> None:
    """Validate and persist one immutable results snapshot.

    R005/CORE-005: the caller's ``results`` and ``cases`` are DETACHED
    first, and every later step -- validation, the ``has_recorded``
    decision, the corpus digest/count/version and the serialization --
    reads only those detached copies. Previously validation ran against
    the caller's live objects and the writer serialized the same live
    objects afterwards, so a mutation in between persisted bytes that had
    never been validated (a state-only ``PASS`` with a null
    ``contract_id``, which ``load_results`` then rejects).

    ``must_not_exist=True`` is the atomic no-clobber commit used by
    ``init-results`` (R013/W2-004).
    """
    snapshot_results = copy.deepcopy(results)
    snapshot_cases = copy.deepcopy(cases)
    validate_results(snapshot_results, snapshot_cases)
    has_recorded = any(
        record["state"] != "NOT_RUN" for record in snapshot_results.values()
    )
    if contract_id is not None:
        validate_contract_id(contract_id, context="write_results")
    elif has_recorded:
        raise EvalError(
            "recorded results (states other than NOT_RUN) must bind a contract_id"
        )
    # SRC-003:R007: compute the immutable corpus binding from the VALIDATED
    # snapshot BEFORE the destination lock. The digest is pure CPU over bytes
    # that are independent of destination state; hashing it inside the lock
    # enlarged the window in which an otherwise valid concurrent caller is
    # rejected by the deliberately non-blocking lock. Snapshot semantics
    # (CORE-005) are preserved: the binding comes from snapshot_cases, never
    # from the caller's live mutable objects. Digest bytes are unchanged.
    corpus = {
        "version": snapshot_cases["version"],
        "cases": len(snapshot_cases["cases"]),
        "digest": corpus_digest(snapshot_cases),
    }
    with _destination_lock(path):
        _write_results_locked(
            snapshot_results,
            path,
            snapshot_cases,
            contract_id,
            must_not_exist=must_not_exist,
            corpus=corpus,
        )


def load_results(
    path: Path,
    cases: dict[str, Any],
    *,
    cases_validated: bool = False,
) -> dict[str, Any]:
    """Load and fully validate a results file and its provenance.

    R019/PERF-004: when *cases_validated* is True the caller already ran
    ``validate_cases`` on the corpus and this function must not re-validate
    it.  Otherwise validate_cases runs exactly once, here -- the nested
    ``validate_results`` call is told the corpus is already validated, so
    the corpus schema really is verified once for the whole pipeline
    instead of twice.
    """
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
    if not cases_validated:
        validate_cases(cases)
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
    # R019/PERF-004: the corpus was validated above (here or by the caller);
    # re-validating it inside validate_results is the dead fast path this
    # flag removes.
    validate_results(results, cases, cases_validated=True)
    contract_id = data.get("contract_id")
    has_recorded = any(record["state"] != "NOT_RUN" for record in results.values())
    if contract_id is not None:
        validate_contract_id(contract_id, context=str(path))
    elif has_recorded:
        raise EvalError(f"{path}: recorded results must bind a contract_id")
    return data


def evaluate_conformance(
    results_path: Path,
    cases: dict[str, Any],
    active_contract_id: str,
    *,
    cases_validated: bool = False,
) -> dict[str, Any]:
    """CORE-003: behavioural conformance gate.

    Returns ``{counts, conformant, reasons, bound_id, active_id}`` for a
    results file bound to ``active_contract_id``.  The gate is nonzero-exit
    unless every recorded case is PASS or SKIP-with-reason, the file is
    bound to the active contract and corpus, and every non-NOT_RUN case
    carries auditable execution provenance.  A bare state-only PASS record
    is rejected as insufficient behavioural evidence.

    R014/W2-005: conformance is NON-VACUOUS. ``SKIP`` means "deliberately
    not run", so a corpus in which nothing passed establishes nothing about
    behaviour no matter how well-formed its provenance is -- an all-SKIP
    file previously returned ``conformant=True`` with ``PASS: 0``. At least
    one real executed PASS is now required. Mixed PASS+SKIP remains valid:
    SKIP is for genuinely inapplicable cases, not for the whole corpus.

    R019/PERF-004: ``cases_validated=True`` skips the duplicate
    ``validate_cases`` call when the caller already holds a validated
    snapshot.

    PERF-003 (SRC-002:R010): the verdict stays exhaustive -- every case is
    counted, bound-id and PASS-requirement checks unchanged -- but detailed
    ``reasons`` are BOUNDED. Only the first
    ``REASON_DETAIL_CAP`` case-specific reasons are retained, then one
    deterministic summary line reports the omitted count, and a machine-
    readable ``reasons_truncated`` flag exposes the cut. The shipped
    corpus is below the cap, so its diagnostics are byte-identical; a
    large failed corpus can no longer turn the failure path into
    proportional memory and proportional stderr output.
    """
    loaded = load_results(
        results_path, cases, cases_validated=cases_validated
    )
    bound_id = loaded.get("contract_id")
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "NOT_RUN": 0}
    reasons: list[str] = []
    omitted = 0
    if bound_id is None:
        reasons.append("no contract_id bound; results do not establish conformance")
    elif bound_id != active_contract_id:
        reasons.append(
            f"bound contract_id {bound_id!r} != active {active_contract_id!r}; "
            "results are stale or for a different package"
        )
    results = loaded["results"]
    for cid, record in results.items():
        counts[record["state"]] += 1
        if record["state"] in ("FAIL", "NOT_RUN"):
            if len(reasons) < REASON_DETAIL_CAP:
                reasons.append(f"{cid}: {record['state']} not permitted for conformance")
            else:
                omitted += 1
            continue
        # PASS and SKIP must carry execution provenance (CORE-003).
        if not isinstance(record.get("execution_provenance"), dict):
            if len(reasons) < REASON_DETAIL_CAP:
                reasons.append(
                    f"{cid}: {record['state']} without execution_provenance is "
                    "not behavioural evidence"
                )
            else:
                omitted += 1
    # R014/W2-005: SKIP is documented as "deliberately not run", so a corpus
    # with zero PASS records is not behavioural evidence of anything.
    if counts["PASS"] == 0:
        reasons.append(
            f"no case PASSed ({counts['SKIP']} SKIP of {len(results)}); SKIP is "
            "deliberately-not-run, so conformance needs at least one executed "
            "PASS"
        )
    if omitted:
        reasons.append(f"... {omitted} additional case failures omitted")
    conformant = not reasons
    return {
        "counts": counts,
        "conformant": conformant,
        "reasons": reasons,
        "reasons_truncated": omitted > 0,
        "omitted_reasons": omitted,
        "bound_id": bound_id,
        "active_id": active_contract_id,
    }


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

    conform_parser = sub.add_parser(
        "conformance",
        help="Behavioural conformance gate: results must be bound to the "
        "active contract and carry runner execution provenance.",
    )
    conform_parser.add_argument("--results", type=Path, default=None)
    conform_parser.add_argument("--cases", type=Path, default=None)
    conform_parser.add_argument("--contract", type=Path, default=None)
    conform_parser.add_argument("--config", type=Path, default=None)
    conform_parser.add_argument("--skill", type=Path, default=None)

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
            force = bool(getattr(args, "force", False))
            with _destination_lock(out):
                if out.exists() and not force:
                    try:
                        existing = load_results(out, data, cases_validated=True)
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
                        # SRC-003:R007: the corpus digest comparison already
                        # happened inside load_results -- it rejects a file
                        # bound to a different digest outright. Re-hashing the
                        # whole corpus here duplicated that exact invariant
                        # while holding the destination lock. Equivalence of
                        # the results payload plus load_results' binding
                        # checks are the remaining no-op conditions.
                        if (
                            not recorded
                            and existing["results"] == results
                        ):
                            print(f"EVALS RESULTS: {out} (already initialized, no-op)")
                            return 0
                    raise EvalError(
                        f"init-results: refusing to overwrite existing results file "
                        f"{out}; use --force to reset recorded evidence"
                    )
                # R013/W2-004: the check above is a good diagnostic, not the
                # guarantee. Without --force the COMMIT itself refuses an
                # existing destination, so a file created between that check
                # and this line survives instead of being replaced.
                _write_results_locked(results, out, data, must_not_exist=not force)
            print(f"EVALS RESULTS: {out} ({len(results)} cases NOT_RUN)")
            return 0
        if args.command == "validate-results":
            data = load_cases(cases_path)
            results_path = args.results or cases_path.with_name("results.json")
            loaded = load_results(results_path, data, cases_validated=True)
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
        if args.command == "conformance":
            data = load_cases(cases_path)
            results_path = args.results or cases_path.with_name("results.json")
            try:
                active_id = current_contract_id(args)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"EVALS FAIL: active package could not be validated: {exc}",
                    file=sys.stderr,
                )
                return 1
            verdict = evaluate_conformance(
                results_path, data, active_id, cases_validated=True
            )
            counts = verdict["counts"]
            summary = (
                f"EVALS CONFORMANCE: PASS={counts['PASS']} FAIL={counts['FAIL']} "
                f"SKIP={counts['SKIP']} NOT_RUN={counts['NOT_RUN']} -> "
                f"{'CONFORMANT' if verdict['conformant'] else 'NOT CONFORMANT'}"
            )
            print(summary)
            if not verdict["conformant"]:
                for reason in verdict["reasons"]:
                    print(f"  - {reason}", file=sys.stderr)
                return 1
            return 0
    except (EvalError, OSError) as exc:
        print(f"EVALS FAIL: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
