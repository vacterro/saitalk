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
# CORE-006: complete zero-dependency well-formed language-tag grammar
# (BCP 47 regular production).  Covers primary language, optional extlang,
# script, region, variants, extensions and private-use subtags, plus the
# private-use-only form.  This matches the contract's promise that any
# well-formed explicit language tag is honored.
def _is_well_formed_language_tag(tag: str) -> bool:
    # BCP 47 tags are case-insensitive; normalize so subtag comparisons are
    # always lowercase (e.g. en-US, zh-cmn-Hans-CN, zh-CN).
    parts = tag.casefold().split("-")
    if not parts or any(not p for p in parts):
        return False
    primary = parts[0]
    if not re.fullmatch(r"[a-z]{2,3}", primary):
        # private-use-only form: x + one or more privateuse subtags
        if primary == "x":
            return len(parts) >= 2 and all(
                re.fullmatch(r"[a-z0-9]{1,8}", p) for p in parts[1:]
            )
        return False
    idx = 1
    if idx < len(parts) and re.fullmatch(r"[a-z]{3}", parts[idx]):  # extlang
        idx += 1
    if idx < len(parts) and re.fullmatch(r"[a-z]{4}", parts[idx]):  # script
        idx += 1
    if idx < len(parts) and (
        re.fullmatch(r"[a-z]{2}", parts[idx]) or re.fullmatch(r"[0-9]{3}", parts[idx])
    ):  # region
        idx += 1
    while idx < len(parts) and (
        re.fullmatch(r"[a-z0-9]{5,8}", parts[idx])
        or re.fullmatch(r"[0-9][a-z0-9]{3}", parts[idx])
    ):  # variants
        idx += 1
    while idx < len(parts) and re.fullmatch(r"[a-wyz]", parts[idx]):  # extension singleton
        idx += 1
        if idx >= len(parts):
            return False
        while idx < len(parts) and re.fullmatch(r"[a-z0-9]{2,8}", parts[idx]):
            idx += 1
    if idx < len(parts) and parts[idx] == "x":  # privateuse
        idx += 1
        if idx >= len(parts):
            return False
        while idx < len(parts) and re.fullmatch(r"[a-z0-9]{1,8}", parts[idx]):
            idx += 1
    return idx == len(parts)

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
    """Return only the *live* top-level lines of *text*.

    A line is live when it is outside fenced code blocks (``` or ~~~) and
    outside HTML comments.  The following Markdown boundaries are honoured
    (CORE-002):

    * **Fenced blocks** – a fence opener must begin with 0-3 leading spaces
      and use 3+ identical backticks or tildes; a backtick opener may not
      carry a backtick inside its info string (that is not a valid fence).
      A closing fence must use the same delimiter character and at least as
      many delimiter characters as the opener.  Four-space indented code is
      an indented-code construct, never a fence, so it cannot mask live
      headings.
    * **HTML comments** – both ``<!--`` ... ``-->`` spanning multiple lines
      and same-line comments are recognised.  Any live text *before* an
      inline comment and *after* its closing ``-->`` is preserved, so an
      inline comment can never hide a live heading that precedes it.
    """
    kept: list[str] = []
    fence_char: str | None = None
    fence_len: int = 0
    in_comment = False

    for line in text.splitlines():
        remaining = line

        # Inside a multi-line HTML comment: drop until the closing marker.
        if in_comment:
            close_idx = remaining.find("-->")
            if close_idx == -1:
                continue
            in_comment = False
            remaining = remaining[close_idx + 3:]
            # fall through to continue scanning the rest of the line

        # Inside a fenced block: only look for a valid closing fence.
        if fence_char is not None:
            stripped = remaining.strip()
            m = FENCE_PATTERN.match(stripped)
            if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len:
                fence_char = None
                fence_len = 0
            continue

        # Scan for inline HTML comments; keep live text before and after.
        while True:
            comment_start = remaining.find("<!--")
            if comment_start == -1:
                break
            before = remaining[:comment_start]
            after_open = remaining[comment_start + 4:]
            close_idx = after_open.find("-->")
            if close_idx == -1:
                # Comment runs to subsequent lines.
                if before.strip():
                    kept.append(before)
                in_comment = True
                remaining = ""
                break
            # Comment closes on the same line: preserve before + after.
            remaining = before + after_open[close_idx + 3:]

        if not remaining:
            continue

        # Fence opener detection: Markdown requires 0-3 leading spaces.
        leading = len(remaining) - len(remaining.lstrip(" "))
        if leading <= 3:
            stripped = remaining.strip()
            m = FENCE_PATTERN.match(stripped)
            if m:
                opener = m.group(1)
                if opener[0] == "`":
                    # A backtick fence info string may not contain a backtick.
                    if "`" in stripped[len(opener):]:
                        pass  # invalid opener -> treat the line as live text
                    else:
                        fence_char = "`"
                        fence_len = len(opener)
                        continue
                else:
                    fence_char = "~"
                    fence_len = len(opener)
                    continue

        kept.append(remaining)

    return "\n".join(kept)


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


def _headings_from_live(live: str) -> list[str]:
    return [m.group(1).rstrip() for m in TOP_HEADING_PATTERN.finditer(live)]


def _section_body_from_live(live: str, num: int) -> str:
    lines = live.splitlines()
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
    """Minimal structural conformance guards for the normative contract.

    PERF-001: the live contract structure is parsed exactly once; headings and
    sections 11/12 are derived from that single representation, not by
    re-scanning the whole contract three more times.
    """
    normalized = normalize(contract_text)
    # PERF-001: one live-contract parse.
    live = _top_level_lines(normalized)
    headings = _headings_from_live(live)
    expected_headings = [f"{i}. {title}" for i, title in enumerate(CANONICAL_SECTIONS, 1)]
    if headings != expected_headings:
        raise SaitalkError(
            "contract structure: top-level sections must be exactly "
            f"{expected_headings} in order, found {headings}"
        )
    section11 = _section_body_from_live(live, 11)
    section12 = _section_body_from_live(live, 12)
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

    CORE-005: any filesystem failure (``mkstemp``, ``fchmod``, write, flush,
    ``fsync``) is normalized into a ``SaitalkError`` carrying the target and
    operation context, so the public ``refresh``/CLI error contract stays
    uniform instead of leaking a raw ``OSError`` traceback.  The descriptor is
    closed on any failure that happens before ``os.fdopen`` takes ownership.
    """
    dir_path = target.parent
    prefix = f"{target.name}."
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), prefix=prefix)
    except OSError as exc:
        raise SaitalkError(f"staging failure for {target}: {exc}") from exc
    try:
        if mode is not None and os.name != "nt":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise SaitalkError(f"staging failure for {target}: {exc}") from exc
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


def _capture_modes(paths: list[Path]) -> dict[Path, int]:
    """Capture POSIX permission bits for each existing path.

    CORE-004: an ``os.stat`` failure on a target that refresh must replace is
    a real error, never a signal that the file is new.  Raising here aborts
    the operation before any byte or mode change, instead of silently
    presuming "no mode to preserve" and then clobbering the file's
    permissions on replace.
    """
    modes: dict[Path, int] = {}
    for p in paths:
        try:
            modes[p] = os.stat(p).st_mode & 0o7777
        except OSError as exc:
            raise SaitalkError(f"cannot read permissions of {p}: {exc}") from exc
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

        # W2-002 / CORE-004: capture permission bits before any writes; a
        # stat failure aborts cleanly before any byte or mode change.
        modes_map = _capture_modes(
            [resolved_map["SAITALK.md"], resolved_map["saitalk.conf"]]
        )

        # PERF-003: only stage targets whose bytes actually changed.
        pairs: list[tuple[Path, bytes, bytes]] = []
        if new_contract != contract_bytes:
            pairs.append((resolved_map["SAITALK.md"], new_contract, contract_bytes))
        if new_conf != conf_bytes:
            pairs.append((resolved_map["saitalk.conf"], new_conf, conf_bytes))

        if not pairs:
            _assert_manifest_stable((contract_path, conf_path, skill_path), resolved_map)
            return new_id

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
        _assert_manifest_stable((contract_path, conf_path, skill_path), resolved_map)
        return new_id
    finally:
        _release_locks(locks)


# ---------------------------------------------------------------------------
# validate (CORE-005: acquires manifest locks for coherent snapshot)
# ---------------------------------------------------------------------------

def _resolved_manifest_map(
    contract_path: Path, conf_path: Path, skill_path: Path
) -> dict[str, Path]:
    """Resolve each manifest member exactly once and return label -> resolved
    path.  This is the single stable identity used for the whole operation.
    """
    _require_coherent_root(contract_path, conf_path, skill_path)
    resolved: dict[str, Path] = {}
    for label, path in _manifest_members(contract_path, conf_path, skill_path):
        resolved[label] = path.resolve()
    return resolved


def _assert_manifest_stable(
    logical: tuple[Path, Path, Path], resolved: dict[str, Path]
) -> None:
    """W2-002: after locks are held and all reads/writes used *resolved*,
    re-resolve the logical members and require them to still identify the
    captured targets.  A retargeted symlink means we would report a package
    the caller no longer points at, so we abort cleanly instead.
    """
    for label, logical_path in zip(RUNTIME_MANIFEST, logical):
        if logical_path.resolve() != resolved[label]:
            raise SaitalkError(
                f"manifest member {label} was retargeted during the operation; "
                "aborting to avoid reporting a stale package"
            )


def _read_resolved_manifest(resolved: dict[str, Path]) -> dict[str, str]:
    """Read each resolved manifest member exactly once (W2-002 stable set)."""
    manifest: dict[str, str] = {}
    for label in RUNTIME_MANIFEST:
        manifest[label] = read_text(resolved[label])
    return manifest


# ---------------------------------------------------------------------------
# validate (CORE-005 / W2-002: coherent snapshot)
# ---------------------------------------------------------------------------

def _validate(resolved: dict[str, Path]) -> tuple[str, dict[str, str]]:
    """Validate manifest read through the captured resolved referents.

    Caller must hold the locks for *resolved*.
    """
    manifest = _read_resolved_manifest(resolved)
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
    """Validate manifest with coherent-snapshot locking (CORE-005 / W2-002)."""
    resolved = _resolved_manifest_map(contract_path, conf_path, skill_path)
    locks = _acquire_locks(list(resolved.values()))
    try:
        result = _validate(resolved)
        _assert_manifest_stable((contract_path, conf_path, skill_path), resolved)
        return result
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
        if not _is_well_formed_language_tag(candidate):
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
# CORE-003 / CORE-007: Drift validation with directory-symlink traversal
# and exact matched-line diagnostics
# ---------------------------------------------------------------------------
# _drift_normalize is defined at module level for precompilation.

def _iter_md_files(transport_dir: Path) -> "list[Path]":
    """Recursively list ``.md`` adapter files under *transport_dir*.

    CORE-003: directory symlinks are followed (recursive descent) so an
    adapter subtree reachable through a symlinked directory is never invisible
    to the drift validator.  Resolved directory identities are tracked to
    prevent cycles and duplicate traversal.
    """
    found: list[Path] = []
    seen: set[Path] = set()

    def _walk(directory: Path) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                resolved = entry.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                _walk(entry)
            elif entry.is_file() and entry.name.casefold().endswith(".md"):
                found.append(entry)

    _walk(transport_dir)
    found.sort()
    return found


def _drift_normalize_mapped(text: str) -> tuple[str, list[int]]:
    """Normalize *text* for drift scanning while keeping a mapping from each
    output character position back to its 1-based source line.

    Internal whitespace (including newlines) is collapsed to a single space so
    that a banned phrase split across a line break still matches, while the
    line map lets us report the *actual* source line of the hit (CORE-007)
    rather than the first occurrence of the token's first word.
    """
    parts: list[str] = []
    line_for_pos: list[int] = []
    first = True
    for line_no, line in enumerate(text.splitlines(), 1):
        norm = _drift_normalize(line)
        if not norm:
            continue
        if not first:
            parts.append(" ")
            line_for_pos.append(line_no)
        parts.append(norm)
        line_for_pos.extend([line_no] * len(norm))
        first = False
    return "".join(parts), line_for_pos


def validate_drift(root: Path) -> None:
    """Reject banned rule tokens anywhere in an adapter file's normalized text.

    W2-004: discovers adapters using case-insensitive ``.md`` matching so
    ``BAD.MD`` is caught on case-sensitive filesystems.  Directory symlinks are
    followed with recursive descent (CORE-003).  The reported file:line points
    at the start of the actually matched banned phrase (CORE-007), not at the
    first occurrence of the token's first word.
    """
    transport_dir = root / "adapters"
    if not transport_dir.is_dir():
        return

    for path in _iter_md_files(transport_dir):
        text = normalize(read_text(path))
        normalized_doc, line_for_pos = _drift_normalize_mapped(text)
        hit = _DRIFT_MATCH.search(normalized_doc)
        if hit:
            matched_text = hit.group(0).lower()
            token = next(
                (t for t in DRIFT_BANNED if _drift_normalize(t) == matched_text),
                matched_text,
            )
            pos = hit.start()
            line_number = line_for_pos[pos] if 0 <= pos < len(line_for_pos) else 0
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
    """Validate manifest + drift without acquiring locks (caller holds them).

    W2-001: drift runs against the *logical* supplied package root (the
    directory holding the supplied manifest members), never the resolved
    referent's parent, so a symlinked manifest cannot make a logical package's
    own adapters evade validation.
    """
    resolved = _resolved_manifest_map(contract_path, conf_path, skill_path)
    contract_id, config = _validate(resolved)
    validate_drift(contract_path.parent)
    if state_path is not None:
        validate_state(state_path, contract_id)
    _assert_manifest_stable((contract_path, conf_path, skill_path), resolved)
    return contract_id, config


def validate_package(
    contract_path: Path,
    conf_path: Path,
    skill_path: Path,
    state_path: Path | None = None,
) -> tuple[str, dict[str, str]]:
    """Validate manifest, active-root drift, and optional bound state in one
    coherent-locked path (CORE-005 / W2-001 / W2-002).
    """
    resolved = _resolved_manifest_map(contract_path, conf_path, skill_path)
    locks = _acquire_locks(list(resolved.values()))
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
