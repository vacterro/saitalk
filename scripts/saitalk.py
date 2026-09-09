#!/usr/bin/env python3
"""Zero-dependency validator for the portable SAITALK contract."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import os
import re
import stat
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
_GRANDFATHERED_LANGUAGE_TAGS = frozenset({
    "art-lojban", "cel-gaulish", "en-gb-oed", "i-ami", "i-bnn",
    "i-default", "i-enochian", "i-hak", "i-klingon", "i-lux", "i-mingo",
    "i-navajo", "i-pwn", "i-tao", "i-tay", "i-tsu", "no-bok", "no-nyn",
    "sgn-be-fr", "sgn-be-nl", "sgn-ch-de", "zh-guoyu", "zh-hakka",
    "zh-min", "zh-min-nan", "zh-xiang",
})


def _is_well_formed_language_tag(tag: str) -> bool:
    normalized = tag.casefold()
    if normalized in _GRANDFATHERED_LANGUAGE_TAGS:
        return True
    parts = normalized.split("-")
    if not parts or any(not part for part in parts):
        return False
    if parts[0] == "x":
        return len(parts) > 1 and all(re.fullmatch(r"[a-z0-9]{1,8}", part) for part in parts[1:])
    if re.fullmatch(r"[a-z]{4,8}", parts[0]):
        index = 1
    elif re.fullmatch(r"[a-z]{2,3}", parts[0]):
        index = 1
        extlangs = 0
        while index < len(parts) and re.fullmatch(r"[a-z]{3}", parts[index]) and extlangs < 3:
            index += 1
            extlangs += 1
    else:
        return False

    if index < len(parts) and re.fullmatch(r"[a-z]{4}", parts[index]):
        index += 1
    if index < len(parts) and (
        re.fullmatch(r"[a-z]{2}", parts[index])
        or re.fullmatch(r"[0-9]{3}", parts[index])
    ):
        index += 1
    variants: set[str] = set()
    while index < len(parts) and (
        re.fullmatch(r"[a-z0-9]{5,8}", parts[index])
        or re.fullmatch(r"[0-9][a-z0-9]{3}", parts[index])
    ):
        if parts[index] in variants:
            return False
        variants.add(parts[index])
        index += 1
    singletons: set[str] = set()
    while index < len(parts) and re.fullmatch(r"[a-wyz0-9]", parts[index]):
        singleton = parts[index]
        if singleton in singletons:
            return False
        singletons.add(singleton)
        index += 1
        start = index
        while index < len(parts) and re.fullmatch(r"[a-z0-9]{2,8}", parts[index]):
            index += 1
        if index == start:
            return False
    if index < len(parts) and parts[index] == "x":
        index += 1
        start = index
        while index < len(parts) and re.fullmatch(r"[a-z0-9]{1,8}", parts[index]):
            index += 1
        if index == start:
            return False
    return index == len(parts)

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

_HTML_COMMENT_OPEN = "<!--"
_HTML_COMMENT_CLOSE = "-->"


def _scan_code_spans(line: str) -> list[tuple[int, int]]:
    """Return [start, end) byte offsets of every inline code span in *line*.

    A code span is a run of N>=1 backticks; it ends at the next run of N
    backticks.  An unmatched run is not a code span (CommonMark rule: an
    inline code span cannot contain a backtick run of equal or greater
    length than the opener).  We use a length-based scan that handles
    the simple CommonMark case (no escaping, no nested backticks inside
    the run) -- sufficient for the comment/fence boundary check the
    validator needs.

    PERF-001 (SRC-002:R008): the backtick runs are tokenized ONCE, and
    each run's next same-length successor is found in ONE reverse pass
    with a ``run_len -> next run index`` map. The previous algorithm
    rescanned the remaining suffix text for every unmatched opener, so a
    descending sequence of unique run lengths made the scan quadratic --
    an ~321 KB line of unmatched runs cost seconds. The greedy walk below
    emits spans in one left-to-right pass over the run list, identical
    span semantics: opener at run i pairs with the next run of the same
    length, then the scan resumes after it; an unmatched opener is plain
    text and the walk advances one run.
    """
    if "`" not in line:
        return []
    runs: list[tuple[int, int, int]] = []  # (start, end, run_len)
    i = 0
    n = len(line)
    while i < n:
        if line[i] != "`":
            i += 1
            continue
        start = i
        while i < n and line[i] == "`":
            i += 1
        runs.append((start, i, i - start))
    # next_same[j] = index of the next run with the same length as run j,
    # computed in one reverse pass so a length's last seen successor is
    # always the nearest one to the right of j.
    next_same: list[int] = [-1] * len(runs)
    seen: dict[int, int] = {}
    for j in range(len(runs) - 1, -1, -1):
        next_same[j] = seen.get(runs[j][2], -1)
        seen[runs[j][2]] = j
    spans: list[tuple[int, int]] = []
    j = 0
    while j < len(runs):
        k = next_same[j]
        if k != -1:
            spans.append((runs[j][0], runs[k][1]))
            j = k + 1
        else:
            j += 1
    return spans


def _strip_html_comments_preserving_spans(
    line: str, in_comment: bool
) -> tuple[str, bool]:
    """Remove ``<!--`` ... ``-->`` runs from *line*; HTML-comment delimiters
    that fall inside a backtick code span are kept literally.

    Returns ``(out, still_in_comment)`` where *out* contains the same
    characters the caller would have obtained from a naive elision, but
    code-span content is preserved verbatim, so a backtick-opener carrying
    a backtick inside its info string is still recognised as text.

    R001/PERF-001: the code spans are scanned ONCE, in absolute
    coordinates over the whole original line, and consumed by a single
    monotonically advancing index.  Rebasing spans onto ``line[cursor:]``
    after every same-line comment close made every later offset
    comparison mix two coordinate systems -- a literal ``<!--`` inside an
    inline code span was then read as a real comment opener, hiding the
    live headings and state fields that followed -- and re-scanned a
    shorter suffix per comment, which is quadratic in line length.

    PERF-001 (SRC-002:R008): the common case gets a fast exit. When no
    comment is being carried and the line contains no ``<!--`` opener,
    there is nothing to strip and no span the elision logic needs: the
    line is returned unchanged without the backtick-run scan. The
    shipped contract documents contain zero comment openers, so this
    removes the expensive disambiguation work from every ordinary line.
    A line that DOES carry an opener (or a carried-over comment) still
    takes the full parser below, unchanged.
    """
    if not in_comment and _HTML_COMMENT_OPEN not in line:
        return line, False
    # Bytes still inside a carried-over comment are comment content, not
    # inline text, so span discovery starts after that comment closes.
    # The offsets are rebased to absolute ONCE, here, and never again.
    scan_base = 0
    if in_comment:
        close = line.find(_HTML_COMMENT_CLOSE)
        scan_base = len(line) if close == -1 else close + len(_HTML_COMMENT_CLOSE)
    spans = [
        (start + scan_base, end + scan_base)
        for start, end in _scan_code_spans(line[scan_base:])
    ]
    out: list[str] = []
    cursor = 0
    span_idx = 0
    while cursor < len(line):
        if in_comment:
            close = line.find(_HTML_COMMENT_CLOSE, cursor)
            if close == -1:
                cursor = len(line)
                break
            cursor = close + len(_HTML_COMMENT_CLOSE)
            in_comment = False
            continue
        # Advance the span cursor to the first span that can still cover
        # `cursor`; spans are sorted and non-overlapping, so this index
        # only ever moves forward across the whole line.
        while span_idx < len(spans) and spans[span_idx][1] <= cursor:
            span_idx += 1
        if span_idx < len(spans) and spans[span_idx][0] <= cursor:
            # Inside a code span: copy it out verbatim, delimiters and all.
            span_end = spans[span_idx][1]
            out.append(line[cursor:span_end])
            cursor = span_end
            continue
        # Live text: it runs until the next comment opener, and any opener
        # inside the upcoming code span is literal text, not a delimiter.
        next_span_start = spans[span_idx][0] if span_idx < len(spans) else len(line)
        next_open = line.find(_HTML_COMMENT_OPEN, cursor)
        if next_open == -1 or next_open >= next_span_start:
            out.append(line[cursor:next_span_start])
            cursor = next_span_start
            continue
        out.append(line[cursor:next_open])
        cursor = next_open + len(_HTML_COMMENT_OPEN)
        in_comment = True
    return "".join(out), in_comment


def _leading_columns(line: str) -> int:
    """Return the column width of leading spaces/tabs in *line*.

    Tabs expand to the next multiple of four columns; a tab therefore
    advances the cursor by ``(leading // 4 + 1) * 4 - leading`` columns.
    """
    leading = 0
    cursor = 0
    while cursor < len(line) and line[cursor] in (" ", "\t"):
        if line[cursor] == " ":
            leading += 1
        else:
            leading = (leading // 4 + 1) * 4
        cursor += 1
    return leading, cursor


def _fence_opener_span(line: str, closing: bool = False) -> tuple[int, int, str] | None:
    """Return ``(indent_cols, run_len, delim)`` for a valid fence opener at
    the start of *line*, or ``None`` if *line* is not a fence opener.

    A fence opener is 0-3 leading columns of spaces/tabs followed by a
    run of 3+ identical `` or ~ characters.  A backtick opener is invalid
    if its info string contains a backtick (CommonMark: a backtick run
    that opens a code span cannot simultaneously close a fence).  A
    *closing* fence (the second argument) must carry no info string
    except whitespace; a backtick run followed by ``extra text`` does not
    close a fence (CommonMark).
    """
    leading, cursor = _leading_columns(line)
    if leading > 3 or cursor >= len(line):
        return None
    delim = line[cursor]
    if delim not in ("`", "~"):
        return None
    run_start = cursor
    while cursor < len(line) and line[cursor] == delim:
        cursor += 1
    run_len = cursor - run_start
    if run_len < 3:
        return None
    info = line[cursor:].rstrip()
    if delim == "`" and "`" in info:
        return None
    if closing and info.strip():
        return None
    return leading, run_len, delim


def _top_level_lines(text: str) -> str:
    """Return only the *live* top-level lines of *text*.

    A line is live when it is outside fenced code blocks (``` or ~~~) and
    outside HTML comments.  The following Markdown boundaries are honoured
    (CORE-001 / CORE-002 / W2-001):

    * **Inline code spans** – backtick code spans on a non-fenced line are
      recognised and their content is preserved verbatim.  An HTML-comment
      delimiter (``<!--`` or ``-->``) inside a code span is literal text,
      not a comment opener/closer.  Conversely, a fence opener whose info
      string contains a backtick is invalid: the backtick opens a code
      span and the run is non-fence text.
    * **Fenced blocks** – fence validity (opener/closer, info string,
      trailing text) is evaluated against the *original* line bytes before
      any HTML-comment elision.  An invalid backtick opener (carrying a
      backtick) or a closing fence carrying non-whitespace info is
      rejected on the original source span, so removing comment bytes can
      never synthesise a fence that did not exist.  Four-space (or wider)
      indented code is an indented-code construct, never a fence, so it
      cannot mask live headings.
    * **HTML comments** – both ``<!--`` ... ``-->`` spanning multiple
      lines and same-line comments are recognised.  Any live text
      *before* an inline comment and *after* its closing ``-->`` is
      preserved, so an inline comment can never hide a live heading that
      precedes it.  Comment content inside a backtick code span is
      preserved verbatim.
    """
    kept: list[str] = []
    fence_char: str | None = None
    fence_len: int = 0
    in_comment = False

    for raw_line in text.splitlines():
        if in_comment:
            close_idx = raw_line.find(_HTML_COMMENT_CLOSE)
            if close_idx == -1:
                continue
            in_comment = False
            head = raw_line[close_idx + len(_HTML_COMMENT_CLOSE):]
        else:
            head = raw_line
        if fence_char is not None:
            opener = _fence_opener_span(head, closing=True)
            if opener is not None and opener[2] == fence_char and opener[1] >= fence_len:
                fence_char = None
                fence_len = 0
            continue
        # CORE-001: fence validity is decided on the ORIGINAL head bytes;
        # HTML-comment elision must never create or destroy a fence.
        opener = _fence_opener_span(head)
        stripped, in_comment = _strip_html_comments_preserving_spans(head, in_comment)
        if not stripped.strip():
            continue
        if stripped != head and _fence_opener_span(stripped) is not None:
            # Removing comment bytes synthesised a fence out of two halves;
            # neither the original nor the synthesised run is a fence, and
            # the line carries no live content the validator can trust.
            continue
        if opener is not None:
            fence_char = opener[2]
            fence_len = opener[1]
            continue
        kept.append(stripped)

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


def _require_coherent_root(
    contract_path: Path, conf_path: Path, skill_path: Path
) -> Path:
    paths = (contract_path, conf_path, skill_path)
    logical_parents = {Path(os.path.abspath(path.parent)) for path in paths}
    if len(logical_parents) != 1:
        raise SaitalkError("manifest members do not form one coherent package root")
    return next(iter(logical_parents))


def _validate_resolved_manifest(resolved: dict[str, Path]) -> None:
    for a, b in itertools.combinations(resolved.values(), 2):
        try:
            if a == b or os.path.samefile(a, b):
                raise SaitalkError("manifest members alias the same file")
        except FileNotFoundError:
            # T-914: NOT an oversight, and deliberately not raised here. This
            # runs before any member has been read, so a missing member is the
            # common case and `samefile` is simply unable to answer. Reporting
            # it here would replace the specific, actionable diagnostic the
            # subsequent read produces -- `missing file: <path>`, naming the
            # member -- with a vague "cannot compare manifest paths", which is
            # exactly the error a caller cannot act on. Two CLI regressions pin
            # that wording (CliTests.test_missing_file_exit_one_no_traceback and
            # ConfigTests.test_missing_file_fails), so this branch is covered by
            # the outcome it protects rather than by a test of its own. A
            # genuine comparison error still raises via the OSError branch.
            pass
        except OSError as exc:
            raise SaitalkError(f"cannot compare manifest paths: {exc}") from exc
    if len({path.parent for path in resolved.values()}) > 1:
        raise SaitalkError("manifest members do not form one coherent package root")


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
    resolved = _resolved_manifest_map(contract_path, conf_path, skill_path)
    return _read_resolved_manifest(resolved)


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
        # T-913: a follow-up close failure must not mask the lock error.
        # This is the same cleanup policy _release_lock documents below:
        # swallow OSError only, so a non-OSError cancellation during cleanup
        # (KeyboardInterrupt, SystemExit) still propagates instead of being
        # reclassified as lock contention. `except BaseException: pass` here
        # previously ate a cancellation and reported contention.
        try:
            fh.close()
        except OSError:
            pass
        raise SaitalkError(
            f"lock contention on {target}: another refresh is in progress"
        ) from exc
    return fh


def _release_lock(fh) -> None:
    # W2-005: a cleanup error must never replace an already-active primary
    # exception. Swallow OSError from unlock and from close so a primary
    # SaitalkError/OpsError raised earlier stays visible. Both branches use
    # bare ``except OSError`` so a non-OSError cancellation during cleanup
    # still propagates.
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


def _acquire_locks(targets: list[Path]) -> list:
    """Acquire one lock per target in deterministic canonical order.

    Accepts both original (possibly symlinked) paths and already-resolved
    paths.  Sorts by resolved form; the lock acquisition order is always
    the same for the same physical files regardless of symlink topology.

    W2-002 (SRC-002:R005): the aggregate cleanup catches ``BaseException``,
    not only ``SaitalkError``. A ``KeyboardInterrupt`` or ``SystemExit``
    arriving during the second acquisition used to bypass the release of
    already-held handles, so cleanup depended on CPython object
    destruction: a host that catches and retains the cancellation (its
    traceback keeps the handles alive) stayed locked and self-deadlocked
    later work. Cleanup now releases every earlier handle in reverse order,
    continues past release failures, and re-raises the PRIMARY exception
    unchanged, so cancellation identity and contention semantics are
    preserved.
    """
    ordered = sorted(targets, key=lambda t: str(t.resolve()))
    handles = []
    try:
        for target in ordered:
            handles.append(_acquire_lock(target))
    except BaseException as primary:
        cleanup_failures: list[str] = []
        for fh in reversed(handles):
            try:
                _release_lock(fh)
            except SaitalkError as exc:
                cleanup_failures.append(str(exc))
            except BaseException as exc:
                cleanup_failures.append(f"{type(exc).__name__}: {exc}")
        # The primary stays the controlling error; cleanup failures are
        # attached as context, never allowed to replace it.
        if cleanup_failures:
            raise primary from SaitalkError(
                "lock cleanup incomplete: " + "; ".join(cleanup_failures)
            )
        raise
    return handles


def _release_locks(handles) -> None:
    # W2-005: every acquired handle receives a release attempt even if an
    # earlier cleanup raised. The first exception observed is re-raised so
    # a primary caller still sees *some* cleanup signal if it asked for one.
    first_exc: BaseException | None = None
    for fh in reversed(handles):
        try:
            _release_lock(fh)
        except BaseException as exc:
            if first_exc is None:
                first_exc = exc
    if first_exc is not None:
        raise first_exc


# ---------------------------------------------------------------------------
# CORE-001 / W2-002: Secure temporary file creation with permission
# preservation.  Uses tempfile.mkstemp (O_CREAT|O_EXCL) so no pre-existing
# hardlink/symlink at a predictable sibling path can be followed.
# ---------------------------------------------------------------------------

def _safe_write_temp(target: Path, data: bytes, mode: int | None = None) -> Path:
    """Create a unique temporary file in the same directory as *target*,
    write *data*, flush, fsync, and return the path.  If *mode* is given,
    final file permissions are applied after content writing (POSIX).

    CORE-005: any filesystem failure (``mkstemp``, ``fchmod``, write, flush,
    ``fsync``) is normalized into a ``SaitalkError`` carrying the target and
    operation context, so the public ``refresh``/CLI error contract stays
    uniform instead of leaking a raw ``OSError`` traceback.  The descriptor is
    closed on any failure that happens before ``os.fdopen`` takes ownership.

    R010/W2-001: process-control exceptions are NOT normalized.  Cleanup
    runs identically for them, but ``KeyboardInterrupt``, ``SystemExit`` and
    any other non-``Exception`` ``BaseException`` are re-raised unchanged,
    so Ctrl+C during staging stays a cancellation instead of being
    reclassified as an ordinary refresh error.  ``refresh``'s rollback
    handler catches ``BaseException`` precisely so it can roll back and then
    let the original cancellation propagate; converting the type here
    destroyed that and broke the documented cancellation-identity
    guarantee.
    """
    dir_path = target.parent
    prefix = f"{target.name}."
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), prefix=prefix)
    except OSError as exc:
        raise SaitalkError(f"staging failure for {target}: {exc}") from exc
    fd_owned = True
    try:
        fh = os.fdopen(fd, "wb")
        fd_owned = False
        with fh:
            fh.write(data)
            fh.flush()
            if mode is not None and os.name != "nt":
                os.fchmod(fh.fileno(), mode)
            os.fsync(fh.fileno())
    except BaseException as exc:
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        if not isinstance(exc, Exception):
            raise
        raise SaitalkError(f"staging failure for {target}: {exc}") from exc
    return Path(tmp_path)


def _replace_temp(target: Path, tmp: Path) -> None:
    try:
        os.replace(tmp, target)
    except OSError as exc:
        raise SaitalkError(f"replacement failure: {target}: {exc}") from exc


def _restore_bytes(target: Path, original: bytes, mode: int | None = None) -> None:
    tmp: Path | None = None
    failure: SaitalkError | None = None
    try:
        tmp = _safe_write_temp(target, original, mode=mode)
        os.replace(tmp, target)
    except OSError as exc:
        failure = SaitalkError(f"restore failure on {target}: {exc}")
    except SaitalkError as exc:
        failure = SaitalkError(f"restore failure on {target}: {exc}")
    if tmp is not None:
        try:
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            cleanup = f"restore cleanup failure on {target}: {exc}"
            if failure is None:
                failure = SaitalkError(cleanup)
            else:
                failure = SaitalkError(f"{failure}; {cleanup}")
    if failure is not None:
        raise failure


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
    and applied to every staged file (     and every rollback file), after content writes complete.


    PERF-003: targets whose replacement bytes are identical to the original
    bytes are skipped entirely -- no staging, no fsync, no inode churn.
    """
    resolved_map = _resolved_manifest_map(contract_path, conf_path, skill_path)

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

        # W2-001: assert manifest stability *before* any writes so a
        # retargeted symlink that was swapped between resolve and lock
        # acquisition is caught before we mutate any file.
        _assert_manifest_stable((contract_path, conf_path, skill_path), resolved_map)

        if not pairs:
            return new_id

        temps: dict[Path, Path] = {}
        replaced: list[Path] = []
        try:
            for target, content, _original in pairs:
                temps[target] = _safe_write_temp(target, content, mode=modes_map.get(target))
            for target, tmp in temps.items():
                _replace_temp(target, tmp)
                replaced.append(target)
            # CORE-001 (SRC-002:R001): the final stability check runs INSIDE
            # the rollback-protected region. A late logical-manifest retarget
            # is detected here after the physical replacements, and this exit
            # path must roll back every member this invocation replaced --
            # otherwise refresh reports failure while leaving the captured
            # physical package mutated. The pre-write check at the W2-001
            # site above is unchanged and still catches the earlier window.
            _assert_manifest_stable((contract_path, conf_path, skill_path), resolved_map)

        except BaseException as primary:
            # W2-002: any unsuccessful exit (including cancellation,
            # OSError, programming errors) restores exactly the targets
            # this invocation already replaced.
            rollback_failures: list[str] = []
            for target, _content, original in pairs:
                if target not in replaced:
                    continue
                try:
                    _restore_bytes(target, original, mode=modes_map.get(target))
                except SaitalkError as exc:
                    rollback_failures.append(f"{target}: {exc}")
                except BaseException as exc:
                    rollback_failures.append(f"{target}: {type(exc).__name__}: {exc}")
            if rollback_failures:
                chained = SaitalkError(
                    f"{primary}; rollback incomplete: " + "; ".join(rollback_failures)
                )
                raise chained from primary
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

def _resolved_manifest_map(
    contract_path: Path, conf_path: Path, skill_path: Path
) -> dict[str, Path]:
    """Resolve manifest members once, then validate captured physical identity."""
    _require_coherent_root(contract_path, conf_path, skill_path)
    paths = (contract_path, conf_path, skill_path)
    try:
        resolved = {
            label: path.resolve()
            for label, path in zip(RUNTIME_MANIFEST, paths)
        }
    except OSError as exc:
        raise SaitalkError(f"cannot resolve manifest path: {exc}") from exc
    _validate_resolved_manifest(resolved)
    _manifest_members(contract_path, conf_path, skill_path)
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


def _decode_manifest_bytes(label: str, blob: bytes) -> str:
    """Decode one captured manifest member exactly as :func:`read_text` would.

    R011/W2-002: ``validate_package`` captures manifest BYTES under its
    phase-1 lock and must validate those bytes rather than re-reading the
    paths, so the returned ``contract_id`` and the phase-3 stability
    baseline always come from one snapshot.
    """
    try:
        return blob.decode("utf-8").removeprefix("\ufeff")
    except UnicodeDecodeError as exc:
        raise SaitalkError(f"not UTF-8: {label}") from exc


# ---------------------------------------------------------------------------
# validate (CORE-005 / W2-002: coherent snapshot)
# ---------------------------------------------------------------------------

def _validate(resolved: dict[str, Path]) -> tuple[str, dict[str, str]]:
    """Validate manifest read through the captured resolved referents.

    Caller must hold the locks for *resolved*.
    """
    return _validate_manifest(_read_resolved_manifest(resolved))


def _validate_manifest(manifest: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Validate one already-captured manifest text snapshot (R011/W2-002).

    This is the only place the contract/config/structure verdict is
    computed, so the ID returned to a caller always belongs to the exact
    text that produced it.  ``_validate`` above is the read-then-validate
    convenience wrapper; ``validate_package`` passes its phase-1 capture
    directly.
    """
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
    validate_state_bytes(read_bytes(path), expected)


def validate_state_bytes(blob: bytes, expected: str) -> None:
    """Validate a state file from a captured byte snapshot (W2-003).

    Equivalent to ``validate_state`` but reads from *blob* so the caller
    can prove a later on-disk change cannot affect the verdict.
    """
    try:
        text = blob.decode("utf-8").removeprefix("\ufeff")
    except UnicodeDecodeError as exc:
        raise SaitalkError(f"state not UTF-8: {exc}") from exc
    normalized = normalize(text)
    top = _top_level_lines(normalized)
    live_fields = set(re.findall(r"(?m)^\s*(saitalk_[a-z0-9_]+):", top))
    allowed_fields = {"saitalk_contract", "saitalk_status", "saitalk_voice"}
    extra_fields = live_fields - allowed_fields
    if extra_fields:
        raise SaitalkError(
            "unknown live SAITALK state fields: " + ", ".join(sorted(extra_fields))
        )
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
    return configured


# ---------------------------------------------------------------------------
# CORE-003 / CORE-007: Drift validation with directory-symlink traversal
# and exact matched-line diagnostics
# ---------------------------------------------------------------------------
# _drift_normalize is defined at module level for precompilation.

def _sorted_dir_entries(directory: Path) -> list[tuple[str, str, bool, bool]]:
    """Return one directory's entries as ``(sort_key, name, is_dir, is_file)``,
    sorted.

    ``os.scandir`` is used so file/directory classification comes from the
    cached ``DirEntry`` instead of a fresh ``os.stat`` per entry, and only
    the names are retained rather than ``Path`` objects.

    CORE-002 (SRC-002:R002): a symlink whose target cannot be resolved is
    neither ``is_dir()`` nor ``is_file()`` -- both follow the link -- so a
    dangling adapter entry would previously fall through the walker as
    neither-file-nor-directory and silently vanish from the drift scan
    while ``validate_package`` still reported PASS. The classification now
    carries ``is_symlink()`` in ``is_dir=False, is_file=False`` pairs and
    the walker refuses such entries by relative path instead of skipping
    them. Valid symlinks classify through their targets as before.

    The sort key appends ``/`` to directory names so the order matches
    lexical order of the RELATIVE PATHS the caller will report: ``z.md``
    precedes ``z/a.md`` because ``.`` sorts before ``/``, while a plain
    name sort would put the directory first.
    """
    try:
        with os.scandir(directory) as scan:
            entries = []
            for entry in scan:
                is_dir = entry.is_dir()
                is_file = False if is_dir else entry.is_file()
                if is_dir:
                    entries.append((entry.name + "/", entry.name, True, False))
                    continue
                if is_file:
                    entries.append((entry.name, entry.name, False, True))
                    continue
                if entry.is_symlink():
                    # Exists by name, resolves to nothing: a refusal, not a
                    # skip. Kept out of is_dir/is_file so the walker can tell
                    # "unresolvable" from "unrelated regular file".
                    entries.append((entry.name, entry.name, False, False))
                    continue
                # An ordinary non-directory, non-Markdown file (or socket
                # etc.): retain as a non-adapter entry the walker ignores.
                entries.append((entry.name, entry.name, False, False))
    except OSError as exc:
        raise SaitalkError(f"cannot inspect adapter directory {directory}: {exc}") from exc
    entries.sort(key=lambda item: item[0])
    return entries


def _iter_md_files(transport_dir: Path):
    """Yield Markdown adapters in GLOBAL lexical path order, following
    directories safely.

    R017/W2-008/PERF-002: the ordering contract is now the one the callers
    actually rely on. The previous traversal yielded a directory's own
    files before descending, so ``adapters/z.md`` was reported before
    ``adapters/a/a.md`` even though the latter is lexically first, and
    ``validate_drift``'s fail-fast diagnostic depended on tree depth.
    Directories and files are merged into ONE ordering, so the file
    reported first is the lexically first one in the whole tree.

    Memory, stated honestly: global lexical order cannot be produced
    without knowing all of a directory's sibling NAMES, so one directory's
    entry names are held at a time (``os.scandir``, cached
    classification, a single sort, no ``Path`` objects). What is never
    materialized is the tree: at most one sorted name list per level of
    depth is live, and a lexically first invalid adapter stops the walk
    before any later directory is opened.

    Cycle detection uses ``(st_dev, st_ino)`` rather than
    ``Path.resolve()``, and symlinked directories are traversed.
    """
    try:
        root_stat = os.stat(transport_dir, follow_symlinks=True)
    except OSError as exc:
        raise SaitalkError(f"cannot stat adapter path {transport_dir}: {exc}") from exc
    seen: set[tuple[int, int]] = {(root_stat.st_dev, root_stat.st_ino)}

    # Each stack frame is one directory's remaining sorted entries, so the
    # walk descends as soon as a directory sorts next -- it never has to
    # finish a level first.
    stack: list[tuple[Path, list[tuple[str, str, bool, bool]], int]] = [
        (transport_dir, _sorted_dir_entries(transport_dir), 0)
    ]
    while stack:
        directory, entries, index = stack.pop()
        while index < len(entries):
            _key, name, is_dir, is_file = entries[index]
            index += 1
            entry_path = directory / name
            if is_dir:
                try:
                    target_stat = os.stat(entry_path, follow_symlinks=True)
                except OSError as exc:
                    raise SaitalkError(
                        f"cannot classify adapter path {entry_path}: {exc}"
                    ) from exc
                entry_id = (target_stat.st_dev, target_stat.st_ino)
                if entry_id in seen:
                    continue
                seen.add(entry_id)
                stack.append((directory, entries, index))
                directory, entries, index = (
                    entry_path,
                    _sorted_dir_entries(entry_path),
                    0,
                )
                continue
            if not is_file:
                # CORE-002 (SRC-002:R002): an entry that is neither a
                # resolvable directory nor a resolvable file but EXISTS by
                # name is a dangling symlink -- refusing it, naming the
                # relative path, is the point of the drift gate. An unrelated
                # non-regular entry (socket, fifo) is not an adapter and is
                # ignored as before.
                if os.path.islink(entry_path):
                    raise SaitalkError(
                        f"adapter symlink does not resolve: {entry_path}"
                    )
                continue
            if name.casefold().endswith(".md"):
                yield entry_path


def _raise_drift(path: Path, root: Path, line_no: int, normalized_text: str) -> None:
    """Raise a drift error for a matched banned token in *normalized_text*.

    The reported line is *line_no* (1-based source line).  The token is
    identified by matching the normalized form against DRIFT_BANNED.
    """
    hit = _DRIFT_MATCH.search(normalized_text)
    if hit is None:
        return
    matched_text = hit.group(0).lower()
    token = next(
        (t for t in DRIFT_BANNED if _drift_normalize(t) == matched_text),
        matched_text,
    )
    raise SaitalkError(
        f"drift: {path.relative_to(root)}:{line_no} contains "
        f"rule token {token!r}; this is a structural drift guard, "
        "not proof of semantic conformance. Transport files must "
        "not restate or extend the contract"
    )


def validate_drift(root: Path) -> None:
    """Reject banned rule tokens anywhere in an adapter file's normalized text.

    W2-004: discovers adapters using case-insensitive ``.md`` matching so
    ``BAD.MD`` is caught on case-sensitive filesystems.  Directory symlinks are
    followed with recursive descent (CORE-003).  The reported file:line points
    at the start of the actually matched banned phrase (CORE-007), not at the
    first occurrence of the token's first word.

    PERF-001: each adapter is opened directly, read one line at a time with
    universal newline handling, and normalized in place.  No whole-file
    ``read_text`` + ``splitlines`` materialization.  Cross-line tokens are
    caught by retaining only the previous normalized line.

    **An absent ``adapters/`` is nothing to scan, and that is a stated
    contract, not an oversight**: adapters are transport and non-normative
    (``SKILL.md``, ``references/INTERPRETATION.md``), so a package need not
    ship any and no document requires the directory.

    T-912: a ``adapters`` entry that EXISTS but does not resolve to a
    directory is the opposite case, and it is a refusal. ``stat()`` follows
    symlinks, so a dangling ``adapters`` symlink previously raised
    ``FileNotFoundError`` and took the nothing-to-scan path -- something
    clearly intended adapters to be there, and the entire drift gate went
    silently dead. ``lstat`` answers "does the NAME exist" without following,
    which is what separates the two cases.
    """
    transport_dir = root / "adapters"
    try:
        os.lstat(transport_dir)
    except FileNotFoundError:
        # No such entry at all: legitimately nothing to scan.
        return
    except OSError as exc:
        raise SaitalkError(f"cannot inspect adapter path {transport_dir}: {exc}") from exc
    try:
        transport_mode = transport_dir.stat().st_mode
    except FileNotFoundError as exc:
        raise SaitalkError(
            f"adapter path exists but does not resolve: {transport_dir}; "
            "a dangling adapters link disables the drift scan"
        ) from exc
    except OSError as exc:
        raise SaitalkError(f"cannot inspect adapter path {transport_dir}: {exc}") from exc
    if not stat.S_ISDIR(transport_mode):
        raise SaitalkError(f"adapter path is not a directory: {transport_dir}")

    for path in _iter_md_files(transport_dir):
        prev_norm = ""
        prev_line_no = 0
        try:
            with path.open("r", encoding="utf-8", errors="strict", newline="") as fh:
                # Universal-newline translation: split on the actual line
                # terminators present in the file (\n, \r, \r\n) without
                # materializing the whole file.
                for line_no, raw in enumerate(fh, 1):
                    line = raw.rstrip("\r\n")
                    norm = _drift_normalize(line)
                    if norm and _DRIFT_MATCH.search(norm):
                        _raise_drift(path, root, line_no, norm)
                    if (
                        prev_norm
                        and norm
                        and _DRIFT_MATCH.search(prev_norm + " " + norm)
                    ):
                        _raise_drift(path, root, prev_line_no, prev_norm + " " + norm)
                    if norm:
                        prev_norm = norm
                        prev_line_no = line_no
        except UnicodeDecodeError as exc:
            raise SaitalkError(
                f"drift: {path.relative_to(root)}: not UTF-8: {exc}"
            ) from exc
        except OSError as exc:
            raise SaitalkError(
                f"drift: {path.relative_to(root)}: cannot read: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# W2-003 / CORE-004: validate_package
# ---------------------------------------------------------------------------

def _stat_state(state_path: Path) -> os.stat_result:
    """``os.stat`` for a bound state file with normalized errors (R004).

    Raw ``FileNotFoundError``/``PermissionError``/``OSError`` from here
    reached ``main()``, which only catches :class:`SaitalkError`, so a
    missing or unreadable ``--state`` argument crashed the public CLI
    with a traceback instead of one ``SAITALK FAIL:`` line.
    """
    try:
        return os.stat(state_path, follow_symlinks=True)
    except FileNotFoundError as exc:
        raise SaitalkError(f"missing state file: {state_path}") from exc
    except PermissionError as exc:
        raise SaitalkError(f"permission denied: {state_path}") from exc
    except OSError as exc:
        raise SaitalkError(f"cannot stat state file {state_path}: {exc}") from exc


def _capture_state_snapshot(state_path: Path) -> tuple[Path, bytes, tuple[int, int, int], float]:
    """Capture the logical path, file identity and bytes for *state_path*.

    Returns ``(path, bytes, stat_identity, mtime)`` where ``stat_identity``
    is ``(st_dev, st_ino, st_size)`` and ``mtime`` is the file's last
    modification time.  A later disagreement is the W2-003 "state changed
    during validation" signal; the bytes are what decide it (R012), the
    metadata only sharpens the diagnostic.

    R004: every filesystem failure here is normalized into
    :class:`SaitalkError`, because this is the first thing the CLI touches
    for a user-supplied ``--state`` path.
    """
    stat = _stat_state(state_path)
    return (
        state_path,
        read_bytes(state_path),
        (stat.st_dev, stat.st_ino, stat.st_size),
        stat.st_mtime,
    )


def _state_snapshot_unchanged(snapshot) -> bool:
    """Is the bound state still byte-identical to its captured snapshot?

    R012/W2-003: the comparison is CONTENT-first. Metadata alone was
    defeatable -- rewriting the same inode in place with a same-length
    payload and restoring ``st_mtime`` via ``os.utime(..., ns=...)`` left
    ``(st_dev, st_ino, st_size, st_mtime)`` identical, so an already
    invalid state certified as fresh. Timestamps are evidence about a
    file, never proof about its bytes.

    A state file that disappeared or became unreadable mid-validation is
    "changed", not an error: the caller turns this into the canonical
    ``state changed during validation`` refusal.
    """
    path, captured, identity, mtime = snapshot
    try:
        current = os.stat(path, follow_symlinks=True)
        current_bytes = path.read_bytes()
    except OSError:
        return False
    if current_bytes != captured:
        return False
    return (
        (current.st_dev, current.st_ino, current.st_size) == identity
        and current.st_mtime == mtime
    )


def _capture_drift_snapshot(root: Path) -> dict:
    """Capture the adapter state a drift scan certified (W2-001, SRC-002:R004).

    Returns ``{"root": root, "present": bool, "entries": ...}`` where
    *root* is the manifest root the snapshot came from (so a later
    comparison can re-derive the same paths), ``present`` records whether
    ``adapters/`` existed, and ``entries`` maps each validated ``.md``
    file's POSIX-style relative path to ``((st_dev, st_ino), sha256)`` of
    its RESOLVED target and bytes. Symlink referent identity makes a
    retarget detectable even when the new target has identical content.
    An absent ``adapters/`` snapshots as ``present=False`` with no entries,
    which is the T-912 nothing-to-scan contract; a present-but-empty
    directory snapshots as ``present=True`` with no entries, so creating
    one during validation is detected.

    Every filesystem failure is normalized to :class:`SaitalkError`: this
    runs unlocked, so a mid-snapshot mutation must be a refusal, never a
    false PASS.
    """
    transport_dir = root / "adapters"
    try:
        os.lstat(transport_dir)
    except FileNotFoundError:
        return {"root": root, "present": False, "entries": {}}
    except OSError as exc:
        raise SaitalkError(f"cannot inspect adapter path {transport_dir}: {exc}") from exc
    entries: dict[str, tuple[tuple[int, int], str]] = {}
    try:
        for path in _iter_md_files(transport_dir):
            target_stat = os.stat(path, follow_symlinks=True)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rel = str(path.relative_to(transport_dir)).replace(os.sep, "/")
            entries[rel] = ((target_stat.st_dev, target_stat.st_ino), digest)
    except OSError as exc:
        raise SaitalkError(
            f"cannot capture adapter snapshot under {transport_dir}: {exc}"
        ) from exc
    return {"root": root, "present": True, "entries": entries}


def _drift_snapshot_unchanged(snapshot: dict) -> bool:
    """Is the adapter tree still the state the drift scan certified?

    Content-first like :func:`_state_snapshot_unchanged`: bytes decide,
    metadata sharpens. Any added, removed or rewritten ``.md`` file, or a
    symlink whose referent identity moved, changes the verdict. A tree that
    became unreadable mid-validation is "changed", never an error, because
    the caller turns that into the canonical refusal.
    """
    root = snapshot.get("root")
    if root is None:
        return False
    try:
        current = _capture_drift_snapshot(root)
    except (SaitalkError, OSError):
        return False
    return current == snapshot


def _validate_package(
    contract_path: Path,
    conf_path: Path,
    skill_path: Path,
    state_path: Path | None = None,
    resolved: dict[str, Path] | None = None,
    logical_root: Path | None = None,
) -> tuple[str, dict[str, str]]:
    """Validate one already-captured manifest snapshot without acquiring locks."""
    if resolved is None:
        resolved = _resolved_manifest_map(contract_path, conf_path, skill_path)
    if logical_root is None:
        logical_root = _require_coherent_root(contract_path, conf_path, skill_path)
    contract_id, config = _validate(resolved)
    # W2-003: capture bound state identity before scanning, scan, then
    # verify the state file's identity has not changed.
    state_snapshot = None
    if state_path is not None:
        state_snapshot = _capture_state_snapshot(state_path)
        validate_state(state_path, contract_id)
    validate_drift(logical_root)
    if state_snapshot is not None and not _state_snapshot_unchanged(state_snapshot):
        raise SaitalkError(
            "state changed during validation: "
            f"{state_path} identity diverged; re-run validate"
        )
    _assert_manifest_stable((contract_path, conf_path, skill_path), resolved)
    return contract_id, config


def validate_package(
    contract_path: Path,
    conf_path: Path,
    skill_path: Path,
    state_path: Path | None = None,
) -> tuple[str, dict[str, str]]:
    """Validate manifest, drift, and optional bound state with bounded
    manifest-lock scope (CORE-005 / W2-001 / W2-002 / W2-003 / PERF-002).

    Phase 1 acquires manifest locks, reads the three manifest members
    exactly once, validates THOSE CAPTURED BYTES, and captures the
    optional state file's bytes and stat identity.  The manifest locks are
    then released so phase 2's drift scan does not block concurrent
    refresh/print-id on the manifest.  Phase 3 re-acquires the manifest
    locks and proves the manifest bytes, the logical referents and the
    bound state are all still identical to the phase-1 capture before
    returning PASS.

    R011/W2-002: phase 1 previously captured bytes and then let
    ``_validate`` re-read the same paths, so an A->B->A mutation between
    the two reads returned B's ``contract_id`` for a package that phase 3
    then confirmed was A.  One capture, one verdict.

    R002/CORE-002: phase 3 re-asserts LOGICAL manifest stability, not only
    physical byte equality.  Retargeting the manifest symlinks during the
    unlocked phase 2 otherwise left the abandoned physical files
    byte-stable and returned a PASS naming a package the caller no longer
    points at.

    R003/CORE-003: the bound state's freshness is re-checked inside phase
    3, after the manifest checks and immediately before the successful
    return, so a mutation during phase 3's own I/O cannot slip through.

    W2-001 (SRC-002:R004): phase 2's drift scan runs unlocked, so the state
    it certified could change before phase 3 confirmed the manifest.  The
    adapter namespace is now captured as a snapshot beside the drift scan --
    absent-vs-present, the entry namespace with symlink identity, and the
    digest of every ``.md`` file actually validated -- and phase 3 requires
    that snapshot unchanged before returning PASS.  An adapter added,
    removed, retargeted or rewritten after the scan makes the package
    re-validation refuse with a normalized error instead of certifying a
    stale scan.
    """
    resolved = _resolved_manifest_map(contract_path, conf_path, skill_path)
    logical_root = _require_coherent_root(contract_path, conf_path, skill_path)
    logical = (contract_path, conf_path, skill_path)
    canonical_targets = sorted(resolved.values(), key=lambda p: str(p.resolve()))
    manifest_bytes: dict[str, bytes] = {}

    # Phase 1: manifest snapshot under lock -- capture, then validate the
    # capture itself. No second read of the paths.
    locks1 = _acquire_locks(canonical_targets)
    try:
        for label, path in resolved.items():
            manifest_bytes[label] = read_bytes(path)
        contract_id, config = _validate_manifest(
            {
                label: _decode_manifest_bytes(label, blob)
                for label, blob in manifest_bytes.items()
            }
        )
        state_snapshot = None
        if state_path is not None:
            state_snapshot = _capture_state_snapshot(state_path)
    finally:
        _release_locks(locks1)

    # Phase 2: drift and (snapshot) state validation without manifest lock.
    if state_snapshot is not None:
        validate_state_bytes(state_snapshot[1], contract_id)
    drift_snapshot = _capture_drift_snapshot(logical_root)
    validate_drift(logical_root)

    if state_snapshot is not None and not _state_snapshot_unchanged(state_snapshot):
        raise SaitalkError(
            "state changed during validation: "
            f"{state_path} identity diverged; re-run validate"
        )

    # Phase 3: manifest stability re-check under lock.
    locks2 = _acquire_locks(canonical_targets)
    try:
        for label, path in resolved.items():
            if read_bytes(path) != manifest_bytes[label]:
                raise SaitalkError(
                    f"manifest member {label} changed during validation; "
                    "aborting to avoid a stale result"
                )
        _assert_manifest_stable(logical, resolved)
        if not _drift_snapshot_unchanged(drift_snapshot):
            raise SaitalkError(
                f"adapters changed during validation: {logical_root / 'adapters'} "
                "identity diverged; re-run validate"
            )
        if state_snapshot is not None and not _state_snapshot_unchanged(state_snapshot):
            raise SaitalkError(
                "state changed during validation: "
                f"{state_path} identity diverged; re-run validate"
            )
        return contract_id, config
    finally:
        _release_locks(locks2)


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
