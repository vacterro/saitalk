#!/usr/bin/env python3
"""Zero-dependency validator for the portable SAITALK contract."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import os
import re
import sys
import tempfile
from pathlib import Path

if os.name == "nt":
    import msvcrt  # Windows lock primitives
else:
    import fcntl  # POSIX lock primitives

SENTINEL = "<SAITALK-CONTRACT>"
DIGEST_HEX = 16
BOM = b"\xef\xbb\xbf"
RUNTIME_MANIFEST = ("SAITALK.md", "saitalk.conf", "SKILL.md")
CONTRACT_ID_FILES = frozenset({"SAITALK.md", "saitalk.conf"})
CONTRACT_PATTERN = re.compile(r"(?m)^contract_id\s*[:=]\s*(\S+)\s*$")
CONTRACT_ID_BYTES_PATTERN = re.compile(rb"(?m)^contract_id\s*[:=]\s*([^\r\n\s]+)")
CONF_PATTERN = re.compile(r"(?m)^([a-z_]+)=(\S.*)$")
STATE_PATTERN = re.compile(r"(?m)^saitalk_contract:\s*(\S+)\s*$")
STATE_STATUS_PATTERN = re.compile(r"(?m)^saitalk_status:\s*(\S+)\s*$")
STATE_VOICE_PATTERN = re.compile(r"(?m)^saitalk_voice:\s*(\S+)\s*$")
FENCE_PATTERN = re.compile(r"^(`{3,}|~{3,})")
TOP_HEADING_PATTERN = re.compile(r"(?m)^## (.+)$")
SECTION_PATTERN = re.compile(r"(?m)^## (\d+)\. ")
CANONICAL_SECTIONS = (
    "Language",
    "Voice",
    "Authority",
    "Hard bans",
    "Completion-first rule",
    "Evidence gate",
    "Review discipline",
    "Exactness",
    "Surfaces",
    "Persistence",
    "Suspension and bound state",
    "Configuration reference",
)
SECTION_COUNT = len(CANONICAL_SECTIONS)
# W2-005: well-formed language tags covering primary + optional
# extlang/script/region/variant/extension/private-use subtags.
LANGUAGE_TAG = re.compile(
    r"^[a-z]{2,3}"
    r"(?:-[a-z]{4})?"
    r"(?:-(?:[a-z]{2}|[0-9]{3}))?"
    r"(?:-[a-z0-9]{5,8}|[0-9][a-z0-9]{3})*"
    r"(?:-[a-wyz](?:-[a-z0-9]{2,8})+)*"
    r"(?:-x(?:-[a-z0-9]{1,8})+)?$"
)

VOICE_STOP = {"stop caveman", "normal mode"}
VOICE_RESUME = {"resume caveman", "saitalk mode"}

DRIFT_BANNED = (
    "authority",
    "completion-first",
    "hard bans",
    "evidence gate",
    "evidence-gated",
    "stop caveman",
    "normal mode",
    "decision table",
    "higher priority",
    "take precedence",
    "overrides",
    "required value",
    "caveman-ded",
)


# PERF-001: Drift normalization helper -- used at module level for
# precompiled pattern and at validation time for document comparison.
def _drift_normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


# PERF-001: Precompiled single-pass matcher for all drift tokens. Each
# token is emitted as a word-bounded pattern so all 13 banned phrases are
# scanned in one regex pass instead of 13 separate full-document searches.
# Tokens are normalized (non-alphanumeric -> space) before escaping so the
# regex matches after document normalization strips hyphens/punctuation.
_DRIFT_ALTERNATION = "|".join(
    rf"\b{re.escape(_drift_normalize(token))}\b"
    for token in DRIFT_BANNED
)
_DRIFT_MATCH = re.compile(_DRIFT_ALTERNATION, re.IGNORECASE)

LEGACY_STYLE = "caveman-ded-en"
ALLOWED = {
    "spec_version": {"2"},
    "reply_language": {"et", "en", "ru", "auto"},
    "chat_style": {"caveman-ded"},
    "artifact_language": {"en", "et", "ru", "auto"},
    "review_mode": {"evidence-gated"},
}
REQUIRED_KEYS = {
    "spec_version",
    "reply_language",
    "chat_style",
    "artifact_language",
    "review_mode",
    "response_budget",
    "contract_id",
}

ARTIFACT_CONFIG = frozenset({"en", "et", "ru", "auto"})

STATE_TOKENS = (
    "saitalk_contract",
    "saitalk_status",
    "saitalk_voice",
    "stop caveman",
    "normal mode",
    "resume caveman",
    "saitalk mode",
)


class SaitalkError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# PERF-002: bounded-memory exactly-one helper
# ---------------------------------------------------------------------------

def _exactly_one_iter(pattern: re.Pattern[str], text: str, label: str) -> re.Match[str]:
    """Return the single match from *pattern* in *text*, counting all matches
    without storing them in a list.  Preserves the exact duplicate count in
    the error message while using O(1) auxiliary memory regardless of how
    many matches exist.
    """
    count = 0
    first: re.Match[str] | None = None
    for match in pattern.finditer(text):
        count += 1
        if first is None:
            first = match
        # Continue scanning so the total count is exact (do not stop at 2).
    if count != 1:
        raise SaitalkError(f"{label}: expected exactly one match, found {count}")
    assert first is not None  # count >= 1
    return first


def read_text(path: Path) -> str:
    try:
        if path.is_dir():
            raise SaitalkError(f"path is a directory: {path}")
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SaitalkError(f"missing file: {path}") from exc
    except PermissionError as exc:
        raise SaitalkError(f"permission denied: {path}") from exc
    except UnicodeDecodeError as exc:
        raise SaitalkError(f"not UTF-8: {path}") from exc
    except OSError as exc:
        raise SaitalkError(f"cannot read {path}: {exc}") from exc
    return text.removeprefix("\ufeff")


def read_bytes(path: Path) -> bytes:
    try:
        if path.is_dir():
            raise SaitalkError(f"path is a directory: {path}")
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise SaitalkError(f"missing file: {path}") from exc
    except PermissionError as exc:
        raise SaitalkError(f"permission denied: {path}") from exc
    except OSError as exc:
        raise SaitalkError(f"cannot read {path}: {exc}") from exc


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------------------
# CORE-002: Fixed Markdown top-level-line filter
# ---------------------------------------------------------------------------

def _top_level_lines(text: str) -> str:
    """Return only lines outside fenced code blocks (``` or ~~~) and HTML
    comments.

    Changes vs. prior implementation:

    * **Inline comments** – an HTML comment that begins anywhere on a line
      (not only at the start) is recognised.  Any text before ``<!--`` on
      the same line is retained; text after the closing ``-->`` is kept
      and recursed through.
    * **Fence closing** – a closing fence must use the *same delimiter
      character* and contain *at least as many consecutive delimiter
      characters* as the opening fence (e.g. `````` is closed by
      `````` but not by ```).
    """
    kept: list[str] = []
    fence_char: str | None = None
    fence_len: int = 0
    in_comment = False

    for line in text.splitlines():
        remaining = line

        # --- inside a multi-line HTML comment: skip until --> ----------
        if in_comment:
            close_idx = remaining.find("-->")
            if close_idx == -1:
                continue
            in_comment = False
            remaining = remaining[close_idx + 3:]
            # fall through to process the rest of the line below

        # --- inside a fenced block: look for a matching close ---------
        if fence_char is not None:
            stripped = remaining.strip()
            m = FENCE_PATTERN.match(stripped)
            if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len:
                fence_char = None
                fence_len = 0
            continue

        # --- scan for inline HTML comments (may appear anywhere) -------
        while True:
            comment_start = remaining.find("<!--")
            if comment_start == -1:
                break
            # text before the comment is live
            before = remaining[:comment_start]
            after_open = remaining[comment_start + 4:]
            close_idx = after_open.find("-->")
            if close_idx == -1:
                # comment extends to subsequent lines
                if before.strip():
                    kept.append(before)
                in_comment = True
                remaining = ""
                break
            # comment closes on the same line
            remaining = after_open[close_idx + 3:]

        if not remaining:
            if remaining is not None and not in_comment:
                # line was entirely consumed by an inline comment (e.g.
                # "<!-- ... -->" alone) – skip it
                pass
            continue

        # --- check for fenced block opening ----------------------------
        stripped = remaining.strip()
        m = FENCE_PATTERN.match(stripped)
        if m:
            fence_char = m.group(1)[0]
            fence_len = len(m.group(1))
            continue

        kept.append(remaining)

    return "\n".join(kept)


def _top_level_headings(text: str) -> list[str]:
    return [m.group(1).rstrip() for m in TOP_HEADING_PATTERN.finditer(_top_level_lines(text))]


# ---------------------------------------------------------------------------
# CORE-002 continued / W2-005 / PERF-002: helpers
# ---------------------------------------------------------------------------

def exactly_one(pattern: re.Pattern[str], text: str, label: str) -> re.Match[str]:
    """Public exact-match validator – delegates to bounded-memory helper."""
    return _exactly_one_iter(pattern, text, label)


def parse_conf(text: str) -> dict[str, str]:
    normalized = normalize(text)
    values: dict[str, str] = {}
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = CONF_PATTERN.fullmatch(line)
        if not match:
            raise SaitalkError(f"invalid config line: {raw_line!r}")
        key, value = match.groups()
        if key in values:
            raise SaitalkError(f"duplicate config key: {key}")
        values[key] = value

    missing = REQUIRED_KEYS - values.keys()
    extra = values.keys() - REQUIRED_KEYS
    if missing:
        raise SaitalkError(f"missing config keys: {', '.join(sorted(missing))}")
    if extra:
        raise SaitalkError(f"unknown config keys: {', '.join(sorted(extra))}")

    for key, allowed in ALLOWED.items():
        if values[key] not in allowed:
            options = ", ".join(sorted(allowed))
            if key == "chat_style" and values[key] == LEGACY_STYLE:
                raise SaitalkError(
                    "chat_style: legacy value 'caveman-ded-en' merged language into "
                    "voice; set chat_style=caveman-ded and reply_language=en|et|ru|auto"
                )
            raise SaitalkError(
                f"{key}: invalid value {values[key]!r}; allowed: {options}"
            )

    try:
        budget = int(values["response_budget"])
    except ValueError as exc:
        raise SaitalkError("response_budget must be an integer") from exc
    if not 1 <= budget <= 20:
        raise SaitalkError("response_budget must be between 1 and 20")

    return values


def _require_coherent_root(contract_path: Path, conf_path: Path, skill_path: Path) -> None:
    paths = (contract_path, conf_path, skill_path)
    try:
        resolved = [path.resolve() for path in paths]
    except OSError as exc:
        raise SaitalkError(f"cannot resolve manifest path: {exc}") from exc
    for a, b in itertools.combinations(resolved, 2):
        try:
            if a == b or os.path.samefile(a, b):
                raise SaitalkError("manifest members alias the same file")
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SaitalkError(f"cannot compare manifest paths: {exc}") from exc
    parents = {path.parent for path in resolved}
    if len(parents) > 1:
        raise SaitalkError(
            "manifest members do not form one coherent package root"
        )


def _manifest_members(
    contract_path: Path, conf_path: Path, skill_path: Path
) -> tuple[tuple[str, Path], ...]:
    """Pair each RUNTIME_MANIFEST label with its path, in canonical order."""
    pairs = tuple(zip(RUNTIME_MANIFEST, (contract_path, conf_path, skill_path)))
    for label, path in pairs:
        if path.name != label:
            raise SaitalkError(
                f"manifest member mismatch: expected {label!r}, got {path.name!r}"
            )
    return pairs


def read_manifest(contract_path: Path, conf_path: Path, skill_path: Path) -> dict[str, str]:
    _require_coherent_root(contract_path, conf_path, skill_path)
    manifest: dict[str, str] = {}
    for label, path in _manifest_members(contract_path, conf_path, skill_path):
        manifest[label] = read_text(path)
    return manifest


def replace_contract_value(text: str, label: str) -> str:
    normalized = normalize(text)
    match = exactly_one(CONTRACT_PATTERN, normalized, label)
    start, end = match.span(1)
    return normalized[:start] + SENTINEL + normalized[end:]


def expected_id(manifest: dict[str, str]) -> str:
    payload = ""
    for member in sorted(manifest):
        if member in CONTRACT_ID_FILES:
            content = replace_contract_value(manifest[member], f"{member} contract_id")
        else:
            content = normalize(manifest[member])
        payload += member + "\n" + content + "\n"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"saitalk-{digest[:DIGEST_HEX]}"


def current_contract_id(text: str, label: str) -> str:
    return exactly_one(CONTRACT_PATTERN, normalize(text), label).group(1)


def replace_id_bytes(data: bytes, new_id: str, label: str) -> bytes:
    """Replace the single contract_id value in raw bytes, leaving all other
    bytes untouched.  Uses bounded-memory matching (PERF-002).
    """
    prefix = BOM if data.startswith(BOM) else b""
    body = data[len(prefix):]
    match = _exactly_one_iter(CONTRACT_ID_BYTES_PATTERN, body, label)
    new_body = body[: match.start(1)] + new_id.encode("ascii") + body[match.end(1):]
    return prefix + new_body


def _section_body(text: str, num: int) -> str:
    lines = _top_level_lines(text).splitlines()
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        match = SECTION_PATTERN.match(line)
        if match and int(match.group(1)) == num:
            start = i
        elif match and start is not None:
            end = i
            break
    if start is None:
        return ""
    return "\n".join(lines[start:end])


def check_structure(contract_text: str, skill_text: str) -> None:
    """Minimal structural conformance guards for the normative contract."""
    normalized = normalize(contract_text)
    headings = _top_level_headings(normalized)
    expected_headings = [f"{i}. {title}" for i, title in enumerate(CANONICAL_SECTIONS, 1)]
    if headings != expected_headings:
        raise SaitalkError(
            "contract structure: top-level sections must be exactly "
            f"{expected_headings} in order, found {headings}"
        )
    section11 = _section_body(normalized, 11)
    section12 = _section_body(normalized, 12)
    for token in STATE_TOKENS:
        if token not in section11:
            raise SaitalkError(
                f"contract structure: section 11 must reference {token!r}"
            )
    for key in sorted(REQUIRED_KEYS):
        if f"`{key}`" not in section12:
            raise SaitalkError(
                f"contract structure: section 12 must document config key {key!r}"
            )

    # CORE-003: validate SKILL ownership against *live* (non-comment,
    # non-fenced) content only, using the corrected top-level filter.
    skill_live = _top_level_lines(skill_text)
    if "SAITALK.md" not in skill_live or "owns all normative behavior" not in skill_live:
        raise SaitalkError(
            "contract structure: SKILL.md must declare SAITALK.md as the "
            "sole normative behavior source"
        )


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

def _acquire_lock(target: Path):
    digest = hashlib.sha256(str(target.resolve()).encode("utf-8")).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"saitalk-{digest}.lock"
    try:
        if os.name == "nt" and (not lock_path.exists() or lock_path.stat().st_size < 1):
            lock_path.write_bytes(b"\0")
        fh = lock_path.open("r+b" if os.name == "nt" else "a+b")
    except OSError as exc:
        raise SaitalkError(f"cannot open lock file {lock_path}: {exc}") from exc
    try:
        if os.name == "nt":
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        raise SaitalkError(
            f"lock contention on {target}: another refresh is in progress"
        ) from exc
    return fh


def _release_lock(fh) -> None:
    try:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    fh.close()


def _acquire_locks(targets: list[Path]) -> list:
    """Acquire one lock per target in deterministic canonical order.

    Accepts both original (possibly symlinked) paths and already-resolved
    paths.  Sorts by resolved form; the lock acquisition order is always
    the same for the same physical files regardless of symlink topology.
    """
    ordered = sorted(targets, key=lambda t: str(t.resolve()))
    handles = []
    try:
        for target in ordered:
            handles.append(_acquire_lock(target))
    except SaitalkError:
        for fh in reversed(handles):
            _release_lock(fh)
        raise
    return handles


def _release_locks(handles) -> None:
    for fh in reversed(handles):
        _release_lock(fh)


# ---------------------------------------------------------------------------
# CORE-001 / W2-002: Secure temporary file creation with permission
# preservation.  Uses tempfile.mkstemp (O_CREAT|O_EXCL) so no pre-existing
# hardlink/symlink at a predictable sibling path can be followed.
# ---------------------------------------------------------------------------

def _safe_write_temp(target: Path, data: bytes, mode: int | None = None) -> Path:
    """Create a unique temporary file in the same directory as *target*,
    write *data*, flush, fsync, and return the path.  If *mode* is given
    the file descriptor is set to that mode before writing (POSIX).
    """
    dir_path = target.parent
    prefix = f"{target.name}."
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), prefix=prefix)
    try:
        if mode is not None and os.name != "nt":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return Path(tmp_path)


def _replace_temp(target: Path, tmp: Path) -> None:
    try:
        os.replace(tmp, target)
    except OSError as exc:
        raise SaitalkError(f"replacement failure: {target}: {exc}") from exc


def _restore_bytes(target: Path, original: bytes, mode: int | None = None) -> None:
    try:
        tmp = _safe_write_temp(target, original, mode=mode)
        os.replace(tmp, target)
    except OSError as exc:
        raise SaitalkError(f"restore failure on {target}: {exc}") from exc
    finally:
        # Clean up any stale .restore.tmp from the old implementation.
        # Also clean the unique tmp if replace succeeded (now at target).
        try:
            # unique tmp was moved by os.replace; unlink is harmless if gone
            pass
        except OSError:
            pass


def _capture_modes(paths: list[Path]) -> dict[Path, int | None]:
    """Capture POSIX permission bits for each path; returns None for paths
    that do not yet exist (new files after the first write).
    """
    modes: dict[Path, int | None] = {}
    for p in paths:
        try:
            modes[p] = os.stat(p).st_mode & 0o7777
        except OSError:
            modes[p] = None
    return modes


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------

def refresh(contract_path: Path, conf_path: Path, skill_path: Path) -> str:
    """Refresh contract_id from a single authoritative byte snapshot.

    Every manifest member (including SKILL.md) is locked before anything is
    read, and every member is read exactly once, as bytes; both the hash and
    the byte-level replacement are derived from that one snapshot, so there
    is no window where a hashed read and a patched read can observe
    different bytes.  Structure is validated against that same snapshot
    before any write, so a structurally invalid package can never be
    blessed with a fresh marker.  Rollback restores only the targets this
    call actually replaced.

    W2-001: resolved referents are used for all read/write/replace/rollback
    operations so file symlinks are preserved rather than destructively
    replaced with regular files.

    W2-002: POSIX permission bits of each target are captured before staging
    and applied to every staged file (and every rollback file).

    PERF-003: targets whose replacement bytes are identical to the original
    bytes are skipped entirely -- no staging, no fsync, no inode churn.
    """
    _require_coherent_root(contract_path, conf_path, skill_path)

    # W2-001: establish one canonical resolved-referent mapping and use it
    # for every mutable operation.  Logical RUNTIME_MANIFEST labels stay
    # based on the supplied canonical filenames.
    resolved_map: dict[str, Path] = {}
    for label, path in _manifest_members(contract_path, conf_path, skill_path):
        resolved_map[label] = path.resolve()

    resolved_targets = [resolved_map["SAITALK.md"], resolved_map["saitalk.conf"], resolved_map["SKILL.md"]]
    locks = _acquire_locks(resolved_targets)
    try:
        raw: dict[str, bytes] = {}
        for label in RUNTIME_MANIFEST:
            raw[label] = read_bytes(resolved_map[label])

        manifest: dict[str, str] = {}
        for label, data in raw.items():
            try:
                manifest[label] = data.decode("utf-8").removeprefix("\ufeff")
            except UnicodeDecodeError as exc:
                raise SaitalkError(f"not UTF-8: {label}") from exc

        parse_conf(manifest["saitalk.conf"])
        check_structure(manifest["SAITALK.md"], manifest["SKILL.md"])
        new_id = expected_id(manifest)

        contract_bytes = raw["SAITALK.md"]
        conf_bytes = raw["saitalk.conf"]
        new_contract = replace_id_bytes(contract_bytes, new_id, "SAITALK.md contract_id")
        new_conf = replace_id_bytes(conf_bytes, new_id, "saitalk.conf contract_id")

        # W2-002: capture permission bits before any writes
        mode_contract = _capture_modes([resolved_map["SAITALK.md"]]).get(resolved_map["SAITALK.md"])
        mode_conf = _capture_modes([resolved_map["saitalk.conf"]]).get(resolved_map["saitalk.conf"])

        # PERF-003: only stage targets whose bytes actually changed.
        pairs: list[tuple[Path, bytes, bytes]] = []
        if new_contract != contract_bytes:
            pairs.append((resolved_map["SAITALK.md"], new_contract, contract_bytes))
        if new_conf != conf_bytes:
            pairs.append((resolved_map["saitalk.conf"], new_conf, conf_bytes))

        if not pairs:
            return new_id

        modes_map = {resolved_map["SAITALK.md"]: mode_contract, resolved_map["saitalk.conf"]: mode_conf}

        temps: dict[Path, Path] = {}
        replaced: list[Path] = []
        try:
            for target, content, _original in pairs:
                temps[target] = _safe_write_temp(target, content, mode=modes_map.get(target))
            for target, tmp in temps.items():
                _replace_temp(target, tmp)
                replaced.append(target)
        except SaitalkError as primary:
            rollback_failures = []
            for target, _content, original in pairs:
                if target not in replaced:
                    continue
                try:
                    _restore_bytes(target, original, mode=modes_map.get(target))
                except SaitalkError as exc:
                    rollback_failures.append(f"{target}: {exc}")
            if rollback_failures:
                raise SaitalkError(
                    f"{primary}; rollback incomplete: " + "; ".join(rollback_failures)
                ) from primary
            raise
        finally:
            for tmp in temps.values():
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
        return new_id
    finally:
        _release_locks(locks)


# ---------------------------------------------------------------------------
# validate (CORE-005: acquires manifest locks for coherent snapshot)
# ---------------------------------------------------------------------------

def _validate(contract_path: Path, conf_path: Path, skill_path: Path) -> tuple[str, dict[str, str]]:
    """Validate manifest without acquiring locks (caller must hold them)."""
    manifest = read_manifest(contract_path, conf_path, skill_path)
    config = parse_conf(manifest["saitalk.conf"])
    check_structure(manifest["SAITALK.md"], manifest["SKILL.md"])

    expected = expected_id(manifest)
    contract_current = current_contract_id(manifest["SAITALK.md"], "SAITALK.md contract_id")
    conf_current = config["contract_id"]

    if contract_current != expected:
        raise SaitalkError(
            f"SAITALK.md stale contract_id: found {contract_current!r}, "
            f"expected {expected!r}"
        )
    if conf_current != expected:
        raise SaitalkError(
            f"saitalk.conf stale contract_id: found {conf_current!r}, "
            f"expected {expected!r}"
        )
    return expected, config


def validate(contract_path: Path, conf_path: Path, skill_path: Path) -> tuple[str, dict[str, str]]:
    """Validate manifest with coherent-snapshot locking (CORE-005)."""
    _require_coherent_root(contract_path, conf_path, skill_path)
    paths = [contract_path, conf_path, skill_path]
    locks = _acquire_locks(paths)
    try:
        return _validate(contract_path, conf_path, skill_path)
    finally:
        _release_locks(locks)


def voice_command(message: str) -> str | None:
    """Return the target voice state for a standalone voice control command."""
    normalized = " ".join(message.strip().split()).casefold()
    if normalized in VOICE_STOP:
        return "suspended"
    if normalized in VOICE_RESUME:
        return "active"
    return None


def validate_state(path: Path, expected: str) -> None:
    text = normalize(read_text(path))
    top = _top_level_lines(text)
    contract_match = exactly_one(STATE_PATTERN, top, "saitalk_contract")
    status_match = exactly_one(STATE_STATUS_PATTERN, top, "saitalk_status")
    voice_match = exactly_one(STATE_VOICE_PATTERN, top, "saitalk_voice")

    actual = contract_match.group(1)
    if actual != expected:
        raise SaitalkError(
            f"saitalk_contract: found {actual!r}, expected {expected!r}"
        )
    if status_match.group(1) != "active":
        raise SaitalkError(
            f"saitalk_status: found {status_match.group(1)!r}, expected 'active'"
        )
    if voice_match.group(1) not in ("active", "suspended"):
        raise SaitalkError(
            f"saitalk_voice: found {voice_match.group(1)!r}, "
            "expected 'active' or 'suspended'"
        )


def _normalize_language(value: str) -> str:
    return value.strip().casefold()


def resolve_artifact_language(
    configured: str,
    task_language: str | None = None,
    existing_language: str | None = None,
    repo_language: str | None = None,
) -> str:
    """Resolve the effective artifact prose language."""
    if configured not in ARTIFACT_CONFIG:
        raise SaitalkError(
            f"artifact_language: invalid value {configured!r}; "
            "allowed: en, et, ru, auto"
        )

    sources = (
        ("task", task_language),
        ("existing", existing_language),
        ("repo", repo_language),
    )
    resolved: dict[str, str] = {}
    for label, value in sources:
        if value is None:
            continue
        candidate = _normalize_language(value)
        if not candidate:
            raise SaitalkError(
                f"artifact_language: explicit {label} language is empty"
            )
        if not LANGUAGE_TAG.fullmatch(candidate):
            raise SaitalkError(
                f"artifact_language: explicit {label} language {value!r} "
                "is not a well-formed language tag"
            )
        resolved[label] = candidate

    for label in ("task", "existing", "repo"):
        if label in resolved:
            return resolved[label]
    if configured == "auto":
        return "en"
    return configured# ---------------------------------------------------------------------------
# PERF-001: Optimized drift validation
# ---------------------------------------------------------------------------
# _drift_normalize is defined at module level for precompilation.

def validate_drift(root: Path) -> None:
    """Reject banned rule tokens anywhere in an adapter file's normalized text.

    W2-004: discovers adapters using case-insensitive ``.md`` matching so
    ``BAD.MD`` is caught on case-sensitive filesystems.  Directory symlinks
    are followed with recursive descent; files are identified purely by
    their resolved name suffix (casefolded to ``.md``).
    """
    transport_dir = root / "adapters"
    if not transport_dir.is_dir():
        return

    # PERF-001: use sorted(rglob) only for deterministic failure order;
    # the per-file matching is one compiled pass instead of 13 searches.
    md_files: list[Path] = []
    for p in transport_dir.rglob("*"):
        if p.is_file() and p.name.casefold().endswith(".md"):
            md_files.append(p)
    md_files.sort()

    for path in md_files:
        text = normalize(read_text(path))
        # PERF-001: single precompiled alternation scan.
        normalized_doc = _drift_normalize(text)
        hit = _DRIFT_MATCH.search(normalized_doc)
        if hit:
            # Identify which token was matched and find its line.
            matched_text = hit.group(0).lower()
            # Find the DRIFT_BANNED entry whose normalized form matches
            # the raw regex match (the match itself is already normalized
            # text, so compare against normalised token forms).
            token = next(
                (t for t in DRIFT_BANNED if _drift_normalize(t) == matched_text),
                matched_text,
            )
            lines = text.splitlines()
            # The first word of the normalized token matches the first word
            # in the normalized doc line (e.g. "completion-first" -> "completion").
            first_word = _drift_normalize(token).split()[0]
            line_number = next(
                (i for i, line in enumerate(lines, 1) if first_word in _drift_normalize(line)),
                0,
            )
            raise SaitalkError(
                f"drift: {path.relative_to(root)}:{line_number} contains "
                f"rule token {token!r}; this is a structural drift guard, "
                "not proof of semantic conformance. Transport files must "
                "not restate or extend the contract"
            )


# ---------------------------------------------------------------------------
# W2-003 / CORE-004: validate_package
# ---------------------------------------------------------------------------

def _validate_package(
    contract_path: Path,
    conf_path: Path,
    skill_path: Path,
    state_path: Path | None = None,
) -> tuple[str, dict[str, str]]:
    """Validate manifest + drift without acquiring locks (caller holds them)."""
    contract_id, config = _validate(contract_path, conf_path, skill_path)
    validate_drift(contract_path.resolve().parent)
    if state_path is not None:
        validate_state(state_path, contract_id)
    return contract_id, config


def validate_package(
    contract_path: Path,
    conf_path: Path,
    skill_path: Path,
    state_path: Path | None = None,
) -> tuple[str, dict[str, str]]:
    """Validate manifest, active-root drift, and optional bound state in one
    coherent-locked path (CORE-005 / W2-003).
    """
    _require_coherent_root(contract_path, conf_path, skill_path)
    paths = [contract_path, conf_path, skill_path]
    locks = _acquire_locks(paths)
    try:
        return _validate_package(contract_path, conf_path, skill_path, state_path)
    finally:
        _release_locks(locks)


def root_paths() -> tuple[Path, Path, Path]:
    root = Path(__file__).resolve().parents[1]
    contract, conf, skill = RUNTIME_MANIFEST
    return root / contract, root / conf, root / skill


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate portable SAITALK.")
    sub = parser.add_subparsers(dest="command", required=True)

    refresh_parser = sub.add_parser("refresh", help="Refresh contract_id.")
    refresh_parser.add_argument("--contract", type=Path)
    refresh_parser.add_argument("--config", type=Path)
    refresh_parser.add_argument("--skill", type=Path)

    validate_parser = sub.add_parser("validate", help="Validate contract and config.")
    validate_parser.add_argument("--contract", type=Path)
    validate_parser.add_argument("--config", type=Path)
    validate_parser.add_argument("--state", type=Path)
    validate_parser.add_argument("--skill", type=Path)

    print_parser = sub.add_parser("print-id", help="Print current valid contract_id.")
    print_parser.add_argument("--contract", type=Path)
    print_parser.add_argument("--config", type=Path)
    print_parser.add_argument("--skill", type=Path)

    return parser


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    default_contract, default_conf, default_skill = root_paths()
    return (
        args.contract or default_contract,
        args.config or default_conf,
        args.skill or default_skill,
    )


def main() -> int:
    args = build_parser().parse_args()
    contract_path, conf_path, skill_path = resolve_paths(args)

    try:
        if args.command == "refresh":
            contract_id = refresh(contract_path, conf_path, skill_path)
            print(f"SAITALK REFRESHED: contract_id={contract_id}")
            return 0

        # W2-003 / CORE-004: print-id uses canonical package validation
        # so it never emits a valid-looking ID for a drift-invalid package.
        if args.command == "print-id":
            contract_id, _ = validate_package(contract_path, conf_path, skill_path)
            print(contract_id)
            return 0

        contract_id, config = validate_package(
            contract_path, conf_path, skill_path, getattr(args, "state", None)
        )

        state = f"; state={args.state}: PASS" if args.state else ""
        print(
            f"SAITALK PASS: contract_id={contract_id}; "
            f"reply_language={config['reply_language']}; "
            f"chat_style={config['chat_style']}; "
            f"artifact_language={config['artifact_language']}{state}"
        )
        return 0
    except SaitalkError as exc:
        print(f"SAITALK FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
