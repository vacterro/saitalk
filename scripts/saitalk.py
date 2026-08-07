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
LANGUAGE_TAG = re.compile(r"^[a-z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$")

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


def _top_level_lines(text: str) -> str:
    """Return only lines outside fenced code blocks (``` or ~~~) and HTML comments.

    Fence close must match the delimiter character that opened it (a ```
    fence cannot be closed by ~~~ and vice versa). A single-line HTML
    comment (`<!-- ... -->` fully on one line) is dropped; a multi-line one
    is dropped until the closing `-->` is seen.
    """
    kept: list[str] = []
    fence_char: str | None = None
    in_comment = False
    for line in text.splitlines():
        stripped = line.strip()
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if fence_char is not None:
            match = FENCE_PATTERN.match(stripped)
            if match and match.group(1)[0] == fence_char:
                fence_char = None
            continue
        match = FENCE_PATTERN.match(stripped)
        if match:
            fence_char = match.group(1)[0]
            continue
        if stripped.startswith("<!--"):
            if "-->" not in stripped[4:]:
                in_comment = True
            continue
        kept.append(line)
    return "\n".join(kept)


def _top_level_headings(text: str) -> list[str]:
    return [m.group(1).rstrip() for m in TOP_HEADING_PATTERN.finditer(_top_level_lines(text))]


def exactly_one(pattern: re.Pattern[str], text: str, label: str) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise SaitalkError(f"{label}: expected exactly one match, found {len(matches)}")
    return matches[0]


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
    """Pair each RUNTIME_MANIFEST label with its path, in canonical order.

    RUNTIME_MANIFEST is the single source of manifest membership and order;
    every caller that needs the (label, path) pairing goes through here.
    """
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
    bytes untouched.

    BOM-aware: a leading UTF-8 BOM is set aside before matching so a
    contract_id on the literal first line still anchors correctly (`^` only
    matches at byte offset 0 or right after a newline; a BOM at offset 0
    would otherwise shift the first line's true start past what `^` sees),
    then the BOM is restored on the result.
    """
    prefix = BOM if data.startswith(BOM) else b""
    body = data[len(prefix):]
    matches = list(CONTRACT_ID_BYTES_PATTERN.finditer(body))
    if len(matches) != 1:
        raise SaitalkError(f"{label}: expected exactly one match, found {len(matches)}")
    match = matches[0]
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
    """Minimal structural conformance guards for the normative contract.

    This checks shape and invariants, never wording: the canonical 12
    numbered section headings must appear exactly once, in order, with their
    exact title text, outside fenced code blocks and HTML comments (so an
    example or an appended unnumbered section cannot pass as structure); §11
    still references the bound state fields and voice commands; §12 still
    documents every required config key; SKILL.md still names SAITALK.md as
    the sole normative source. It is not a semantic proof.
    """
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
    if "SAITALK.md" not in skill_text or "owns all normative behavior" not in skill_text:
        raise SaitalkError(
            "contract structure: SKILL.md must declare SAITALK.md as the "
            "sole normative behavior source"
        )


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
    """Acquire one lock per target in deterministic canonical order."""
    ordered = sorted((target.resolve() for target in targets), key=str)
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


def _write_temp(target: Path, data: bytes) -> Path:
    tmp = target.with_name(target.name + ".tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise SaitalkError(f"write failure: {tmp}: {exc}") from exc
    return tmp


def _replace_temp(target: Path, tmp: Path) -> None:
    try:
        os.replace(tmp, target)
    except OSError as exc:
        raise SaitalkError(f"replacement failure: {target}: {exc}") from exc


def _restore_bytes(target: Path, original: bytes) -> None:
    try:
        tmp = target.with_name(target.name + ".restore.tmp")
        with tmp.open("wb") as fh:
            fh.write(original)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        raise SaitalkError(f"restore failure on {target}: {exc}") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def refresh(contract_path: Path, conf_path: Path, skill_path: Path) -> str:
    """Refresh contract_id from a single authoritative byte snapshot.

    Every manifest member (including SKILL.md) is locked before anything is
    read, and every member is read exactly once, as bytes; both the hash and
    the byte-level replacement are derived from that one snapshot, so there
    is no window where a hashed read and a patched read can observe
    different bytes. Structure is validated against that same snapshot
    before any write, so a structurally invalid package can never be
    blessed with a fresh marker. Rollback restores only the targets this
    call actually replaced.
    """
    _require_coherent_root(contract_path, conf_path, skill_path)
    locks = _acquire_locks([contract_path, conf_path, skill_path])
    try:
        raw: dict[str, bytes] = {}
        for label, path in _manifest_members(contract_path, conf_path, skill_path):
            raw[label] = read_bytes(path)

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

        pairs = (
            (contract_path, new_contract, contract_bytes),
            (conf_path, new_conf, conf_bytes),
        )
        temps: dict[Path, Path] = {}
        replaced: list[Path] = []
        try:
            for target, content, _original in pairs:
                temps[target] = _write_temp(target, content)
            for target, tmp in temps.items():
                _replace_temp(target, tmp)
                replaced.append(target)
        except SaitalkError as primary:
            rollback_failures = []
            for target, _content, original in pairs:
                if target not in replaced:
                    continue
                try:
                    _restore_bytes(target, original)
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


def validate(contract_path: Path, conf_path: Path, skill_path: Path) -> tuple[str, dict[str, str]]:
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


def voice_command(message: str) -> str | None:
    """Return the target voice state for a standalone voice control command.

    A standalone phrase is trimmed of outer whitespace, internal ASCII
    whitespace is collapsed, and the phrase is casefolded before exact
    comparison. Punctuation, quotes, backticks, or extra words make the
    message non-standalone and return None.
    """
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
    """Resolve the effective artifact prose language.

    One global precedence, identical for a fixed and `auto` configuration:

    1. explicit language required by the current artifact task;
    2. existing artifact language when editing;
    3. repository-local artifact language contract;
    4. configured `artifact_language`, or English when configured is `auto`.

    Each explicit source (task, existing, repo) is honored as long as it is
    a well-formed language tag, even when it is not one of the configured
    `en`/`et`/`ru` languages. A malformed or empty explicit value at any of
    the three levels fails loudly instead of being silently ignored. An
    invalid configured value fails loudly instead of being returned.
    """
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
    return configured


def _drift_normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


DRIFT_TOKENS = tuple(_drift_normalize(token) for token in DRIFT_BANNED)


def validate_drift(root: Path) -> None:
    """Reject banned rule tokens anywhere in an adapter file's normalized text.

    Normalization runs over the whole document, not line by line, so a
    banned phrase split across a markdown line-wrap (`completion-\\nfirst`)
    cannot evade the guard. The reported line number is best-effort (the
    first line containing the token's first word).
    """
    transport_dir = root / "adapters"
    if not transport_dir.is_dir():
        return
    for path in sorted(transport_dir.rglob("*.md")):
        text = normalize(read_text(path))
        lines = text.splitlines()
        normalized_doc = _drift_normalize(text)
        for token in DRIFT_TOKENS:
            if re.search(rf"\b{re.escape(token)}\b", normalized_doc):
                first_word = token.split()[0]
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


def validate_package(
    contract_path: Path,
    conf_path: Path,
    skill_path: Path,
    state_path: Path | None = None,
) -> tuple[str, dict[str, str]]:
    """Validate manifest, active-root drift, and optional bound state in one path."""
    contract_id, config = validate(contract_path, conf_path, skill_path)
    validate_drift(contract_path.resolve().parent)
    if state_path is not None:
        validate_state(state_path, contract_id)
    return contract_id, config


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

        if args.command == "print-id":
            contract_id, _ = validate(contract_path, conf_path, skill_path)
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
