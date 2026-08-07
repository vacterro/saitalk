#!/usr/bin/env python3
"""Zero-dependency validator for the portable SAITALK contract."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

SENTINEL = "<SAITALK-CONTRACT>"
CONTRACT_PATTERN = re.compile(r"(?m)^contract_id\s*[:=]\s*(\S+)\s*$")
CONF_PATTERN = re.compile(r"(?m)^([a-z_]+)=(\S.*)$")
STATE_PATTERN = re.compile(r"(?m)^saitalk_contract:\s*(\S+)\s*$")
STATE_STATUS_PATTERN = re.compile(r"(?m)^saitalk_status:\s*(\S+)\s*$")

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


class SaitalkError(RuntimeError):
    pass


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SaitalkError(f"missing file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise SaitalkError(f"not UTF-8: {path}") from exc


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


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


def replace_contract_value(text: str, label: str) -> str:
    normalized = normalize(text)
    match = exactly_one(CONTRACT_PATTERN, normalized, label)
    start, end = match.span(1)
    return normalized[:start] + SENTINEL + normalized[end:]


def expected_id(contract_text: str, conf_text: str) -> str:
    canonical_contract = replace_contract_value(contract_text, "SAITALK.md contract_id")
    canonical_conf = replace_contract_value(conf_text, "saitalk.conf contract_id")
    payload = canonical_contract + "\n---SAITALK-CONF---\n" + canonical_conf
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"saitalk-{digest[:8]}"


def current_contract_id(text: str, label: str) -> str:
    return exactly_one(CONTRACT_PATTERN, normalize(text), label).group(1)


def replace_current_id(text: str, new_id: str, label: str) -> str:
    normalized = normalize(text)
    match = exactly_one(CONTRACT_PATTERN, normalized, label)
    start, end = match.span(1)
    return normalized[:start] + new_id + normalized[end:]


def refresh(contract_path: Path, conf_path: Path) -> str:
    contract_text = read_text(contract_path)
    conf_text = read_text(conf_path)
    parse_conf(conf_text)

    new_id = expected_id(contract_text, conf_text)

    contract_path.write_text(
        replace_current_id(contract_text, new_id, "SAITALK.md contract_id"),
        encoding="utf-8",
        newline="\n",
    )
    conf_path.write_text(
        replace_current_id(conf_text, new_id, "saitalk.conf contract_id"),
        encoding="utf-8",
        newline="\n",
    )
    return new_id


def validate(contract_path: Path, conf_path: Path) -> tuple[str, dict[str, str]]:
    contract_text = read_text(contract_path)
    conf_text = read_text(conf_path)
    config = parse_conf(conf_text)

    expected = expected_id(contract_text, conf_text)
    contract_current = current_contract_id(contract_text, "SAITALK.md contract_id")
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


def validate_state(path: Path, expected: str) -> None:
    text = normalize(read_text(path))
    actual = exactly_one(STATE_PATTERN, text, "saitalk_contract").group(1)
    if actual != expected:
        raise SaitalkError(
            f"saitalk_contract: found {actual!r}, expected {expected!r}"
        )
    status_match = STATE_STATUS_PATTERN.search(text)
    if status_match is not None and status_match.group(1) != "active":
        raise SaitalkError(
            f"saitalk_status: found {status_match.group(1)!r}, expected 'active'"
        )


def root_paths() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1]
    return root / "SAITALK.md", root / "saitalk.conf"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate portable SAITALK.")
    sub = parser.add_subparsers(dest="command", required=True)

    refresh_parser = sub.add_parser("refresh", help="Refresh contract_id.")
    refresh_parser.add_argument("--contract", type=Path)
    refresh_parser.add_argument("--config", type=Path)

    validate_parser = sub.add_parser("validate", help="Validate contract and config.")
    validate_parser.add_argument("--contract", type=Path)
    validate_parser.add_argument("--config", type=Path)
    validate_parser.add_argument("--state", type=Path)

    print_parser = sub.add_parser("print-id", help="Print current valid contract_id.")
    print_parser.add_argument("--contract", type=Path)
    print_parser.add_argument("--config", type=Path)

    return parser


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    default_contract, default_conf = root_paths()
    return args.contract or default_contract, args.config or default_conf


def main() -> int:
    args = build_parser().parse_args()
    contract_path, conf_path = resolve_paths(args)

    try:
        if args.command == "refresh":
            contract_id = refresh(contract_path, conf_path)
            print(f"SAITALK REFRESHED: contract_id={contract_id}")
            return 0

        contract_id, config = validate(contract_path, conf_path)

        if args.command == "print-id":
            print(contract_id)
            return 0

        if args.state is not None:
            validate_state(args.state, contract_id)

        state = f"; state={args.state}: PASS" if args.state else ""
        print(
            f"SAITALK PASS: contract_id={contract_id}; "
            f"reply_language={config['reply_language']}; "
            f"chat_style={config['chat_style']}{state}"
        )
        return 0
    except SaitalkError as exc:
        print(f"SAITALK FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
